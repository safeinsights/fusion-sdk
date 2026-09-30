"""Content-free event logging (spec/log-events.md).

One line per event: ``fusion <event> key=value ...``. Field names are whitelisted so a
params or body value can never reach a log line by accident; the audit tests in both
languages assert that marker strings placed in payloads never appear in captured logs.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

LOGGER_NAME = "safeinsights_fusion"
logger = logging.getLogger(LOGGER_NAME)

#: The only keys an event line may carry: exactly spec/log-events.md "Allowed fields" (a test checks).
ALLOWED_FIELDS = frozenset(
    {
        "peer",
        "legId",
        "role",
        "operation",
        "operations",
        "correlationId",
        "messageId",
        "bytes",
        "durationMs",
        "attempt",
        "reissue",
        "state",
        "code",
        "guard",
        "limit",
        "observed",
        "cap",
        "roundsUsed",
        "roundsMax",
        "maxDistinctPersonIds",
        "minGroupSize",
        "endpointCount",
        "apiVersion",
    }
)

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}
_configured = False


def configure(level: str = "INFO") -> None:
    """Attach a stderr handler to the SDK logger unless the application configured logging itself."""
    global _configured
    logger.setLevel(_LEVELS.get(level.upper(), logging.INFO))
    if _configured:
        return
    _configured = True
    if logger.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ",".join(_fmt(v) for v in value)
    text = str(value)
    return f'"{text}"' if (" " in text or not text) else text


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one content-free event line. Unknown field names raise so they are caught in tests."""
    bad = set(fields) - ALLOWED_FIELDS
    if bad:
        raise ValueError(f"log field(s) not in the content-free vocabulary: {sorted(bad)}")
    if not logger.isEnabledFor(level):
        return
    parts = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)
    logger.log(level, "fusion %s %s", name, parts)


__all__ = ["ALLOWED_FIELDS", "LOGGER_NAME", "configure", "event", "logger"]
