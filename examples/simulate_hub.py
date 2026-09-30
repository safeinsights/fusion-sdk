"""Run the hub example in this process with two simulated Data Partners; leg B is capped at two rounds."""

from __future__ import annotations

import sys
from pathlib import Path

from safeinsights_fusion import SimFaults, simulate

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_two_party import HERE, load

if __name__ == "__main__":
    a = load(HERE / "hub" / "source-a.py")
    b = load(HERE / "hub" / "source-b.py")
    destination = load(HERE / "hub" / "destination.py")
    result = simulate(
        {"dp-a": a.operations, "dp-b": b.operations},  # type: ignore[attr-defined]
        destination.analysis,  # type: ignore[attr-defined]
        guards={"maxDistinctPersonIds": 100, "minGroupSize": 2},
        faults={"dp-b": SimFaults(max_rounds=1)},  # the totals round trips LIMIT_EXCEEDED on B
    )
    print("simulated result:", result)
    sys.exit(0 if result == {"known_by_a": 4, "b_groups": 2, "b_total": None} else 1)
