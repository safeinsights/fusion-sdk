"""Error taxonomy (spec/errors.md). Every message is content-free by construction:
it may name a peer, an operation, a correlationId, sizes and durations — never params or bodies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FusionError(Exception):
    """Base class for every error the SDK raises. Never raised directly."""

    #: True for errors after which the peer is unusable.
    terminal: bool = False

    def __init__(self, message: str, *, peer: str | None = None) -> None:
        super().__init__(message)
        self.peer = peer

    def __str__(self) -> str:
        base = super().__str__()
        return f"[peer={self.peer}] {base}" if self.peer else base


class ConfigError(FusionError):
    """Environment missing or malformed, non-local endpoint, incompatible apiVersion, 401, 403."""

    terminal = True


class NotReadyError(FusionError):
    """Readiness timeout waiting for CHANNEL_UP."""

    terminal = True


class ConcurrencyError(FusionError):
    """A second in-flight request on a peer, or 409 from the tunnel."""


class RoundTimeoutError(FusionError):
    """Round timeout elapsed after the configured number of same-correlationId re-issues.

    The round is abandoned; the peer stays usable.
    """

    def __init__(
        self, message: str, *, peer: str | None = None, correlation_id: str | None = None, reissues: int = 0
    ) -> None:
        super().__init__(message, peer=peer)
        self.correlation_id = correlation_id
        self.reissues = reissues


class RemoteError(FusionError):
    """The source returned an error envelope (UNKNOWN_OPERATION, BAD_PARAMS, HANDLER_ERROR, GUARD_REFUSED)."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
        *,
        peer: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}", peer=peer)
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = dict(detail or {})
        self.operation = operation


class TerminalError(FusionError):
    """Base of the two leg-terminal errors. Never raised directly."""

    terminal = True

    def __init__(
        self, code: str, message: str = "", detail: Mapping[str, Any] | None = None, *, peer: str | None = None
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code, peer=peer)
        self.code = code
        self.detail: dict[str, Any] = dict(detail or {})


class LimitExceededError(TerminalError):
    """The tunnel reported LIMIT_EXCEEDED on this leg: a source-approved cap was breached."""

    def __init__(self, message: str = "", detail: Mapping[str, Any] | None = None, *, peer: str | None = None) -> None:
        super().__init__("LIMIT_EXCEEDED", message, detail, peer=peer)

    @property
    def cap(self) -> str | None:
        cap = self.detail.get("cap")
        return cap if isinstance(cap, str) else None


class SessionError(TerminalError):
    """The tunnel reported SESSION_ERRORED (dead-letter, un-ACKed expiry, relay closed, tunnel ERRORED)."""

    def __init__(
        self,
        message: str = "",
        detail: Mapping[str, Any] | None = None,
        *,
        peer: str | None = None,
        code: str = "SESSION_ERRORED",
    ) -> None:
        super().__init__(code, message, detail, peer=peer)


class ProtocolError(FusionError):
    """Undecodable local-API response or envelope (still ACKed), or 422 from the tunnel."""


def terminal_error(
    code: str, message: str = "", detail: Mapping[str, Any] | None = None, *, peer: str | None = None
) -> TerminalError:
    """Map a terminal body's code to its error class. STUDY_COMPLETE is not an error and is handled by callers."""
    if code == "LIMIT_EXCEEDED":
        return LimitExceededError(message, detail, peer=peer)
    return SessionError(message, detail, peer=peer, code=code)


__all__ = [
    "ConcurrencyError",
    "ConfigError",
    "FusionError",
    "LimitExceededError",
    "NotReadyError",
    "ProtocolError",
    "RemoteError",
    "RoundTimeoutError",
    "SessionError",
    "TerminalError",
    "terminal_error",
]
