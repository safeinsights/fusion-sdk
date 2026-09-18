"""Hub fusion study, SOURCE B (Data Partner B, in Python). Per-group outcomes and totals."""

from __future__ import annotations

from typing import Any

from safeinsights_fusion import Context, OperationRegistry, Table, serve

SAMPLE = {"p-001": ("2024", 1), "p-002": ("2024", 0), "p-003": ("2025", 1), "p-004": ("2025", 1), "p-005": ("2025", 0)}

operations = OperationRegistry()


@operations.register("outcomes_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
def outcomes_by_group(params: dict[str, Any], ctx: Context) -> Table:
    groups: dict[str, list[int]] = {}
    for pid in params.get("person_ids", []):
        if pid in SAMPLE:
            cohort, outcome = SAMPLE[pid]
            groups.setdefault(cohort, []).append(outcome)
    return Table.from_columns(
        {
            "cohort": list(groups),
            "n": [len(v) for v in groups.values()],
            "positive_rate": [sum(v) / len(v) for v in groups.values()],
        }
    )


@operations.register("totals", person_id_param="person_ids", cardinality="aggregate")
def totals(params: dict[str, Any], ctx: Context) -> dict[str, Any]:
    known = [p for p in params.get("person_ids", []) if p in SAMPLE]
    return {"total": len(known)}


if __name__ == "__main__":
    serve(operations)
