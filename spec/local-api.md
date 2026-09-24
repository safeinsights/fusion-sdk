# Local API (version 2)

The research-container-facing API of the Fusion Tunnel App, as the SDK uses it. **The tunnel repo's `src/local-api.ts` is canonical**; this file records what the SDK relies on. `tools/fake_tunnel_pair` implements exactly this document.

## Conventions

- Base URL: one tunnel endpoint per leg, always enclave-local (`http://127.0.0.1:<port>` or a sidecar hostname). The SDK refuses non-local schemes and hosts with `ConfigError`.
- Auth: every `/v1/*` call carries `Authorization: Bearer <token>`, one token per tunnel.
- Bodies: JSON both ways. `apiVersion` is `2.0.0`; the SDK requires major `2` and raises `ConfigError` for anything else before the first round.
- Errors: one flat shape on every error status, `{"code": string, "message": string, "correlationId"?: string, "issues"?: [{"path", "message"}]}`.

| Status | `code` | Meaning |
| :-- | :-- | :-- |
| `400` | `VALIDATION` | body failed validation (`issues` says where) |
| `401` | `UNAUTHORIZED` | missing or wrong bearer token |
| `403` | `FORBIDDEN` | the route is not for this tunnel's role |
| `404` | `NOT_FOUND` | unknown route, or unknown `correlationId` on the responses poll |
| `409` | `CONFLICT` | another round is in flight (`correlationId` names it), or `inReplyTo` is not a delivered query |
| `413` | `TOO_LARGE` | the body exceeds the tunnel's size limit |
| `429` | `BACKPRESSURE` | relay window full; retryable |
| `503` | `NOT_READY` | before `CHANNEL_UP` |
| `500` | `INTERNAL` | tunnel fault; retryable |

- Terminal: once the leg has ended, **every** `/v1/*` route except `/v1/info` answers `200 {"terminal": true, "code": "STUDY_COMPLETE" | "SESSION_ERRORED" | "LIMIT_EXCEEDED", "message"?: string, "detail"?: object}`. The SDK checks `terminal` on every JSON body before anything else. For `LIMIT_EXCEEDED`, `detail` is `{"cap": <cap name>, "limit": n, "observed": n}` with the manifest cap names below.
- Payloads: `payload` is a JSON value passed through untouched. The bytes the tunnel meters are the canonical UTF-8 serialization of that value.
- Long-poll hold: `GET /v1/responses/{id}` and `GET /v1/messages/next` hold up to the tunnel's `FUSION_LONGPOLL_MS` (default 25 000) and return `204` on an empty hold. The SDK's per-call HTTP timeout (`FUSION_POLL_HTTP_TIMEOUT_S`, default 40) must exceed the hold.
- Delivery is the acknowledgement: a `200` message on either long-poll route completes that hop. There is no ack route.

## `GET /v1/info` — both roles, every state

```jsonc
200 {
  "apiVersion": "2.0.0",
  "studyId": "…", "jobId": "…", "legId": "leg-a",
  "orgSlug": "hub", "peerOrgSlug": "dp-a",
  "role": "destination" | "source",
  "direction": "dst_to_src",
  "state": "AWAITING_CONFIG" | "CONFIGURED" | "PEER_KEY_VERIFIED" | "RELAY_ATTACHED" | "CHANNEL_UP" | "CLOSING" | "CLOSED" | "ERRORED" | "LIMIT_EXCEEDED",
  "caps": { "maxRounds": 500, "maxRoundsPerHour": 120, "maxResponsePlaintextBytesPerRound": 268435456,
            "maxCumulativeResponsePlaintextBytes": 4294967296, "maxQueryPlaintextBytesPerRound": 67108864,
            "maxCumulativeQueryPlaintextBytes": 1073741824 },          // absent key = unlimited
  "guards": { "maxDistinctPersonIds": 5000, "minGroupSize": 11 },     // optional; absent ⇒ guards disabled (startup warning)
  "operations": [ { "name": "counts_by_group", "cardinality": "per-group" } ]   // optional
}
```

