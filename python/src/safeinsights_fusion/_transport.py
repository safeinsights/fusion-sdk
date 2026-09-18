"""The internal transport interface: everything the destination and source logic need from a tunnel.

`_tunnel.TunnelClient` implements it over HTTP; `simulate` implements it in-process (Phase 6).
Nothing above this layer knows about URLs, tokens or status codes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from . import _log
from ._http import Backoff
from .errors import RoundTimeoutError

JSON = Any
T = TypeVar("T")

READY_STATE = "CHANNEL_UP"
TERMINAL_STATES = {"CLOSED": "STUDY_COMPLETE", "ERRORED": "SESSION_ERRORED", "LIMIT_EXCEEDED": "LIMIT_EXCEEDED"}


class Retryable(Exception):
    """A transient condition the SDK retries with backoff: BACKPRESSURE, NOT_READY, SERVER_ERROR, TRANSPORT."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UnknownCorrelation(Exception):
    """404 on the responses poll: the destination tunnel restarted and lost the round (re-issue)."""


@dataclass(frozen=True)
class Budget:
    rounds_used: int | None = None
    rounds_max: int | None = None
    response_bytes_used: int | None = None
    response_bytes_max: int | None = None
    query_bytes_used: int | None = None
    query_bytes_max: int | None = None
    rounds_per_hour_used: int | None = None
    rounds_per_hour_max: int | None = None

    @classmethod
    def from_json(cls, obj: JSON) -> Budget | None:
        if not isinstance(obj, dict):
            return None

        def g(key: str) -> int | None:
            v = obj.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        return cls(
            rounds_used=g("roundsUsed"),
            rounds_max=g("roundsMax"),
            response_bytes_used=g("responseBytesUsed"),
            response_bytes_max=g("responseBytesMax"),
            query_bytes_used=g("queryBytesUsed"),
            query_bytes_max=g("queryBytesMax"),
            rounds_per_hour_used=g("roundsPerHourUsed"),
            rounds_per_hour_max=g("roundsPerHourMax"),
        )

    def near_limit(self, fraction: float = 0.9) -> list[tuple[str, int, int]]:
        """(name, used, max) for every capped counter at or above `fraction` of its cap."""
        out: list[tuple[str, int, int]] = []
        for name, used, cap in (
            ("rounds", self.rounds_used, self.rounds_max),
            ("responseBytes", self.response_bytes_used, self.response_bytes_max),
            ("queryBytes", self.query_bytes_used, self.query_bytes_max),
            ("roundsPerHour", self.rounds_per_hour_used, self.rounds_per_hour_max),
        ):
            if used is not None and cap is not None and cap > 0 and used >= fraction * cap:
                out.append((name, used, cap))
        return out

    def log_fields(self) -> dict[str, int | None]:
        return {
            "roundsUsed": self.rounds_used,
            "roundsMax": self.rounds_max,
            "responseBytesUsed": self.response_bytes_used,
            "responseBytesMax": self.response_bytes_max,
            "queryBytesUsed": self.query_bytes_used,
            "queryBytesMax": self.query_bytes_max,
        }


@dataclass(frozen=True)
class Info:
    api_version: str
    leg_id: str
    peer_org_slug: str
    role: str
    direction: str
    state: str
    guards: dict[str, Any] | None = None
    caps: dict[str, Any] | None = None
    operations: list[dict[str, Any]] | None = None

    @property
    def api_major(self) -> int | None:
        head = self.api_version.split(".", 1)[0]
        return int(head) if head.isdigit() else None


@dataclass(frozen=True)
class Delivered:
    message_id: str
    correlation_id: str
    payload: JSON
    budget: Budget | None
    received_at: str | None


@dataclass(frozen=True)
class Terminal:
    code: str
    message: str = ""
    detail: dict[str, Any] | None = None


class Empty:
    """An empty long-poll hold (204)."""


EMPTY = Empty()


class Transport(Protocol):
    """One tunnel = one leg. Methods raise Retryable for transient conditions, and the SDK's
    typed errors (ConfigError, ConcurrencyError, ProtocolError) for the rest."""

    def info(self) -> Info: ...

    def submit(self, payload: JSON, correlation_id: str | None = None) -> str | Terminal: ...

    def poll_response(self, correlation_id: str, timeout_s: float) -> Delivered | Terminal | Empty: ...

    def abandon(self, correlation_id: str) -> bool: ...

    def next_message(self, timeout_s: float) -> Delivered | Terminal | Empty: ...

    def post_response(self, in_reply_to: str, payload: JSON) -> Budget | Terminal | None: ...

    def ack(self, message_id: str) -> None: ...

    def complete(self) -> Terminal | None: ...


def retry_until(
    fn: Callable[[], T],
    *,
    deadline: float,
    backoff: Backoff,
    peer: str,
    what: str,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn` until it stops raising Retryable or the monotonic `deadline` passes."""
    attempt = 0
    while True:
        try:
            return fn()
        except Retryable as exc:
            now = time.monotonic()
            if now >= deadline:
                raise RoundTimeoutError(f"{what}: gave up retrying after {exc.code}", peer=peer) from None
            if exc.code == "BACKPRESSURE":
                _log.event("round.backpressure", logging.WARNING, peer=peer, attempt=attempt)
            else:
                _log.event("round.retry", logging.DEBUG, peer=peer, attempt=attempt, code=exc.code)
            sleep(min(backoff.delay(attempt), max(0.0, deadline - now)))
            attempt += 1


__all__ = [
    "EMPTY",
    "READY_STATE",
    "TERMINAL_STATES",
    "Budget",
    "Delivered",
    "Empty",
    "Info",
    "Retryable",
    "Terminal",
    "Transport",
    "UnknownCorrelation",
    "retry_until",
]
