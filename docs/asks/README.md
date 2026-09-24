# Cross-repo asks

Issue drafts for the owning repositories (plan §4). Each file is ready to paste as a GitHub issue. Status is tracked here.

| Ask | Owner repo | File | Status |
| :-- | :-- | :-- | :-- |
| S1 | setup-app | `S1-env-contract.md` | draft |
| S2 | setup-app | `S2-base-image-cadence.md` | draft |
| B1 | management-app | `B1-starter-code-and-simulator.md` | draft |

The tunnel asks T1–T7 (same-`correlationId` re-issue, budget hints, guards/caps/operations on `/v1/info`,
JSON payloads, terminal bodies and `apiVersion`, message shape, abandon) were delivered in local API 2
(`fusion-tunnel-app/src/local-api.ts`); `spec/local-api.md` is the SDK's mirror of it and
`tools/fake_tunnel_pair` its executable form.

## Handoff to fusion-tunnel-app

`tools/conformance/{dest,source}.{py,R}` are ready to serve as the research-container drivers of the
tunnel's own harness: point `tools/matrix.py --announce <file>` at real tunnels described in the fake
pair's announce format, or set `FUSION_TEST_ANNOUNCE` for the SDK test suites
(`python/tests/test_real_tunnel.py`, `r/tests/testthat/test-real-tunnel.R`).
