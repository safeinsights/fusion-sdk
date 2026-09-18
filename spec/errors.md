# Errors

Two vocabularies, deliberately separate:

- **Remote error codes** travel inside the envelope (`status: "error"`, `error.code`). They mean *the round completed and the source declined to answer*. The destination raises them as `RemoteError` and the peer stays usable.
- **Local error classes** are what the SDK raises to researcher code. Both languages expose the same set with the same names (Python class / R condition class).

`spec/errors.json` is the machine-readable twin of this file; both test suites assert against it.

## Remote error codes (in the envelope)

| Code | Raised by the source when | `detail` |
| :-- | :-- | :-- |
| `UNKNOWN_OPERATION` | no handler is registered under `operation` | `{ "operation" }` |
| `BAD_PARAMS` | the query envelope fails to decode, or `params` is not an object | `{ "reason" }` |
| `HANDLER_ERROR` | the handler raised | `{ "type" }` — the exception class name; `message` carries the traceback unless the operation was registered with `sanitize` (ADR 0005) |
| `GUARD_REFUSED` | a guard refused the query or the result (ADR 0002) | `{ "guard", "limit", "observed"? }` — `observed` is omitted for `minGroupSize` so the small cell's size does not cross |

The list is closed for envelope `v: 1`. A destination that receives an unknown code raises `ProtocolError`.

## Terminal codes (on the local API)

Delivered by the tunnel on any `/v1/*` route (except `/v1/info`) as `200 {"terminal": true, "code": …}` once the leg has ended (ask T5).

| Code | Meaning | Destination `request()` | Source `serve()` |
| :-- | :-- | :-- | :-- |
| `STUDY_COMPLETE` | CLOSE completed | n/a (the destination initiated it) | returns normally |
| `SESSION_ERRORED` | dead-letter, un-ACKed expiry, relay closed the session, tunnel `ERRORED` | raises `SessionError` | raises `SessionError` |
| `LIMIT_EXCEEDED` | a source-approved cap was breached on this leg | raises `LimitExceededError` | raises `LimitExceededError` |

After a terminal code every further call on that **peer** raises the same error immediately, without touching the tunnel. Other peers are unaffected; `complete()` still fans out to them.

## Local error classes

| Python | R condition class | Terminal for the peer | When |
| :-- | :-- | :-- | :-- |
| `FusionError` | `fusion_error` | — | base; never raised directly |
| `ConfigError` | `fusion_config_error` | yes (never connected) | env missing or malformed; endpoint not `http://127.0.0.1`/`localhost`/a private hostname on the enclave network; incompatible `apiVersion`; `401`; `403` (wrong role — a programming error, never retried) |
| `NotReadyError` | `fusion_not_ready_error` | yes | readiness timeout waiting for `CHANNEL_UP` |
| `ConcurrencyError` | `fusion_concurrency_error` | no | second in-flight request on a peer; `409` from the tunnel |
| `RoundTimeoutError` | `fusion_round_timeout_error` | no | round timeout after `FUSION_ROUND_MAX_REISSUES` re-issues; the round is abandoned, the peer stays usable |
| `RemoteError` (`code`, `message`, `detail`) | `fusion_remote_error` | no | the source returned an error envelope |
| `TerminalError` | `fusion_terminal_error` | yes | base of the two below; never raised directly |
| `LimitExceededError` | `fusion_limit_exceeded_error` | **yes** | tunnel reported `LIMIT_EXCEEDED` |
| `SessionError` (`code`) | `fusion_session_error` | **yes** | tunnel reported `SESSION_ERRORED` |
| `ProtocolError` | `fusion_protocol_error` | no | undecodable local-API response or envelope (the message is still ACKed so it is not redelivered); `422` |

R condition objects carry the same fields as the Python classes (`code`, `message`, `detail`, `peer`) and inherit from `error` and `condition`, so `tryCatch(fusion_remote_error = function(e) …)` works.

Every error message is **content-free**: it may name the peer, the operation, a `correlationId`, sizes and durations, never `params` or a body. The exception is `RemoteError.message` for `HANDLER_ERROR`, which by default carries the source handler's traceback (ADR 0005) — the source decides what crosses, and output review gates what leaves the destination.

## HTTP status → behavior (both roles)

| Status / condition | Behavior |
| :-- | :-- |
| `200` with `terminal: true` | per the terminal table |
| `200` / `202` | proceed |
| `204` on a long-poll route | empty hold → re-poll |
| `204` on ack | proceed |
| `401` | `ConfigError` (bad bearer token) — never retried |
| `403` | direction violation → `ConfigError` (wrong role) — never retried |
| `404` on `GET /v1/responses/{id}` | the destination tunnel restarted and lost the correlation → re-issue with the **same** `correlationId` (ask T1) |
| `404` on ack | log `ack.unknown`, proceed |
| `409` | `ConcurrencyError` |
| `422` | `ProtocolError` (SDK/tunnel contract drift) |
| `429` `BACKPRESSURE` | backoff + retry within the round timeout |
| `503` not ready | readiness wait before a round; backoff + retry during one |
| `5xx`, connection refused/reset, timeout on a non-long-poll call | backoff + retry, bounded by the round timeout |

Retryable conditions never reach researcher code unless the round timeout is exhausted, in which case they surface as `RoundTimeoutError`.
