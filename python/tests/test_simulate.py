"""The in-process simulator runs the real destination and source code paths without a tunnel."""

from __future__ import annotations

from typing import Any

import pytest

from safeinsights_fusion import (
    ConfigError,
    Fusion,
    LimitExceededError,
    OperationRegistry,
    RemoteError,
    SessionError,
    SimFaults,
    Simulator,
    Table,
    simulate,
)


def registry() -> OperationRegistry:
    reg = OperationRegistry()
    calls = {"n": 0}

    @reg.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
    def counts(params: dict[str, Any], ctx: Any) -> Table:
        calls["n"] += 1
        n = len(params["person_ids"])
        return Table.from_columns({"grade": ["9", "10"], "n": [n, n + 5]})

    @reg.register("total")
    def total(params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {"total": 42, "calls": calls["n"], "peer": ctx.peer, "cid": ctx.correlation_id}

    @reg.register("boom")
    def boom(params: dict[str, Any], ctx: Any) -> None:
        raise RuntimeError("nope")

    return reg


def test_two_party_simulation_returns_the_analysis_result() -> None:
    def analysis(fusion: Fusion) -> list[dict[str, Any]]:
        peer = fusion.peer()
        assert peer.name == "dp-sim"
        r = peer.request("counts_by_group", {"person_ids": ["a", "b", "c"]})
        assert r.budget is not None and r.budget.rounds_used == 1
        t = peer.request("total", {})
        assert t.body["peer"] == "dp-sim" and t.body["cid"] == t.correlation_id
        with pytest.raises(RemoteError) as exc:
            peer.request("boom", {})
        assert exc.value.code == "HANDLER_ERROR" and "RuntimeError" in exc.value.message
        with pytest.raises(RemoteError) as exc:
            peer.request("nope", {})
        assert exc.value.code == "UNKNOWN_OPERATION"
        return r.to_records()

    assert simulate(registry(), analysis) == [{"grade": "9", "n": 3}, {"grade": "10", "n": 8}]


def test_guards_apply_in_simulation() -> None:
    def analysis(fusion: Fusion) -> None:
        peer = fusion.peer()
        with pytest.raises(RemoteError) as exc:
            peer.request("counts_by_group", {"person_ids": [str(i) for i in range(6)]})
        assert exc.value.code == "GUARD_REFUSED" and exc.value.detail["guard"] == "maxDistinctPersonIds"
        with pytest.raises(RemoteError) as exc:
            peer.request("counts_by_group", {"person_ids": ["a"]})  # n = 1 < minGroupSize 3
        assert exc.value.detail == {"guard": "minGroupSize", "limit": 3}
        assert peer.request("counts_by_group", {"person_ids": ["a", "b", "c"]}).is_table

    simulate(registry(), analysis, guards={"maxDistinctPersonIds": 5, "minGroupSize": 3})


def test_faults_timeout_reissue_session_error_and_caps() -> None:
    def replay(fusion: Fusion) -> Any:
        r = fusion.peer().request("total", {}, timeout=0.3)  # response withheld until the re-issue
        return r.body["calls"]

    assert simulate(registry(), replay, faults=SimFaults(drop_response_rounds=frozenset({1}))) == 1  # handler ran once

    def errored(fusion: Fusion) -> None:
        fusion.peer().request("total", {})
        with pytest.raises(SessionError):
            fusion.peer().request("total", {})

    simulate(registry(), errored, faults=SimFaults(error_after_round=1))

    def capped(fusion: Fusion) -> dict[str, str]:
        fusion.peer().request("total", {})
        with pytest.raises(LimitExceededError) as exc:
            fusion.peer().request("total", {})
        assert exc.value.cap == "maxRounds"
        return fusion.complete()

    assert simulate(registry(), capped, faults=SimFaults(max_rounds=1)) == {"dp-sim": "LIMIT_EXCEEDED"}

    def big(fusion: Fusion) -> None:
        with pytest.raises(LimitExceededError) as exc:
            fusion.peer().request("counts_by_group", {"person_ids": ["a"]})
        assert exc.value.cap == "maxResponsePlaintextBytesPerRound"

    simulate(registry(), big, faults=SimFaults(max_response_bytes_per_round=10))


def test_hub_simulation_with_two_peers() -> None:
    def analysis(fusion: Fusion) -> dict[str, str]:
        a, b = fusion.peer("dp-a"), fusion.peer("dp-b")
        ra = a.request("total", {})
        rb = b.request("counts_by_group", {"person_ids": ["x"] * ra.body["total"]})
        assert rb.to_records()[0]["n"] == 42
        with pytest.raises(LimitExceededError):
            b.request("total", {})
        assert a.request("total", {}).body["total"] == 42
        return fusion.complete()

    outcomes = simulate({"dp-a": registry(), "dp-b": registry()}, analysis, faults={"dp-b": SimFaults(max_rounds=1)})
    assert outcomes == {"dp-a": "OK", "dp-b": "LIMIT_EXCEEDED"}


def test_preflight_and_completion() -> None:
    with pytest.raises(ConfigError, match="approved"):
        simulate(registry(), lambda f: None, approved_operations=[{"name": "total", "cardinality": "aggregate"}])
    sim = Simulator(registry())
    sim.run(lambda f: f.peer().request("total", {}))
    assert sim.fusion.completed and sim.legs["dp-sim"].closed
    assert sim.servers["dp-sim"].rounds_served == 1
