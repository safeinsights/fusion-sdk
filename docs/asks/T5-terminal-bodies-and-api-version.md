# T5 — Terminal states as `200 {terminal: true, code}`; `apiVersion` on `/v1/info`

**Repo:** fusion-tunnel-app · **Plan section:** Phase 8 terminal errors · **Requested by:** fusion-sdk

**Ask 1.** Once a leg has ended, every `/v1/*` route except `/v1/info` returns

```json
200 { "terminal": true, "code": "STUDY_COMPLETE" | "SESSION_ERRORED" | "LIMIT_EXCEEDED", "message": "…", "detail": { } }
```

rather than an HTTP error, so the SDK distinguishes "the study ended" from "the tunnel is broken" and never retries a terminal state. `LIMIT_EXCEEDED` at `POST /v1/messages` carries `detail: {cap, limit, observed}`.

**Ask 2.** `GET /v1/info.apiVersion` (semver). The SDK refuses a tunnel whose major differs with `ConfigError` at `connect()` / `serve()` instead of failing mid-study.
