"""Scenario definitions: typed view of spec/scenarios/*.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SPEC_DIR = Path(__file__).resolve().parents[2] / "spec" / "scenarios"

#: The manifest cap names, as the tunnel reports them on /v1/info and in LIMIT_EXCEEDED details.
CAP_NAMES = (
    "maxRounds",
    "maxRoundsPerHour",
    "maxResponsePlaintextBytesPerRound",
    "maxCumulativeResponsePlaintextBytes",
    "maxQueryPlaintextBytesPerRound",
    "maxCumulativeQueryPlaintextBytes",
)


@dataclass(frozen=True)
class Faults:
    backpressure_on_request: frozenset[int] = frozenset()
    backpressure_on_respond: frozenset[int] = frozenset()
    not_ready_on_request: frozenset[int] = frozenset()
    server_error_on_request: frozenset[int] = frozenset()
    drop_response_rounds: frozenset[int] = frozenset()
    forget_correlation_rounds: frozenset[int] = frozenset()
    redeliver_rounds: frozenset[int] = frozenset()
    delay_delivery_ms: dict[int, int] = field(default_factory=dict)
    error_after_round: int | None = None

    @staticmethod
    def from_json(obj: dict[str, Any] | None) -> Faults:
        if not obj:
            return Faults()

        def fs(key: str) -> frozenset[int]:
            return frozenset(int(x) for x in obj.get(key, []))

        return Faults(
            backpressure_on_request=fs("backpressureOnRequest"),
            backpressure_on_respond=fs("backpressureOnRespond"),
            not_ready_on_request=fs("notReadyOnRequest"),
            server_error_on_request=fs("serverErrorOnRequest"),
            drop_response_rounds=fs("dropResponseRounds"),
            forget_correlation_rounds=fs("forgetCorrelationRounds"),
            redeliver_rounds=fs("redeliverRounds"),
            delay_delivery_ms={int(k): int(v) for k, v in obj.get("delayDeliveryMs", {}).items()},
            error_after_round=obj.get("errorAfterRound"),
        )


@dataclass(frozen=True)
class LegSpec:
    leg_id: str
    peer_org_slug: str
    caps: dict[str, int] = field(default_factory=dict)  # manifest cap name → limit; absent = unlimited
    guards: dict[str, Any] | None = None
    operations: list[dict[str, Any]] | None = None
    faults: Faults = Faults()


@dataclass(frozen=True)
class Scenario:
    id: str
    legs: tuple[LegSpec, ...]
    description: str = ""
    ready_delay_ms: int = 0
    longpoll_ms: int = 25_000
    max_body_bytes: int = 64 * 1024 * 1024

    @staticmethod
    def from_json(obj: dict[str, Any]) -> Scenario:
        legs: list[LegSpec] = []
        for leg in obj["legs"]:
            caps = leg.get("caps", obj.get("caps")) or {}
            legs.append(
                LegSpec(
                    leg_id=leg["legId"],
                    peer_org_slug=leg["peerOrgSlug"],
                    caps={k: int(v) for k, v in caps.items() if k in CAP_NAMES and v is not None},
                    guards=leg.get("guards", obj.get("guards")),
                    operations=leg.get("operations", obj.get("operations")),
                    faults=Faults.from_json(leg.get("faults", obj.get("faults"))),
                )
            )
        return Scenario(
            id=obj["id"],
            description=obj.get("description", ""),
            legs=tuple(legs),
            ready_delay_ms=int(obj.get("readyDelayMs", 0)),
            longpoll_ms=int(obj.get("longpollMs", 25_000)),
            max_body_bytes=int(obj.get("maxBodyBytes", 64 * 1024 * 1024)),
        )


def load_scenario(name_or_path: str | Path, spec_dir: Path | None = None) -> Scenario:
    """Load `spec/scenarios/<name>.json` (or an explicit path)."""
    path = Path(name_or_path)
    if not path.suffix:
        path = (spec_dir or DEFAULT_SPEC_DIR) / f"{name_or_path}.json"
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"scenario {path} is not an object")
    return Scenario.from_json(data)
