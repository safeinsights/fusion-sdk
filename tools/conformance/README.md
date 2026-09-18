# Cross-language conformance drivers

Scripted research containers used by `tools/matrix.py` (Phase 5) to prove that every
destination/source language pair interoperates through the fake tunnel pair:
{Python, R} destination × {Python, R} source, plus the two-leg hub with mixed sources.

| File | Role |
| :-- | :-- |
| `source.py`, `source.R` | register the conformance operations below and `serve()` |
| `dest.py`, `dest.R` | run the round script below against the peer(s) and assert every answer |

## Conformance operations (identical in both source languages)

| Operation | Cardinality | Returns |
| :-- | :-- | :-- |
| `counts_by_group` | per-group | table `grade:string, n:integer` with `n = [len(ids), 2*len(ids)+1]` |
| `total` | aggregate | `{"total": 42, "correlation_id": ctx.correlation_id, "peer": ctx.peer, "operation": "total"}` |
| `echo` | aggregate | the params object, unchanged |
| `table_types` | aggregate | one table row with every column type and one all-null row |
| `boom` | aggregate | raises |

## Destination round script

`CONF_MODE=two-party` (default): `CONF_ROUNDS` iterations of `counts_by_group`, `total`, `echo`,
`table_types`, then `boom` (expects `HANDLER_ERROR`) and `nope` (expects `UNKNOWN_OPERATION`), then
`complete()`. `CONF_MODE=hub`: alternate legs, derive the query to B from A's answer, expect leg B
to hit `LIMIT_EXCEEDED` (scenario `hub-two-legs` caps it at one round), `complete()` fans out.
`CONF_MODE=chaos`: `CONF_ROUNDS` rounds of `total`; `RoundTimeoutError` is tolerated.

Exit codes: `0` every assertion held · `3` a typed terminal error ended the leg (accepted in chaos) ·
`1` anything else (an SDK bug or a parity break). Output is one JSON line per event.
