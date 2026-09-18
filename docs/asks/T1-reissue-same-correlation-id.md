# T1 — `POST /v1/request` accepts a client-supplied `correlationId` on re-issue

**Repo:** fusion-tunnel-app · **Plan section:** Phase 2 routes, Phase 5 reliability · **Requested by:** fusion-sdk (ADR 0004)

Architecture Doc v2 §7.3 assigns round liveness to the destination SDK: on round timeout it re-issues the query with the **same** `correlationId`, and the source tunnel replays its cached response. The tunnel plan's `POST /v1/request` mints the id and does not accept one.

**Ask.** Accept an optional `correlationId` in the request body:

- If it names the in-flight round: return `202 {correlationId}` and create no second outbox entry (idempotent).
- If the tunnel does not know it (the tunnel restarted): accept it as the new outbox entry **with that id**, so the source tunnel's `correlationId` dedup and cached-response replay apply.
- If another round is in flight under a different id: `409 IN_FLIGHT_CONFLICT`.

**Alternative** the SDK can accommodate with a one-line change: the SDK mints the `correlationId` up front (idempotency-key style) and every submit carries it, making the route a plain idempotent POST. Or `POST /v1/request/{correlationId}/reissue`.

**Why the tunnel cannot own this:** after its own restart the tunnel's memory-only outbox is gone; only the research container still holds the query. A fresh id would recompute at the source and double-count against `maxRounds`.

**Reference implementation:** `tools/fake_tunnel_pair/server.py` (`handle_request`), `spec/local-api.md`.
