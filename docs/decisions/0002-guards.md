# ADR 0002 — Source-side guards refuse, never suppress

**Status:** accepted 2026-09-17 (product owner, plan §11 Q2) · **Date recorded:** 2026-09-18

## Context

The hub-topology review (§2.3, §6 item 2) placed a distinct-Person-ID guard and a minimum-group-size guard in the SDK's handler wrapper at the **source**: the tunnel cannot parse query semantics, but the source research container can count. The security review (§7.3) requires that hitting any bound is "a reviewable event, not a slowdown" and that there is no silent throttling.

## Decision

1. Two guards, enforced in `serve()` around every handler call, using limits read from the source tunnel's `GET /v1/info.guards` (ask T3):
   - `maxDistinctPersonIds`: the number of distinct values in the parameter the handler registered as `person_id_param` must not exceed the limit. Checked **before** the handler runs.
     The parameter must be absent, one scalar, or a flat array of scalars (string, number, boolean); an object, a nested array or an array with non-scalar members is refused as `GUARD_REFUSED` without being counted, because handlers routinely flatten such shapes (a dict's keys, `unlist()`) and a destination could otherwise name any number of ids as "one value".
   - `minGroupSize`: for operations registered as `cardinality = "per-group"`, every group count in the result table must be ≥ the limit. Checked **after** the handler runs, on the `count_column` named at registration, which must be an `integer` or `integer64` column. The column is never inferred: "the single integer column" heuristic let a table whose only integer column was a key (a year, an age) pass with its float-typed counts never inspected, so a per-group operation cannot be registered without `count_column`.
2. A breach **refuses the whole round**: the destination receives an error envelope `GUARD_REFUSED` with `detail.guard` and `detail.limit` (plus `detail.observed` for the distinct-ID guard only); the source logs `serve.guard_refused`; no partial result crosses.
3. Guards are optional: if `/v1/info` carries no `guards`, `serve()` logs `guards.disabled` once and runs without them, so the SDK does not block on manifest work in the BMA.
4. Handlers declare `cardinality ∈ {aggregate, per-group, per-record}` at registration. When `/v1/info.operations` is present, `serve()` refuses to start if a registered operation is not in the approved list or declares a different cardinality (pre-flight, not a security boundary).

## Alternatives rejected

- **Suppress small cells and return the rest.** Turns a reviewable event into a silent data-shaping step, invisible to the destination and the reviewer; contradicts "no silent throttling".
- **Guards in the tunnel.** The tunnel sees plaintext only at the source and does not understand parameter names or result shapes.

## Consequences

- Researchers see `RemoteError(code="GUARD_REFUSED")` and can re-batch; the simulator (Phase 6) lets them provoke it locally.
- `observed` for `minGroupSize` is deliberately withheld: the small cell's size is what the guard protects.
