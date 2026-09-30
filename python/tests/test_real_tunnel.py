"""Phase 8 switch: run the happy path against REAL tunnels when FUSION_TEST_ANNOUNCE points at an announce file.

The announce file uses the fake pair's format (see FakeTunnelPair.announce): one leg with `destination`
and `source` {endpoint, token}. Produced by the tunnel repo's Phase 9 harness. Skipped otherwise.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from safeinsights_fusion import Fusion, OperationRegistry, RemoteError, Settings, Table, serve

ANNOUNCE = os.environ.get("FUSION_TEST_ANNOUNCE")
pytestmark = pytest.mark.skipif(
    not ANNOUNCE, reason="set FUSION_TEST_ANNOUNCE=<announce.json> to run against real tunnels"
)


def _env(role: str) -> dict[str, str]:
    assert ANNOUNCE is not None
    leg = json.loads(Path(ANNOUNCE).read_text())["legs"][0]
    return {
        "FUSION_ROLE": role,
        "FUSION_TUNNEL_ENDPOINT": leg[role]["endpoint"],
        "FUSION_TUNNEL_TOKEN": leg[role]["token"],
    }


def test_real_tunnel_happy_path() -> None:
    reg = OperationRegistry()

    @reg.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
    def counts(params: dict[str, Any], ctx: Any) -> Table:
        n = len(params["person_ids"])
        return Table.from_columns({"grade": ["9", "10"], "n": [n, n + 5]})

    @reg.register("boom")
    def boom(params: dict[str, Any], ctx: Any) -> None:
        raise RuntimeError("real tunnel boom")

    settings = Settings.from_env({})
    errors: list[BaseException] = []

    def source() -> None:
        try:
            serve(reg, _env("source"), settings=settings)
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=source, daemon=True)
    t.start()
    with Fusion.connect(_env("destination"), settings=settings) as fusion:
        peer = fusion.peer()
        for i in range(3):
            r = peer.request("counts_by_group", {"person_ids": ["a", "b", "c"]})
            assert r.to_records() == [{"grade": "9", "n": 3}, {"grade": "10", "n": 8}]
            assert r.budget is None or r.budget.rounds_used == i + 1
        with pytest.raises(RemoteError) as exc:
            peer.request("boom", {})
        assert exc.value.code == "HANDLER_ERROR"
    t.join(timeout=120)
    assert not t.is_alive() and errors == []
