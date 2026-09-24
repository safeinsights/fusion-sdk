# Compatibility

Two independently rebuilt images may pair an older SDK on one side with a newer one on the other,
and either with a newer tunnel (plan Risk 8). Three versioned surfaces make that safe:

| Surface | Checked where | Rule |
| :-- | :-- | :-- |
| Envelope `v` | every decode | decoders accept `v: 1`; a new envelope major is a new SDK major |
| Fusion table type vocabulary | every table decode | additive only within a major; unknown types are rejected |
| Tunnel local API `apiVersion` | `connect()` / `serve()` | the SDK requires major `2`; anything else is `ConfigError` before the first round |

## SDK release vs tunnel apiVersion

| SDK (Python / R) | Tunnel `apiVersion` | Envelope | Notes |
| :-- | :-- | :-- | :-- |
| 0.2.x | 2.x | 1 | Local API 2 (`spec/local-api.md`): asks T1–T6 adopted (same-`correlationId` re-issue, budget on the local API, guards/caps/operations on `/v1/info`, JSON payloads, terminal bodies and `apiVersion`, message shape), T7 adopted (`DELETE /v1/request/{id}`), ack route removed (delivery is the acknowledgement), one flat error body |
| 0.1.x | 1.x (fake tunnel pair only) | 1 | pre-release; two-stage ACK, drain-on-409 fallback |

The cross-language matrix (`tools/matrix.py`) is the executable form of the Python ↔ R row; once the
first release exists, CI also runs the previous minor of each package against the current one.
