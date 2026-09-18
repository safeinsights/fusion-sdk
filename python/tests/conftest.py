"""Shared fixtures: the in-process fake tunnel pair and fast tuning settings."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from fake_tunnel_pair import FakeTunnelPair, load_scenario  # noqa: E402
from safeinsights_fusion import Settings  # noqa: E402

SPEC = ROOT / "spec"


@pytest.fixture
def settings() -> Settings:
    """Fast knobs for tests: short holds, short round timeouts, two re-issues."""
    return Settings(
        ready_timeout_s=10.0,
        ready_poll_s=0.1,
        ready_poll_max_s=0.5,
        poll_http_timeout_s=5.0,
        http_timeout_s=5.0,
        round_timeout_s=5.0,
        round_max_reissues=2,
        retry_base_ms=20,
        retry_max_ms=200,
        memo_max_entries=8,
        warn_bytes=1 << 20,
        log_level="DEBUG",
    )


FakeFactory = Callable[..., FakeTunnelPair]


@pytest.fixture
def fake() -> Iterator[FakeFactory]:
    """`fake("happy", longpoll_ms=200)` starts a fake pair for a scenario; stopped at teardown."""
    started: list[FakeTunnelPair] = []

    def start(name: str, *, api_version: str = "1.0.0", **overrides: Any) -> FakeTunnelPair:
        sc = load_scenario(name, SPEC / "scenarios")
        sc = replace(sc, longpoll_ms=overrides.pop("longpoll_ms", 200), **overrides)
        pair = FakeTunnelPair(sc, api_version=api_version)
        pair.start()
        started.append(pair)
        return pair

    yield start
    for pair in started:
        pair.stop()
