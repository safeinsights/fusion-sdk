"""Hub fusion study, DESTINATION side: a SafeInsights-hosted enclave with two Data Partner sources.

Peers are keyed by the Data Partner's organization slug. Different peers may be in flight at the
same time; one round at a time per peer. The query to B is derived from A's answer: exactly the
information path the source-side caps and guards bound.
"""

from __future__ import annotations

from typing import Any

from safeinsights_fusion import Fusion, LimitExceededError

MY_PERSON_IDS = ["p-001", "p-002", "p-003", "p-004", "p-005"]


def analysis(fusion: Fusion) -> dict[str, Any]:
    a, b = fusion.peer("dp-a"), fusion.peer("dp-b")
    enrolled = a.request("enrolled_ids", {"person_ids": MY_PERSON_IDS})
    ids = enrolled.body["person_ids"]  # A tells us which of our people it knows
    print(
        f"dp-a knows {len(ids)} of {len(MY_PERSON_IDS)}; rounds used on A: {enrolled.budget.rounds_used if enrolled.budget else '?'}"
    )

    # The query to B depends on A's answer (bounded by B's query-side caps and guards).
    outcomes = b.request("outcomes_by_group", {"person_ids": ids, "group_by": "cohort"})
    table = outcomes.to_records()
    print(f"dp-b outcomes by cohort: {table}")

    summary: dict[str, Any] = {"known_by_a": len(ids), "b_groups": len(table)}
    try:
        totals = b.request("totals", {"person_ids": ids})
        summary["b_total"] = totals.body["total"]
    except LimitExceededError as exc:
        # Leg B ended (a cap was reached); leg A is unaffected and complete() will still CLOSE it.
        print(f"dp-b ended the leg: {exc.cap}")
        summary["b_total"] = None
    return summary


if __name__ == "__main__":
    with Fusion.connect() as fusion:
        result = analysis(fusion)
        print("per-leg outcomes:", fusion.complete())
    print(result)
