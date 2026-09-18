# Environment contract

Shared with `setup-app` (ask S1). The Setup App injects these into the research container at launch; the SDK reads them at `connect()` / `serve()` and nowhere else. Tokens travel as ECS `secrets` (or the backend's equivalent), never plaintext `environment`.

## Required

| Variable | Shape | Notes |
| :-- | :-- | :-- |
| `FUSION_ROLE` | `source` \| `destination` | Lets the SDK fail fast with `ConfigError` on misuse before the tunnel's `403`. |
| `FUSION_TUNNEL_ENDPOINT` | `http://127.0.0.1:8471` | Single-leg shorthand. Mutually exclusive with `FUSION_TUNNEL_ENDPOINTS`. |
| `FUSION_TUNNEL_TOKEN` | opaque string | Bearer token for that tunnel. Required with `FUSION_TUNNEL_ENDPOINT`. |
| `FUSION_TUNNEL_ENDPOINTS` | JSON object `{ "<label>": "<url>" }` | N legs. Labels are the Setup App's leg labels; the SDK re-keys peers by `peerOrgSlug` from `GET /v1/info`, so labels are never seen by researcher code. |
| `FUSION_TUNNEL_TOKENS` | JSON object `{ "<label>": "<token>" }` | Same keys as `FUSION_TUNNEL_ENDPOINTS`; a mismatch is `ConfigError`. |

Endpoints must be `http://` (the tunnel is a sidecar on the enclave network; TLS is the tunnel's job outward) and must resolve to a loopback address, a private (RFC 1918 / link-local / ULA) address, or a bare hostname with no dots (a compose/ECS service name). Anything else is `ConfigError`.

## Tuning (all optional; defaults are `// PROVISIONAL — v2 §15.6`)

| Variable | Default | Meaning |
| :-- | :-- | :-- |
| `FUSION_READY_TIMEOUT_S` | `900` | how long `connect()` / `serve()` wait for `CHANNEL_UP` (= relay `UNPAIRED_SESSION_TIMEOUT_S`) |
| `FUSION_READY_POLL_S` | `2` | first readiness poll interval; doubles to a 15 s ceiling |
| `FUSION_POLL_HTTP_TIMEOUT_S` | `40` | per-call HTTP timeout on the two long-poll routes; must exceed the tunnel hold (25 s) |
| `FUSION_HTTP_TIMEOUT_S` | `10` | per-call HTTP timeout on every other route |
| `FUSION_ROUND_TIMEOUT_S` | `600` | round timeout; `request(..., timeout=)` overrides per call |
| `FUSION_ROUND_MAX_REISSUES` | `3` | same-`correlationId` re-issues before `RoundTimeoutError` |
| `FUSION_RETRY_BASE_MS` | `250` | first backoff for `429` / `503` / `5xx` / connection errors |
| `FUSION_RETRY_MAX_MS` | `10000` | backoff ceiling; full jitter |
| `FUSION_MEMO_MAX_ENTRIES` | `256` | source idempotency memo (LRU of `correlationId → response`) |
| `FUSION_WARN_BYTES` | `8388608` | log `envelope.large` above this many serialized bytes |
| `FUSION_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |

## Not read by the SDK

`TRUSTED_OUTPUT_ENDPOINT`, `TRUSTED_OUTPUT_BASIC_AUTH` (result release stays with the Data Partner package, ADR 0003 / A12); anything about the relay or the BMA (the SDK never sees them).

## Example — hub destination, two legs

```sh
FUSION_ROLE=destination
FUSION_TUNNEL_ENDPOINTS='{"a":"http://tunnel-a:8471","b":"http://tunnel-b:8471"}'
FUSION_TUNNEL_TOKENS='{"a":"…","b":"…"}'
```

## Example — two-party source

```sh
FUSION_ROLE=source
FUSION_TUNNEL_ENDPOINT=http://127.0.0.1:8471
FUSION_TUNNEL_TOKEN=…
```
