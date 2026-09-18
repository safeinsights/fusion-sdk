"""Source side: the operation registry and `serve()` (plan §3 "Source serve() loop").

A handler exception never crashes the loop: it becomes a HANDLER_ERROR envelope (ADR 0005). Redelivered
queries are answered from the in-memory memo without re-running the handler; guards are enforced around
every handler call (ADR 0002).
"""

from __future__ import annotations

import logging
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import _log
from ._config import Settings, read_role, read_tunnels
from ._envelope import (
    EnvelopeError,
    Query,
    canonical_bytes,
    decode,
    encode_body,
    encode_response_error,
    encode_response_ok,
)
from ._http import Backoff
from ._transport import EMPTY, Budget, Delivered, Info, Retryable, Terminal, Transport, retry_until
from ._tunnel import TunnelClient
from .destination import API_MAJOR, wait_ready
from .errors import ConfigError, RoundTimeoutError, terminal_error
from .guards import (
    CARDINALITIES,
    Cardinality,
    GuardRefused,
    Guards,
    Handler,
    OperationSpec,
    check_query,
    check_result,
    preflight,
)

JSON = Any
_trace_logger = logging.getLogger(_log.LOGGER_NAME + ".handler_trace")


@dataclass(frozen=True)
class Context:
    """What a handler may know about the round. `correlation_id` keys idempotent side effects."""

    correlation_id: str
    message_id: str
    peer: str
    operation: str
    budget: Budget | None
    logger: logging.Logger


class OperationRegistry:
    """Named, Data-Partner-approved operations. Use as a decorator or with `add()`."""

    def __init__(self) -> None:
        self._ops: OrderedDict[str, OperationSpec] = OrderedDict()

    def register(
        self,
        name: str,
        *,
        person_id_param: str | None = None,
        cardinality: Cardinality = "aggregate",
        count_column: str | None = None,
        sanitize: bool = False,
    ) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.add(
                name,
                fn,
                person_id_param=person_id_param,
                cardinality=cardinality,
                count_column=count_column,
                sanitize=sanitize,
            )
            return fn

        return deco

    def add(
        self,
        name: str,
        handler: Handler,
        *,
        person_id_param: str | None = None,
        cardinality: Cardinality = "aggregate",
        count_column: str | None = None,
        sanitize: bool = False,
    ) -> OperationSpec:
        if cardinality not in CARDINALITIES:
            raise ValueError(f"cardinality must be one of {CARDINALITIES}")
        if name in self._ops:
            raise ValueError(f"operation {name!r} is already registered")
        spec = OperationSpec(name, handler, person_id_param, cardinality, count_column, sanitize)
        self._ops[name] = spec
        return spec

    def get(self, name: str) -> OperationSpec | None:
        return self._ops.get(name)

    def names(self) -> list[str]:
        return list(self._ops)

    def specs(self) -> list[OperationSpec]:
        return list(self._ops.values())

    def __contains__(self, name: object) -> bool:
        return name in self._ops

    def __len__(self) -> int:
        return len(self._ops)

    def clear(self) -> None:
        self._ops.clear()


#: Module-level default registry: `@operations.register(...)` then `serve()`.
operations = OperationRegistry()


class ResponseMemo:
    """LRU of correlationId → response payload, so a redelivered query replays without re-running the handler."""

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max(1, max_entries)
        self._data: OrderedDict[str, JSON] = OrderedDict()

    def get(self, cid: str) -> JSON | None:
        if cid in self._data:
            self._data.move_to_end(cid)
            return self._data[cid]
        return None

    def put(self, cid: str, payload: JSON) -> None:
        self._data[cid] = payload
        self._data.move_to_end(cid)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def __contains__(self, cid: object) -> bool:
        return cid in self._data

    def __len__(self) -> int:
        return len(self._data)


