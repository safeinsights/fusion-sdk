"""Two-party fusion study, SOURCE side (Data Partner).

Registers the operations the Data Partner approved at review and serves them until the destination
completes the study. Replace the sample data with your Data Partner package's data access.
"""

from __future__ import annotations

from typing import Any

from safeinsights_fusion import Context, OperationRegistry, Table, serve

# Sample data standing in for the Data Partner's Person-ID → record lookup.
SAMPLE = {
    "p-001": {"grade": "9", "score": 71.0},
    "p-002": {"grade": "9", "score": 64.5},
    "p-003": {"grade": "10", "score": 88.0},
    "p-004": {"grade": "10", "score": 79.5},
    "p-005": {"grade": "10", "score": 91.0},
}

operations = OperationRegistry()


@operations.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
def counts_by_group(params: dict[str, Any], ctx: Context) -> Table:
    """How many of the requested people fall in each group. Pure function of params: idempotent."""
    group_by = params.get("group_by", "grade")
    counts: dict[str, int] = {}
    for pid in params.get("person_ids", []):
        rec = SAMPLE.get(pid)
        if rec is not None:
            key = str(rec[group_by])
            counts[key] = counts.get(key, 0) + 1
    return Table.from_columns({group_by: list(counts), "n": list(counts.values())})


@operations.register("mean_score", person_id_param="person_ids", cardinality="aggregate")
def mean_score(params: dict[str, Any], ctx: Context) -> dict[str, Any]:
    scores = [SAMPLE[p]["score"] for p in params.get("person_ids", []) if p in SAMPLE]
    return {"n": len(scores), "mean": (sum(scores) / len(scores)) if scores else None}


if __name__ == "__main__":
    serve(operations)  # returns when the destination completes the study
