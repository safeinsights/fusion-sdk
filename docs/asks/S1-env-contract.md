# S1 — Env contract for the fusion SDK

**Repo:** setup-app · **Plan section:** fusion launch (tunnel plan Phase 9, hub memo §3) · **Requested by:** fusion-sdk (`spec/env.md`)

**Ask.** Inject into the research container at launch:

| Variable | Value |
| :-- | :-- |
| `FUSION_ROLE` | `source` or `destination` |
| `FUSION_TUNNEL_ENDPOINT` + `FUSION_TUNNEL_TOKEN` | one leg |
| `FUSION_TUNNEL_ENDPOINTS` + `FUSION_TUNNEL_TOKENS` | JSON maps keyed by the same leg labels, N legs (hub) |

Tokens via `secrets` (Secrets Manager on ECS), never plaintext `environment` (memo §5.2). The SDK re-keys peers by `peerOrgSlug` from each tunnel's `GET /v1/info`, so the labels are free-form. Full contract with tuning knobs: `spec/env.md`.
