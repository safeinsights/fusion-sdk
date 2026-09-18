"""Backoff, retry_until and the log vocabulary guard."""

from __future__ import annotations

import logging
import time

import pytest

from safeinsights_fusion import RoundTimeoutError, _log
from safeinsights_fusion._http import Backoff
from safeinsights_fusion._transport import Budget, Retryable, retry_until


def test_backoff_is_bounded_and_grows() -> None:
    b = Backoff(base_ms=100, max_ms=1000)
    for attempt in range(10):
        d = b.delay(attempt)
        assert 0 <= d <= min(1.0, 0.1 * 2**attempt)


def test_retry_until_returns_first_success_and_logs_backpressure(caplog: pytest.LogCaptureFixture) -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise Retryable("BACKPRESSURE")
        return "ok"

    slept: list[float] = []
    with caplog.at_level(logging.DEBUG, logger=_log.LOGGER_NAME):
        out = retry_until(
            flaky, deadline=time.monotonic() + 5, backoff=Backoff(1, 2), peer="p", what="x", sleep=slept.append
        )
    assert out == "ok" and calls["n"] == 3 and len(slept) == 2
    assert sum("round.backpressure" in r.getMessage() for r in caplog.records) == 2


def test_retry_until_gives_up_at_deadline() -> None:
    def always() -> None:
        raise Retryable("TRANSPORT")

    with pytest.raises(RoundTimeoutError, match="TRANSPORT"):
        retry_until(
            always,
            deadline=time.monotonic() + 0.05,
            backoff=Backoff(1, 2),
            peer="p",
            what="submit",
            sleep=lambda _s: None,
        )


def test_log_vocabulary_is_closed() -> None:
    with pytest.raises(ValueError, match="content-free"):
        _log.event("round.start", params={"secret": 1})
    _log.event("round.start", peer="p", bytes=1)  # allowed fields do not raise


def test_budget_from_json_and_near_limit() -> None:
    b = Budget.from_json(
        {
            "roundsUsed": 9,
            "roundsMax": 10,
            "responseBytesUsed": 1,
            "responseBytesMax": None,
            "queryBytesUsed": 5,
            "queryBytesMax": 100,
        }
    )
    assert b is not None and b.rounds_used == 9 and b.response_bytes_max is None
    assert b.near_limit() == [("rounds", 9, 10)]
    assert Budget.from_json(None) is None
    assert Budget.from_json({"roundsUsed": True}) == Budget()
