# T4 — `payload` on the local API is a JSON value, not a base64 string

**Repo:** fusion-tunnel-app · **Plan section:** `schemas/local-api.ts` · **Requested by:** fusion-sdk (ADR 0001)

**Ask.** Define `payload` on `POST /v1/request`, `GET /v1/responses/{id}`, `GET /v1/messages/next` and `POST /v1/messages` as a JSON value passed through untouched. The bytes the tunnel encrypts and meters are the canonical UTF-8 serialization of that value.

**Why.** No double encoding, simplest for `jsonlite` and `json`, and the tunnel never interprets it anyway. If binary is ever needed, the SDK's `encoding: "arrow"` base64s **inside** the envelope.
