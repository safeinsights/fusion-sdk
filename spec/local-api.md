# Local API mirror

The SDK's mirror of the Fusion Tunnel App's research-container-facing API (Architecture Doc v2 §4.1; tunnel plan Phase 2, 5, 8). **The tunnel repo's `src/schemas/local-api.ts` is canonical**; this file records what the SDK relies on and marks the proposals (asks T1–T6, `docs/asks/`) that are not yet in the tunnel plan. `tools/fake_tunnel_pair` implements exactly this document, and Phase 8 diffs it against a JSON-Schema export of the zod schemas.

## Conventions

- Base URL: one tunnel endpoint per leg, always enclave-local (`http://127.0.0.1:<port>` or a sidecar hostname). The SDK refuses non-local schemes and hosts with `ConfigError`.
- Auth: every `/v1/*` call carries `Authorization: Bearer <token>`, one token per tunnel. Missing or wrong → `401 {"code": "UNAUTHORIZED"}`.
- Bodies: `application/json; charset=utf-8` both ways. Error bodies are `{"code": string, "message"?: string, "retryable"?: boolean, "detail"?: object}`.
- Pre-channel: every `/v1/*` route except `/v1/info` returns `503 {"code": "NOT_READY", "state": <state>}` until the tunnel is `CHANNEL_UP`.
- Direction: a route the role may not call returns `403 {"code": "FORBIDDEN_DIRECTION", "role": <role>}`. This is structural at the tunnel and mirrored in the SDK's role-specific API surface.
- Terminal (**T5**): once the leg has ended, every `/v1/*` route except `/v1/info` returns `200 {"terminal": true, "code": "STUDY_COMPLETE" | "SESSION_ERRORED" | "LIMIT_EXCEEDED", "message"?: string, "detail"?: object}`. The SDK checks `terminal` on every JSON body before anything else.
- Payloads (**T4**): `payload` is a JSON value passed through untouched. The bytes the tunnel meters are the canonical UTF-8 serialization of that value.
- Long-poll hold: `GET /v1/responses/{id}` and `GET /v1/messages/next` hold up to `FUSION_LONGPOLL_MS` (tunnel default 25 000) and return `204` on an empty hold. The SDK's per-call HTTP timeout (`FUSION_POLL_HTTP_TIMEOUT_S`, default 40) must exceed the hold.

## `GET /v1/info` — both roles, available in every state

```jsonc
200 {
  "apiVersion": "1.0.0",                       // T5 — semver; the SDK requires major == 1
  "legId": "leg-a",
  "peerOrgSlug": "dp-a",
  "role": "destination" | "source",
  "direction": "dst_to_src",
  "state": "AWAITING_CONFIG" | "CONFIGURED" | "PEER_KEY_VERIFIED" | "RELAY_ATTACHED" | "CHANNEL_UP" | "CLOSING" | "CLOSED" | "ERRORED" | "LIMIT_EXCEEDED",
  "guards": { "maxDistinctPersonIds": 5000, "minGroupSize": 11 },   // T3 — optional; absent ⇒ guards disabled (startup warning)
  "caps": { "maxRounds": 500, "maxResponseBytes": 4294967296, "maxResponseBytesPerRound": 268435456,
            "maxQueryBytes": 1073741824, "maxQueryBytesPerRound": 67108864, "maxRoundsPerHour": 120 },   // T3 — optional
  "operations": [ { "name": "counts_by_group", "cardinality": "per-group" } ]                          // T3 — optional
}
```

Content-free. The SDK polls it during readiness (logging `ready.wait {state}`) and reads `guards`/`caps`/`operations` once at `serve()`.

## `POST /v1/request` — destination only

Submit a query. Returns immediately.

```jsonc
→ { "payload": <envelope>, "correlationId"?: "…" }      // T1 — correlationId only on re-issue
← 202 { "correlationId": "…" }
```

| Status | Body | Meaning |
| :-- | :-- | :-- |
| `202` | `{correlationId}` | accepted; a re-issue of the in-flight `correlationId` is idempotent and creates no second outbox entry (**T1**) |
| `403` | `FORBIDDEN_DIRECTION` | role is `source` |
| `409` | `{"code": "IN_FLIGHT_CONFLICT", "correlationId": <inFlight>}` | another round is in flight on this tunnel |
| `422` | `{"code": "SCHEMA_REJECTED", "detail"}` | body failed schema validation |
| `429` | `{"code": "BACKPRESSURE", "retryable": true}` | relay window full; retry with backoff |
| `503` | `NOT_READY` | before `CHANNEL_UP` |
| `200` | terminal body | leg ended |

