"""In-process simulator for the SafeInsights IDE / CRATE.

Runs a source handler set and a destination analysis in one process with no tunnel and no HTTP.
The simulator is an implementation of the same internal `Transport` interface the real tunnel client
implements, so `Fusion.connect()`, `Peer.request()` and the source `Server` run their real code paths:
envelopes, guards, the memo, error envelopes, budget hints and typed errors all behave as in a study.

It is synchronous: a destination `submit()` runs the source pipeline inline, so it works the same way
in single-threaded R. Optional fault injection lets researchers test their error handling.

    from safeinsights_fusion import OperationRegistry, simulate

    ops = OperationRegistry()
    @ops.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
    def counts_by_group(params, ctx): ...

    def analysis(fusion):
        return fusion.peer().request("counts_by_group", {"person_ids": ids}).to_pandas()

    df = simulate(ops, analysis, guards={"maxDistinctPersonIds": 5000, "minGroupSize": 11})
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ._config import Settings
from ._envelope import canonical_bytes
from ._transport import EMPTY, Budget, Delivered, Empty, Info, Terminal
from .destination import Fusion
from .errors import TerminalError
from .source import OperationRegistry, Server

JSON = Any
T = TypeVar("T")
API_VERSION = "2.0.0"


@dataclass(frozen=True)
class SimFaults:
    """Fault injection, numbered by round (1-based) like spec/scenarios.

    drop_response_rounds: the response for round n is not delivered until the SDK re-issues the same
        correlationId (exercises RoundTimeoutError handling when combined with a short `timeout`).
    error_after_round: the leg becomes SESSION_ERRORED after round n completes.
    max_rounds / max_response_bytes_per_round: caps the simulated source tunnel enforces (LIMIT_EXCEEDED
        with `cap` maxRounds / maxResponsePlaintextBytesPerRound).
    """

    drop_response_rounds: frozenset[int] = frozenset()
    error_after_round: int | None = None
    max_rounds: int | None = None
    max_response_bytes_per_round: int | None = None


@dataclass
class _Round:
    correlation_id: str
    round_no: int
    query: JSON
    response: JSON = None
    response_message_id: str | None = None
    deliverable: bool = False
    delivered: bool = False  # the destination has received the response (delivery is the acknowledgement)


@dataclass
class _LegState:
    peer: str
    leg_id: str
    guards: dict[str, Any] | None
    caps: dict[str, Any] | None
    operations: list[dict[str, Any]] | None
    faults: SimFaults
    rounds: dict[str, _Round] = field(default_factory=dict)
    round_counter: int = 0
    rounds_used: int = 0
    response_bytes_used: int = 0
    query_bytes_used: int = 0
    terminal: Terminal | None = None
    closed: bool = False

    def budget(self) -> Budget:
        return Budget(
            rounds_used=self.rounds_used,
            rounds_max=self.faults.max_rounds,
            response_bytes_used=self.response_bytes_used,
            response_bytes_max=None,
            query_bytes_used=self.query_bytes_used,
            query_bytes_max=None,
        )

    def info(self, role: str) -> Info:
        state = (
            "CLOSED"
            if self.closed
            else (
                "LIMIT_EXCEEDED"
                if self.terminal and self.terminal.code == "LIMIT_EXCEEDED"
                else ("ERRORED" if self.terminal else "CHANNEL_UP")
            )
        )
        return Info(
            api_version=API_VERSION,
            leg_id=self.leg_id,
            peer_org_slug=self.peer,
            role=role,
            direction="dst_to_src",
            state=state,
            guards=self.guards if role == "source" else None,
            caps=self.caps if role == "source" else None,
            operations=self.operations if role == "source" else None,
        )


def _wire(payload: JSON) -> JSON:
    """Every payload crosses the simulated wire as JSON text, exactly as it would through the tunnel."""
    return json.loads(json.dumps(payload, ensure_ascii=False))


class _SourceView:
    """What the simulated source `Server` sees: responses land in the shared leg state."""

    def __init__(self, leg: _LegState) -> None:
        self._leg = leg

    def info(self) -> Info:
        return self._leg.info("source")

    def submit(self, payload: JSON, correlation_id: str | None = None) -> str | Terminal:
        raise NotImplementedError("a source never submits")

    def poll_response(self, correlation_id: str, timeout_s: float) -> Delivered | Terminal | Empty:
        raise NotImplementedError("a source never polls responses")

    def abandon(self, correlation_id: str) -> None:
        raise NotImplementedError("a source never abandons")

    def next_message(self, timeout_s: float) -> Delivered | Terminal | Empty:
        # The simulator drives Server.step() directly; the loop is never run.
        return Terminal("STUDY_COMPLETE") if self._leg.closed else EMPTY

    def post_response(self, in_reply_to: str, payload: JSON) -> Budget | Terminal | None:
        leg = self._leg
        rnd = leg.rounds[in_reply_to]
        if rnd.response_message_id is not None:
            return leg.budget()
        nbytes = canonical_bytes(payload)
        cap = leg.faults.max_response_bytes_per_round
        if cap is not None and nbytes > cap:
            leg.terminal = Terminal(
                "LIMIT_EXCEEDED",
                "response exceeds maxResponsePlaintextBytesPerRound",
                {"cap": "maxResponsePlaintextBytesPerRound", "limit": cap, "observed": nbytes},
            )
            return leg.terminal
        rnd.response = _wire(payload)
        rnd.response_message_id = str(uuid.uuid4())
        rnd.deliverable = rnd.round_no not in leg.faults.drop_response_rounds
        leg.response_bytes_used += nbytes
        leg.rounds_used += 1
        return leg.budget()

    def complete(self) -> Terminal | None:
        raise NotImplementedError("a source never completes")


class _DestinationView:
    """What the destination `Peer` sees. `submit()` runs the source pipeline inline."""

    def __init__(self, leg: _LegState, server: Server) -> None:
        self._leg = leg
        self._server = server

    def info(self) -> Info:
        return self._leg.info("destination")

    def submit(self, payload: JSON, correlation_id: str | None = None) -> str | Terminal:
        leg = self._leg
        if leg.terminal is not None:
            return leg.terminal
        if leg.closed:
            return Terminal("STUDY_COMPLETE")
        if correlation_id is not None and correlation_id in leg.rounds:
            rnd = leg.rounds[correlation_id]
            if rnd.response_message_id is not None:
                rnd.deliverable = True  # the source tunnel replays its cached response
            return correlation_id
        leg.round_counter += 1
        if leg.faults.max_rounds is not None and leg.rounds_used + 1 > leg.faults.max_rounds:
            leg.terminal = Terminal(
                "LIMIT_EXCEEDED",
                "round count exceeds maxRounds",
                {"cap": "maxRounds", "limit": leg.faults.max_rounds, "observed": leg.rounds_used + 1},
            )
            return leg.terminal
        rnd = _Round(correlation_id or str(uuid.uuid4()), leg.round_counter, payload)
        leg.rounds[rnd.correlation_id] = rnd
        leg.query_bytes_used += canonical_bytes(payload)
        # Deliver to the source RC now: memo, guards, handler, encode, post_response.
        outcome = self._server.step(
            Delivered(str(uuid.uuid4()), rnd.correlation_id, _wire(payload), leg.budget(), None)
        )
        if outcome == "STUDY_COMPLETE":
            leg.closed = True
        return rnd.correlation_id

    def poll_response(self, correlation_id: str, timeout_s: float) -> Delivered | Terminal | Empty:
        leg = self._leg
        if leg.terminal is not None:
            return leg.terminal
        rnd = leg.rounds.get(correlation_id)
        if rnd is not None and rnd.deliverable and rnd.response_message_id is not None and not rnd.delivered:
            rnd.delivered = True
            if leg.faults.error_after_round == rnd.round_no:
                leg.terminal = Terminal("SESSION_ERRORED", "simulated session error")
            return Delivered(rnd.response_message_id, correlation_id, rnd.response, leg.budget(), None)
        time.sleep(min(timeout_s, 0.02))  # nothing to deliver yet: yield briefly instead of spinning
        return EMPTY

    def abandon(self, correlation_id: str) -> None:
        rnd = self._leg.rounds.get(correlation_id)
        if rnd is not None:
            rnd.delivered = True

    def next_message(self, timeout_s: float) -> Delivered | Terminal | Empty:
        raise NotImplementedError("a destination never polls messages")

    def post_response(self, in_reply_to: str, payload: JSON) -> Budget | Terminal | None:
        raise NotImplementedError("a destination never posts responses")

    def complete(self) -> Terminal | None:
        if self._leg.terminal is not None:
            return self._leg.terminal
        self._leg.closed = True
        return None


class Simulator:
    """A simulated study: one leg per peer, each with its own operations, guards and faults."""

    def __init__(
        self,
        operations: OperationRegistry | Mapping[str, OperationRegistry],
        *,
        guards: Mapping[str, Any] | None = None,
        caps: Mapping[str, Any] | None = None,
        approved_operations: list[dict[str, Any]] | None = None,
        faults: SimFaults | Mapping[str, SimFaults] | None = None,
        settings: Settings | None = None,
    ) -> None:
        regs: Mapping[str, OperationRegistry] = (
            operations if isinstance(operations, Mapping) else {"dp-sim": operations}
        )
        self.settings = settings or Settings(
            ready_timeout_s=5,
            ready_poll_s=0.01,
            poll_http_timeout_s=0.1,
            http_timeout_s=1,
            round_timeout_s=30,
            retry_base_ms=1,
            retry_max_ms=5,
        )
        self.legs: dict[str, _LegState] = {}
        self.servers: dict[str, Server] = {}
        transports = []
        for i, (peer, reg) in enumerate(regs.items()):
            f = faults.get(peer, SimFaults()) if isinstance(faults, Mapping) else (faults or SimFaults())
            leg = _LegState(
                peer,
                f"leg-{i + 1}",
                dict(guards) if guards else None,
                dict(caps) if caps else None,
                approved_operations,
                f,
            )
            server = Server(_SourceView(leg), reg, self.settings, label=peer)
            server.start(time.monotonic() + self.settings.ready_timeout_s)
            self.legs[peer] = leg
            self.servers[peer] = server
            transports.append((peer, _DestinationView(leg, server)))
        self.fusion = Fusion.connect(settings=self.settings, transports=transports)

    def run(self, analysis: Callable[[Fusion], T]) -> T:
        """Run `analysis(fusion)`; CLOSE every leg on a clean return (like the context manager)."""
        result = analysis(self.fusion)
        if not self.fusion.completed:
            with contextlib.suppress(TerminalError):
                self.fusion.complete()
        return result


def simulate(
    operations: OperationRegistry | Mapping[str, OperationRegistry],
    analysis: Callable[[Fusion], T],
    *,
    guards: Mapping[str, Any] | None = None,
    caps: Mapping[str, Any] | None = None,
    approved_operations: list[dict[str, Any]] | None = None,
    faults: SimFaults | Mapping[str, SimFaults] | None = None,
    settings: Settings | None = None,
) -> T:
    """Run a fusion analysis end to end in this process, with no tunnel.

    `operations` is one registry (two-party, peer "dp-sim") or {peer: registry} (hub). `analysis` receives a
    connected `Fusion` exactly as in a study and its return value is returned. `guards`, `caps` and
    `approved_operations` are what the source tunnel's /v1/info would carry; `faults` injects failures.
    """
    return Simulator(
        operations, guards=guards, caps=caps, approved_operations=approved_operations, faults=faults, settings=settings
    ).run(analysis)


__all__ = ["SimFaults", "Simulator", "simulate"]
