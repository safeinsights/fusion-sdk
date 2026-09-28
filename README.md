# fusion-sdk

The SafeInsights **Fusion SDK**: the researcher-facing **R** and **Python** libraries for the Enclave Fusion Framework, plus the language-neutral contract they share.

Researcher code never touches the transport. On the **destination** side it calls `request(peer, operation, params)` and gets the source's answer back; on the **source** side it registers Data-Partner-approved operation handlers and calls `serve()`. The SDK drives the per-job Fusion Tunnel App's enclave-local API (version 2): submit/poll, duplicate suppression, single in-flight round per peer, same-`correlationId` re-issue on round timeout, typed errors, budget hints, source-side guards, and content-free logging. It also owns the **fusion envelope** that travels inside the tunnel's end-to-end-encrypted payload, so an R source and a Python destination interoperate.

**Status:** 0.2.0 implements local API 2 ([`spec/local-api.md`](spec/local-api.md), mirrored from `fusion-tunnel-app/src/local-api.ts`) and is proven against the in-repo fake tunnel pair; the real-tunnel switch (`FUSION_TEST_ANNOUNCE`) is ready for the tunnel's harness. Package names are working names until decision A2 closes.

## Layout

| Path | What |
| :-- | :-- |
| [`spec/`](spec/) | The cross-language contract: envelope JSON Schema, error table, local API 2 mirror, env contract, log vocabulary, golden fixtures, fake-tunnel scenarios. Both test suites load it. |
| [`python/`](python/) | `safeinsights-fusion` (PyPI). Stdlib only; `[pandas]` extra. |
| [`r/`](r/) | `sifusion`. Imports: `curl`, `jsonlite`. |
| [`tools/fake_tunnel_pair/`](tools/fake_tunnel_pair/) | Two local API 2 servers per leg joined by an in-memory relay, with scenario-driven fault injection. `python3 -m fake_tunnel_pair --scenario happy` |
| [`tools/validate_spec.py`](tools/validate_spec.py) | Stdlib JSON Schema validator that gates `spec/` in CI. |
| [`docker/`](docker/) | Dependency-footprint smoke on `python:3.12-slim` and `r-base`. |
| [`docs/decisions/`](docs/decisions/) | ADRs 0001–0005. [`docs/asks/`](docs/asks/) — issue drafts for the setup-app and management-app repos; [`docs/compatibility.md`](docs/compatibility.md) — SDK ↔ tunnel `apiVersion` matrix. |
| [`examples/`](examples/) | Two-party and hub starter templates in both languages, runnable under the simulator (`simulate_*.{py,R}`) and against the fake pair (`tools/run_examples.py`). |
| [`tools/matrix.py`](tools/matrix.py), [`tools/chaos.py`](tools/chaos.py) | Cross-language matrix ({Py,R} destination x source, mixed-source hub, fixture parity) and the nightly random fault injection, driving `tools/conformance/`. |
| [`docs/`](docs/) | [Researcher guide](docs/researcher-guide.md), [writing idempotent operations](docs/idempotent-operations.md), [the content-free logging contract](docs/logging-contract.md). |

## Development

```sh
uv sync --group dev                 # root workspace: python/ + tools/ with one config
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q                    # python/tests + tools/tests (starts the fake in-process)
python3 tools/validate_spec.py

cd r && Rscript -e 'roxygen2::roxygenise()' && cd .. && R CMD build r && R CMD check --as-cran sifusion_*.tar.gz
```

CI: `checks.yml` runs the spec gate, Python 3.10–3.13, R 4.1 and release, Trivy (vulnerabilities and licenses), Semgrep SAST, and the Docker smoke; `matrix.yml` runs the cross-language matrix, fixture parity and the examples; `nightly.yml` runs random fault injection.

```sh
python3 tools/matrix.py --r-src r          # {py,r} x {py,r} + mixed-source hub through the fake
python3 tools/matrix.py --fixtures --r-src r
python3 tools/run_examples.py --r-src r
python3 tools/chaos.py --r-src r --seeds 3
```

## References

- Authoritative spec: `SafeInsights Enclave Fusion Architecture Doc-v2.md` (§4.1, §4.5, §7, §12) in the parent workspace
- Sequence diagrams: `fusion-rc-querys.md` (two-party); `drawings/fusion/FusionWithSafeInsightsEnclave-technical-phases/phase6-analysis-rounds.md` (hub)
- Local-API contract: mirrored in [`spec/local-api.md`](spec/local-api.md) from `fusion-tunnel-app/src/local-api.ts` (canonical); the envelope contract in [`spec/`](spec/) is owned here

## License

AGPL-3.0-or-later ([LICENSE](LICENSE)). Contributions are covered by the [CLA](CLA.md).
