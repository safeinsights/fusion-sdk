# Examples

Researcher-facing starter code for a fusion study, in both languages. Each file is a complete
research-container script *and* importable/sourceable into the simulator, so the same code runs
in the SafeInsights IDE with no tunnel (`simulate_*.py` / `simulate_*.R`) and unchanged inside an
enclave (the Setup App injects the `FUSION_*` environment, see `spec/env.md`).

| Directory | Files | Study shape |
| :-- | :-- | :-- |
| `two-party/` | `source.{py,R}`, `destination.{py,R}` | one Data Partner source, one destination |
| `hub/` | `destination.{py,R}`, `source-a.{R,py}`, `source-b.{py,R}` | SafeInsights-hosted destination with two Data Partner sources; the query to B is derived from A's answer |

Rules the examples follow (and your study code should too):

- **Source handlers are idempotent per `correlationId`** (`docs/idempotent-operations.md`): the SDK may
  hand a handler the same query twice after a restart; here every handler is a pure function of its params.
- **Declare guards at registration**: `person_id_param` names the Person-ID list so `maxDistinctPersonIds`
  can be enforced (the list must be a flat array of scalars; other shapes are refused before the
  handler runs); `cardinality="per-group"` enables `minGroupSize` on the result table.
- **Nothing but the SDK talks to the network.** Result release stays with your Data Partner package
  (for example `osenclave::toa_results_upload()`), outside these files.
- **Logs are content-free** (`docs/logging-contract.md`): print summaries, never row values.

Run under the simulator:

```sh
python3 examples/simulate_two_party.py        # or: Rscript examples/simulate_two_party.R
python3 examples/simulate_hub.py              # or: Rscript examples/simulate_hub.R
```

Run against the fake tunnel pair (what CI does): `python3 tools/run_examples.py --r-src r`.
