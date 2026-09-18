"""Destination behavior (plan §3) against the fake tunnel pair, scenario by scenario."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from safeinsights_fusion import (
    ConcurrencyError,
    ConfigError,
    Fusion,
    LimitExceededError,
    NotReadyError,
    ProtocolError,
    RemoteError,
    RoundTimeoutError,
    SessionError,
    Settings,
)

from .conftest import FakeFactory
from .helpers import ScriptedSource, error_envelope, ok_table


def connect(pair: Any, settings: Settings, **kw: Any) -> Fusion:
    return Fusion.connect(pair.env("destination"), settings=settings, **kw)


def source(pair: Any, leg: int = 0, **kw: Any) -> ScriptedSource:
    ep = pair.endpoints[leg].source
    return ScriptedSource(ep.endpoint, ep.token, **kw)


def test_happy_rounds_budget_and_complete(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("happy")
    with source(pair) as src:
        fusion = connect(pair, settings)
        assert [p.peer for p in fusion.peers()] == ["dp-a"]
        peer = fusion.peer()
        assert peer.name == "dp-a" and peer.leg_id == "leg-a" and fusion.peer("dp-a") is peer
        for i in range(1, 4):
            r = peer.request("counts_by_group", {"person_ids": ["p1", "p2"], "group_by": "grade"})
            assert r.is_table and r.to_records() == [{"operation": "counts_by_group", "n_params": 2}]
            assert r.budget is not None and r.budget.rounds_used == i and r.budget.rounds_max is None
            assert r.peer == "dp-a" and r.bytes > 0 and r.correlation_id
        assert peer.budget() is not None and peer.budget().rounds_used == 3  # type: ignore[union-attr]
        df = r.to_pandas()
        assert list(df.columns) == ["operation", "n_params"]
        assert fusion.complete() == {"dp-a": "OK"}
        with pytest.raises(ConcurrencyError, match="complete"):
            peer.request("counts_by_group", {})
        time.sleep(0.3)
        assert src.terminal_code == "STUDY_COMPLETE"
        assert src.calls == 3
    snap = pair.leg("leg-a").snapshot()
    assert snap["state"] == "CLOSED" and all(r["responseAcked"] for r in snap["rounds"])


def test_context_manager_completes_only_on_clean_exit(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("happy")
    with source(pair), connect(pair, settings) as fusion:
        fusion.peer().request("total", {})
    assert pair.leg("leg-a").snapshot()["state"] == "CLOSED"

    pair2 = fake("happy")
    with source(pair2), pytest.raises(RuntimeError), connect(pair2, settings) as fusion:
        fusion.peer().request("total", {})
        raise RuntimeError("analysis crashed")
    assert pair2.leg("leg-a").snapshot()["state"] == "CHANNEL_UP"


def test_fusion_request_convenience_and_unknown_peer(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("happy")
    with source(pair):
        fusion = connect(pair, settings)
        assert fusion.request(None, "total", {}).is_table
        assert fusion.request("dp-a", "total").is_table
        with pytest.raises(KeyError, match="dp-a"):
            fusion.peer("dp-z")


def test_backpressure_and_transient_errors_are_invisible(fake: FakeFactory, settings: Settings) -> None:
    for name in ("backpressure", "transient-errors"):
        pair = fake(name)
        with source(pair):
            fusion = connect(pair, settings)
            for _ in range(3):
                assert fusion.peer().request("total", {}).is_table
        snap = pair.leg("leg-a").snapshot()
        assert snap["budget"]["roundsUsed"] == 3
        assert snap["requestCalls"] > 3  # the injected failures were retried


def test_timeout_reissue_replays_cached_response_without_rerunning_handler(
    fake: FakeFactory, settings: Settings
) -> None:
    pair = fake("timeout-reissue")
    with source(pair) as src:
        fusion = connect(pair, settings)
        peer = fusion.peer()
        peer.request("total", {})
        t0 = time.monotonic()
        r = peer.request("total", {"round": 2}, timeout=0.7)
        assert r.is_table and 0.7 <= time.monotonic() - t0 < 5
        peer.request("total", {})
        assert src.calls == 3  # round 2's handler ran once; the re-issue was answered from the tunnel cache
    log = pair.leg("leg-a").snapshot()["log"]
    assert any(e["event"] == "reissue" for e in log) and any(e["event"] == "cached_replay" for e in log)


def test_restart_404_reissues_same_correlation_id(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("restart-404")
    with source(pair) as src:
        fusion = connect(pair, settings)
        peer = fusion.peer()
        peer.request("total", {})
        r = peer.request("total", {})
        assert r.is_table
        assert src.delivered[1] == r.correlation_id
    log = pair.leg("leg-a").snapshot()["log"]
    assert [e["event"] for e in log if e["event"] in ("forget", "reissue")] == ["forget", "reissue"]


def test_round_timeout_abandons_and_peer_stays_usable(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("slow-source")  # query 2 is held 3 s at the source tunnel
    fast = Settings(**{**settings.__dict__, "round_max_reissues": 1})
    with source(pair) as src:
        fusion = connect(pair, fast)
        peer = fusion.peer()
        peer.request("total", {})
        t0 = time.monotonic()
        with pytest.raises(RoundTimeoutError) as exc:
            peer.request("total", {}, timeout=0.4)
        assert exc.value.reissues == 1 and exc.value.correlation_id and 0.8 <= time.monotonic() - t0 < 3
        assert not peer.in_flight and peer.state == "CHANNEL_UP"
        # The abandoned round was released at the tunnel (T7); the next round works.
        r = peer.request("total", {})
        assert r.is_table
        time.sleep(3.2)  # the held query is eventually delivered and answered; nobody is listening
        assert src.calls == 3
        assert fusion.complete() == {"dp-a": "OK"}
    log = pair.leg("leg-a").snapshot()["log"]
    assert any(e["event"] == "abandon" for e in log)


def test_concurrent_request_on_same_peer_is_rejected_immediately(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("slow-source")
    with source(pair):
        fusion = connect(pair, settings)
        peer = fusion.peer()
        peer.request("total", {})
        started = threading.Event()
        results: list[Any] = []

        def slow() -> None:
            started.set()
            results.append(peer.request("total", {}))

        t = threading.Thread(target=slow)
        t.start()
        started.wait()
        time.sleep(0.1)
        assert peer.in_flight
        with pytest.raises(ConcurrencyError, match="in flight"):
            peer.request("total", {})
        t.join()
        assert results and results[0].is_table


def test_remote_error_envelope_raises_remote_error_and_peer_stays_usable(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("happy")

    def handler(q: Any) -> Any:
        if q["params"].get("bad"):
            return error_envelope(
                "GUARD_REFUSED", "too many ids", {"guard": "maxDistinctPersonIds", "limit": 5, "observed": 6}
            )
        return ok_table([["9", 1]])

    with source(pair, handler=handler):
        fusion = connect(pair, settings)
        peer = fusion.peer()
        with pytest.raises(RemoteError) as exc:
            peer.request("counts_by_group", {"bad": True})
        assert (
            exc.value.code == "GUARD_REFUSED"
            and exc.value.detail["limit"] == 5
            and exc.value.operation == "counts_by_group"
        )
        assert "GUARD_REFUSED" in str(exc.value) and "dp-a" in str(exc.value)
        assert peer.request("counts_by_group", {}).to_records() == [{"grade": "9", "n": 1}]


def test_undecodable_response_is_acked_and_raises_protocol_error(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("happy")
    replies: list[Any] = [
        {"not": "an envelope"},
        {"v": 1, "kind": "query", "operation": "x", "params": {}},
        ok_table([["9", 1]]),
    ]
    with source(pair, handler=lambda _q: replies.pop(0)):
        fusion = connect(pair, settings)
        peer = fusion.peer()
        with pytest.raises(ProtocolError, match="invalid"):
            peer.request("total", {})
        with pytest.raises(ProtocolError, match="query"):
            peer.request("total", {})
        assert peer.request("total", {}).is_table
    snap = pair.leg("leg-a").snapshot()
    assert all(r["responseAcked"] for r in snap["rounds"]) and len(snap["rounds"]) == 3


def test_limit_exceeded_is_terminal_for_the_peer(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("limit-exceeded-rounds")
    with source(pair) as src:
        fusion = connect(pair, settings)
        peer = fusion.peer()
        peer.request("total", {})
        r = peer.request("total", {})
        assert r.budget is not None and (r.budget.rounds_used, r.budget.rounds_max) == (2, 2)
        with pytest.raises(LimitExceededError) as exc:
            peer.request("total", {})
        assert exc.value.cap == "maxRounds" and exc.value.terminal and exc.value.code == "LIMIT_EXCEEDED"
        calls = pair.leg("leg-a").snapshot()["requestCalls"]
        with pytest.raises(LimitExceededError):
            peer.request("total", {})  # raised locally: no HTTP call
        assert pair.leg("leg-a").snapshot()["requestCalls"] == calls
        assert peer.state == "TERMINAL:LIMIT_EXCEEDED"
        assert fusion.complete() == {"dp-a": "LIMIT_EXCEEDED"}
        time.sleep(0.3)
        assert src.terminal_code == "LIMIT_EXCEEDED"


def test_session_errored_is_terminal(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("session-errored")
    with source(pair):
        fusion = connect(pair, settings)
        peer = fusion.peer()
        peer.request("total", {})
        with pytest.raises(SessionError) as exc:
            peer.request("total", {})
        assert exc.value.code == "SESSION_ERRORED"
        with pytest.raises(SessionError):
            peer.request("total", {})


def test_hub_two_legs_independent_and_complete_fans_out(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("hub-two-legs")
    with source(pair, 0) as src_a, source(pair, 1) as src_b:
        fusion = connect(pair, settings)
        assert sorted(p.peer for p in fusion.peers()) == ["dp-a", "dp-b"]
        with pytest.raises(KeyError, match="2 peers"):
            fusion.peer()
        a, b = fusion.peer("dp-a"), fusion.peer("dp-b")
        # Different peers may be in flight at the same time.
        out: dict[str, Any] = {}
        ta = threading.Thread(target=lambda: out.__setitem__("a", a.request("total", {"from": "a"})))
        tb = threading.Thread(target=lambda: out.__setitem__("b", b.request("total", {"from": "b"})))
        ta.start()
        tb.start()
        ta.join()
        tb.join()
        assert out["a"].peer == "dp-a" and out["b"].peer == "dp-b"
        # Leg B is capped at one round; leg A keeps going.
        with pytest.raises(LimitExceededError):
            b.request("total", {})
        assert a.request("total", {"derived_from": out["b"].body}).is_table
        assert fusion.complete() == {"dp-a": "OK", "dp-b": "LIMIT_EXCEEDED"}
        time.sleep(0.3)
        assert src_a.terminal_code == "STUDY_COMPLETE" and src_b.terminal_code == "LIMIT_EXCEEDED"
        assert src_a.calls == 2 and src_b.calls == 1


def test_readiness_wait_and_timeout(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("slow-ready", ready_delay_ms=800)
    t0 = time.monotonic()
    fusion = connect(pair, settings)
    assert 0.7 <= time.monotonic() - t0 < 5 and fusion.peer().state == "CHANNEL_UP"

    never = fake("never-ready")
    with pytest.raises(NotReadyError, match="RELAY_ATTACHED"):
        connect(never, settings, ready_timeout=0.5)


def test_config_errors(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("happy")
    with pytest.raises(ConfigError, match="FUSION_ROLE"):
        Fusion.connect(pair.env("source"), settings=settings)
    env = pair.env("destination")
    env["FUSION_TUNNEL_TOKEN"] = "wrong"
    with pytest.raises(ConfigError, match="401"):
        Fusion.connect(env, settings=settings)
    # Pointing a destination at a source tunnel: the tunnel says role=source.
    env = pair.env("destination")
    env["FUSION_TUNNEL_ENDPOINT"] = pair.endpoints[0].source.endpoint
    env["FUSION_TUNNEL_TOKEN"] = pair.endpoints[0].source.token
    with pytest.raises(ConfigError, match="role"):
        Fusion.connect(env, settings=settings)
    old = fake("happy", api_version="2.1.0")
    with pytest.raises(ConfigError, match="major"):
        connect(old, settings)
