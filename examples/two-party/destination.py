"""Two-party fusion study, DESTINATION side (the researcher's analysis).

`analysis(fusion)` is the whole study. Run inside the enclave it connects through the env the
Setup App injects; under the simulator it receives a simulated `Fusion`.
"""

from __future__ import annotations

from typing import Any

from safeinsights_fusion import Fusion, RemoteError

# Person IDs the destination already holds (from its own data); the source answers about them.
MY_PERSON_IDS = ["p-001", "p-002", "p-003", "p-004", "p-005", "p-999"]


def analysis(fusion: Fusion) -> dict[str, Any]:
    peer = fusion.peer()  # the only peer of a two-party study
    counts = peer.request("counts_by_group", {"person_ids": MY_PERSON_IDS, "group_by": "grade"})
    by_grade = {row["grade"]: row["n"] for row in counts.to_records()}
    print(f"counts by grade: {by_grade}; budget rounds {counts.budget.rounds_used if counts.budget else '?'}")

    # Round 2 depends on round 1: ask about the largest group only.
    largest = max(by_grade, key=lambda g: by_grade[g])
    ids_in_largest = MY_PERSON_IDS[: by_grade[largest]]  # in a real study: the IDs you know are in that grade
    try:
        mean = peer.request("mean_score", {"person_ids": ids_in_largest})
    except RemoteError as exc:
        print(f"source declined: {exc.code}")  # e.g. GUARD_REFUSED: re-batch and retry
        raise
    print(f"mean score in grade {largest}: {mean.body['mean']:.2f} over {mean.body['n']} people")
    return {"by_grade": by_grade, "largest": largest, "mean": mean.body["mean"]}


if __name__ == "__main__":
    with Fusion.connect() as fusion:  # CLOSE is sent on a clean exit
        result = analysis(fusion)
    # Result release stays with your Data Partner package (e.g. the TOA upload helper), not the SDK.
    print(result)
