# fusion-sdk

The SafeInsights **Fusion SDK**: the researcher-facing **R** and **Python** libraries for the Enclave Fusion Framework, plus the language-neutral contract they share.

Researcher code never touches the transport. On the **destination** side it calls `request(peer, operation, params)` and gets the source's answer back; on the **source** side it registers Data-Partner-approved operation handlers and calls `serve()`. The SDK drives the per-job Fusion Tunnel App's enclave-local API: submit/poll, two-stage ACK, duplicate suppression, single in-flight round per peer, same-`correlationId` re-issue on round timeout, typed errors, budget hints, source-side guards, and content-free logging. It also owns the **fusion envelope** that travels inside the tunnel's end-to-end-encrypted payload, so an R source and a Python destination interoperate.

**Status:** implementation in progress against [the plan](.claude/plans/2026-09-17-fusion-sdk-implementation.md). The Tunnel App does not exist yet; everything here is proven against the in-repo fake tunnel pair.

## Layout

| Path | What |
| :-- | :-- |
| [`spec/`](spec/) | The cross-language contract: envelope JSON Schema, error table, local-API mirror, env contract, log vocabulary, golden fixtures, fake-tunnel scenarios. Both test suites load it. |
| [`python/`](python/) | `safeinsights-fusion` (PyPI). Stdlib only; `[pandas]` extra. |
| [`r/`](r/) | `sifusion`. Imports: `curl`, `jsonlite`. |
| [`tools/fake_tunnel_pair/`](tools/fake_tunnel_pair/) | Two local-API servers per leg joined by an in-memory relay, with scenario-driven fault injection. `python3 -m fake_tunnel_pair --scenario happy` |
| [`tools/validate_spec.py`](tools/validate_spec.py) | Stdlib JSON Schema validator that gates `spec/` in CI. |
| [`docker/`](docker/) | Dependency-footprint smoke on `python:3.12-slim` and `r-base`. |
| [`docs/decisions/`](docs/decisions/) | ADRs 0001–0005. [`docs/asks/`](docs/asks/) — issue drafts for the tunnel, setup-app and management-app repos. |
| `examples/` | Two-party and hub templates in both languages (Phase 6). |

## Development

```sh
uv sync --group dev                 # root workspace: python/ + tools/ with one config
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q                    # python/tests + tools/tests (starts the fake in-process)
python3 tools/validate_spec.py

cd r && Rscript -e 'roxygen2::roxygenise()' && cd .. && R CMD build r && R CMD check --as-cran sifusion_*.tar.gz
```

CI (`.github/workflows/checks.yml`) runs the spec gate, Python 3.10–3.13, R 4.1 and release, Trivy (vulnerabilities and licenses), Sonar, and the Docker smoke.

## References

- Authoritative spec: `SafeInsights Enclave Fusion Architecture Doc-v2.md` (§4.1, §4.5, §7, §12) in the parent workspace
- Sequence diagrams: `fusion-rc-querys.md` (two-party); `drawings/fusion/FusionWithSafeInsightsEnclave-technical-phases/phase6-analysis-rounds.md` (hub)
- Local-API contract: mirrored in [`spec/local-api.md`](spec/local-api.md) from `fusion-tunnel-app/src/schemas/local-api.ts` (canonical, once it exists); the envelope contract in [`spec/`](spec/) is owned here

## License

AGPL-3.0-or-later ([LICENSE](LICENSE)). Contributions are covered by the [CLA](CLA.md).
