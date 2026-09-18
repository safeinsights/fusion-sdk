# Log events

Architecture Doc v2 §12: logging is **event-level, never content-level**. Both SDKs emit the same vocabulary with the same field names so a reviewer reads one contract. Python logs through the stdlib logger `safeinsights_fusion`; R through `message()` with `FUSION_LOG_LEVEL` filtering. Every record is one line: `fusion <event> key=value …`.

## Forbidden fields

`params`, `body`, `payload`, any table cell, any error message that could echo a value (the source logs its own tracebacks locally; the destination logs only `RemoteError.code`). The test suites include a **content-free audit**: marker strings placed in params and bodies must never appear in captured logs.

## Allowed fields

`peer`, `legId`, `role`, `operation`, `correlationId`, `messageId`, `bytes`, `durationMs`, `attempt`, `reissue`, `state`, `code`, `guard`, `limit`, `observed` (distinct-ID count only), `roundsUsed`, `roundsMax`, `responseBytesUsed`, `responseBytesMax`, `queryBytesUsed`, `queryBytesMax`, `endpointCount`, `apiVersion`, `cap`, `label`, `level`.

## Vocabulary

| Event | Level | Side | Fields |
| :-- | :-- | :-- | :-- |
| `connect.start` | INFO | dst | `endpointCount` |
| `ready.wait` | INFO | both | `peer`, `state`, `attempt` |
| `ready.ok` | INFO | both | `peer`, `legId`, `role`, `durationMs` |
| `ready.timeout` | ERROR | both | `peer`, `state`, `durationMs` |
| `guards.loaded` | INFO | src | `peer`, `guard`×n as `maxDistinctPersonIds=`, `minGroupSize=` |
| `guards.disabled` | WARNING | src | `peer` |
| `round.start` | INFO | dst | `peer`, `operation`, `correlationId`?, `bytes` |
| `round.reissue` | WARNING | dst | `peer`, `correlationId`, `reissue`, `code` (`TIMEOUT` \| `UNKNOWN_CORRELATION`) |
| `round.backpressure` | WARNING | both | `peer`, `attempt` |
| `round.retry` | DEBUG | both | `peer`, `attempt`, `code` |
| `round.complete` | INFO | dst | `peer`, `operation`, `correlationId`, `messageId`, `bytes`, `durationMs`, `roundsUsed`, `roundsMax` |
| `round.remote_error` | WARNING | dst | `peer`, `operation`, `correlationId`, `code` |
| `round.timeout` | ERROR | dst | `peer`, `operation`, `correlationId`, `durationMs` |
| `round.protocol_error` | ERROR | both | `peer`, `correlationId`?, `messageId`? |
| `serve.start` | INFO | src | `peer`, `operation`×n as `operations=` (names only) |
| `serve.received` | INFO | src | `peer`, `correlationId`, `messageId`, `bytes` |
| `serve.memo_replay` | INFO | src | `peer`, `correlationId` |
| `serve.unknown_operation` | WARNING | src | `peer`, `correlationId`, `operation` |
| `serve.bad_params` | WARNING | src | `peer`, `correlationId` |
| `serve.guard_refused` | WARNING | src | `peer`, `correlationId`, `operation`, `guard`, `limit`, `observed`? |
| `serve.handler_error` | ERROR | src | `peer`, `correlationId`, `operation`, `durationMs` (traceback goes to the local log at DEBUG, never to this line) |
| `round.served` | INFO | src | `peer`, `correlationId`, `operation`, `bytes`, `durationMs`, `roundsUsed`, `roundsMax` |
| `envelope.large` | WARNING | both | `peer`, `bytes`, `limit` |
| `ack.unknown` | WARNING | both | `peer`, `messageId` |
| `ack.failed` | WARNING | both | `peer`, `messageId` | the ACK could not be delivered after bounded retries; the message may be redelivered |
| `budget.near_limit` | WARNING | both | `peer`, one `*Used`/`*Max` pair | emitted when any counter passes 90 % |
| `session.complete` | INFO | src | `peer`, `roundsUsed` |
| `session.terminal` | ERROR | both | `peer`, `code` |
| `complete.start` | INFO | dst | `endpointCount` |
| `complete.leg` | INFO | dst | `peer`, `code` (`OK` \| terminal code \| `ERROR`) |
| `doctor.*` | INFO | both | `peer`, `state`, `apiVersion` |
