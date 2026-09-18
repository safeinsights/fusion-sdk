# Changelog

All notable changes to the SafeInsights Fusion SDK (Python `safeinsights-fusion`, R `sifusion`).
The two packages are released together and share a version. Compatibility with the Fusion Tunnel
App's local API is tracked in `docs/compatibility.md`.

## Unreleased

### Added

- Python and R packages with identical semantics over the tunnel's local API: destination
  `connect()` / peer handles / `request()` / `complete()`, source registry / `serve()`, typed errors,
  budget hints, source-side guards, content-free logging, `doctor`.
- The fusion envelope and typed-columnar fusion table (`spec/`), with 71 golden fixtures both
  languages must agree on.
- In-process simulator for the IDE (`simulate()` / `fusion_simulate()`), examples for the two-party
  and hub study shapes, cross-language conformance matrix and nightly chaos.
- Vendoring drops (`python/vendor.sh`, `r/vendor.R`) for base images that do not ship the SDK.

### Contract asks pending on the tunnel (spec/local-api.md, docs/asks/)

T1 same-`correlationId` re-issue · T2 budget on the local API · T3 guards/caps/operations on
`/v1/info` · T4 JSON payloads · T5 terminal bodies and `apiVersion` · T6 message shape · T7 abandon.
