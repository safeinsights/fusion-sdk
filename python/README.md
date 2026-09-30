# safeinsights-fusion

The SafeInsights **Fusion SDK** for Python: the researcher-facing client for the Enclave Fusion Framework.

- **Destination side:** `Fusion.connect()` discovers the per-job Fusion Tunnel App(s) from the environment and exposes one peer handle per leg; `peer.request(operation, params)` is the blocking round facade.
- **Source side:** register Data-Partner-approved operation handlers with `@operations.register(...)` and call `serve()`.

Researcher code never sees a URL, a token, a `correlationId` or a retransmission.

Runtime dependencies: **none** (stdlib only). Optional extra `pandas` adds `Response.to_pandas()` and DataFrame return values from handlers.

```sh
pip install safeinsights-fusion            # or: pip install "safeinsights-fusion[pandas]"
python -m safeinsights_fusion doctor       # env + tunnel /v1/info check, content-free
```

Full documentation, the cross-language contract (`spec/`) and the R package live in the repository: https://github.com/safeinsights/fusion-sdk. License: AGPL-3.0-or-later.
