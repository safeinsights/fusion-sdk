# ADR 0004 — Round liveness policy lives in the SDK; transport recovery lives in the tunnel

**Status:** accepted 2026-09-17 (product owner, second round, plan §11 Q3) · **Date recorded:** 2026-09-18

## Context

Architecture Doc v2 §7.3: "Round liveness is owned by the destination. The SDK applies a configurable per-round timeout. On expiry it re-issues the query with the same `correlationId`." The product owner asked whether the tunnel should own re-issue instead, so researchers never manage reliability.

## Decision

The goal (researchers never manage reliability) is met with the mechanism in the SDK: re-issue is SafeInsights' SDK code, never researcher code. `request()` returns a result or raises a typed error.

The mechanism cannot move into the tunnel for one case: when the **destination tunnel itself restarts**, its memory-only outbox and every in-flight `correlationId` die with it (read-only root filesystem at the hub; no persistence). The research container is then the only process still holding the query, and the SDK already holds it inside the blocked `request()` call.

Therefore:

- **SDK:** round timeout (default 600 s, per-request override), up to `FUSION_ROUND_MAX_REISSUES` (3) same-`correlationId` re-issues on timeout or on `404 UNKNOWN_CORRELATION`, then `RoundTimeoutError`. Invisible to researcher code.
- **Tunnel:** outbox re-encryption and retransmission across epoch changes, relay redelivery within an epoch, cached-response replay at the source so a resend never re-runs the operation or spends a second round of budget.
- **Same `correlationId` on resend is mandatory** (ask T1): a fresh id would recompute at the source and double-count against `maxRounds`.

## Alternatives rejected

- **Tunnel-owned re-issue.** Impossible after the tunnel's own restart; would also require the tunnel to persist queries, which the hub's read-only, no-volume posture forbids.
- **SDK mints the `correlationId` up front (idempotency-key style).** Acceptable variant of T1 if the tunnel team prefers a plain idempotent POST; the SDK is written so either shape is a one-line change in `_tunnel` / `tunnel.R`.

## Consequences

- The fake tunnel pair implements T1; the real tunnel must add it (filed as ask T1).
- The source SDK keeps its own `correlationId → response` memo (in memory, LRU) for the RC-crash-after-ACK case the tunnel's replay cannot cover.
