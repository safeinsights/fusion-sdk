"""Source-side guards (ADR 0002): maxDistinctPersonIds before the handler, minGroupSize after it.

The tunnel cannot parse query semantics; the source research container can count. A breach refuses the
whole round with GUARD_REFUSED; there is no partial result and no silent suppression.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ._envelope import Table, is_table

JSON = Any
Cardinality = Literal["aggregate", "per-group", "per-record"]
CARDINALITIES: tuple[Cardinality, ...] = ("aggregate", "per-group", "per-record")

Handler = Callable[[dict[str, Any], Any], Any]


@dataclass(frozen=True)
class OperationSpec:
    """A registered operation and its guard declarations (plan §2.2)."""

    name: str
    handler: Handler
    person_id_param: str | None = None
    cardinality: Cardinality = "aggregate"
    count_column: str | None = None
    sanitize: bool = False


@dataclass(frozen=True)
class Guards:
    """Limits read from the source tunnel's GET /v1/info.guards (ask T3). `None` means disabled."""

    max_distinct_person_ids: int | None = None
    min_group_size: int | None = None

    @classmethod
    def from_info(cls, guards: Mapping[str, Any] | None) -> Guards | None:
        if guards is None:
            return None

        def g(key: str) -> int | None:
            v = guards.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

        return cls(max_distinct_person_ids=g("maxDistinctPersonIds"), min_group_size=g("minGroupSize"))

    @property
    def enabled(self) -> bool:
        return self.max_distinct_person_ids is not None or self.min_group_size is not None


class GuardRefused(Exception):
    """Raised inside serve(); becomes a GUARD_REFUSED error envelope. `observed` is None for minGroupSize."""

    def __init__(self, guard: str, limit: int, message: str, observed: int | None = None) -> None:
        super().__init__(message)
        self.guard = guard
        self.limit = limit
        self.observed = observed
        self.message = message

    def detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"guard": self.guard, "limit": self.limit}
        if self.observed is not None:
            d["observed"] = self.observed
        return d


def distinct_count(values: JSON) -> int:
    """Distinct values of a param: a list counts its unique members, a scalar counts one, missing counts zero."""
    if values is None:
        return 0
    if isinstance(values, (list, tuple, set, frozenset)):
        seen: set[str] = set()
        for v in values:
            seen.add(_canonical_scalar(v))
        return len(seen)
    return 1


def _canonical_scalar(v: JSON) -> str:
    """Stable key for a Person-ID value. 1 and 1.0 are the same id (R cannot tell them apart after parsing)."""
    if isinstance(v, str):
        return "s:" + v
    if isinstance(v, bool):
        return "b:" + str(v)
    if isinstance(v, float) and v.is_integer():
        return "n:" + str(int(v))
    if isinstance(v, (int, float)):
        return "n:" + str(v)
    return "j:" + json.dumps(v, sort_keys=True, separators=(",", ":"))


def check_query(params: Mapping[str, Any], spec: OperationSpec, guards: Guards | None) -> None:
    """maxDistinctPersonIds: bound the distinct values of the handler's declared Person-ID parameter."""
    if guards is None or guards.max_distinct_person_ids is None or spec.person_id_param is None:
        return
    n = distinct_count(params.get(spec.person_id_param))
    if n > guards.max_distinct_person_ids:
        raise GuardRefused(
            "maxDistinctPersonIds",
            guards.max_distinct_person_ids,
            f"query names {n} distinct values of {spec.person_id_param!r}; the limit is {guards.max_distinct_person_ids}",
            observed=n,
        )


def find_count_column(table: Table, declared: str | None) -> str:
    if declared is not None:
        if declared not in table.names:
            raise GuardRefused("minGroupSize", 0, f"declared count column {declared!r} is not in the result")
        return declared
    candidates = [c.name for c in table.columns if c.type in ("integer", "integer64")]
    if len(candidates) != 1:
        raise GuardRefused(
            "minGroupSize",
            0,
            f"cannot identify the group-count column ({len(candidates)} integer columns); register the operation with count_column",
        )
    return candidates[0]


def check_result(body: JSON, spec: OperationSpec, guards: Guards | None) -> None:
    """minGroupSize: every group count in a per-group result table must be at least the limit."""
    if guards is None or guards.min_group_size is None or spec.cardinality != "per-group":
        return
    limit = guards.min_group_size
    if not is_table(body):
        raise GuardRefused(
            "minGroupSize", limit, "per-group results must be a fusion table so group sizes can be checked"
        )
    table = Table.from_json(body)
    try:
        col = find_count_column(table, spec.count_column)
    except GuardRefused as exc:
        raise GuardRefused("minGroupSize", limit, exc.message) from None
    for value in table.column(col):
        if value is None or not isinstance(value, int) or value < limit:
            # The small cell's size deliberately does not cross: no `observed`.
            raise GuardRefused(
                "minGroupSize", limit, f"a group in {col!r} is smaller than the minimum group size {limit}"
            )


def preflight(registered: Iterable[OperationSpec], approved: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """Compare the registry with the manifest's approved operation list (ask T3). Returns problems; empty is fine."""
    if approved is None:
        return []
    problems: list[str] = []
    by_name: dict[str, str] = {}
    for op in approved:
        name, card = op.get("name"), op.get("cardinality")
        if isinstance(name, str):
            by_name[name] = card if isinstance(card, str) else ""
    regs = {s.name: s for s in registered}
    for name, spec in regs.items():
        if name not in by_name:
            problems.append(f"operation {name!r} is registered but not in the approved list")
        elif by_name[name] and by_name[name] != spec.cardinality:
            problems.append(f"operation {name!r} is registered as {spec.cardinality} but approved as {by_name[name]}")
    for name in by_name:
        if name not in regs:
            problems.append(f"operation {name!r} is approved but no handler is registered")
    return problems


__all__ = [
    "CARDINALITIES",
    "Cardinality",
    "GuardRefused",
    "Guards",
    "Handler",
    "OperationSpec",
    "check_query",
    "check_result",
    "distinct_count",
    "find_count_column",
    "preflight",
]
