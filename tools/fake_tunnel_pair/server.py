"""Two local-API servers per leg (destination + source) joined by an in-memory relay.

One `Leg` object is the single source of truth for a leg: the destination tunnel's
in-flight bookkeeping, the relay's mailbox, and the source tunnel's delivery and
cached-response state. Both HTTP handlers mutate it under one condition variable, and
long-polls wait on that condition. Fault injection (spec/scenarios/README.md) is applied
exactly where the corresponding real component would misbehave.

Local API 2.0 (spec/local-api.md): delivery is the acknowledgement, error bodies are one flat
shape, and every route but /v1/info answers 200 {terminal: true, ...} once the leg has ended.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .scenarios import LegSpec, Scenario

API_VERSION = "2.0.0"
JSON = Any

STATE_ATTACHED = "RELAY_ATTACHED"
STATE_UP = "CHANNEL_UP"
STATE_CLOSED = "CLOSED"
STATE_ERRORED = "ERRORED"
STATE_LIMIT = "LIMIT_EXCEEDED"

TERMINAL_STATE = {"STUDY_COMPLETE": STATE_CLOSED, "SESSION_ERRORED": STATE_ERRORED, "LIMIT_EXCEEDED": STATE_LIMIT}


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _id() -> str:
    return str(uuid.uuid4())


def canonical_bytes(payload: JSON) -> int:
    """The byte count the tunnel meters: canonical UTF-8 serialization of the payload."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """The one error body shape: {code, message, correlationId?, issues?}."""
    return {"code": code, "message": message, **extra}


@dataclass
class Terminal:
    code: str
    message: str = ""
    detail: dict[str, Any] | None = None

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"terminal": True, "code": self.code}
        if self.message:
            out["message"] = self.message
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class Round:
    correlation_id: str
    round_no: int
    query_payload: JSON
    query_bytes: int
    submitted_at: float
    query_message_id: str = field(default_factory=_id)
    query_deliverable_at: float = 0.0
    query_delivery_count: int = 0
    redeliver_pending: bool = False  # deliver the query once more on the next poll (RC-crash simulation)
    dst_knows: bool = True
    forgotten_once: bool = False
    response_payload: JSON = None
    response_message_id: str | None = None
    response_bytes: int = 0
    response_received_at: str | None = None
    response_dropped: bool = False
    response_delivery_count: int = 0
    response_delivered: bool = False  # delivered to the destination RC: the round is complete
    abandoned: bool = False

    @property
    def has_response(self) -> bool:
        return self.response_message_id is not None

    def query_message(self, budget: dict[str, Any]) -> dict[str, Any]:
        return {
            "messageId": self.query_message_id,
            "correlationId": self.correlation_id,
            "payload": self.query_payload,
            "budget": budget,
            "receivedAt": _now_iso(),
        }

    def response_message(self, budget: dict[str, Any]) -> dict[str, Any]:
        return {
            "messageId": self.response_message_id,
            "correlationId": self.correlation_id,
            "payload": self.response_payload,
            "budget": budget,
            "receivedAt": self.response_received_at,
        }


class Leg:
    """All state for one leg: destination tunnel + relay + source tunnel."""

    def __init__(
        self, spec: LegSpec, scenario: Scenario, fixed_tokens: bool = False, api_version: str = API_VERSION
    ) -> None:
        self.spec = spec
        self.scenario = scenario
        self.api_version = api_version
        self.cond = threading.Condition()
        self.started_at = time.monotonic()
        self.ready_at = self.started_at + scenario.ready_delay_ms / 1000.0
        self.stopping = False
        self.tokens = {
            "destination": f"fake-{spec.leg_id}-destination" if fixed_tokens else secrets.token_urlsafe(24),
            "source": f"fake-{spec.leg_id}-source" if fixed_tokens else secrets.token_urlsafe(24),
        }
        self.terminal: Terminal | None = None
        self.rounds: dict[str, Round] = {}
        self.order: deque[Round] = deque()  # FIFO of rounds awaiting first delivery at the source
        self.in_flight: Round | None = None
        self.round_counter = 0
        self.request_calls = 0
        self.respond_calls = 0
        self.rounds_used = 0
        self.round_times: deque[float] = deque()
        self.response_bytes_used = 0
        self.query_bytes_used = 0
        self.log: list[dict[str, Any]] = []
        self._injected: set[str] = set()
        if spec.faults.error_after_round == 0:
            self.terminal = Terminal("SESSION_ERRORED", "relay: session errored at channel up")

    # -- helpers ----------------------------------------------------------

    def _event(self, name: str, **fields: Any) -> None:
        self.log.append({"t": round(time.monotonic() - self.started_at, 3), "event": name, **fields})

    def _inject(self, key: str, rounds: frozenset[int], n: int) -> bool:
        """True once per (fault, call number) when call `n` is listed for the fault."""
        if n in rounds and f"{key}{n}" not in self._injected:
            self._injected.add(f"{key}{n}")
            return True
        return False

    def ready(self) -> bool:
        return time.monotonic() >= self.ready_at

    def state(self) -> str:
        if self.terminal is not None:
            return TERMINAL_STATE[self.terminal.code]
        return STATE_UP if self.ready() else STATE_ATTACHED

    def budget(self) -> dict[str, Any]:
        caps = self.spec.caps
        cutoff = time.monotonic() - 3600
        while self.round_times and self.round_times[0] < cutoff:
            self.round_times.popleft()
        out: dict[str, Any] = {
            "roundsUsed": self.rounds_used,
            "responseBytesUsed": self.response_bytes_used,
            "queryBytesUsed": self.query_bytes_used,
            "roundsPerHourUsed": len(self.round_times),
        }
        for key, cap in (
            ("roundsMax", "maxRounds"),
            ("responseBytesMax", "maxCumulativeResponsePlaintextBytes"),
            ("queryBytesMax", "maxCumulativeQueryPlaintextBytes"),
            ("roundsPerHourMax", "maxRoundsPerHour"),
        ):
            if cap in caps:
                out[key] = caps[cap]
        return out

    def go_terminal(self, code: str, message: str, detail: dict[str, Any] | None = None) -> Terminal:
        if self.terminal is None:
            self.terminal = Terminal(code, message, detail)
            self._event("terminal", code=code)
            self.cond.notify_all()
        return self.terminal

    def _limit(self, cap: str, limit: int, observed: int) -> Terminal:
        return self.go_terminal("LIMIT_EXCEEDED", f"{cap} exceeded", {"cap": cap, "limit": limit, "observed": observed})

    def info(self, role: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "apiVersion": self.api_version,
            "studyId": f"study-{self.scenario.id}",
            "jobId": f"job-{self.spec.leg_id}",
            "legId": self.spec.leg_id,
            "orgSlug": "hub" if role == "destination" else self.spec.peer_org_slug,
            "peerOrgSlug": self.spec.peer_org_slug,
            "role": role,
            "direction": "dst_to_src",
            "state": self.state(),
            "caps": dict(self.spec.caps),
        }
        if role == "source":
            if self.spec.guards is not None:
                out["guards"] = self.spec.guards
            if self.spec.operations is not None:
                out["operations"] = self.spec.operations
        return out

    def snapshot(self) -> dict[str, Any]:
        with self.cond:
            return {
                "legId": self.spec.leg_id,
                "state": self.state(),
                "terminal": self.terminal.body() if self.terminal else None,
                "inFlight": self.in_flight.correlation_id if self.in_flight else None,
                "budget": self.budget(),
                "requestCalls": self.request_calls,
                "respondCalls": self.respond_calls,
                "rounds": [
                    {
                        "correlationId": r.correlation_id,
                        "roundNo": r.round_no,
                        "queryDeliveries": r.query_delivery_count,
                        "hasResponse": r.has_response,
                        "responseDropped": r.response_dropped,
                        "responseDeliveries": r.response_delivery_count,
                        "responseDelivered": r.response_delivered,
                        "abandoned": r.abandoned,
                    }
                    for r in self.rounds.values()
                ],
                "log": list(self.log),
            }

    # -- caps at the source tunnel ------------------------------------------

    def _check_query_caps(self, rnd: Round) -> Terminal | None:
        caps = self.spec.caps
        per_round = caps.get("maxQueryPlaintextBytesPerRound")
        if per_round is not None and rnd.query_bytes > per_round:
            return self._limit("maxQueryPlaintextBytesPerRound", per_round, rnd.query_bytes)
        total = caps.get("maxCumulativeQueryPlaintextBytes")
        if total is not None and self.query_bytes_used + rnd.query_bytes > total:
            return self._limit("maxCumulativeQueryPlaintextBytes", total, self.query_bytes_used + rnd.query_bytes)
        max_rounds = caps.get("maxRounds")
        if max_rounds is not None and self.rounds_used + 1 > max_rounds:
            return self._limit("maxRounds", max_rounds, self.rounds_used + 1)
        per_hour = caps.get("maxRoundsPerHour")
        if per_hour is not None and len(self.round_times) + 1 > per_hour:
            return self._limit("maxRoundsPerHour", per_hour, len(self.round_times) + 1)
        return None

    def _check_response_caps(self, nbytes: int) -> Terminal | None:
        caps = self.spec.caps
        per_round = caps.get("maxResponsePlaintextBytesPerRound")
        if per_round is not None and nbytes > per_round:
            return self._limit("maxResponsePlaintextBytesPerRound", per_round, nbytes)
        total = caps.get("maxCumulativeResponsePlaintextBytes")
        if total is not None and self.response_bytes_used + nbytes > total:
            return self._limit("maxCumulativeResponsePlaintextBytes", total, self.response_bytes_used + nbytes)
        return None

    # -- destination routes ---------------------------------------------------

    def dst_request(self, body: JSON) -> tuple[int, JSON]:
        with self.cond:
            self.request_calls += 1
            n = self.request_calls
            faults = self.spec.faults
            if self._inject("nr", faults.not_ready_on_request, n):
                return 503, error("NOT_READY", f"tunnel is {STATE_ATTACHED} (injected)")
            if self._inject("se", faults.server_error_on_request, n):
                return 500, error("INTERNAL", "injected")
            if self._inject("bp", faults.backpressure_on_request, n):
                self._event("backpressure", route="request")
                return 429, error("BACKPRESSURE", "relay window full (injected)")
            if not isinstance(body, dict) or "payload" not in body:
                return 400, error("VALIDATION", "invalid body", issues=[{"path": "payload", "message": "required"}])
            cid = body.get("correlationId")
            if cid is not None and not isinstance(cid, str):
                return 400, error(
                    "VALIDATION", "invalid body", issues=[{"path": "correlationId", "message": "must be a string"}]
                )
            # Re-issue of a known round: idempotent, same id.
            if cid is not None and cid in self.rounds and not self.rounds[cid].abandoned:
                rnd = self.rounds[cid]
                if self.in_flight is not None and self.in_flight is not rnd:
                    return 409, error(
                        "CONFLICT", "another round is in flight", correlationId=self.in_flight.correlation_id
                    )
                if rnd.response_delivered:
                    return 409, error("CONFLICT", "round already complete", correlationId=cid)
                rnd.dst_knows = True
                self.in_flight = rnd
                self._event("reissue", correlationId=cid)
                # The source tunnel dedups by correlationId: replay a cached response, else nothing to do.
                if rnd.has_response and rnd.response_dropped:
                    rnd.response_dropped = False
                    self._event("cached_replay", correlationId=cid)
                self.cond.notify_all()
                return 202, {"correlationId": cid, "reissued": True}
            if self.in_flight is not None:
                return 409, error("CONFLICT", "another round is in flight", correlationId=self.in_flight.correlation_id)
            self.round_counter += 1
            payload = body["payload"]
            rnd = Round(
                correlation_id=cid or _id(),
                round_no=self.round_counter,
                query_payload=payload,
                query_bytes=canonical_bytes(payload),
                submitted_at=time.monotonic(),
            )
            delay = faults.delay_delivery_ms.get(rnd.round_no, 0)
            rnd.query_deliverable_at = time.monotonic() + delay / 1000.0
            self.rounds[rnd.correlation_id] = rnd
            self.in_flight = rnd
            self._event("request", correlationId=rnd.correlation_id, roundNo=rnd.round_no, bytes=rnd.query_bytes)
            # Relay → source tunnel: query-side caps are metered on receipt.
            if self._check_query_caps(rnd) is None:
                self.query_bytes_used += rnd.query_bytes
                self.order.append(rnd)
            self.cond.notify_all()
            return 202, {"correlationId": rnd.correlation_id, "reissued": False}

    def dst_poll_response(self, cid: str, hold_s: float) -> tuple[int, JSON]:
        deadline = time.monotonic() + hold_s
        with self.cond:
            rnd = self.rounds.get(cid)
            if rnd is None or not rnd.dst_knows or rnd.abandoned:
                return 404, error("NOT_FOUND", "unknown correlationId", correlationId=cid)
            if rnd.round_no in self.spec.faults.forget_correlation_rounds and not rnd.forgotten_once:
                rnd.forgotten_once = True
                rnd.dst_knows = False
                if self.in_flight is rnd:
                    self.in_flight = None
                self._event("forget", correlationId=cid)
                return 404, error("NOT_FOUND", "unknown correlationId", correlationId=cid)
            while True:
                if self.terminal is not None:
                    return 200, self.terminal.body()
                if rnd.has_response and not rnd.response_dropped and not rnd.response_delivered:
                    # Delivery is the acknowledgement: the round is complete.
                    rnd.response_delivery_count += 1
                    rnd.response_delivered = True
                    if self.in_flight is rnd:
                        self.in_flight = None
                    self._event("response_delivered", correlationId=cid)
                    message = rnd.response_message(self.budget())
                    if self.spec.faults.error_after_round == rnd.round_no:
                        self.go_terminal("SESSION_ERRORED", "relay: session errored (injected)")
                    self.cond.notify_all()
                    return 200, message
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stopping:
                    return 204, None
                self.cond.wait(remaining)

    def dst_abandon(self, cid: str) -> tuple[int, JSON]:
        """Drop the in-flight entry; a late response is swallowed. Idempotent, also for unknown ids."""
        with self.cond:
            rnd = self.rounds.get(cid)
            if rnd is not None:
                rnd.abandoned = True
                if self.in_flight is rnd:
                    self.in_flight = None
                if rnd.has_response:
                    rnd.response_delivered = True
                self._event("abandon", correlationId=cid)
                self.cond.notify_all()
            return 204, None

    def dst_complete(self) -> tuple[int, JSON]:
        with self.cond:
            self.go_terminal("STUDY_COMPLETE", "analysis complete")
            self._event("complete")
            return 202, {"state": self.state()}

    # -- source routes -----------------------------------------------------------

    def _next_deliverable(self) -> Round | None:
        now = time.monotonic()
        for rnd in sorted(self.rounds.values(), key=lambda r: r.submitted_at):
            if rnd.redeliver_pending:
                return rnd
        if self.order and self.order[0].query_deliverable_at <= now:
            return self.order[0]
        return None

    def src_next(self, hold_s: float) -> tuple[int, JSON]:
        deadline = time.monotonic() + hold_s
        with self.cond:
            while True:
                if self.terminal is not None:
                    return 200, self.terminal.body()
                rnd = self._next_deliverable()
                if rnd is not None:
                    if rnd.query_delivery_count == 0:
                        self.order.popleft()
                    rnd.query_delivery_count += 1
                    # The RC-crash simulation: deliver this query once more on the next poll, regardless of
                    # whether a response arrives in between; the SDK memo must answer it.
                    rnd.redeliver_pending = (
                        rnd.query_delivery_count == 1 and rnd.round_no in self.spec.faults.redeliver_rounds
                    )
                    self._event("query_delivered", correlationId=rnd.correlation_id, count=rnd.query_delivery_count)
                    return 200, rnd.query_message(self.budget())
                wait_for = deadline - time.monotonic()
                if self.order:
                    wait_for = min(wait_for, max(0.0, self.order[0].query_deliverable_at - time.monotonic()))
                if deadline - time.monotonic() <= 0 or self.stopping:
                    return 204, None
                self.cond.wait(max(0.001, wait_for))

    def src_respond(self, body: JSON) -> tuple[int, JSON]:
        with self.cond:
            self.respond_calls += 1
            if self._inject("bpr", self.spec.faults.backpressure_on_respond, self.respond_calls):
                self._event("backpressure", route="respond")
                return 429, error("BACKPRESSURE", "relay window full (injected)")
            if not isinstance(body, dict) or "payload" not in body or not isinstance(body.get("inReplyTo"), str):
                return 400, error(
                    "VALIDATION", "invalid body", issues=[{"path": "inReplyTo", "message": "required string"}]
                )
            if self.terminal is not None:
                return 200, self.terminal.body()
            rnd = self.rounds.get(body["inReplyTo"])
            if rnd is None or rnd.query_delivery_count == 0:
                return 409, error("CONFLICT", "inReplyTo is not a delivered query", correlationId=body["inReplyTo"])
            if rnd.has_response:
                # Idempotent: the tunnel already holds a response for this correlationId.
                self._event("respond_replayed", correlationId=rnd.correlation_id)
                return 202, {"messageId": rnd.response_message_id, "replayed": True, "budget": self.budget()}
            nbytes = canonical_bytes(body["payload"])
            t = self._check_response_caps(nbytes)
            if t is not None:
                return 200, t.body()
            rnd.response_payload = body["payload"]
            rnd.response_bytes = nbytes
            rnd.response_message_id = _id()
            rnd.response_received_at = _now_iso()
            rnd.response_dropped = rnd.round_no in self.spec.faults.drop_response_rounds
            if rnd.abandoned:
                rnd.response_delivered = True
            self.response_bytes_used += nbytes
            self.rounds_used += 1
            self.round_times.append(time.monotonic())
            self._event("respond", correlationId=rnd.correlation_id, bytes=nbytes, dropped=rnd.response_dropped)
            self.cond.notify_all()
            return 202, {"messageId": rnd.response_message_id, "replayed": False, "budget": self.budget()}


# -- HTTP layer -----------------------------------------------------------------


def _make_handler(leg: Leg, role: str, verbose: bool) -> type[BaseHTTPRequestHandler]:
    hold_s = leg.scenario.longpoll_ms / 1000.0

    class Handler(BaseHTTPRequestHandler):
        server_version = "fake-tunnel-pair/2"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            if verbose:
                super().log_message(fmt, *args)

        # -- plumbing --

        def _send(self, status: int, body: JSON) -> None:
            data = b"" if status == 204 or body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            if data:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _read_body(self) -> tuple[int, JSON]:
            """(0, body) for a JSON body, else (status, error body) to send."""
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if length > leg.scenario.max_body_bytes:
                return 413, error("TOO_LARGE", f"body exceeds {leg.scenario.max_body_bytes} bytes")
            if not raw:
                return 0, {}
            try:
                return 0, json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, error("VALIDATION", "body is not JSON", issues=[{"path": "", "message": "invalid JSON"}])

        def _authorized(self) -> bool:
            return self.headers.get("Authorization", "") == f"Bearer {leg.tokens[role]}"

        def _gate(self, path: str, allowed_role: str | None) -> bool:
            """Common checks. Returns True when the request was already answered."""
            if not self._authorized():
                self._send(401, error("UNAUTHORIZED", "missing or wrong bearer token"))
                return True
            if path == "/v1/info":
                return False
            with leg.cond:
                if leg.terminal is not None:
                    self._send(200, leg.terminal.body())
                    return True
                if not leg.ready():
                    self._send(503, error("NOT_READY", f"tunnel is {leg.state()}"))
                    return True
            if allowed_role is not None and role != allowed_role:
                self._send(403, error("FORBIDDEN", f"not allowed for role {role}"))
                return True
            return False

        # -- routes --

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/_fake/snapshot":
                self._send(200, leg.snapshot())
                return
            if path == "/v1/info":
                if not self._gate(path, None):
                    with leg.cond:
                        self._send(200, leg.info(role))
                return
            if path.startswith("/v1/responses/"):
                if not self._gate(path, "destination"):
                    self._send(*leg.dst_poll_response(path[len("/v1/responses/") :], hold_s))
                return
            if path == "/v1/messages/next":
                if not self._gate(path, "source"):
                    self._send(*leg.src_next(hold_s))
                return
            self._send(404, error("NOT_FOUND", "no such route"))

        def do_DELETE(self) -> None:
            path = self.path.split("?", 1)[0]
            if path.startswith("/v1/request/"):
                if not self._gate(path, "destination"):
                    self._send(*leg.dst_abandon(path[len("/v1/request/") :]))
                return
            self._send(404, error("NOT_FOUND", "no such route"))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            routes = {"/v1/request": "destination", "/v1/complete": "destination", "/v1/messages": "source"}
            if path not in routes:
                self._send(404, error("NOT_FOUND", "no such route"))
                return
            if self._gate(path, routes[path]):
                return
            status, body = self._read_body()
            if status:
                self._send(status, body)
            elif path == "/v1/request":
                self._send(*leg.dst_request(body))
            elif path == "/v1/complete":
                self._send(*leg.dst_complete())
            else:
                self._send(*leg.src_respond(body))

    return Handler


@dataclass(frozen=True)
class TunnelEndpoint:
    endpoint: str
    token: str


@dataclass(frozen=True)
class LegEndpoints:
    leg_id: str
    peer_org_slug: str
    destination: TunnelEndpoint
    source: TunnelEndpoint

    def to_json(self) -> dict[str, Any]:
        return {
            "legId": self.leg_id,
            "peerOrgSlug": self.peer_org_slug,
            "destination": {"endpoint": self.destination.endpoint, "token": self.destination.token},
            "source": {"endpoint": self.source.endpoint, "token": self.source.token},
        }


class FakeTunnelPair:
    """Start/stop the servers for every leg of a scenario."""

    def __init__(
        self,
        scenario: Scenario,
        *,
        host: str = "127.0.0.1",
        port_base: int = 0,
        fixed_tokens: bool = False,
        verbose: bool = False,
        advertise_host: str | None = None,
        api_version: str = API_VERSION,
    ) -> None:
        self.scenario = scenario
        self.api_version = api_version
        self.host = host
        self.port_base = port_base
        self.fixed_tokens = fixed_tokens
        self.verbose = verbose
        self.advertise_host = advertise_host or host
        self.legs: list[Leg] = []
        self._servers: list[ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []
        self.endpoints: list[LegEndpoints] = []

    def start(self) -> list[LegEndpoints]:
        next_port = self.port_base
        for spec in self.scenario.legs:
            leg = Leg(spec, self.scenario, fixed_tokens=self.fixed_tokens, api_version=self.api_version)
            self.legs.append(leg)
            urls: dict[str, str] = {}
            for role in ("destination", "source"):
                srv = ThreadingHTTPServer(
                    (self.host, next_port if self.port_base else 0), _make_handler(leg, role, self.verbose)
                )
                srv.daemon_threads = True
                port = srv.server_address[1]
                if self.port_base:
                    next_port += 1
                urls[role] = f"http://{self.advertise_host}:{port}"
                t = threading.Thread(
                    target=srv.serve_forever,
                    kwargs={"poll_interval": 0.1},
                    daemon=True,
                    name=f"fake-{spec.leg_id}-{role}",
                )
                t.start()
                self._servers.append(srv)
                self._threads.append(t)
            self.endpoints.append(
                LegEndpoints(
                    leg_id=spec.leg_id,
                    peer_org_slug=spec.peer_org_slug,
                    destination=TunnelEndpoint(urls["destination"], leg.tokens["destination"]),
                    source=TunnelEndpoint(urls["source"], leg.tokens["source"]),
                )
            )
        return self.endpoints

    def stop(self) -> None:
        for leg in self.legs:
            with leg.cond:
                leg.stopping = True
                leg.cond.notify_all()
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        for t in self._threads:
            t.join(timeout=5)
        self._servers.clear()
        self._threads.clear()

    def __enter__(self) -> FakeTunnelPair:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def leg(self, leg_id: str) -> Leg:
        for leg in self.legs:
            if leg.spec.leg_id == leg_id:
                return leg
        raise KeyError(leg_id)

    def env(self, role: str, style: str = "auto") -> dict[str, str]:
        """Environment variables per spec/env.md for one role.

        style: "single" (FUSION_TUNNEL_ENDPOINT/TOKEN, one leg only), "map"
        (FUSION_TUNNEL_ENDPOINTS/TOKENS keyed by legId) or "auto" (single when
        there is exactly one leg).
        """
        if role not in ("destination", "source"):
            raise ValueError(role)
        if style == "auto":
            style = "single" if len(self.endpoints) == 1 else "map"
        env = {"FUSION_ROLE": role}
        if style == "single":
            if len(self.endpoints) != 1:
                raise ValueError("single style needs exactly one leg")
            ep = getattr(self.endpoints[0], role)
            env["FUSION_TUNNEL_ENDPOINT"] = ep.endpoint
            env["FUSION_TUNNEL_TOKEN"] = ep.token
        else:
            env["FUSION_TUNNEL_ENDPOINTS"] = json.dumps({e.leg_id: getattr(e, role).endpoint for e in self.endpoints})
            env["FUSION_TUNNEL_TOKENS"] = json.dumps({e.leg_id: getattr(e, role).token for e in self.endpoints})
        return env

    def announce(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pid": os.getpid(),
            "scenario": self.scenario.id,
            "legs": [e.to_json() for e in self.endpoints],
            "env": {},
        }
        for role in ("destination", "source"):
            out["env"][role] = {
                "map": self.env(role, "map"),
                "single": self.env(role, "single") if len(self.endpoints) == 1 else None,
            }
        return out
