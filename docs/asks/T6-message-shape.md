# T6 — Delivered message shape and `POST /v1/messages` body

**Repo:** fusion-tunnel-app · **Plan section:** `schemas/local-api.ts` · **Requested by:** fusion-sdk

**Ask.** Delivered query/response objects on both long-poll routes:

```json
{ "messageId": "…", "correlationId": "…", "payload": {}, "budget": {}, "receivedAt": "2026-09-18T12:00:00.000Z" }
```

`POST /v1/messages` body: `{ "inReplyTo": "<correlationId>", "payload": {} }` → `202 { "messageId": "…", "budget": {} }`; a second response for the same `inReplyTo` returns the first `messageId` idempotently. `POST /v1/messages/{messageId}/ack` → `204`, idempotent, `404 UNKNOWN_MESSAGE` for an unknown id.

`messageId` is the §12 audit join key; the SDK logs it and never logs `payload`.
