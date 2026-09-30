"""Source behavior (plan §3 "Source serve() loop") against the fake, with the Python destination as the peer."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from safeinsights_fusion import (
    ConcurrencyError,
    ConfigError,
    Fusion,
    LimitExceededError,
    OperationRegistry,
    RemoteError,
    SessionError,
    Settings,
    Table,
    _log,
)
from safeinsights_fusion import serve as serve_fn
from safeinsights_fusion._transport import EMPTY, Budget, Delivered, Empty, Info, Terminal
from safeinsights_fusion.guards import (
    GuardRefused,
    Guards,
    OperationSpec,
    check_query,
    check_result,
    distinct_count,
    preflight,
)
from safeinsights_fusion.source import ResponseMemo, Server

from .conftest import FakeFactory
from .helpers import RawTunnel

ROOT = Path(__file__).resolve().parents[2]


class ServeThread:
    """Run serve() in a thread; capture its outcome."""

    def __init__(self, pair: Any, registry: OperationRegistry, settings: Settings, leg: int = 0, **kw: Any) -> None:
        env = pair.env("source") if len(pair.endpoints) == 1 else _single_source_env(pair, leg)
        self.error: BaseException | None = None
        self.done = threading.Event()

        def run() -> None:
            try:
                serve_fn(registry, env, settings=settings, **kw)
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True, name="serve")

    def __enter__(self) -> ServeThread:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.done.wait(15)

    def wait(self, timeout: float = 15) -> BaseException | None:
        assert self.done.wait(timeout), "serve() did not finish"
        return self.error


def _single_source_env(pair: Any, leg: int) -> dict[str, str]:
    ep = pair.endpoints[leg].source
    return {"FUSION_ROLE": "source", "FUSION_TUNNEL_ENDPOINT": ep.endpoint, "FUSION_TUNNEL_TOKEN": ep.token}


def make_registry() -> tuple[OperationRegistry, dict[str, int]]:
    reg = OperationRegistry()
    calls = {"n": 0}

    @reg.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
    def counts_by_group(params: dict[str, Any], ctx: Any) -> Any:
        calls["n"] += 1
        ids = params.get("person_ids", [])
        return Table.from_columns({"grade": ["9", "10"], "n": [len(ids), 2 * len(ids) + 1]})

    @reg.register("total")
    def total(params: dict[str, Any], ctx: Any) -> Any:
        calls["n"] += 1
        return {"total": 42, "correlation_id": ctx.correlation_id, "peer": ctx.peer, "operation": ctx.operation}

    @reg.register("boom")
    def boom(params: dict[str, Any], ctx: Any) -> Any:
        calls["n"] += 1
        raise ZeroDivisionError("division by zero MARKER_TRACE_11aa")

    @reg.register("boom_sanitized", sanitize=True)
    def boom_sanitized(params: dict[str, Any], ctx: Any) -> Any:
        raise ValueError("bad value MARKER_SANITIZED_22bb")

    @reg.register("frame")
    def frame(params: dict[str, Any], ctx: Any) -> Any:
        pd = pytest.importorskip("pandas")
        return pd.DataFrame({"k": ["a", "b"], "v": [1.5, 2.5]})

    @reg.register("unencodable")
    def unencodable(params: dict[str, Any], ctx: Any) -> Any:
        return {"obj": object()}

    return reg, calls


@pytest.fixture
def registry() -> Iterator[tuple[OperationRegistry, dict[str, int]]]:
    yield make_registry()


def test_serve_happy_path_all_body_kinds_and_clean_exit(
    fake: FakeFactory, settings: Settings, registry: tuple[OperationRegistry, dict[str, int]]
) -> None:
    pair = fake("happy")
    reg, calls = registry
    with ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        peer = fusion.peer()
        r = peer.request("counts_by_group", {"person_ids": ["p1", "p2", "p3"], "group_by": "grade"})
        assert r.to_records() == [{"grade": "9", "n": 3}, {"grade": "10", "n": 7}]
        r = peer.request("total", {})
        assert (
            r.body["total"] == 42
            and r.body["correlation_id"] == r.correlation_id
            and r.body["peer"] == "dp-a"
            and r.body["operation"] == "total"
        )
        r = peer.request("frame", {})
        assert r.to_records() == [{"k": "a", "v": 1.5}, {"k": "b", "v": 2.5}]
        assert peer.budget() is not None and peer.budget().rounds_used == 3  # type: ignore[union-attr]
        fusion.complete()
        assert st.wait() is None  # STUDY_COMPLETE → serve() returned
    assert calls["n"] == 2


def test_handler_errors_become_envelopes_and_loop_continues(
    fake: FakeFactory, settings: Settings, registry: tuple[OperationRegistry, dict[str, int]]
) -> None:
    pair = fake("happy")
    reg, _ = registry
    with ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        peer = fusion.peer()
        with pytest.raises(RemoteError) as exc:
            peer.request("boom", {})
        assert exc.value.code == "HANDLER_ERROR" and exc.value.detail == {"type": "ZeroDivisionError"}
        assert (
            "Traceback" in exc.value.message and "MARKER_TRACE_11aa" in exc.value.message
        )  # ADR 0005: full traceback by default
        with pytest.raises(RemoteError) as exc:
            peer.request("boom_sanitized", {})
        assert (
            exc.value.message == "ValueError: bad value MARKER_SANITIZED_22bb" and "Traceback" not in exc.value.message
        )
        with pytest.raises(RemoteError) as exc:
            peer.request("nope", {})
        assert exc.value.code == "UNKNOWN_OPERATION" and exc.value.detail == {"operation": "nope"}
        with pytest.raises(RemoteError) as exc:
            peer.request("unencodable", {})
        assert exc.value.code == "HANDLER_ERROR" and exc.value.detail["type"] == "EnvelopeError"
        assert peer.request("total", {}).body["total"] == 42  # the loop is alive
        fusion.complete()
        assert st.wait() is None


def test_bad_params_for_a_non_envelope_query(
    fake: FakeFactory, settings: Settings, registry: tuple[OperationRegistry, dict[str, int]]
) -> None:
    pair = fake("happy")
    reg, _ = registry
    dst = RawTunnel(pair.endpoints[0].destination.endpoint, pair.endpoints[0].destination.token)
    with ServeThread(pair, reg, settings):
        for payload in (
            {"hello": "world"},
            {"v": 1, "kind": "response", "status": "ok", "body": 1},
            {"v": 1, "kind": "query", "operation": "total", "params": [1]},
        ):
            cid = dst.call("POST", "/v1/request", {"payload": payload})[1]["correlationId"]
            status, msg = dst.call("GET", f"/v1/responses/{cid}")
            assert (
                status == 200
                and msg["payload"]["status"] == "error"
                and msg["payload"]["error"]["code"] == "BAD_PARAMS"
            )
        dst.call("POST", "/v1/complete", {})


def test_redelivered_query_is_answered_from_the_memo(
    fake: FakeFactory, settings: Settings, registry: tuple[OperationRegistry, dict[str, int]]
) -> None:
    pair = fake("redeliver-query")  # query 2 is delivered a second time
    reg, calls = registry
    with ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        peer = fusion.peer()
        peer.request("total", {})
        peer.request("total", {})
        time.sleep(0.5)  # let the redelivery arrive and be replayed
        peer.request("total", {})
        fusion.complete()
        assert st.wait() is None
    assert calls["n"] == 3  # handler ran once per distinct round
    snap = pair.leg("leg-a").snapshot()
    assert snap["respondCalls"] == 4 and snap["budget"]["roundsUsed"] == 3
    assert snap["rounds"][1]["queryDeliveries"] == 2


def test_guards_refuse_loudly_without_partial_results(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("guards")  # maxDistinctPersonIds 5, minGroupSize 3
    reg = OperationRegistry()
    seen: list[int] = []

    @reg.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
    def counts_by_group(params: dict[str, Any], ctx: Any) -> Any:
        seen.append(len(params["person_ids"]))
        small = params.get("small", False)
        return Table.from_columns({"grade": ["9", "10"], "n": [5, 2 if small else 4]})

    # The count column is never inferred: a per-group operation cannot be registered without it.
    with pytest.raises(ValueError, match="count_column"):
        reg.add("two_counts", lambda p, c: Table.from_columns({"a": [5], "b": [6]}), cardinality="per-group")

    @reg.register("two_counts_declared", cardinality="per-group", count_column="b")
    def two_counts_declared(params: dict[str, Any], ctx: Any) -> Any:
        return Table.from_columns({"a": [1], "b": [6]})

    @reg.register("count_missing", cardinality="per-group", count_column="n")
    def count_missing(params: dict[str, Any], ctx: Any) -> Any:
        return Table.from_columns({"a": [5], "b": [6]})

    @reg.register("float_counts", cardinality="per-group", count_column="n")
    def float_counts(params: dict[str, Any], ctx: Any) -> Any:
        return Table.from_columns({"year": [2024], "n": [1.0]})  # a key >= limit must not stand in for the count

    @reg.register("per_group_scalar", cardinality="per-group", count_column="n")
    def per_group_scalar(params: dict[str, Any], ctx: Any) -> Any:
        return 7

    @reg.register("aggregate_small", cardinality="aggregate")
    def aggregate_small(params: dict[str, Any], ctx: Any) -> Any:
        return Table.from_columns({"n": [1]})

    with ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        peer = fusion.peer()
        assert peer.request("counts_by_group", {"person_ids": ["a", "b", "c", "d", "e", "a"]}).is_table  # 5 distinct
        with pytest.raises(RemoteError) as exc:
            peer.request("counts_by_group", {"person_ids": ["a", "b", "c", "d", "e", "f"]})
        assert exc.value.code == "GUARD_REFUSED" and exc.value.detail == {
            "guard": "maxDistinctPersonIds",
            "limit": 5,
            "observed": 6,
        }
        assert seen == [6], "the handler must not run for a refused query"
        for shape in ({f"p{i}": f"p{i}" for i in range(6)}, [[f"p{i}" for i in range(6)]]):
            with pytest.raises(RemoteError) as exc:
                peer.request("counts_by_group", {"person_ids": shape})
            assert exc.value.code == "GUARD_REFUSED" and exc.value.detail == {
                "guard": "maxDistinctPersonIds",
                "limit": 5,
            }
        assert seen == [6], "object and nested-array Person-ID shapes must not reach the handler"
        with pytest.raises(RemoteError) as exc:
            peer.request("counts_by_group", {"person_ids": ["a"], "small": True})
        assert exc.value.detail == {"guard": "minGroupSize", "limit": 3}  # no observed: the small cell stays home
        assert peer.request("two_counts_declared", {}).is_table
        with pytest.raises(RemoteError, match="not in the result"):
            peer.request("count_missing", {})
        with pytest.raises(RemoteError) as exc:
            peer.request("float_counts", {})
        assert exc.value.detail == {"guard": "minGroupSize", "limit": 3} and "integer column" in exc.value.message
        with pytest.raises(RemoteError, match="fusion table"):
            peer.request("per_group_scalar", {})
        assert peer.request("aggregate_small", {}).is_table  # minGroupSize applies to per-group only
        fusion.complete()
        assert st.wait() is None


def test_guards_disabled_when_info_has_none(
    fake: FakeFactory,
    settings: Settings,
    registry: tuple[OperationRegistry, dict[str, int]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    pair = fake("happy")
    reg, _ = registry
    with caplog.at_level(logging.DEBUG, logger=_log.LOGGER_NAME), ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        assert fusion.peer().request("counts_by_group", {"person_ids": [str(i) for i in range(1000)]}).is_table
        fusion.complete()
        assert st.wait() is None
    assert any("guards.disabled" in r.getMessage() for r in caplog.records)


def test_operations_preflight_against_approved_list(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("operations-declared")  # approved: counts_by_group (per-group), total (aggregate)
    reg = OperationRegistry()
    reg.add("counts_by_group", lambda p, c: 1, cardinality="per-group", count_column="n")
    reg.add("total", lambda p, c: 1)
    reg.add("extra", lambda p, c: 1)
    with pytest.raises(ConfigError, match="'extra' is registered but not in the approved list"):
        serve_fn(reg, pair.env("source"), settings=settings)
    reg2 = OperationRegistry()
    reg2.add("counts_by_group", lambda p, c: 1, cardinality="aggregate")
    with pytest.raises(ConfigError, match="approved as per-group"):
        serve_fn(reg2, pair.env("source"), settings=settings)
    reg3 = OperationRegistry()
    reg3.add("counts_by_group", lambda p, c: 1, cardinality="per-group", count_column="n")
    with pytest.raises(ConfigError, match="'total' is approved but no handler"):
        serve_fn(reg3, pair.env("source"), settings=settings)
    reg3.add("total", lambda p, c: {"ok": True})
    with ServeThread(pair, reg3, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        assert fusion.peer().request("total", {}).body == {"ok": True}
        fusion.complete()
        assert st.wait() is None


def test_limit_exceeded_and_session_errored_raise_from_serve(
    fake: FakeFactory, settings: Settings, registry: tuple[OperationRegistry, dict[str, int]]
) -> None:
    pair = fake("limit-exceeded-bytes")  # maxResponseBytesPerRound 64
    reg, _ = registry
    with ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        with pytest.raises(LimitExceededError) as exc:
            fusion.peer().request("counts_by_group", {"person_ids": ["a"]})
        assert exc.value.cap == "maxResponsePlaintextBytesPerRound"
        err = st.wait()
        assert isinstance(err, LimitExceededError) and err.cap == "maxResponsePlaintextBytesPerRound"

    pair = fake("session-errored")
    with ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        fusion.peer().request("total", {})
        with pytest.raises(SessionError):
            fusion.peer().request("total", {})
        assert isinstance(st.wait(), SessionError)


def test_source_config_errors(
    fake: FakeFactory, settings: Settings, registry: tuple[OperationRegistry, dict[str, int]]
) -> None:
    reg, _ = registry
    pair = fake("hub-two-legs")
    with pytest.raises(ConfigError, match="exactly one tunnel"):
        serve_fn(reg, pair.env("source"), settings=settings)
    single = fake("happy")
    with pytest.raises(ConfigError, match="FUSION_ROLE"):
        serve_fn(reg, single.env("destination"), settings=settings)
    env = single.env("source")
    env["FUSION_TUNNEL_ENDPOINT"] = single.endpoints[0].destination.endpoint
    env["FUSION_TUNNEL_TOKEN"] = single.endpoints[0].destination.token
    with pytest.raises(ConfigError, match="role"):
        serve_fn(reg, env, settings=settings)
    with pytest.raises(ConfigError, match="no operations"):
        serve_fn(OperationRegistry(), single.env("source"), settings=settings)


def test_registry_validation() -> None:
    reg = OperationRegistry()
    reg.add("a", lambda p, c: 1)
    with pytest.raises(ValueError, match="already registered"):
        reg.add("a", lambda p, c: 1)
    with pytest.raises(ValueError, match="cardinality"):
        reg.add("b", lambda p, c: 1, cardinality="per-thing")  # type: ignore[arg-type]
    assert "a" in reg and reg.names() == ["a"] and len(reg) == 1
    reg.clear()
    assert len(reg) == 0


class StubTransport:
    """A source-side transport whose POST /v1/messages outcome is scripted."""

    def __init__(self, outcome: Exception | Terminal | Budget) -> None:
        self.outcome = outcome
        self.posted: list[str] = []

    def info(self) -> Info:
        return Info("2.0.0", "leg-a", "dp-a", "source", "dst_to_src", "CHANNEL_UP")

    def submit(self, payload: Any, correlation_id: str | None = None) -> str | Terminal:
        raise NotImplementedError

    def poll_response(self, correlation_id: str, timeout_s: float) -> Delivered | Terminal | Empty:
        raise NotImplementedError

    def abandon(self, correlation_id: str) -> None:
        raise NotImplementedError

    def next_message(self, timeout_s: float) -> Delivered | Terminal | Empty:
        return EMPTY

    def post_response(self, in_reply_to: str, payload: Any) -> Budget | Terminal | None:
        self.posted.append(in_reply_to)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def complete(self) -> Terminal | None:
        raise NotImplementedError


def test_409_on_respond_drops_the_round_without_raising(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    reg, _ = make_registry()
    transport = StubTransport(ConcurrencyError("409", peer="dp-a", correlation_id="c1"))
    server = Server(transport, reg, settings, label="x")
    server.start(time.monotonic() + 1)
    query = {"v": 1, "kind": "query", "operation": "total", "params": {}, "encoding": "json"}
    with caplog.at_level(logging.DEBUG, logger=_log.LOGGER_NAME):
        assert server.step(Delivered("m1", "c1", query, None, None)) is None
    assert transport.posted == ["c1"] and "c1" not in server.memo and server.rounds_served == 0
    assert any("round.protocol_error" in r.getMessage() for r in caplog.records)
    # A terminal body on the post ends the leg like one on the poll.
    transport = StubTransport(Terminal("LIMIT_EXCEEDED", "cap", {"cap": "maxRounds", "limit": 1, "observed": 2}))
    server = Server(transport, reg, settings, label="x")
    server.start(time.monotonic() + 1)
    with pytest.raises(LimitExceededError) as exc:
        server.step(Delivered("m2", "c2", query, None, None))
    assert exc.value.cap == "maxRounds"


def test_memo_is_an_lru() -> None:
    memo = ResponseMemo(2)
    memo.put("a", 1)
    memo.put("b", 2)
    assert memo.get("a") == 1  # touch a
    memo.put("c", 3)  # evicts b
    assert "b" not in memo and "a" in memo and "c" in memo and len(memo) == 2


def test_guard_helpers_directly() -> None:
    assert (
        distinct_count(None) == 0
        and distinct_count("x") == 1
        and distinct_count(["a", "a", "b", 1, 1.0, {"k": 1}, {"k": 1}]) == 4
    )
    spec = OperationSpec("op", lambda p, c: 1, person_id_param="ids", cardinality="per-group", count_column="n")
    check_query({"ids": ["a", "b"]}, spec, Guards(max_distinct_person_ids=2))
    check_query({"ids": ["a", "b", "c"]}, spec, None)  # disabled
    check_query(
        {}, spec, Guards(max_distinct_person_ids=0)
    )  # 0 means "no limit" after from_info; direct None-equivalent
    with pytest.raises(GuardRefused, match="distinct"):
        check_query({"ids": ["a", "b", "c"]}, spec, Guards(max_distinct_person_ids=2))
    # Shapes the guard cannot count are refused, not counted as one value (an object's keys or a nested
    # array's members would reach the handler as N ids).
    for shape in ({"a": "a", "b": "b", "c": "c"}, [["a", "b", "c"]], ["a", ["b", "c"]], ["a", {"k": "b"}], [None]):
        with pytest.raises(GuardRefused, match="flat array") as exc:
            check_query({"ids": shape}, spec, Guards(max_distinct_person_ids=100))
        assert exc.value.detail() == {"guard": "maxDistinctPersonIds", "limit": 100}
    check_query({"ids": {"a": 1}}, spec, None)  # not enforced when the guard is disabled
    table = Table.from_columns({"g": ["x"], "n": [3]}).to_json()
    check_result(table, spec, Guards(min_group_size=3))
    with pytest.raises(GuardRefused, match="smaller"):
        check_result(table, spec, Guards(min_group_size=4))
    null_count = Table.from_columns({"g": ["x"], "n": [None]}, {"n": "integer"}).to_json()
    with pytest.raises(GuardRefused, match="smaller"):
        check_result(null_count, spec, Guards(min_group_size=1))
    # The count column is never inferred from column types, and must be integer-typed.
    undeclared = OperationSpec("op", lambda p, c: 1, cardinality="per-group")
    with pytest.raises(GuardRefused, match="count_column"):
        check_result(table, undeclared, Guards(min_group_size=1))
    key_only = Table.from_columns({"year": [2024], "n": [1.0]}).to_json()
    with pytest.raises(GuardRefused, match="integer column") as exc:
        check_result(key_only, spec, Guards(min_group_size=11))
    assert exc.value.detail() == {"guard": "minGroupSize", "limit": 11}
    with pytest.raises(GuardRefused, match="not in the result"):
        check_result(Table.from_columns({"year": [2024]}).to_json(), spec, Guards(min_group_size=1))
    assert Guards.from_info(None) is None
    assert Guards.from_info({}) == Guards() and not Guards().enabled
    assert Guards.from_info({"maxDistinctPersonIds": 10, "minGroupSize": True}) == Guards(max_distinct_person_ids=10)
    assert preflight([spec], None) == []
    assert preflight([spec], [{"name": "op", "cardinality": "per-group"}]) == []
    assert preflight([spec], [{"name": "op"}]) == []  # approved without a cardinality: not compared


SUBPROCESS_SOURCE = """
import sys
from safeinsights_fusion import operations, serve
@operations.register("total")
def total(params, ctx):
    return {"total": 1, "big": "x" * int(params.get("pad", 0))}