A re-issue (`correlationId` present) whose id the tunnel does not know (the tunnel restarted) is accepted as a new outbox entry **with that id**; the source tunnel dedups by `correlationId` and replays its cached response if it has one.

## `GET /v1/responses/{correlationId}` — destination only, long-poll

```jsonc
← 200 { "messageId": "…", "correlationId": "…", "payload": <envelope>, "budget": <budget>, "receivedAt": "2026-09-18T12:00:00.000Z" }   // T6, T2
```

| Status | Meaning |
| :-- | :-- |
| `200` message | the correlated response; must be ACKed |
| `204` | empty hold → re-poll |
| `404 {"code": "UNKNOWN_CORRELATION"}` | the tunnel does not know this id (restart) → re-issue with the same id |
| `403` / `503` / `200` terminal | as above |

A response the tunnel has already delivered but not seen ACKed is redelivered on the next poll (same `messageId`).

## `GET /v1/messages/next` — source only, long-poll

```jsonc
← 200 { "messageId": "…", "correlationId": "…", "payload": <envelope>, "budget": <budget>, "receivedAt": "…" }
```

| Status | Meaning |
| :-- | :-- |
| `200` message | next inbound query, in FIFO order; must be ACKed |
| `204` | empty hold → re-poll |
| `403` / `503` | as above |
| `200 {"terminal": true, "code": "STUDY_COMPLETE"}` | CLOSE arrived; the loop ends cleanly |
| `200` other terminal | `SESSION_ERRORED` / `LIMIT_EXCEEDED` |

Un-ACKed deliveries are redelivered (same `messageId`). A query whose response the source tunnel has already cached is **not** redelivered to the RC; the tunnel replays the cached response (tunnel Phase 5).

## `POST /v1/messages` — source only

```jsonc
→ { "inReplyTo": "<correlationId>", "payload": <envelope> }     // T6
← 202 { "messageId": "…", "budget": <budget> }
```

| Status | Meaning |
| :-- | :-- |
| `202` | accepted and metered; a second response for the same `inReplyTo` is accepted idempotently and returns the first `messageId` |
| `400 {"code": "UNKNOWN_IN_REPLY_TO"}` | `inReplyTo` does not match a delivered query |
| `403` | role is `destination` |
| `422` / `429` / `503` | as above |
| `200 {"terminal": true, "code": "LIMIT_EXCEEDED", "detail": {"cap": "maxResponseBytesPerRound", "limit": …, "observed": …}}` | the response breached a cap; the tunnel has already told the destination and the BMA; nothing was sent |

## `POST /v1/messages/{messageId}/ack` — both roles

Stage two of the two-stage ACK. `204` on success, idempotent. `404 {"code": "UNKNOWN_MESSAGE"}` for an id the tunnel no longer holds (the SDK logs and proceeds).

## `POST /v1/complete` — destination only

```jsonc
→ {}
← 202 {}
```

Starts CLOSE for this leg: state `CLOSING` → `CLOSED`; the source's next long-poll returns `STUDY_COMPLETE`. A second call while `CLOSING`/`CLOSED` returns `202` idempotently. `403` on a source. Calling it with a round in flight returns `409 {"code": "IN_FLIGHT_CONFLICT"}`.

## Budget object (**T2**)

```jsonc
{ "roundsUsed": 3, "roundsMax": 500,
  "responseBytesUsed": 10240, "responseBytesMax": 4294967296,
  "queryBytesUsed": 2048, "queryBytesMax": 1073741824,
  "roundsPerHourUsed": 3, "roundsPerHourMax": 120 }
```

Every `*Max` is `null` when the manifest sets no cap. Present on delivered messages and on the `202` of `POST /v1/messages`. The SDK exposes the last seen value as `response.budget` and `peer.budget()`.

## Message object (**T6**)

`{ "messageId": string, "correlationId": string, "payload": JSON, "budget"?: budget, "receivedAt": RFC 3339 UTC }`. `messageId` is the audit join key (§12) and is logged; `payload` never is.

## Not on this API

Provisioning (`/local/*`) is Setup-App-facing and the SDK never calls it. There is no route to fetch the manifest, the peer key, or relay state; the SDK learns everything it needs from `/v1/info` and the env (`spec/env.md`).
