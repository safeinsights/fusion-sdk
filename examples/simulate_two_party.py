"""Run the two-party example end to end in this process (no tunnel): what the IDE does."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from safeinsights_fusion import simulate

HERE = Path(__file__).resolve().parent


def load(path: Path) -> object:
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    source = load(HERE / "two-party" / "source.py")
    destination = load(HERE / "two-party" / "destination.py")
    result = simulate(source.operations, destination.analysis, guards={"maxDistinctPersonIds": 5000, "minGroupSize": 2})  # type: ignore[attr-defined]
    print("simulated result:", result)
    sys.exit(0 if result["by_grade"] == {"9": 2, "10": 3} else 1)