serve()
"""


def _spawn_source(pair: Any) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        **pair.env("source"),
        "FUSION_READY_TIMEOUT_S": "20",
        "FUSION_POLL_HTTP_TIMEOUT_S": "5",
        "FUSION_LOG_LEVEL": "DEBUG",
    }
    env["PYTHONPATH"] = str(ROOT / "python" / "src")
    return subprocess.Popen(
        [sys.executable, "-c", SUBPROCESS_SOURCE], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def test_study_complete_exits_zero_and_limit_exceeded_exits_nonzero_in_a_subprocess(
    fake: FakeFactory, settings: Settings
) -> None:
    pair = fake("happy")
    proc = _spawn_source(pair)
    try:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        assert fusion.peer().request("total", {}).body["total"] == 1
        fusion.complete()
        _, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        assert "session.complete" in err
    finally:
        if proc.poll() is None:
            proc.kill()

    pair = fake("limit-exceeded-bytes")
    proc = _spawn_source(pair)
    try:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        with pytest.raises(LimitExceededError):
            fusion.peer().request("total", {"pad": 500})
        _, err = proc.communicate(timeout=30)
        assert proc.returncode != 0
        assert "LimitExceededError" in err and "LIMIT_EXCEEDED" in err
    finally:
        if proc.poll() is None:
            proc.kill()


def test_source_logs_are_content_free(
    fake: FakeFactory,
    settings: Settings,
    registry: tuple[OperationRegistry, dict[str, int]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    pair = fake("happy")
    reg, _ = registry
    with caplog.at_level(logging.DEBUG), ServeThread(pair, reg, settings) as st:
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        peer = fusion.peer()
        peer.request("counts_by_group", {"person_ids": ["MARKER_PARAM_33cc"]})
        with pytest.raises(RemoteError):
            peer.request("boom", {"x": "MARKER_PARAM_44dd"})
        fusion.complete()
        assert st.wait() is None
    event_lines = [r.getMessage() for r in caplog.records if r.name == _log.LOGGER_NAME]
    text = "\n".join(event_lines)
    for marker in (
        "MARKER_PARAM_33cc",
        "MARKER_PARAM_44dd",
        "MARKER_TRACE_11aa",
        "division by zero",
        pair.endpoints[0].source.token,
    ):
        assert marker not in text
    events = {line.split()[1] for line in event_lines}
    assert {
        "serve.start",
        "guards.disabled",
        "serve.received",
        "round.served",
        "serve.handler_error",
        "session.complete",
    } <= events
    # The traceback exists only on the separate local trace logger, at DEBUG.
    traces = [r for r in caplog.records if r.name == _log.LOGGER_NAME + ".handler_trace"]
    assert traces and all(r.levelno == logging.DEBUG for r in traces) and "MARKER_TRACE_11aa" in traces[0].getMessage()
