"""Backoff, retry_until, the log vocabulary guard and the budget helpers."""

from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path

import pytest

from safeinsights_fusion import RoundTimeoutError, SessionError, Settings, _log
from safeinsights_fusion._transport import Backoff, Budget, Info, Retryable, retry_until
from safeinsights_fusion.destination import wait_ready

SPEC = Path(__file__).resolve().parents[2] / "spec"


def test_backoff_is_bounded_and_grows() -> None:
    b = Backoff(base_ms=100, max_ms=1000)
    for attempt in range(10):
        d = b.delay(attempt)
        assert 0 <= d <= min(1.0, 0.1 * 2**attempt)
    assert 0 <= b.delay(10_000) <= 1.0  # the exponent is capped: a source polls forever


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


def test_retry_until_forever_with_an_infinite_deadline() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 5:
            raise Retryable("TRANSPORT")
        return "ok"

    assert (
        retry_until(flaky, deadline=math.inf, backoff=Backoff(1, 2), peer="p", what="poll", sleep=lambda _s: None)
        == "ok"
    )


def test_log_vocabulary_is_closed_and_matches_the_spec() -> None:
    with pytest.raises(ValueError, match="content-free"):
        _log.event("round.start", params={"secret": 1})
    _log.event("round.start", peer="p", bytes=1)  # allowed fields do not raise
    section = (SPEC / "log-events.md").read_text(encoding="utf-8").split("## Allowed fields", 1)[1].split("\n## ", 1)[0]
    assert set(re.findall(r"`([A-Za-z]+)`", section)) == set(_log.ALLOWED_FIELDS)


def test_wait_ready_treats_closing_as_study_complete() -> None:
    class Closing:
        def info(self) -> Info:
            return Info("2.0.0", "leg-a", "dp-a", "destination", "dst_to_src", "CLOSING")

    with pytest.raises(SessionError) as exc:
        wait_ready(Closing(), deadline=time.monotonic() + 1, settings=Settings(), label="x")  # type: ignore[arg-type]
    assert exc.value.code == "STUDY_COMPLETE"


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
    assert b.near_limit() == [("maxRounds", 9, 10)]
    assert Budget.from_json(None) is None
    assert Budget.from_json({"roundsUsed": True}) == Budget()
