# T7 — `DELETE /v1/request/{correlationId}` abandons a round

**Repo:** fusion-tunnel-app · **Plan section:** Phase 2 routes, Phase 5 reliability · **Requested by:** fusion-sdk (ADR 0004)

When the destination SDK exhausts its re-issues it raises `RoundTimeoutError` and, per the plan, the peer stays usable. The tunnel, however, still counts the abandoned round as in flight and would answer the next `POST /v1/request` with `409`.

**Ask.** `DELETE /v1/request/{correlationId}` (destination role): drop the in-flight entry; if the correlated response arrives later, ACK it end-to-end and discard it (the source's round is complete; nothing is redelivered). `204`, idempotent, including for an unknown id.

**Fallback the SDK implements without T7:** on `409` naming the abandoned id, poll it once with a short hold, ACK whatever arrived, retry the submit; otherwise raise `ConcurrencyError`. T7 removes the race and the hidden budget spend of a late response nobody will read.

**Reference implementation:** `tools/fake_tunnel_pair/server.py` (`dst_abandon`).
