"""Scenario definitions: typed view of spec/scenarios/*.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SPEC_DIR = Path(__file__).resolve().parents[2] / "spec" / "scenarios"


@dataclass(frozen=True)
class Caps:
    max_rounds: int | None = None
    max_response_bytes: int | None = None
    max_response_bytes_per_round: int | None = None
    max_query_bytes: int | None = None
    max_query_bytes_per_round: int | None = None
    max_rounds_per_hour: int | None = None

    @staticmethod
    def from_json(obj: dict[str, Any] | None) -> Caps:
        if not obj:
            return Caps()
        return Caps(
            max_rounds=obj.get("maxRounds"),
            max_response_bytes=obj.get("maxResponseBytes"),
            max_response_bytes_per_round=obj.get("maxResponseBytesPerRound"),
            max_query_bytes=obj.get("maxQueryBytes"),
            max_query_bytes_per_round=obj.get("maxQueryBytesPerRound"),
            max_rounds_per_hour=obj.get("maxRoundsPerHour"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "maxRounds": self.max_rounds,
            "maxResponseBytes": self.max_response_bytes,
            "maxResponseBytesPerRound": self.max_response_bytes_per_round,
            "maxQueryBytes": self.max_query_bytes,
            "maxQueryBytesPerRound": self.max_query_bytes_per_round,
            "maxRoundsPerHour": self.max_rounds_per_hour,
        }

    def any_set(self) -> bool:
        return any(v is not None for v in self.to_json().values())


@dataclass(frozen=True)
class Faults:
    backpressure_on_request: frozenset[int] = frozenset()
    backpressure_on_respond: frozenset[int] = frozenset()
    not_ready_on_request: frozenset[int] = frozenset()
    server_error_on_request: frozenset[int] = frozenset()
    drop_response_rounds: frozenset[int] = frozenset()
    forget_correlation_rounds: frozenset[int] = frozenset()
    redeliver_after_ack_rounds: frozenset[int] = frozenset()
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
            redeliver_after_ack_rounds=fs("redeliverAfterAckRounds"),
            delay_delivery_ms={int(k): int(v) for k, v in obj.get("delayDeliveryMs", {}).items()},
            error_after_round=obj.get("errorAfterRound"),
        )


@dataclass(frozen=True)
class LegSpec:
    leg_id: str
    peer_org_slug: str
    caps: Caps = Caps()
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
    redelivery_ms: int = 5_000
    max_deliveries: int = 5

    @staticmethod
    def from_json(obj: dict[str, Any]) -> Scenario:
        legs: list[LegSpec] = []
        for leg in obj["legs"]:
            legs.append(
                LegSpec(
                    leg_id=leg["legId"],
                    peer_org_slug=leg["peerOrgSlug"],
                    caps=Caps.from_json(leg.get("caps", obj.get("caps"))),
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
            redelivery_ms=int(obj.get("redeliveryMs", 5_000)),
            max_deliveries=int(obj.get("maxDeliveries", 5)),
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
