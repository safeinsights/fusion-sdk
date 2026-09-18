"""Two local-API servers per leg (destination + source) joined by an in-memory relay.

One `Leg` object is the single source of truth for a leg: the destination tunnel's
in-flight bookkeeping, the relay's mailbox, and the source tunnel's delivery, ACK and
cached-response state. Both HTTP handlers mutate it under one condition variable, and
long-polls wait on that condition. Fault injection (spec/scenarios/README.md) is applied
exactly where the corresponding real component would misbehave.
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

API_VERSION = "1.0.0"
JSON = Any

STATE_ATTACHED = "RELAY_ATTACHED"
STATE_UP = "CHANNEL_UP"
STATE_CLOSING = "CLOSING"
STATE_CLOSED = "CLOSED"
STATE_ERRORED = "ERRORED"
STATE_LIMIT = "LIMIT_EXCEEDED"

TERMINAL_STATE = {"STUDY_COMPLETE": STATE_CLOSED, "SESSION_ERRORED": STATE_ERRORED, "LIMIT_EXCEEDED": STATE_LIMIT}


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def canonical_bytes(payload: JSON) -> int:
    """The byte count the tunnel meters: canonical UTF-8 serialization of the payload (T4)."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


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
    query_message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    query_deliverable_at: float = 0.0
    query_delivery_count: int = 0
    query_delivered_at: float | None = None
    query_acked: bool = False
    redeliver_after_ack_pending: bool = False
    dst_knows: bool = True
    forgotten_once: bool = False
    response_payload: JSON = None
    response_message_id: str | None = None
    response_bytes: int = 0
    response_received_at: str | None = None
    response_dropped: bool = False
    response_delivered_count: int = 0
    response_acked: bool = False

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

    def __init__(self, spec: LegSpec, scenario: Scenario, fixed_tokens: bool = False) -> None:
        self.spec = spec
        self.scenario = scenario
        self.cond = threading.Condition()
        self.started_at = time.monotonic()
        self.ready_at = self.started_at + scenario.ready_delay_ms / 1000.0
        self.stopping = False
        self.tokens = {
            "destination": f"fake-{spec.leg_id}-destination" if fixed_tokens else secrets.token_urlsafe(24),
            "source": f"fake-{spec.leg_id}-source" if fixed_tokens else secrets.token_urlsafe(24),
        }
        self.terminal: Terminal | None = None
        self.closing = False
        self.closed = False
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
        if spec.faults.error_after_round == 0:
            self.terminal = Terminal("SESSION_ERRORED", "relay: session errored at channel up")

    # -- helpers ----------------------------------------------------------

    def _event(self, name: str, **fields: Any) -> None:
        self.log.append({"t": round(time.monotonic() - self.started_at, 3), "event": name, **fields})

    def ready(self) -> bool:
        return time.monotonic() >= self.ready_at

    def state(self) -> str:
        if self.terminal is not None:
            return TERMINAL_STATE[self.terminal.code]
        if self.closed:
            return STATE_CLOSED
        if self.closing:
            return STATE_CLOSING
        return STATE_UP if self.ready() else STATE_ATTACHED

    def budget(self) -> dict[str, Any]:
        caps = self.spec.caps
        cutoff = time.monotonic() - 3600
        while self.round_times and self.round_times[0] < cutoff:
            self.round_times.popleft()
        return {
            "roundsUsed": self.rounds_used,
            "roundsMax": caps.max_rounds,
            "responseBytesUsed": self.response_bytes_used,
            "responseBytesMax": caps.max_response_bytes,
            "queryBytesUsed": self.query_bytes_used,
            "queryBytesMax": caps.max_query_bytes,
            "roundsPerHourUsed": len(self.round_times),
            "roundsPerHourMax": caps.max_rounds_per_hour,
        }

    def go_terminal(self, code: str, message: str, detail: dict[str, Any] | None = None) -> Terminal:
        if self.terminal is None:
            self.terminal = Terminal(code, message, detail)
            self._event("terminal", code=code)
            self.cond.notify_all()
        return self.terminal

    def info(self, role: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "apiVersion": API_VERSION,
            "legId": self.spec.leg_id,
            "peerOrgSlug": self.spec.peer_org_slug,
            "role": role,
            "direction": "dst_to_src",
            "state": self.state(),
        }
        if role == "source":
            if self.spec.guards is not None:
                out["guards"] = self.spec.guards
            if self.spec.caps.any_set():
                out["caps"] = self.spec.caps.to_json()
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
                        "queryAcked": r.query_acked,
                        "hasResponse": r.has_response,
                        "responseDropped": r.response_dropped,
                        "responseDeliveries": r.response_delivered_count,
                        "responseAcked": r.response_acked,
                    }
                    for r in self.rounds.values()
                ],
                "log": list(self.log),
            }

    # -- caps at the source tunnel ------------------------------------------

    def _check_query_caps(self, rnd: Round) -> Terminal | None:
        caps = self.spec.caps
        if caps.max_query_bytes_per_round is not None and rnd.query_bytes > caps.max_query_bytes_per_round:
            return self.go_terminal(
                "LIMIT_EXCEEDED",
                "query exceeds maxQueryBytesPerRound",
                {"cap": "maxQueryBytesPerRound", "limit": caps.max_query_bytes_per_round, "observed": rnd.query_bytes},
            )
        if caps.max_query_bytes is not None and self.query_bytes_used + rnd.query_bytes > caps.max_query_bytes:
            return self.go_terminal(
                "LIMIT_EXCEEDED",
                "cumulative query bytes exceed maxQueryBytes",
                {
                    "cap": "maxQueryBytes",
                    "limit": caps.max_query_bytes,
                    "observed": self.query_bytes_used + rnd.query_bytes,
                },
            )
        if caps.max_rounds is not None and self.rounds_used + 1 > caps.max_rounds:
            return self.go_terminal(
                "LIMIT_EXCEEDED",
                "round count exceeds maxRounds",
                {"cap": "maxRounds", "limit": caps.max_rounds, "observed": self.rounds_used + 1},
            )
        if caps.max_rounds_per_hour is not None and len(self.round_times) + 1 > caps.max_rounds_per_hour:
            return self.go_terminal(
                "LIMIT_EXCEEDED",
                "round rate exceeds maxRoundsPerHour",
                {"cap": "maxRoundsPerHour", "limit": caps.max_rounds_per_hour, "observed": len(self.round_times) + 1},
            )
        return None

    def _check_response_caps(self, nbytes: int) -> Terminal | None:
        caps = self.spec.caps
        if caps.max_response_bytes_per_round is not None and nbytes > caps.max_response_bytes_per_round:
            return self.go_terminal(
                "LIMIT_EXCEEDED",
                "response exceeds maxResponseBytesPerRound",
                {"cap": "maxResponseBytesPerRound", "limit": caps.max_response_bytes_per_round, "observed": nbytes},
            )
        if caps.max_response_bytes is not None and self.response_bytes_used + nbytes > caps.max_response_bytes:
            return self.go_terminal(
                "LIMIT_EXCEEDED",
                "cumulative response bytes exceed maxResponseBytes",
                {
                    "cap": "maxResponseBytes",
                    "limit": caps.max_response_bytes,
                    "observed": self.response_bytes_used + nbytes,
                },
            )
        return None

    # -- destination routes ---------------------------------------------------

    def dst_request(self, body: JSON) -> tuple[int, JSON]:
        with self.cond:
            self.request_calls += 1
            n = self.request_calls
            faults = self.spec.faults
            if n in faults.not_ready_on_request and not getattr(self, f"_nr{n}", False):
                setattr(self, f"_nr{n}", True)
                return 503, {"code": "NOT_READY", "state": STATE_ATTACHED}
            if n in faults.server_error_on_request and not getattr(self, f"_se{n}", False):
                setattr(self, f"_se{n}", True)
                return 500, {"code": "INTERNAL", "message": "injected"}
            if n in faults.backpressure_on_request and not getattr(self, f"_bp{n}", False):
                setattr(self, f"_bp{n}", True)
                self._event("backpressure", route="request")
                return 429, {"code": "BACKPRESSURE", "retryable": True}
            if not isinstance(body, dict) or "payload" not in body:
                return 422, {"code": "SCHEMA_REJECTED", "detail": "body must be {payload, correlationId?}"}
            cid = body.get("correlationId")
            if cid is not None and not isinstance(cid, str):
                return 422, {"code": "SCHEMA_REJECTED", "detail": "correlationId must be a string"}
            if self.closing or self.closed:
                return 200, Terminal("STUDY_COMPLETE").body()
            # Re-issue (T1).
            if cid is not None and cid in self.rounds:
                rnd = self.rounds[cid]
                if self.in_flight is not None and self.in_flight is not rnd:
                    return 409, {"code": "IN_FLIGHT_CONFLICT", "correlationId": self.in_flight.correlation_id}
                if rnd.response_acked:
                    # A completed round cannot be re-issued.
                    return 409, {
                        "code": "IN_FLIGHT_CONFLICT",
                        "correlationId": cid,
                        "message": "round already complete",
                    }
                rnd.dst_knows = True
                self.in_flight = rnd
                self._event("reissue", correlationId=cid)
                # The source tunnel dedups by correlationId: replay a cached response, else nothing to do.
                if rnd.has_response and rnd.response_dropped:
                    rnd.response_dropped = False
                    self._event("cached_replay", correlationId=cid)
                self.cond.notify_all()
                return 202, {"correlationId": cid}
            if self.in_flight is not None:
                return 409, {"code": "IN_FLIGHT_CONFLICT", "correlationId": self.in_flight.correlation_id}
            self.round_counter += 1
            payload = body["payload"]
            rnd = Round(
                correlation_id=cid or uuid.uuid4().hex,
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
            return 202, {"correlationId": rnd.correlation_id}

    def dst_poll_response(self, cid: str, hold_s: float) -> tuple[int, JSON]:
        deadline = time.monotonic() + hold_s
        with self.cond:
            rnd = self.rounds.get(cid)
            if rnd is None or not rnd.dst_knows:
                return 404, {"code": "UNKNOWN_CORRELATION", "correlationId": cid}
            if rnd.round_no in self.spec.faults.forget_correlation_rounds and not rnd.forgotten_once:
                rnd.forgotten_once = True
                rnd.dst_knows = False
                if self.in_flight is rnd:
                    self.in_flight = None
                self._event("forget", correlationId=cid)
                return 404, {"code": "UNKNOWN_CORRELATION", "correlationId": cid}
            while True:
                if self.terminal is not None:
                    return 200, self.terminal.body()
                if rnd.has_response and not rnd.response_dropped and not rnd.response_acked:
                    rnd.response_delivered_count += 1
                    self._event("response_delivered", correlationId=cid, count=rnd.response_delivered_count)
                    return 200, rnd.response_message(self.budget())
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stopping:
                    return 204, None
                self.cond.wait(remaining)

    def dst_ack(self, message_id: str) -> tuple[int, JSON]:
        with self.cond:
            for rnd in self.rounds.values():
                if rnd.response_message_id == message_id:
                    if not rnd.response_acked:
                        rnd.response_acked = True
                        if self.in_flight is rnd:
                            self.in_flight = None
                        self._event("response_acked", correlationId=rnd.correlation_id)
                        if self.spec.faults.error_after_round == rnd.round_no:
                            self.go_terminal("SESSION_ERRORED", "relay: session errored (injected)")
                        self.cond.notify_all()
                    return 204, None
            return 404, {"code": "UNKNOWN_MESSAGE", "messageId": message_id}

    def dst_complete(self) -> tuple[int, JSON]:
        with self.cond:
            if self.closing or self.closed:
                return 202, {}
            if self.in_flight is not None:
                return 409, {"code": "IN_FLIGHT_CONFLICT", "correlationId": self.in_flight.correlation_id}
            self.closing = True
            self.closed = True
            self.terminal = Terminal("STUDY_COMPLETE", "analysis complete")
            self._event("complete")
            self.cond.notify_all()
            return 202, {}

    # -- source routes -----------------------------------------------------------

    def _next_deliverable(self) -> Round | None:
        now = time.monotonic()
        # Redelivery of un-ACKed or fault-flagged deliveries takes precedence (FIFO by submission).
        for rnd in sorted(self.rounds.values(), key=lambda r: r.submitted_at):
            if rnd.query_delivery_count == 0:
                continue
            if rnd.redeliver_after_ack_pending:
                return rnd
            if (
                not rnd.query_acked
                and rnd.query_delivered_at is not None
                and now - rnd.query_delivered_at >= self.scenario.redelivery_ms / 1000.0
            ):
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
                    rnd.query_delivered_at = time.monotonic()
                    rnd.redeliver_after_ack_pending = False
                    self._event("query_delivered", correlationId=rnd.correlation_id, count=rnd.query_delivery_count)
                    if rnd.query_delivery_count > self.scenario.max_deliveries:
                        t = self.go_terminal(
                            "SESSION_ERRORED",
                            "relay: dead-letter, MAX_DELIVERIES exceeded",
                            {"correlationId": rnd.correlation_id, "deliveries": rnd.query_delivery_count},
                        )
                        return 200, t.body()
                    return 200, rnd.query_message(self.budget())
                # Wake-ups for scheduled deliveries.
                wait_for = deadline - time.monotonic()
                if self.order:
                    wait_for = min(wait_for, max(0.0, self.order[0].query_deliverable_at - time.monotonic()))
                pending = [
                    r
                    for r in self.rounds.values()
                    if r.query_delivery_count and not r.query_acked and r.query_delivered_at is not None
                ]
                if pending:
                    soonest = min(
                        r.query_delivered_at + self.scenario.redelivery_ms / 1000.0
                        for r in pending
                        if r.query_delivered_at is not None
                    )
                    wait_for = min(wait_for, max(0.0, soonest - time.monotonic()))
                if deadline - time.monotonic() <= 0 or self.stopping:
                    return 204, None
                self.cond.wait(max(0.001, wait_for))

    def src_ack(self, message_id: str) -> tuple[int, JSON]:
        with self.cond:
            for rnd in self.rounds.values():
                if rnd.query_message_id == message_id:
                    if not rnd.query_acked:
                        rnd.query_acked = True
                        self._event("query_acked", correlationId=rnd.correlation_id)
                        if rnd.round_no in self.spec.faults.redeliver_after_ack_rounds:
                            rnd.redeliver_after_ack_pending = True
                            self._event("redeliver_after_ack_scheduled", correlationId=rnd.correlation_id)
                        self.cond.notify_all()
                    return 204, None
            return 404, {"code": "UNKNOWN_MESSAGE", "messageId": message_id}

    def src_respond(self, body: JSON) -> tuple[int, JSON]:
        with self.cond:
            self.respond_calls += 1
            n = self.respond_calls
            if n in self.spec.faults.backpressure_on_respond and not getattr(self, f"_bpr{n}", False):
                setattr(self, f"_bpr{n}", True)
                self._event("backpressure", route="respond")
                return 429, {"code": "BACKPRESSURE", "retryable": True}
            if not isinstance(body, dict) or "payload" not in body or not isinstance(body.get("inReplyTo"), str):
                return 422, {"code": "SCHEMA_REJECTED", "detail": "body must be {inReplyTo, payload}"}
            if self.terminal is not None:
                return 200, self.terminal.body()
            rnd = self.rounds.get(body["inReplyTo"])
            if rnd is None or rnd.query_delivery_count == 0:
                return 400, {"code": "UNKNOWN_IN_REPLY_TO", "inReplyTo": body["inReplyTo"]}
            if rnd.has_response:
                # Idempotent: the tunnel already holds a response for this correlationId.
                self._event("respond_duplicate", correlationId=rnd.correlation_id)
                return 202, {"messageId": rnd.response_message_id, "budget": self.budget()}
            nbytes = canonical_bytes(body["payload"])
            t = self._check_response_caps(nbytes)
            if t is not None:
                return 200, t.body()
            rnd.response_payload = body["payload"]
            rnd.response_bytes = nbytes
            rnd.response_message_id = uuid.uuid4().hex
            rnd.response_received_at = _now_iso()
            rnd.response_dropped = rnd.round_no in self.spec.faults.drop_response_rounds
            self.response_bytes_used += nbytes
            self.rounds_used += 1
            self.round_times.append(time.monotonic())
            self._event("respond", correlationId=rnd.correlation_id, bytes=nbytes, dropped=rnd.response_dropped)
            self.cond.notify_all()
            return 202, {"messageId": rnd.response_message_id, "budget": self.budget()}


# -- HTTP layer -----------------------------------------------------------------


def _make_handler(leg: Leg, role: str, verbose: bool) -> type[BaseHTTPRequestHandler]:
    hold_s = leg.scenario.longpoll_ms / 1000.0

    class Handler(BaseHTTPRequestHandler):
        server_version = "fake-tunnel-pair/1"
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

        def _read_json(self) -> tuple[bool, JSON]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return True, {}
            try:
                return True, json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False, None

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {leg.tokens[role]}"

        def _gate(self, path: str) -> bool:
            """Common checks. Returns True when the request was already answered."""
            if path.startswith("/_fake/"):
                return False
            if not self._authorized():
                self._send(401, {"code": "UNAUTHORIZED"})
                return True
            if path == "/v1/info":
                return False
            with leg.cond:
                if leg.terminal is not None and not (role == "destination" and path == "/v1/complete"):
                    self._send(200, leg.terminal.body())
                    return True
                if not leg.ready():
                    self._send(503, {"code": "NOT_READY", "state": leg.state()})
                    return True
            return False

        def _forbidden(self) -> None:
            self._send(403, {"code": "FORBIDDEN_DIRECTION", "role": role})

        # -- routes --

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/_fake/snapshot":
                self._send(200, leg.snapshot())
                return
            if self._gate(path):
                return
            if path == "/v1/info":
                with leg.cond:
                    self._send(200, leg.info(role))
                return
            if path.startswith("/v1/responses/"):
                if role != "destination":
                    self._forbidden()
                    return
                cid = path[len("/v1/responses/") :]
                status, body = leg.dst_poll_response(cid, hold_s)
                self._send(status, body)
                return
            if path == "/v1/messages/next":
                if role != "source":
                    self._forbidden()
                    return
                status, body = leg.src_next(hold_s)
                self._send(status, body)
                return
            self._send(404, {"code": "NOT_FOUND"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if self._gate(path):
                return
            ok, body = self._read_json()
            if not ok:
                self._send(422, {"code": "SCHEMA_REJECTED", "detail": "body is not JSON"})
                return
            if path == "/v1/request":
                if role != "destination":
                    self._forbidden()
                    return
                self._send(*leg.dst_request(body))
                return
            if path == "/v1/complete":
                if role != "destination":
                    self._forbidden()
                    return
                self._send(*leg.dst_complete())
                return
            if path == "/v1/messages":
                if role != "source":
                    self._forbidden()
                    return
                self._send(*leg.src_respond(body))
                return
            if path.startswith("/v1/messages/") and path.endswith("/ack"):
                message_id = path[len("/v1/messages/") : -len("/ack")]
                status, out = leg.dst_ack(message_id) if role == "destination" else leg.src_ack(message_id)
                self._send(status, out)
                return
            self._send(404, {"code": "NOT_FOUND"})

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
    ) -> None:
        self.scenario = scenario
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
            leg = Leg(spec, self.scenario, fixed_tokens=self.fixed_tokens)
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
