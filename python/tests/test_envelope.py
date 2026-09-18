"""Envelope + fusion table: every fixture in spec/fixtures round-trips; encoder edge cases."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from safeinsights_fusion import _envelope as env
from safeinsights_fusion._envelope import Column, EnvelopeError, Query, ResponseEnvelope, Table

FIXTURES = sorted((Path(__file__).resolve().parents[2] / "spec" / "fixtures").glob("*.json"))


def json_equal(a: Any, b: Any) -> bool:
    """Structural equality with JSON number semantics (2 == 2.0), booleans distinct from ints."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def reencode(decoded: Query | ResponseEnvelope) -> dict[str, Any]:
    if isinstance(decoded, Query):
        return env.encode_query(decoded.operation, decoded.params)
    if decoded.error is not None:
        return env.encode_response_error(decoded.error.code, decoded.error.message, decoded.error.detail or None)
    body = Table.from_json(decoded.body) if env.is_table(decoded.body) else decoded.body
    return env.encode_response_ok(body)


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_fixture(path: Path) -> None:
    fx = json.loads(path.read_text(encoding="utf-8"))
    expect = fx.get("decoded", {})
    if fx["kind"] == "table":
        if not fx["valid"]:
            with pytest.raises(EnvelopeError):
                Table.from_json(fx["input"])
            return
        table = Table.from_json(fx["input"])
        assert table.nrow == expect["nrow"] and len(table.columns) == expect["ncol"]
        assert table.names == expect["names"] and [c.type for c in table.columns] == expect["types"]
        assert json_equal(table.to_json(), fx.get("canonical", fx["input"]))
        return
    if not fx["valid"]:
        with pytest.raises(EnvelopeError):
            env.decode(fx["input"])
        return
    decoded = env.decode(fx["input"])
    if isinstance(decoded, Query):
        assert expect["kind"] == "query" and decoded.operation == expect["operation"]
        if "paramKeys" in expect:
            assert sorted(decoded.params) == expect["paramKeys"]
    else:
        assert expect["kind"] == "response" and decoded.status == expect["status"]
        if "code" in expect:
            assert decoded.error is not None and decoded.error.code == expect["code"]
        if "bodyIsTable" in expect:
            assert env.is_table(decoded.body) is expect["bodyIsTable"]
        if expect.get("bodyIsTable"):
            t = Table.from_json(decoded.body)
            assert t.nrow == expect["table"]["nrow"] and t.names == expect["table"]["names"]
    assert json_equal(reencode(decoded), fx.get("canonical", fx["input"]))


def test_fixture_count_is_the_plan_minimum() -> None:
    assert len(FIXTURES) >= 40


# -- encoder edge cases ---------------------------------------------------------


def test_from_columns_infers_types_and_encodes_specials() -> None:
    t = Table.from_columns(
        {
            "s": ["a", None],
            "i": [1, 2],
            "big": [2**60, None],
            "x": [1.5, math.nan],
            "mixed_num": [1, 2.5],
            "b": [True, None],
            "d": [date(2026, 2, 28), None],
            "t": [datetime(2026, 9, 18, 12, 0, 0, 123_456, tzinfo=timezone.utc), datetime(2026, 9, 18, 14, 0)],
        }
    )
    assert [c.type for c in t.columns] == [
        "string",
        "integer",
        "integer64",
        "number",
        "number",
        "boolean",
        "date",
        "datetime",
    ]
    j = t.to_json()
    assert j["rows"][0] == ["a", 1, str(2**60), 1.5, 1.0, True, "2026-02-28", "2026-09-18T12:00:00.123Z"]
    assert j["rows"][1] == [None, 2, None, None, 2.5, None, None, "2026-09-18T14:00:00Z"]  # naive datetime == UTC
    decoded = env.decode(env.encode_response_ok(t))
    assert isinstance(decoded, ResponseEnvelope)
    assert decoded.body["columns"][2] == {"name": "big", "type": "integer64"}


