# T3 — `GET /v1/info` carries `apiVersion`, `guards`, `caps`, `operations`

**Repo:** fusion-tunnel-app · **Plan section:** Phase 2 `GET /v1/info` · **Requested by:** fusion-sdk (ADR 0002)

The hub review (§2.3) puts `maxDistinctPersonIds` / `minGroupSize` enforcement in the SDK's handler wrapper at the source. The SDK needs the numbers; the manifest is the natural origin and the tunnel receives it in its configuration bundle.

**Ask.** Extend the content-free `/v1/info` body with optional fields:

```jsonc
{ "apiVersion": "1.0.0",
  "legId": "…", "peerOrgSlug": "…", "role": "…", "direction": "…", "state": "…",
  "guards": { "maxDistinctPersonIds": 5000, "minGroupSize": 11 },
  "caps": { "maxRounds": 500, "maxResponseBytes": …, "maxResponseBytesPerRound": …, "maxQueryBytes": …, "maxQueryBytesPerRound": …, "maxRoundsPerHour": … },
  "operations": [ { "name": "counts_by_group", "cardinality": "per-group" } ] }
```

All three are optional; when absent the SDK disables guards with a startup warning. `operations`, if the manifest carries the approved list, lets `serve()` refuse to start with an unregistered-but-approved or registered-but-unapproved handler — a pre-flight, not a security boundary. `/v1/info` must be answerable in every lifecycle state (the SDK polls it during readiness).
