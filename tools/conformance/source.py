"""Conformance source (Python). See README.md for the operation contract."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from typing import Any

from safeinsights_fusion import Context, OperationRegistry, Table, serve

ops = OperationRegistry()


@ops.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
def counts_by_group(params: dict[str, Any], ctx: Context) -> Table:
    n = len(params.get("person_ids") or [])
    return Table.from_columns({"grade": ["9", "10"], "n": [n, 2 * n + 1]})


@ops.register("total")
def total(params: dict[str, Any], ctx: Context) -> dict[str, Any]:
    return {"total": 42, "correlation_id": ctx.correlation_id, "peer": ctx.peer, "operation": ctx.operation}


@ops.register("echo")
def echo(params: dict[str, Any], ctx: Context) -> dict[str, Any]:
    return params


@ops.register("table_types")
def table_types(params: dict[str, Any], ctx: Context) -> Table:
    return Table.from_columns(
        {
            "s": ["héllo ✓", None],
            "i": [412, None],
            "i64": [9007199254740993, None],
            "x": [1.5, None],
            "b": [True, None],
            "d": [date(2024, 2, 29), None],
            "t": [datetime(2026, 9, 18, 12, 0, 0, 250_000, tzinfo=timezone.utc), None],
        },
        {"i64": "integer64"},
    )


@ops.register("boom")
def boom(params: dict[str, Any], ctx: Context) -> None:
    raise ZeroDivisionError("conformance boom")


if __name__ == "__main__":
    serve(ops)
    sys.exit(0)
