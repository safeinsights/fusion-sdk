"""Destination side: `Fusion.connect()`, one `Peer` per leg, `peer.request()` and `fusion.complete()`.

Behavioral specification: plan §3 "Destination request() lifecycle"; liveness policy: ADR 0004.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import _log
from ._config import Settings, TunnelConfig, read_role, read_tunnels
from ._envelope import EnvelopeError, ResponseEnvelope, Table, canonical_bytes, decode, encode_query, is_table
from ._http import Backoff
from ._transport import (
    EMPTY,
    READY_STATE,
    TERMINAL_STATES,
    Budget,
    Delivered,
    Info,
    Retryable,
    Terminal,
    Transport,
    UnknownCorrelation,
    retry_until,
)
from ._tunnel import TunnelClient
from .errors import (
    ConcurrencyError,
    ConfigError,
    FusionError,
    NotReadyError,
    ProtocolError,
    RemoteError,
    RoundTimeoutError,
    TerminalError,
    terminal_error,
)

if TYPE_CHECKING:
    import pandas as pd

JSON = Any
API_MAJOR = 1


@dataclass(frozen=True)
class Response:
    """One completed round. `body` is the raw JSON body; `to_table()` / `to_pandas()` decode a fusion table."""

    body: JSON
    operation: str
    peer: str
    correlation_id: str
    message_id: str
    budget: Budget | None
    bytes: int
    duration_s: float

    @property
    def is_table(self) -> bool:
        return is_table(self.body)

    def to_table(self) -> Table:
        if not self.is_table:
            raise TypeError("response body is not a fusion table; use .body")
        return Table.from_json(self.body)

    def to_records(self) -> list[dict[str, Any]]:
        return self.to_table().to_records()

    def to_pandas(self) -> pd.DataFrame:
        return self.to_table().to_pandas()


@dataclass(frozen=True)
class PeerInfo:
    peer: str
    leg_id: str
    direction: str
    state: str


def wait_ready(
    transport: Transport,
    *,
    deadline: float,
    settings: Settings,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
) -> Info:
    """Poll GET /v1/info until CHANNEL_UP (content-free progress log), a terminal state, or the deadline."""
    start = time.monotonic()
    interval = settings.ready_poll_s
    attempt = 0
    while True:
        state = "UNREACHABLE"
        try:
            info = transport.info()
        except Retryable:
            info = None
        if info is not None:
            state = info.state
            if state == READY_STATE:
                _log.event(
                    "ready.ok",
                    peer=label,
                    legId=info.leg_id,
                    role=info.role,
                    durationMs=int((time.monotonic() - start) * 1000),
                )
                return info
            if state in TERMINAL_STATES:
                code = TERMINAL_STATES[state]
                _log.event("session.terminal", logging.ERROR, peer=label, code=code)
                raise terminal_error(code, f"tunnel is already {state}", peer=label)
        _log.event("ready.wait", peer=label, state=state, attempt=attempt)
        now = time.monotonic()
        if now >= deadline:
            _log.event("ready.timeout", logging.ERROR, peer=label, state=state, durationMs=int((now - start) * 1000))
            raise NotReadyError(f"tunnel not CHANNEL_UP after {int(now - start)} s (state {state})", peer=label)
        sleep(min(interval, max(0.0, deadline - now)))
        interval = min(interval * 2, settings.ready_poll_max_s)
        attempt += 1


class Peer:
    """A destination's handle on one leg. `request()` blocks; one round in flight per peer."""

    def __init__(self, transport: Transport, info: Info, settings: Settings, *, label: str) -> None:
        self._transport = transport
        self._settings = settings
        self._backoff = Backoff(settings.retry_base_ms, settings.retry_max_ms)
        self.label = label
        self.name = info.peer_org_slug
        self.leg_id = info.leg_id
        self.direction = info.direction
        self._state = info.state
        self._lock = threading.Lock()
        self._terminal: TerminalError | None = None
        self._budget: Budget | None = None
        self._abandoned: str | None = None
        self.closed = False

    def __repr__(self) -> str:
        return f"Peer({self.name!r}, leg={self.leg_id!r}, state={self.state!r})"

    # -- state --

    @property
    def state(self) -> str:
        if self._terminal is not None:
            return f"TERMINAL:{self._terminal.code}"
        return "CLOSED" if self.closed else self._state

    @property
    def terminal(self) -> TerminalError | None:
        return self._terminal

    @property
    def in_flight(self) -> bool:
        return self._lock.locked()

    def budget(self) -> Budget | None:
        """The last budget hint seen on this leg (ask T2)."""
        return self._budget

    def info(self) -> PeerInfo:
        return PeerInfo(self.name, self.leg_id, self.direction, self.state)

    def _check_usable(self) -> None:
        if self._terminal is not None:
            raise self._terminal
        if self.closed:
            raise ConcurrencyError("complete() was already called on this peer", peer=self.name)

    def _go_terminal(self, t: Terminal) -> TerminalError:
        err = terminal_error(t.code, t.message, t.detail, peer=self.name)
        self._terminal = err
        _log.event("session.terminal", logging.ERROR, peer=self.name, code=t.code)
        return err

    # -- the round --

    def request(
        self, operation: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Response:
        """Run one round: submit → long-poll → ACK → decode. Blocks until a response, a typed error, or the round timeout.

        `timeout` is the per-attempt round timeout in seconds (default FUSION_ROUND_TIMEOUT_S); the SDK re-issues
        the same correlationId up to FUSION_ROUND_MAX_REISSUES times before raising RoundTimeoutError.
        """
        self._check_usable()
        if not self._lock.acquire(blocking=False):
            raise ConcurrencyError("a request is already in flight on this peer", peer=self.name)
        try:
            return self._request_locked(operation, params or {}, timeout)
        finally:
            self._lock.release()

    def _request_locked(self, operation: str, params: Mapping[str, Any], timeout: float | None) -> Response:
        s = self._settings
        payload = encode_query(operation, params)
        nbytes = canonical_bytes(payload)
        if nbytes > s.warn_bytes:
            _log.event("envelope.large", logging.WARNING, peer=self.name, bytes=nbytes, limit=s.warn_bytes)
        _log.event("round.start", peer=self.name, operation=operation, bytes=nbytes)
        per_attempt = s.round_timeout_s if timeout is None else float(timeout)
        started = time.monotonic()
        attempt_deadline = started + per_attempt
        reissues = 0
        cid = self._submit(payload, None, attempt_deadline)

        def reissue(code: str) -> None:
            nonlocal reissues, attempt_deadline
            if reissues >= s.round_max_reissues:
                _log.event(
                    "round.timeout",
                    logging.ERROR,
                    peer=self.name,
                    operation=operation,
                    correlationId=cid,
                    durationMs=_ms(started),
                )
                self._abandon(cid)
                raise RoundTimeoutError(
                    f"round did not complete within {per_attempt:g} s after {reissues} re-issue(s)",
                    peer=self.name,
                    correlation_id=cid,
                    reissues=reissues,
                )
            reissues += 1
            _log.event("round.reissue", logging.WARNING, peer=self.name, correlationId=cid, reissue=reissues, code=code)
            attempt_deadline = time.monotonic() + per_attempt
            self._submit(payload, cid, attempt_deadline)

        retry_attempt = 0
        while True:
            remaining = attempt_deadline - time.monotonic()
            if remaining <= 0:
                reissue("TIMEOUT")
                retry_attempt = 0
                continue
            try:
                result = self._transport.poll_response(cid, timeout_s=min(s.poll_http_timeout_s, max(remaining, 1.0)))
            except UnknownCorrelation:
                reissue("UNKNOWN_CORRELATION")
                retry_attempt = 0
                continue
            except Retryable as exc:
                if time.monotonic() >= attempt_deadline:
                    continue
                if exc.code == "BACKPRESSURE":
                    _log.event("round.backpressure", logging.WARNING, peer=self.name, attempt=retry_attempt)
                else:
                    _log.event("round.retry", logging.DEBUG, peer=self.name, attempt=retry_attempt, code=exc.code)
                time.sleep(min(self._backoff.delay(retry_attempt), max(0.0, attempt_deadline - time.monotonic())))
                retry_attempt += 1
                continue
            if result is EMPTY:
                continue
            if isinstance(result, Terminal):
                raise self._go_terminal(result)
            assert isinstance(result, Delivered)
            return self._finish(result, operation, started, nbytes)

    def _finish(self, delivered: Delivered, operation: str, started: float, query_bytes: int) -> Response:
        # ACK first, even if the payload turns out undecodable: otherwise it is redelivered until dead-lettered.
        self._ack(delivered.message_id)
        if delivered.budget is not None:
            self._budget = delivered.budget
        try:
            env = decode(delivered.payload)
        except EnvelopeError as exc:
            _log.event(
                "round.protocol_error",
                logging.ERROR,
                peer=self.name,
                correlationId=delivered.correlation_id,
                messageId=delivered.message_id,
            )
            raise ProtocolError(f"response envelope is invalid: {exc.message}", peer=self.name) from None
        if not isinstance(env, ResponseEnvelope):
            _log.event(
                "round.protocol_error",
                logging.ERROR,
                peer=self.name,
                correlationId=delivered.correlation_id,
                messageId=delivered.message_id,
            )
            raise ProtocolError("expected a response envelope, got a query", peer=self.name)
        duration = time.monotonic() - started
        if env.error is not None:
            _log.event(
                "round.remote_error",
                logging.WARNING,
                peer=self.name,
                operation=operation,
                correlationId=delivered.correlation_id,
                code=env.error.code,
            )
            raise RemoteError(env.error.code, env.error.message, env.error.detail, peer=self.name, operation=operation)
        resp_bytes = canonical_bytes(delivered.payload)
        fields = self._budget.log_fields() if self._budget else {}
        _log.event(
            "round.complete",
            peer=self.name,
            operation=operation,
            correlationId=delivered.correlation_id,
            messageId=delivered.message_id,
            bytes=resp_bytes,
            durationMs=int(duration * 1000),
            roundsUsed=fields.get("roundsUsed"),
            roundsMax=fields.get("roundsMax"),
        )
        if self._budget is not None:
            for name, used, cap in self._budget.near_limit():
                _log.event(
                    "budget.near_limit", logging.WARNING, peer=self.name, **{f"{name}Used": used, f"{name}Max": cap}
                )
        return Response(
            env.body,
            operation,
            self.name,
            delivered.correlation_id,
            delivered.message_id,
            self._budget,
            resp_bytes,
            duration,
        )

    def _submit(self, payload: JSON, correlation_id: str | None, deadline: float) -> str:
        def call() -> str | Terminal:
            return self._transport.submit(payload, correlation_id)

        try:
            result = retry_until(call, deadline=deadline, backoff=self._backoff, peer=self.name, what="submit")
        except ConcurrencyError as exc:
            stale = getattr(exc, "correlation_id", None)
            if correlation_id is None and self._abandoned is not None and stale in (None, self._abandoned):
                self._drain_abandoned()
                result = retry_until(call, deadline=deadline, backoff=self._backoff, peer=self.name, what="submit")
            else:
                raise
        if isinstance(result, Terminal):
            raise self._go_terminal(result)
        return result

    def _abandon(self, correlation_id: str) -> None:
        """Best effort T7; fall back to draining on the next 409."""
        try:
            done = self._transport.abandon(correlation_id)
        except FusionError:
            done = False
        if not done:
            self._abandoned = correlation_id

    def _drain_abandoned(self) -> None:
        cid = self._abandoned
        assert cid is not None
        try:
            result = self._transport.poll_response(cid, timeout_s=self._settings.poll_http_timeout_s)
        except UnknownCorrelation:
            self._abandoned = None
            return
        except Retryable as exc:
            raise ConcurrencyError(
                f"abandoned round {cid} is still in flight at the tunnel ({exc.code})", peer=self.name
            ) from None
        if isinstance(result, Delivered):
            self._ack(result.message_id)
            self._abandoned = None
            return
        if isinstance(result, Terminal):
            raise self._go_terminal(result)
        raise ConcurrencyError(f"abandoned round {cid} is still in flight at the tunnel", peer=self.name)

    def _ack(self, message_id: str) -> None:
        deadline = time.monotonic() + max(5.0, self._settings.http_timeout_s * 3)
        try:
            retry_until(
                lambda: self._transport.ack(message_id),
                deadline=deadline,
                backoff=self._backoff,
                peer=self.name,
                what="ack",
            )
        except RoundTimeoutError:
            _log.event("ack.failed", logging.WARNING, peer=self.name, messageId=message_id)

    def _complete(self) -> str:
        """Send CLOSE for this leg. Returns the per-leg outcome code."""
        if self._terminal is not None:
            return self._terminal.code
        if self.closed:
            return "OK"
        if self._lock.locked():
            raise ConcurrencyError("cannot complete while a request is in flight", peer=self.name)
        deadline = time.monotonic() + max(10.0, self._settings.http_timeout_s * 3)
        t = retry_until(
            self._transport.complete, deadline=deadline, backoff=self._backoff, peer=self.name, what="complete"
        )
        if t is not None and t.code != "STUDY_COMPLETE":
            self._go_terminal(t)
            return t.code
        self.closed = True
        return "OK"


class Fusion:
    """The destination's view of a study: one peer per leg, `complete()` fan-out, context manager."""

    def __init__(self, peers: Sequence[Peer], settings: Settings) -> None:
        self._peers: dict[str, Peer] = {}
        for p in peers:
            if p.name in self._peers:
                raise ConfigError(f"two tunnels report the same peerOrgSlug {p.name!r}")
            self._peers[p.name] = p
        self.settings = settings
        self.completed = False

    @classmethod
    def connect(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        settings: Settings | None = None,
        ready_timeout: float | None = None,
        transports: Sequence[tuple[str, Transport]] | None = None,
    ) -> Fusion:
        """Read the env (spec/env.md), wait for CHANNEL_UP on every leg, and return peers keyed by peerOrgSlug.

        `transports` bypasses the env and HTTP (simulator and tests): a sequence of (label, Transport).
        """
        s = settings or Settings.from_env(env)
        _log.configure(s.log_level)
        pairs: list[tuple[str, Transport]]
        if transports is None:
            role = read_role(env)
            if role != "destination":
                raise ConfigError("Fusion.connect() is for FUSION_ROLE=destination; a source calls serve()")
            pairs = [(t.label, cls._make_transport(t, s)) for t in read_tunnels(env)]
        else:
            pairs = list(transports)
        _log.event("connect.start", endpointCount=len(pairs))
        deadline = time.monotonic() + (s.ready_timeout_s if ready_timeout is None else ready_timeout)
        peers: list[Peer] = []
        for label, transport in pairs:
            info = wait_ready(transport, deadline=deadline, settings=s, label=label)
            if info.role != "destination":
                raise ConfigError(
                    f"tunnel {label!r} reports role {info.role!r}; this process is a destination", peer=label
                )
            if info.api_major != API_MAJOR:
                raise ConfigError(
                    f"tunnel {label!r} speaks local API {info.api_version}; this SDK requires major {API_MAJOR}",
                    peer=label,
                )
            peers.append(Peer(transport, info, s, label=label))
        return cls(peers, s)

    @staticmethod
    def _make_transport(t: TunnelConfig, s: Settings) -> Transport:
        return TunnelClient(t.endpoint, t.token, peer=t.label, http_timeout_s=s.http_timeout_s)

    # -- peers --

    def peer(self, name: str | None = None) -> Peer:
        """The peer named by `peerOrgSlug`; with no argument, the only peer of a single-leg study."""
        if name is None:
            if len(self._peers) != 1:
                raise KeyError(f"this study has {len(self._peers)} peers; name one of {sorted(self._peers)}")
            return next(iter(self._peers.values()))
        try:
            return self._peers[name]
        except KeyError:
            raise KeyError(f"unknown peer {name!r}; known peers: {sorted(self._peers)}") from None

    def peers(self) -> list[PeerInfo]:
        return [p.info() for p in self._peers.values()]

    def request(
        self, peer: str | None, operation: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Response:
        """Convenience: `fusion.request(peer, operation, params)`."""
        return self.peer(peer).request(operation, params, timeout=timeout)

    def complete(self) -> dict[str, str]:
        """Fan POST /v1/complete out to every leg. Returns {peer: "OK" | terminal code | "ERROR"}.

        Every leg is attempted; if any leg failed with a non-terminal error, the first such error is raised afterwards.
        """
        _log.event("complete.start", endpointCount=len(self._peers))
        results: dict[str, str] = {}
        errors: list[FusionError] = []
        for name, p in self._peers.items():
            try:
                results[name] = p._complete()
            except FusionError as exc:
                results[name] = "ERROR"
                errors.append(exc)
            _log.event("complete.leg", peer=name, code=results[name])
        self.completed = True
        if errors:
            raise errors[0]
        return results

    def __enter__(self) -> Fusion:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        # CLOSE only on a clean exit: an exception means the analysis did not finish.
        if exc_type is None and not self.completed:
            self.complete()


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


__all__ = ["Fusion", "Peer", "PeerInfo", "Response", "wait_ready"]
