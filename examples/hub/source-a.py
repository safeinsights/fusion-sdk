"""Hub fusion study, SOURCE A (Data Partner A, in Python). Answers which of the requested people it knows."""

from __future__ import annotations

from typing import Any

from safeinsights_fusion import Context, OperationRegistry, serve

KNOWN = {"p-001", "p-002", "p-003", "p-004"}

operations = OperationRegistry()


@operations.register("enrolled_ids", person_id_param="person_ids", cardinality="per-record")
def enrolled_ids(params: dict[str, Any], ctx: Context) -> dict[str, Any]:
    return {"person_ids": [p for p in params.get("person_ids", []) if p in KNOWN]}


if __name__ == "__main__":
    serve(operations)
