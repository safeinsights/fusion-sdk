# fusion-sdk

The SafeInsights **Fusion SDK**: the researcher-facing R and Python libraries for the Enclave Fusion Framework. Researcher code never touches the transport — it calls `request(peer, operation, params)` on the destination side or registers Data-Partner-approved operation handlers on the source side, and the SDK drives the per-job Fusion Tunnel App's enclave-local API (submit/poll, two-stage ACK, duplicate suppression, single in-flight round per peer, typed errors, budget hints). The SDK also owns the language-neutral **fusion envelope** that travels inside the tunnel's end-to-end-encrypted payload, so an R source and a Python destination interoperate.

**Status: design only — no implementation yet.**

- Authoritative spec: `SafeInsights Enclave Fusion Architecture Doc-v2.md` (§4.1, §4.5, §7) in the parent workspace
- Sequence diagrams: `fusion-rc-querys.md` (two-party); `drawings/fusion/FusionWithSafeInsightsEnclave-technical-phases/phase6-analysis-rounds.md` (hub)
- Implementation plan: [`.claude/plans/2026-09-17-fusion-sdk-implementation.md`](.claude/plans/2026-09-17-fusion-sdk-implementation.md) — the local-API contract is mirrored from `fusion-tunnel-app/src/schemas/local-api.ts` (canonical); the envelope contract in `spec/` is owned here