Content-free. The SDK polls it during readiness (logging `ready.wait {state}`) until `CHANNEL_UP`; `CLOSING`/`CLOSED` map to `STUDY_COMPLETE`, `ERRORED` to `SESSION_ERRORED`, `LIMIT_EXCEEDED` to itself. `guards` and `operations` are read once at `serve()`; `caps` is passed through.

## `POST /v1/request` — destination

```jsonc
→ { "payload": <envelope>, "correlationId"?: "…" }      // correlationId only on a re-issue
← 202 { "correlationId": "…", "reissued": false }
```

`409 CONFLICT {correlationId: <inFlight>}` while another round is in flight; `413`, `429`, `503` as above. A re-issue (same `correlationId`) of the in-flight round is idempotent (`reissued: true`); one whose id the tunnel does not know (the tunnel restarted) is accepted as a new entry **with that id**, and the source tunnel dedups by `correlationId`, replaying its cached response if it has one.

## `DELETE /v1/request/{correlationId}` — destination

Abandon a round the SDK has given up on (`RoundTimeoutError` after the last re-issue). `204`, idempotent, also for an unknown id. A late response for that id is discarded. The SDK calls it once, best effort, and ignores failure.

## `GET /v1/responses/{correlationId}` — destination, long-poll

`200` message (the round is complete) · `204` empty hold → re-poll · `404 NOT_FOUND` the tunnel does not know this id (restart) → re-issue with the same `correlationId`.

## `GET /v1/messages/next` — source, long-poll

`200` message (the next inbound query, FIFO) · `204` empty hold → re-poll. An unanswered query is delivered again on a later poll (a restarted source RC sees it again); the SDK memo dedups by `correlationId` and replays its answer without re-running the handler. `STUDY_COMPLETE` arrives here as a terminal body and ends the loop cleanly.

## `POST /v1/messages` — source

```jsonc
→ { "inReplyTo": "<correlationId>", "payload": <envelope> }
← 202 { "messageId": "…", "replayed": false, "budget"?: <budget> }
```

`202` accepted and metered; a second response for the same `inReplyTo` returns the first `messageId` with `replayed: true`. `409 CONFLICT` when `inReplyTo` is not a delivered query (the SDK logs `round.protocol_error`, drops the response and returns to the loop). `413`, `429` as above. A cap breach answers `200` terminal `LIMIT_EXCEEDED` with `detail.cap` = `maxResponsePlaintextBytesPerRound` or `maxCumulativeResponsePlaintextBytes`; nothing was sent.

## `POST /v1/complete` — destination

`→ {}` · `← 202 {"state": "CLOSING" | "CLOSED"}`. Starts CLOSE for this leg; the source's next long-poll returns `STUDY_COMPLETE`. From then on every route answers the terminal body.

## Message object

`{ "messageId": string, "correlationId": string, "payload": JSON, "budget"?: budget, "receivedAt": RFC 3339 UTC }`. Ids are UUIDs. `messageId` is the audit join key and is logged; `payload` never is.

## Budget object

```jsonc
{ "roundsUsed": 3, "roundsMax"?: 500,
  "responseBytesUsed": 10240, "responseBytesMax"?: 4294967296,
  "queryBytesUsed": 2048, "queryBytesMax"?: 1073741824,
  "roundsPerHourUsed"?: 3, "roundsPerHourMax"?: 120 }
```

A `*Max` is absent when the manifest sets no cap. Present on delivered messages and on the `202` of `POST /v1/messages`. The SDK exposes the last seen value as `response.budget` and `peer.budget()`, and logs `budget.near_limit` at 90 %.

## Not on this API

Provisioning (`/local/*`) is Setup-App-facing and the SDK never calls it. There is no route to fetch the manifest, the peer key, or relay state; the SDK learns everything it needs from `/v1/info` and the env (`spec/env.md`).