def test_nan_and_inf_become_null_and_integer_beyond_safe_needs_integer64() -> None:
    t = Table.from_columns({"x": [math.inf, -math.inf, math.nan]}, {"x": "number"})
    assert t.to_json()["rows"] == [[None], [None], [None]]
    with pytest.raises(EnvelopeError, match="integer64"):
        Table.from_columns({"n": [2**53]}, {"n": "integer"})
    with pytest.raises(EnvelopeError, match="mixed"):
        Table.from_columns({"m": ["a", 1]})
    with pytest.raises(EnvelopeError, match="different lengths"):
        Table.from_columns({"a": [1], "b": [1, 2]})


def test_records_round_trip_and_column_access() -> None:
    recs: list[dict[str, Any]] = [{"grade": "9", "n": 412}, {"grade": "10", "n": 388}, {"grade": None, "n": 0}]
    t = Table.from_records(recs)
    assert t.to_records() == recs
    assert t.column("n") == [412, 388, 0]
    assert Table.from_json(t.to_json()) == t


def test_datetime_parsing_normalises_offsets_and_truncates() -> None:
    assert env.format_datetime(env.parse_datetime("2026-09-18T12:00:00.123456789+02:00")) == "2026-09-18T10:00:00.123Z"
    assert env.format_datetime(env.parse_datetime("2026-09-18T12:00:00Z")) == "2026-09-18T12:00:00Z"
    for bad in ("2026-09-18T12:00:00", "2026-09-18 12:00:00Z", "2026-13-01T00:00:00Z", "yesterday"):
        with pytest.raises(EnvelopeError):
            env.parse_datetime(bad)
    with pytest.raises(EnvelopeError):
        env.parse_date("2026-02-30")


def test_query_encoding_validates_operation_and_params() -> None:
    q = env.encode_query("counts_by_group", {"person_ids": ["a"], "n": 1})
    assert q == {
        "v": 1,
        "kind": "query",
        "operation": "counts_by_group",
        "params": {"person_ids": ["a"], "n": 1},
        "encoding": "json",
    }
    with pytest.raises(EnvelopeError):
        env.encode_query("9bad", {})
    with pytest.raises(EnvelopeError):
        env.encode_query("ok", {"x": math.nan})
    with pytest.raises(EnvelopeError):
        env.encode_query("ok", {"x": object()})
    with pytest.raises(EnvelopeError):
        env.encode_response_error("NOPE", "x")


def test_decode_rejects_nested_table_errors_at_decode_time() -> None:
    bad = {
        "v": 1,
        "kind": "response",
        "status": "ok",
        "body": {"__table__": 1, "columns": [{"name": "n", "type": "integer"}], "rows": [["x"]]},
    }
    with pytest.raises(EnvelopeError):
        env.decode(bad)


def test_canonical_bytes_counts_utf8_compact() -> None:
    assert env.canonical_bytes({"a": "é"}) == len('{"a":"é"}'.encode())


def test_pandas_round_trip() -> None:
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        {
            "grade": ["9", "10", None],
            "n": pd.array([412, 388, None], dtype="Int64"),
            "share": [0.5, 0.25, None],
            "flag": pd.array([True, False, None], dtype="boolean"),
            "when": pd.to_datetime(
                ["2026-09-18T12:00:00Z", "2026-09-18T13:00:00.5Z", None], utc=True, format="ISO8601"
            ),
        }
    )
    t = Table.from_pandas(df)
    assert [c.type for c in t.columns] == ["string", "integer", "number", "boolean", "datetime"]
    back = t.to_pandas()
    assert list(back.columns) == ["grade", "n", "share", "flag", "when"]
    assert back["n"].tolist()[:2] == [412, 388] and pd.isna(back["n"][2])
    assert str(back["when"].dtype).startswith("datetime64")
    assert t.to_json()["rows"][1][4] == "2026-09-18T13:00:00.500Z"
    assert env.encode_body(df) == t.to_json()


def test_column_dataclass_is_hashable_and_types_are_closed() -> None:
    assert {Column("a", "string")} == {Column("a", "string")}
    assert env.COLUMN_TYPES == ("string", "integer", "integer64", "number", "boolean", "date", "datetime")
    assert env.REMOTE_CODES == ("UNKNOWN_OPERATION", "BAD_PARAMS", "HANDLER_ERROR", "GUARD_REFUSED")
