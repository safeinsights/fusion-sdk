# Changelog

All notable changes to the SafeInsights Fusion SDK (Python `safeinsights-fusion`, R `sifusion`).
The two packages are released together and share a version. Compatibility with the Fusion Tunnel
App's local API is tracked in `docs/compatibility.md`.

## 0.2.0

### Changed

- Local API 2 (`spec/local-api.md`, mirrored from `fusion-tunnel-app/src/local-api.ts`): the SDK
  requires `apiVersion` major 2. Delivery is the acknowledgement: the ack route, `Transport.ack`, the
  `ack.*` log events and the "ACK first" rule are gone. `DELETE /v1/request/{id}` is real, so the
  drain-on-409 fallback is gone and abandon is one best-effort call. Error bodies are one flat shape;
  `413 TOO_LARGE` maps to `ProtocolError`; a `409` on `POST /v1/messages` (the tunnel no longer knows
  the round) is logged and dropped instead of crashing `serve()`. `CLOSING` counts as `STUDY_COMPLETE`
  during the readiness wait. `LimitExceededError.cap` carries the manifest cap name the tunnel sends
  (`maxRounds`, `maxRoundsPerHour`, `maxResponsePlaintextBytesPerRound`,
  `maxCumulativeResponsePlaintextBytes`, `maxQueryPlaintextBytesPerRound`,
  `maxCumulativeQueryPlaintextBytes`).
- `budget.near_limit` logs fixed `cap`, `limit`, `observed` fields; the allowed log fields are exactly
  `spec/log-events.md` (checked by a test in each language).
- Internals: one retry loop (`retry_until`) in both roles, the destination round is a plain loop,
  the HTTP client lives in `_tunnel.py` / `transport.R`.
- Fake tunnel pair speaks local API 2; scenarios drop `maxDeliveries`/`redeliveryMs`/`dead-letter`,
  rename `redeliverAfterAckRounds` to `redeliverRounds` and use the manifest cap names.

### Fixed

- R: dates and datetimes before year 1000 encode with a zero-padded year (`0001-01-01`), as Python does.

## 0.1.0

### Added

- Python and R packages with identical semantics over the tunnel's local API: destination
  `connect()` / peer handles / `request()` / `complete()`, source registry / `serve()`, typed errors,
  budget hints, source-side guards, content-free logging, `doctor`.
- The fusion envelope and typed-columnar fusion table (`spec/`), with 71 golden fixtures both
  languages must agree on.
- In-process simulator for the IDE (`simulate()` / `fusion_simulate()`), examples for the two-party
  and hub study shapes, cross-language conformance matrix and nightly chaos.
- Vendoring drops (`python/vendor.sh`, `r/vendor.R`) for base images that do not ship the SDK.

### Security

- `maxDistinctPersonIds` refuses a Person-ID parameter that is an object, a nested array or an array
  with non-scalar members instead of counting it as one value; handlers that flatten such shapes
  could otherwise be handed an unbounded number of ids in one round.

### Contract

- Local API 1 with asks T1–T7 (`docs/asks/`) implemented by the fake tunnel pair; two-stage ACK and a
  drain-on-409 fallback for tunnels without T7.