class Server:
    """The serve loop, split from `serve()` so tests and the simulator can drive it step by step."""

    def __init__(self, transport: Transport, registry: OperationRegistry, settings: Settings, *, label: str) -> None:
        self._transport = transport
        self._registry = registry
        self._settings = settings
        self._backoff = Backoff(settings.retry_base_ms, settings.retry_max_ms)
        self.label = label
        self.peer = label
        self.info: Info | None = None
        self.guards: Guards | None = None
        self.memo = ResponseMemo(settings.memo_max_entries)
        self.in_progress: set[str] = set()
        self.budget: Budget | None = None
        self.rounds_served = 0

    # -- startup --

    def start(self, deadline: float) -> Info:
        info = wait_ready(self._transport, deadline=deadline, settings=self._settings, label=self.label)
        if info.role != "source":
            raise ConfigError(f"tunnel reports role {info.role!r}; serve() is for a source", peer=self.label)
        if info.api_major != API_MAJOR:
            raise ConfigError(
                f"tunnel speaks local API {info.api_version}; this SDK requires major {API_MAJOR}", peer=self.label
            )
        self.info = info
        self.peer = info.peer_org_slug
        self.guards = Guards.from_info(info.guards)
        if self.guards is None or not self.guards.enabled:
            _log.event("guards.disabled", logging.WARNING, peer=self.peer)
        else:
            _log.event(
                "guards.loaded",
                peer=self.peer,
                maxDistinctPersonIds=self.guards.max_distinct_person_ids,
                minGroupSize=self.guards.min_group_size,
            )
        problems = preflight(self._registry.specs(), info.operations)
        if problems:
            raise ConfigError(
                "operation registry does not match the approved list: " + "; ".join(problems), peer=self.peer
            )
        if len(self._registry) == 0:
            raise ConfigError("no operations registered", peer=self.peer)
        _log.event("serve.start", peer=self.peer, operations=self._registry.names())
        return info

    # -- the loop --

    def run(self) -> None:
        """Serve until STUDY_COMPLETE (returns) or a terminal error (raises)."""
        s = self._settings
        retry_attempt = 0
        while True:
            try:
                msg = self._transport.next_message(timeout_s=s.poll_http_timeout_s)
            except Retryable as exc:
                # The source has no round timeout: keep polling with capped backoff, forever.
                if exc.code == "BACKPRESSURE":
                    _log.event("round.backpressure", logging.WARNING, peer=self.peer, attempt=retry_attempt)
                else:
                    _log.event("round.retry", logging.DEBUG, peer=self.peer, attempt=retry_attempt, code=exc.code)
                time.sleep(self._backoff.delay(retry_attempt))
                retry_attempt = min(retry_attempt + 1, 10)
                continue
            retry_attempt = 0
            if msg is EMPTY:
                continue
            if isinstance(msg, Terminal):
                if msg.code == "STUDY_COMPLETE":
                    _log.event("session.complete", peer=self.peer, roundsUsed=self.rounds_served)
                    return
                _log.event("session.terminal", logging.ERROR, peer=self.peer, code=msg.code)
                raise terminal_error(msg.code, msg.message, msg.detail, peer=self.peer)
            assert isinstance(msg, Delivered)
            if self.step(msg) == "STUDY_COMPLETE":
                return

    def step(self, msg: Delivered) -> str | None:
        """Handle one delivered query end to end. Returns a terminal code if the leg ended, else None."""
        started = time.monotonic()
        self._ack(msg.message_id)
        if msg.budget is not None:
            self.budget = msg.budget
        _log.event(
            "serve.received",
            peer=self.peer,
            correlationId=msg.correlation_id,
            messageId=msg.message_id,
            bytes=canonical_bytes(msg.payload),
        )
        cached = self.memo.get(msg.correlation_id)
        if cached is not None:
            _log.event("serve.memo_replay", peer=self.peer, correlationId=msg.correlation_id)
            return self._respond(msg, cached, started, operation=None)
        if msg.correlation_id in self.in_progress:
            # Cannot happen under sequential processing; guarded so a future concurrent loop stays safe.
            return None
        self.in_progress.add(msg.correlation_id)
        try:
            operation, payload = self.handle(msg)
            return self._respond(msg, payload, started, operation=operation)
        finally:
            self.in_progress.discard(msg.correlation_id)

    def handle(self, msg: Delivered) -> tuple[str | None, JSON]:
        """Decode → look up → guards → handler → guards → encode. Never raises for handler faults."""
        try:
            env = decode(msg.payload)
        except EnvelopeError as exc:
            _log.event("serve.bad_params", logging.WARNING, peer=self.peer, correlationId=msg.correlation_id)
            return None, encode_response_error(
                "BAD_PARAMS", f"query envelope is invalid: {exc.message}", {"reason": exc.message}
            )
        if not isinstance(env, Query):
            _log.event("serve.bad_params", logging.WARNING, peer=self.peer, correlationId=msg.correlation_id)
            return None, encode_response_error("BAD_PARAMS", "expected a query envelope", {"reason": "not a query"})
        spec = self._registry.get(env.operation)
        if spec is None:
            _log.event(
                "serve.unknown_operation",
                logging.WARNING,
                peer=self.peer,
                correlationId=msg.correlation_id,
                operation=env.operation,
            )
            return env.operation, encode_response_error(
                "UNKNOWN_OPERATION",
                f"no handler registered for operation {env.operation!r}",
                {"operation": env.operation},
            )
        try:
            check_query(env.params, spec, self.guards)
        except GuardRefused as exc:
            return env.operation, self._refuse(msg, env.operation, exc)
        ctx = Context(
            msg.correlation_id,
            msg.message_id,
            self.peer,
            env.operation,
            self.budget,
            logging.getLogger(f"{_log.LOGGER_NAME}.handlers.{env.operation}"),
        )
        t0 = time.monotonic()
        try:
            result = spec.handler(dict(env.params), ctx)
            body = encode_body(result)
        except Exception as exc:
            return env.operation, self._handler_error(msg, env.operation, spec, exc, t0)
        try:
            check_result(body, spec, self.guards)
        except GuardRefused as exc:
            return env.operation, self._refuse(msg, env.operation, exc)
        return env.operation, encode_response_ok(body)

    def _refuse(self, msg: Delivered, operation: str, exc: GuardRefused) -> JSON:
        _log.event(
            "serve.guard_refused",
            logging.WARNING,
            peer=self.peer,
            correlationId=msg.correlation_id,
            operation=operation,
            guard=exc.guard,
            limit=exc.limit,
            observed=exc.observed,
        )
        return encode_response_error("GUARD_REFUSED", exc.message, exc.detail())

    def _handler_error(
        self, msg: Delivered, operation: str, spec: OperationSpec, exc: BaseException, t0: float
    ) -> JSON:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _log.event(
            "serve.handler_error",
            logging.ERROR,
            peer=self.peer,
            correlationId=msg.correlation_id,
            operation=operation,
            durationMs=int((time.monotonic() - t0) * 1000),
        )
        # The full traceback always goes to the local log (DEBUG, separate logger), never to the event line.
        _trace_logger.debug("handler traceback correlationId=%s\n%s", msg.correlation_id, tb)
        if spec.sanitize:
            message = f"{type(exc).__name__}: {exc}".splitlines()[0] if str(exc) else type(exc).__name__
        else:
            message = tb
        return encode_response_error("HANDLER_ERROR", message, {"type": type(exc).__name__})

    def _respond(self, msg: Delivered, payload: JSON, started: float, *, operation: str | None) -> str | None:
        nbytes = canonical_bytes(payload)
        if nbytes > self._settings.warn_bytes:
            _log.event("envelope.large", logging.WARNING, peer=self.peer, bytes=nbytes, limit=self._settings.warn_bytes)
        deadline = time.monotonic() + max(30.0, self._settings.round_timeout_s)
        try:
            result = retry_until(
                lambda: self._transport.post_response(msg.correlation_id, payload),
                deadline=deadline,
                backoff=self._backoff,
                peer=self.peer,
                what="respond",
            )
        except RoundTimeoutError:
            # Could not hand the response to the tunnel; the query will be redelivered and answered from the memo.
            self.memo.put(msg.correlation_id, payload)
            _log.event(
                "round.protocol_error",
                logging.ERROR,
                peer=self.peer,
                correlationId=msg.correlation_id,
                messageId=msg.message_id,
            )
            return None
        if isinstance(result, Terminal):
            if result.code == "STUDY_COMPLETE":
                _log.event("session.complete", peer=self.peer, roundsUsed=self.rounds_served)
                return result.code
            _log.event("session.terminal", logging.ERROR, peer=self.peer, code=result.code)
            raise terminal_error(result.code, result.message, result.detail, peer=self.peer)
        if isinstance(result, Budget):
            self.budget = result
        self.memo.put(msg.correlation_id, payload)
        self.rounds_served += 1
        fields = self.budget.log_fields() if self.budget else {}
        _log.event(
            "round.served",
            peer=self.peer,
            correlationId=msg.correlation_id,
            operation=operation,
            bytes=nbytes,
            durationMs=int((time.monotonic() - started) * 1000),
            roundsUsed=fields.get("roundsUsed"),
            roundsMax=fields.get("roundsMax"),
        )
        if self.budget is not None:
            for name, used, cap in self.budget.near_limit():
                _log.event(
                    "budget.near_limit", logging.WARNING, peer=self.peer, **{f"{name}Used": used, f"{name}Max": cap}
                )
        return None

    def _ack(self, message_id: str) -> None:
        deadline = time.monotonic() + max(5.0, self._settings.http_timeout_s * 3)
        try:
            retry_until(
                lambda: self._transport.ack(message_id),
                deadline=deadline,
                backoff=self._backoff,
                peer=self.peer,
                what="ack",
            )
        except RoundTimeoutError:
            _log.event("ack.failed", logging.WARNING, peer=self.peer, messageId=message_id)


