# T2 — Budget hints on the local API

**Repo:** fusion-tunnel-app · **Plan section:** Phase 5 `reliability/caps.ts` · **Requested by:** fusion-sdk

Security review §7.3: "the destination SDK receives remaining-budget hints piggybacked on responses so researcher code can see consumption approaching limits". The tunnel plan meters caps at the source but does not say where the hints surface on the local API.

**Ask.** Add a `budget` object to:

- `GET /v1/responses/{id}` (destination) — the source's counters as carried in the AEAD-protected response header;
- `GET /v1/messages/next` and the `202` of `POST /v1/messages` (source) — the source's own view.

```jsonc
"budget": { "roundsUsed": 3, "roundsMax": 500,
            "responseBytesUsed": 10240, "responseBytesMax": 4294967296,
            "queryBytesUsed": 2048, "queryBytesMax": 1073741824,
            "roundsPerHourUsed": 3, "roundsPerHourMax": 120 }
```

`*Max` is `null` when the manifest sets no cap. The SDK exposes it as `response.budget` / `peer.budget()` and logs `budget.near_limit` at 90 %.