def serve(
    registry: OperationRegistry | None = None,
    env: Mapping[str, str] | None = None,
    *,
    settings: Settings | None = None,
    ready_timeout: float | None = None,
    transport: Transport | None = None,
) -> None:
    """Serve registered operations until the study completes.

    Blocks. Returns normally on STUDY_COMPLETE; raises LimitExceededError / SessionError on a terminal error,
    ConfigError on a misconfigured environment or registry. A source serves exactly one tunnel.
    """
    s = settings or Settings.from_env(env)
    _log.configure(s.log_level)
    reg = registry if registry is not None else operations
    label = "default"
    if transport is None:
        if read_role(env) != "source":
            raise ConfigError("serve() is for FUSION_ROLE=source; a destination calls Fusion.connect()")
        tunnels = read_tunnels(env)
        if len(tunnels) != 1:
            raise ConfigError(f"a source serves exactly one tunnel; {len(tunnels)} configured")
        label = tunnels[0].label
        transport = TunnelClient(tunnels[0].endpoint, tunnels[0].token, peer=label, http_timeout_s=s.http_timeout_s)
    server = Server(transport, reg, s, label=label)
    deadline = time.monotonic() + (s.ready_timeout_s if ready_timeout is None else ready_timeout)
    server.start(deadline)
    server.run()


__all__ = ["Context", "OperationRegistry", "ResponseMemo", "Server", "operations", "serve"]
