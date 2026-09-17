# Fusion SDK — Implementation Plan (R and Python)

**Date:** 2026-09-17
**Status:** Plan only — `fusion-sdk` is an empty scaffold (README, no code). The decisions in §0.1 were confirmed by the product owner on 2026-09-17 (§11 records the answers and the one discussion, on round-liveness ownership); package naming (A2) is the only item still open, and it does not block Phase 0.
**Authoritative spec:** `../SafeInsights Enclave Fusion Architecture Doc-v2.md` — §3 (invariants 1 and 6: outbound-only, researcher never touches the transport), §4.1 (the research-container-facing local API), **§4.5 (the SDK's two contracts: idempotency per `correlationId`, single in-flight round; ACK and duplicate suppression; typed errors)**, §7.1 (a round end to end), §7.3 (round liveness is owned by the destination SDK), §7.4, §7.5 (back-pressure surfaced as retryable), §7.6 (CLOSE via `POST /v1/complete`), §12 (event-level logging).
**Authoritative sequence:** `../fusion-rc-querys.md` (two-party); `../drawings/fusion/FusionWithSafeInsightsEnclave-technical-phases/phase6-analysis-rounds.md` (hub: peer handle, single in-flight per leg, `complete()` fan-out).
**Hub topology analysis:** `../.claude/fusion-hub-topology-review-2026-09-16.md` — §2.3 (source-side distinct-Person-ID guard lives in the SDK handler wrapper), §2.4 (per-leg items), §3 SDK row ("make it peer-addressed from day one"), §11 item 4 (SDK contract must land before the local API freezes).
**Security review:** `../fusion_security_review.md` §7.3 — `LIMIT_EXCEEDED` surfaced as a terminal typed error; remaining-budget hints piggybacked on responses; cardinality declarations per operation.
**Counterparty plans:** `../fusion-tunnel-app/.claude/plans/2026-07-09-implementation-plan.md` — §0.3 (`schemas/local-api.ts` is **owned by the tunnel repo**; the SDK mirrors it), Phase 2 (routes, role × endpoint direction matrix, 409/403/503 semantics), Phase 5 (source-side cached-response replay, caps), Phase 8 (terminal errors and exit codes), Phase 9 (env contract `FUSION_TUNNEL_ENDPOINT` / `FUSION_TUNNEL_ENDPOINTS`, `testing/rc-client.ts`), Risk 12 (the SDK lives outside the tunnel repo; its contract is `GET /v1/info` + the endpoints map + one bearer token per tunnel). `../fusion-relay/.claude/plans/2026-07-09-fusion-relay-implementation.md` — the SDK never sees the relay; only `BACKPRESSURE` (retryable) and the session-fatal error codes reach it, already translated by the tunnel.
**Convention templates:** none of the sibling repos is R or Python. Repo hygiene (CI shape, Trivy, Sonar, license, CLA) mirrors `../trusted-output-app`; language tooling is chosen in §0.2. The researcher-facing ergonomics donor is the OpenStax `osenclave` R package pattern visible in `../management-app/tests/fixtures/code-samples/main.r` (`osenclave::initialize()`, `osenclave::query_tutor()`, `osenclave::toa_results_upload()`).

## What we're building

Two thin, dependency-light client libraries — **Python** and **R** — with identical semantics over the Tunnel App's enclave-local HTTP API, plus the language-neutral **fusion envelope** they share (the JSON that travels *inside* the tunnel's opaque payload), plus the test doubles that let both be built and proven before the tunnel exists. Researcher code never sees a URL, a token, a `correlationId`, an ACK or a retransmission; it sees `request(peer, operation, params)` on the destination and a registry of operation handlers on the source.

1. **Destination side.** `connect()` discovers one or more tunnels from the environment, waits for `CHANNEL_UP`, and exposes a **peer handle per leg** (v2 §4.5, memo §3). `request(peer, operation, params)` is the blocking facade over `POST /v1/request` → `GET /v1/responses/{correlationId}` long-poll → `POST /v1/messages/{id}/ack`, with the round timeout and same-`correlationId` re-issue the design assigns to the SDK (§7.3). **Single in-flight round per peer**, enforced in the SDK before the tunnel's 409 backstop. `complete()` fans `POST /v1/complete` out to every peer.
2. **Source side.** `serve(operations)` runs the long-poll loop (`GET /v1/messages/next`), ACKs each query, dispatches to the named DO-approved handler, and posts the response (`POST /v1/messages`, `inReplyTo`). It owns the source's share of the reliability contract: **duplicate suppression and in-progress dedup by `correlationId`**, an **idempotency memo** so a redelivered query replays the cached answer, and the rule that **a handler exception never crashes the loop** — it becomes a typed error envelope to the destination, so a deterministic handler bug cannot become a poison message (§7.3 dead-letter after `MAX_DELIVERIES`). It exits cleanly on the terminal `STUDY_COMPLETE` (§7.6) and raises a typed error on `ERRORED` / `LIMIT_EXCEEDED`.
3. **Source-side guards** (memo §2.3, §6 item 2): `maxDistinctPersonIds` and `minGroupSize` enforced in the handler wrapper — the tunnel cannot parse query semantics; the SDK can count. Refusal is loud (typed error to the destination, event log at the source), never silent suppression.
4. **The envelope and table encoding** (§1 below): the only cross-language contract in the whole framework that is not owned by a TypeScript service. A Python destination must talk to an R source and vice versa; the conformance fixtures in `spec/` are what make that true.
5. **Typed errors and budget visibility**: round timeout, remote handler error, guard refusal, `LIMIT_EXCEEDED`, session errored, not ready — each a distinct class in both languages; remaining-budget hints exposed on every response so analyses can see a cap approaching (security review §7.3).
6. **Local simulator** for the SafeInsights IDE / CRATE: run a source handler set and a destination script in one process with no tunnel, so researchers can develop and test fusion code against sample data before submission (v2 §2 component inventory: "CRATE — R in Mgmt App: IDE specific for Fusion study").
7. **Content-free logging** (§12): round started/completed, `correlationId`, `messageId`, sizes, durations, budget — never params or bodies.

**Not in scope (boundaries stated so nobody looks for them here):** no cryptography, no networking beyond the tunnel endpoint(s), no data access (DP packages such as `osenclave` own the Person-ID → native-ID lookup and the data queries), no result release (the TOA upload stays with the Data Partner package — decided 2026-09-17, §11 Q6), no TypeScript SDK (the tunnel repo's `testing/rc-client.ts` is a harness driver, not a product), no persistence or checkpointing (the hub restart policy is "the run fails", memo §5.3).

## Hard constraints shaping this plan

1. **The Tunnel App does not exist**, and its local API schema (`schemas/local-api.ts`) will be owned there. Every phase here must be testable against an in-repo **fake tunnel pair** (§0.5, Phase 1) that implements the §4.1 table and the tunnel plan's Phase 2 status semantics, and that later gets swapped for the tunnel plan's Phase 9 harness. The fake is also the executable statement of what the SDK needs from the tunnel (§4 below lists the asks).
2. **The research-container image is assembled by `crane mutate`, not `docker build`** (`../iac/management-app/codebuild/scripts/containerizer.ts`): the researcher's code is appended as files at the base image's `WORKDIR`; there is no `pip install` / `install.packages()` step at packaging time. So the SDK must either be **pre-installed in the Data Partner's base image** (how `osenclave` gets there today) or be **vendorable as plain source files** alongside the researcher's code. Both routes demand a **pure-language package with a minimal, ubiquitous dependency set** — no compiled extensions, no heavy transitive trees. This shapes §0.2 more than anything else.
3. **The RC has no network beyond the tunnel and the TOA** (§4.1 posture, §13 SGs). The SDK makes no outbound call of its own — no telemetry, no update check, no schema fetch. Anything it needs at runtime is in the package or in the environment.
4. **Read-only root filesystem at the hub** (tunnel plan Phase 1, memo §5.2). The SDK writes nothing to disk on its own: no cache files, no lock files, no logs to files. Researcher-directed writes go to whatever `TMPDIR`/scratch the container provides.
5. **Two implementations of one behavior.** Parity is a maintenance risk the sibling plans do not have. The mitigation is structural: a single `spec/` (envelope JSON Schema, error-code table, local-API mirror, golden fixtures, scenario definitions) that both test suites consume, and a cross-language integration matrix in CI (Phase 5).

---

## Review: what the relay and tunnel designs fix for the SDK

The relay is invisible to the SDK by construction (§4.2: it reads routing metadata only; `correlationId` lives inside the ciphertext). Everything below is what the **tunnel** presents at its local API and what the **relay's** behavior implies through it.

| Design fact (source) | Consequence for the SDK |
| :-- | :-- |
| Local API is async-symmetric: `POST /v1/request` returns `correlationId` immediately; `GET /v1/responses/{id}` is a long-poll with a ~25 s hold (v2 §4.1, F13; tunnel §0.2 `FUSION_LONGPOLL_MS`) | The blocking facade is a poll loop: re-poll on each empty hold until the **round timeout**; no single HTTP call is ever held for minutes, so R/Python client timeouts stay short and fixed. |
| **Round liveness is owned by the destination SDK**: on round timeout, re-issue the *same* `correlationId`; the source dedups and replays its cached response (v2 §7.3, §8 row 1) | `POST /v1/request` must accept a client-supplied `correlationId` on re-issue — **not in the tunnel plan's route today (§4 ask T1)**. The SDK also re-issues on `404` from the responses poll (the destination tunnel restarted and lost the correlation). Bounded re-issue count, then `RoundTimeoutError`. |
| Tunnel enforces single in-flight with `409` and direction with `403` (tunnel Phase 2) | The SDK enforces both first — a per-peer lock and a role-specific API surface (a source handle has no `request()`; a destination handle has no `serve()`) — and treats `409`/`403` as programming errors, not retries. |
| `503` until `CHANNEL_UP`; the peer may take up to the launch window to appear (tunnel Phase 2; relay `UNPAIRED_SESSION_TIMEOUT_S` 900) | `connect()` / `serve()` wait with backoff up to a **readiness timeout** (default 15 min), reading `GET /v1/info.state` for a content-free progress log. |
| `BACKPRESSURE` is a retryable `429` at the local API (v2 §7.5, tunnel Phase 5) | Retry with jittered backoff, invisible to researcher code, bounded by the round timeout. With single in-flight per leg the SDK should almost never see it; if it does, it is a signal the source is slow to ACK, and the log says so. |
| **Two-stage ACK**: the source RC ACKs the query *before* running the operation (v2 §7.1 steps 6–7); the destination ACKs the response after receipt | `serve()` ACKs on receipt, then runs the handler. `request()` ACKs as soon as the response decodes, before returning. A response that fails to decode is **still ACKed** and surfaced as `ProtocolError` — otherwise it would be redelivered until dead-lettered. |
| Source tunnel replays a cached response for a redelivered `correlationId` (tunnel Phase 5) | The tunnel covers the "tunnel redelivered" case; the SDK covers the **"RC crashed after ACK, before responding"** case (v2 §8 row 3) with its own `correlationId` → response memo and in-progress set, and by handing `correlation_id` to handlers so their side effects can be keyed. |
| Poison message: `MAX_DELIVERIES` (5) redeliveries → dead-letter → session errored (v2 §7.3; relay §4) | A handler exception must **never** propagate out of the serve loop. Catch → error envelope (`HANDLER_ERROR`, sanitized) → respond → loop. Only a hard process death (OOM, segfault) can still poison a round. |
| Terminal states on long-poll routes: `STUDY_COMPLETE` (§7.6), `ERRORED` (dead-letter, un-ACKed TTL expiry), `LIMIT_EXCEEDED` (tunnel Phase 8) | `serve()` returns normally on `STUDY_COMPLETE`; raises `SessionError` / `LimitExceededError` otherwise. `request()` raises the same classes; after a terminal error every further call on that peer raises immediately. |
| Source-enforced caps and **budget hints piggybacked on responses** (security review §7.3; tunnel `caps.ts`) | Every response object carries `budget` (rounds / response bytes / query bytes, used vs max); exposed as `response.budget` and `peer.budget()`. **Needs the tunnel to place it on the local API response (§4 ask T2).** |
| Guards `maxDistinctPersonIds` / `minGroupSize` are "an SDK handler-wrapper concern" (tunnel Risk 12; memo §2.3) | The SDK needs the numbers and the operation's declared cardinality class. Proposed: the tunnel exposes the manifest's `guards` and `caps` on `GET /v1/info` (**§4 ask T3**); handlers declare `person_id_param` and `cardinality` at registration. |
| Hub: **one tunnel instance per leg**, peer-addressed SDK, `GET /v1/info` → `{legId, peerOrgSlug, role, direction, state}`, env `FUSION_TUNNEL_ENDPOINT` (one leg) or `FUSION_TUNNEL_ENDPOINTS` (map), **one bearer token per tunnel** (tunnel §10, Phase 9) | `connect()` reads the map, calls `/v1/info` on each, and keys peers by `peerOrgSlug`. Single-leg studies get the same API with one peer (and a convenience default peer). `complete()` fans out and reports per-leg outcome. |
| The tunnel treats the payload as opaque; the SDK owns what is inside (v2 §4.1 "payloads on this API are plaintext"; §7.2 chunking is below the SDK) | The **envelope** (§1) is SDK-owned and must be language-neutral. Chunking (32 KiB), padding and the 256 KiB inline cap / blob path are transparent — but JSON verbosity is not free against per-study byte caps, so the table encoding is typed-columnar, not records. |
| Plaintext response bytes are metered at `POST /v1/messages` before encryption (security review §7.3) | The bytes the tunnel counts are the serialized envelope. The SDK reports envelope size in its event log so researchers can correlate with budget. |
| Egress is tunnel + TOA only; event-level logging only (v2 §4.1, §12; threat model SC-02 "the fusion SDK joins that image") | Zero runtime network calls other than the tunnel; zero content in logs; minimal dependency footprint — the SDK is inside the supply-chain boundary of every DP base image. |
| Containerizer is `crane mutate` (constraint 2) | Distribution = pre-installed in base images and/or vendorable; pure language; §0.2 and Phase 7. |
| Hub restart policy: task failure fails the run; no persistence (memo §5.3) | No checkpoint/resume feature. The SDK's memo is in-memory only and dies with the process, which is correct: the destination re-issues by `correlationId`. |

**Contradictions or gaps found** (none block the SDK; all are flagged in §4): the client-supplied `correlationId` on re-issue (T1); where `budget` and `guards` surface (T2, T3); the token env-var shape for N legs (S1); whether `payload` on the local API is a JSON value or an opaque string (T4); the `apiVersion` field the SDK needs to refuse an incompatible tunnel (T5).

---

## 0. Guiding decisions

### 0.1 Decisions (confirmed by the product owner 2026-09-17 unless marked **open**)

| # | Decision | Chosen | Rejected alternative | Note |
| :-- | :-- | :-- | :-- | :-- |
| A1 | Repo shape | One repo: `python/` + `r/` + `spec/` + `tools/` | Two repos | Shared `spec/` and one CI matrix. |
| A2 | Package names | **Open.** Working names: Python `safeinsights_fusion` (PyPI `safeinsights-fusion`); R `sifusion` | — | Rename is mechanical; the PyPI/CRAN collision check happens in Phase 0 and the decision is due before Phase 7 publishes anything. |
| A3 | Envelope body encoding | UTF-8 JSON; tabular data in the typed-columnar **fusion table** shape (§1) | Apache Arrow IPC as the default | Arrow stays a reserved optional `encoding` for v1.1; as a default it would add `pyarrow` / `arrow` as hard dependencies, against constraint 2. |
| A4 | Guard breach behavior | **Refuse**: `GUARD_REFUSED` error envelope to the destination, event log at the source, no partial result | Suppress small cells and return the rest | Matches "no silent throttling" (security review §7.3). |
| A5 | Source loop concurrency | Sequential: one query at a time per tunnel | Threaded handlers | Single in-flight per leg means at most one outstanding query per tunnel. |
| A6 | Round liveness | **Stays in the SDK** (v2 §7.3), invisible to researcher code: default 600 s round timeout, up to 3 same-`correlationId` re-issues, per-request override | Tunnel-owned re-issue | §11 Q3 explains why the tunnel cannot own the restart case; the tunnel owns every transport-level recovery. |
| A7 | Python floor | ≥ 3.10 (tested 3.10–3.13) | ≥ 3.9 | Pure stdlib, so lowering later is cheap if a Data Partner image needs it. |
| A8 | R floor and HTTP client | R ≥ 4.1; Imports: `curl`, `jsonlite` only | `httr2` | Footprint (constraint 2); both are on effectively every Data Partner image. |
| A9 | Python runtime deps | **Stdlib only** | `requests` / `httpx` | Extras: `[pandas]`; `[arrow]` later. |
| A10 | Env contract for N legs | `FUSION_TUNNEL_ENDPOINTS` + `FUSION_TUNNEL_TOKENS` JSON maps keyed by leg label; single-leg shorthand `FUSION_TUNNEL_ENDPOINT` + `FUSION_TUNNEL_TOKEN`; `FUSION_ROLE` | One env var per leg | Alignment with setup-app is ask S1 (§4). |
| A11 | Distribution | **Data Partners rebuild their base images** to pick up SDK releases — the `osenclave` model: the SDK is installed in the image at `org_code_env.url`, and the containerizer only appends researcher code. SafeInsights builds the hub's image the same way. Published to PyPI and r-universe/GitHub | Containerizer installs at build time | Vendoring (`vendor.sh` / `vendor.R`) stays a documented fallback, not the primary path. Consequence: compatibility across independently rebuilt images is a hard requirement (Risk 8). |
| A12 | TOA upload helper | **Out of scope.** Result release stays with the Data Partner package | `release_results()` in the SDK | — |
| A13 | Handler error detail across the channel | **Full traceback by default** | Sanitized class + one-line message | Everything reaching the destination enclave is gated by output review before release, and error envelopes are metered against the source's response-byte caps like any response. Per-operation `sanitize` opt-in retained (§1). |
| A14 | License | **AGPL-3.0-or-later**, matching setup-app and the TOA | MIT (fusion-relay) | `LICENSE` and `CLA.md` land in Phase 1. |
| A15 | Simulator timing | Phase 6 is **not required for the first fusion study** but is close behind | Ship with v1 | Phases 2–4 keep the transport behind one internal interface so the simulator is a transport swap, not a fork (Phase 6). |

### 0.2 Library and tooling choices

| Concern | Python | R | Rationale |
| :-- | :-- | :-- | :-- |
| HTTP | `urllib.request` with explicit per-call timeouts | `curl` (`curl::curl_fetch_memory`, `handle_setopt(timeout_ms=…)`) | Constraint 2: no compiled extensions, minimal footprint. Long-poll needs precise per-request timeouts, which both give. |
| JSON | `json` (stdlib) | `jsonlite` | Ubiquitous. `jsonlite::toJSON(digits = NA, na = "null", auto_unbox = …)` settings are pinned in one internal function so encoding is deterministic. |
| Tabular helpers | optional `pandas` extra (`to_table()` / `from_table()`); base helper works on lists of columns | base `data.frame`; optional `tibble`/`data.table` support via generic | The fusion table shape (§1) is defined independently of any dataframe library. |
| IDs | `uuid.uuid4()` | `uuid::UUIDgenerate()` — **or** avoid: the tunnel mints `correlationId`; the SDK only echoes it | Prefer no dependency: the SDK never mints IDs in v1 (see T1 — re-issue echoes the tunnel-minted id). |
| Logging | `logging` (stdlib), logger `safeinsights_fusion` | `message()`-based with level filter; `FUSION_LOG_LEVEL` | Structured, content-free event vocabulary shared across both (`spec/log-events.md`). |
| Tests | `pytest`, `pytest-timeout` | `testthat` (3e), `withr` | Both consume `spec/fixtures/`. |
| Lint / format / types | `ruff` (lint + format), `mypy --strict` | `lintr`, `styler`, `R CMD check --as-cran` | House principle: type-check and lint gate CI. |
| Packaging | `pyproject.toml` (hatchling or setuptools), `uv` for dev | `DESCRIPTION`/`NAMESPACE`, `roxygen2`, `pkgdown` (docs) | `uv` is already the local toolchain. |
| CI | GitHub Actions matrix: Python 3.10–3.13 × R 4.1/release; Trivy fs + license; Sonar; Docker smoke in `python:3.12-slim` and `r-base` images | | Mirrors TOA `checks.yml` (lint → trivy → sonar → typecheck → test → build). |
| License | AGPL-3.0-or-later (A14) | | Matches setup-app and the TOA; decided 2026-09-17. |

### 0.3 Tuning defaults (env-overridable; `// PROVISIONAL — v2 §15.6`)

| Knob | Env var | Default | Note |
| :-- | :-- | :-- | :-- |
| Readiness timeout | `FUSION_READY_TIMEOUT_S` | 900 | = relay `UNPAIRED_SESSION_TIMEOUT_S` / BMA launch window |
| Readiness poll interval | `FUSION_READY_POLL_S` | 2 → 15 s exponential | |
| Long-poll HTTP timeout | `FUSION_POLL_HTTP_TIMEOUT_S` | 40 | must exceed the tunnel's hold (25 s) plus slack |
| Round timeout | `FUSION_ROUND_TIMEOUT_S` | 600 | per-request override |
| Max re-issues per round | `FUSION_ROUND_MAX_REISSUES` | 3 | then `RoundTimeoutError` |
| Retry backoff (429/5xx/connection) | `FUSION_RETRY_*` | 250 ms → 10 s, jitter, bounded by round timeout | |
| Idempotency memo size (source) | `FUSION_MEMO_MAX_ENTRIES` | 256 | in-memory LRU of `correlationId → response` |
| Response envelope soft warning | `FUSION_WARN_BYTES` | 8 MiB | logs a warning; the tunnel's caps are authoritative |
| Log level | `FUSION_LOG_LEVEL` | `INFO` | |

### 0.4 Contract-first: `spec/` is the single source of truth

Everything both languages must agree on lives once, in `spec/`, and both test suites load it:

- `spec/envelope.schema.json` — JSON Schema (draft 2020-12) for query / response / error envelopes and the fusion table shape (§1).
- `spec/errors.md` + `spec/errors.json` — remote error codes (travel in the envelope) and local error classes (raised to the researcher), with the mapping from tunnel HTTP status and `code` to SDK class (§2.3).
- `spec/local-api.md` — the SDK's **mirror** of the tunnel's `schemas/local-api.ts`, request/response shapes per route, status semantics, terminal bodies. Reviewed against the tunnel repo; **the tunnel's zod schema is canonical**. Phase 1 adds a script to diff this mirror against a JSON-Schema export of the tunnel's zod schemas once that repo exists.
- `spec/env.md` — the env contract (A10) shared with setup-app.
- `spec/fixtures/` — golden envelopes (`*.json`) with expected decoded values, including edge cases (NA/None/null, integers vs floats, dates, empty tables, unicode, 1-row tables) — every fixture must encode/decode identically in both languages.
- `spec/scenarios/` — the fake-tunnel-pair scenario definitions (happy N rounds, timeout+re-issue, 404 re-issue, backpressure, handler error, unknown operation, guard refusal, limit exceeded, session errored, study complete, hub two legs).
- `spec/log-events.md` — the content-free event vocabulary.

### 0.5 Repo layout

```
fusion-sdk/
├── README.md                     # what it is, status, links (rewrite of today's one-liner)
├── LICENSE  CLA.md               # AGPL-3.0-or-later (A14)
├── .github/workflows/checks.yml  # lint → trivy fs/license → sonar → typecheck → test (matrix) → build → docker smoke
├── spec/                         # §0.4 — the cross-language contract; no code
├── tools/
│   └── fake-tunnel-pair/         # Python, stdlib only: TWO local-API servers (destination + source) joined by an
│       ├── server.py             #   in-memory "relay"; implements §4.1 routes + tunnel Phase 2/5/8 semantics
│       ├── scenarios.py          #   fault injection driven by spec/scenarios/*.json
│       └── cli.py                #   `fake-tunnel-pair --scenario happy --legs 2` for local dev and both test suites
├── python/
│   ├── pyproject.toml            # safeinsights-fusion; deps: none; extras: pandas, dev
│   ├── src/safeinsights_fusion/
│   │   ├── __init__.py           # public API re-exports
│   │   ├── _http.py              # urllib client: bearer, timeouts, retry/backoff, content-free errors
│   │   ├── _envelope.py          # encode/decode per spec/envelope.schema.json; fusion table helpers
│   │   ├── _tunnel.py            # typed wrapper of the six /v1 routes + /v1/info (mirror of local-api.md)
│   │   ├── destination.py        # Fusion.connect(), Peer, request(), complete(), single-in-flight lock
│   │   ├── source.py             # operation registry, serve() loop, memo, guards, error envelopes
│   │   ├── guards.py             # maxDistinctPersonIds / minGroupSize / cardinality checks
│   │   ├── errors.py             # error taxonomy (§2.3)
│   │   ├── simulate.py           # in-process simulator for IDE/CRATE (Phase 6)
│   │   ├── _log.py               # event vocabulary
│   │   └── __main__.py           # `python -m safeinsights_fusion doctor` — env + /v1/info check, content-free
│   └── tests/                    # unit (fixtures) + integration (fake-tunnel-pair)
├── r/
│   ├── DESCRIPTION  NAMESPACE     # sifusion; Imports: curl, jsonlite; Suggests: testthat, withr, tibble
│   ├── R/
│   │   ├── http.R  envelope.R  tunnel.R  destination.R  source.R  guards.R  errors.R  simulate.R  log.R  doctor.R
│   ├── tests/testthat/           # same fixtures and scenarios
│   └── vendor.R                  # emits a single-directory drop for `source()`-style vendoring (A11)
├── examples/
│   ├── two-party/{source.py, destination.py, source.R, destination.R}
│   └── hub/{destination.py, destination.R, source-a.R, source-b.py}   # cross-language on purpose
└── docs/decisions/               # ADR 0001 envelope encoding, 0002 guards semantics, 0003 distribution
```

---

## 1. The fusion envelope (SDK-owned wire contract)

The tunnel carries an opaque payload; this is what the SDK puts in it. Version-tagged from day one; the table shape exists so a data frame round-trips **with types** between R and Python without Arrow.

```jsonc
// Query (destination → source)
{ "v": 1, "kind": "query", "operation": "counts_by_group",
  "params": { "person_ids": ["p-…"], "group_by": "grade" },
  "encoding": "json" }

// Response (source → destination), success
{ "v": 1, "kind": "response", "status": "ok",
  "body": { "__table__": 1,
            "columns": [ { "name": "grade", "type": "string" }, { "name": "n", "type": "integer" } ],
            "rows": [ ["9", 412], ["10", 388] ] },
  "encoding": "json" }

// Response, error (never a transport failure — the round completes; the destination raises RemoteError)
{ "v": 1, "kind": "response", "status": "error",
  "error": { "code": "UNKNOWN_OPERATION | BAD_PARAMS | HANDLER_ERROR | GUARD_REFUSED",
             "message": "sanitized, no data values", "detail": { "guard": "maxDistinctPersonIds", "limit": 5000 } } }
```

- **Bodies are arbitrary JSON.** The fusion table is a *convention* the helpers produce and recognize (`__table__` marker), not a requirement; scalar and nested-object bodies are fine.
- **Type vocabulary:** `string`, `integer`, `number`, `boolean`, `date` (ISO-8601 date), `datetime` (RFC 3339 UTC), `null`-able by default; `NA`/`None`/`NaN` → `null`. Integers that exceed 2^53 are encoded as strings with `type: "integer64"`. Encoding rules and every edge case are fixtures in `spec/fixtures/`.
- **`encoding`** is `json` in v1. `arrow` (base64 Arrow IPC stream, optional extras on both sides) is reserved for v1.1 when a study's payloads justify it; the field exists now so the switch is additive.
- **Size guidance** (documented, not enforced by the SDK): the tunnel's inline cap is 256 KiB and the blob path handles larger payloads transparently, but every byte counts against the source-approved caps. The SDK logs the envelope size per round and warns above `FUSION_WARN_BYTES`.
- **Error `message` carries the full traceback by default** (A13): the destination enclave is the approved channel, whatever reaches it is gated by output review before release, and error envelopes are metered against the source's response-byte caps like any other response. A per-operation `sanitize` flag keeps only the exception class and a one-line message for operations where a traceback could echo values the Data Partner does not want crossing; the full traceback always goes to the source's local log.

## 2. Public API (mirrored across languages)

### 2.1 Destination

```python
from safeinsights_fusion import Fusion

with Fusion.connect() as fusion:                 # reads env (A10), waits for CHANNEL_UP on every leg
    a = fusion.peer("dp-a")                       # by peerOrgSlug from GET /v1/info; fusion.peer() when one leg
    r = a.request("counts_by_group", {"person_ids": ids, "group_by": "grade"}, timeout=900)
    df = r.to_pandas()                            # or r.body for raw JSON
    print(r.budget)                               # Budget(rounds_used=3, rounds_max=500, response_bytes_used=…, …)
    fusion.complete()                             # fan-out; context manager calls it on CLEAN exit only
```

```r
library(sifusion)
fusion <- fusion_connect()
a <- fusion_peer(fusion, "dp-a")
r <- fusion_request(a, "counts_by_group", list(person_ids = ids, group_by = "grade"), timeout = 900)
df <- fusion_as_data_frame(r)      # r$body for raw
r$budget
fusion_complete(fusion)
```

- `request()` blocks; a second concurrent `request()` on the same peer raises `ConcurrencyError` immediately (the tunnel's `409` is the backstop). Different peers may be in flight simultaneously — that is the hub's whole point.
- `peers()` lists `{peer, legId, direction, state}`; `budget(peer)` reads the last-seen hint.
- After a terminal error on a leg, every call on that peer raises the same terminal error; `complete()` still attempts the other legs and reports per-leg results.

### 2.2 Source

```python
from safeinsights_fusion import operations, serve

@operations.register("counts_by_group", person_id_param="person_ids", cardinality="per-group")
def counts_by_group(params, ctx):               # ctx.correlation_id, ctx.peer, ctx.budget, ctx.log
    return {"__table__": 1, ...}                  # or a pandas DataFrame with the extra installed

serve()                                           # blocks until STUDY_COMPLETE; raises SessionError / LimitExceededError
```

```r
ops <- fusion_operations(
  counts_by_group = fusion_operation(function(params, ctx) { ... },
                                     person_id_param = "person_ids", cardinality = "per-group")
)
fusion_serve(ops)
```

- Handlers return a JSON-able value, a data frame, or raise. Any raise → `HANDLER_ERROR` envelope; unknown operation → `UNKNOWN_OPERATION`; guard breach → `GUARD_REFUSED`. The loop continues in every case.
- `cardinality ∈ {aggregate, per-group, per-record}` mirrors the manifest's cardinality class (security review §7.3); `person_id_param` names the parameter whose distinct count is bounded by `maxDistinctPersonIds`; `minGroupSize` applies to `per-group` results (a table with an integer count column named at registration or inferred as the single integer column).
- `ctx.correlation_id` lets side-effecting handlers stay idempotent; the SDK's own memo replays the previous response for a repeated `correlationId` without re-running the handler.

### 2.3 Error taxonomy

| Class (Python / R condition class) | When | Terminal for the peer? |
| :-- | :-- | :-- |
| `FusionError` / `fusion_error` | base | — |
| `ConfigError` | env missing/malformed, non-local endpoint, incompatible `apiVersion` | yes (never connected) |
| `NotReadyError` | readiness timeout waiting for `CHANNEL_UP` | yes |
| `ConcurrencyError` | second in-flight request on a peer; `409` from tunnel | no |
| `RoundTimeoutError` | round timeout after max re-issues | no (peer stays usable; the round is abandoned) |
| `RemoteError` (`code`, `message`, `detail`) | source returned an error envelope (`UNKNOWN_OPERATION`, `BAD_PARAMS`, `HANDLER_ERROR`, `GUARD_REFUSED`) | no |
| `LimitExceededError` | tunnel reports `LIMIT_EXCEEDED` on this leg | **yes** |
| `SessionError` (`code`) | dead-letter, un-ACKed expiry, session closed, tunnel `ERRORED` | **yes** |
| `ProtocolError` | undecodable local-API response or envelope (still ACKed) | no |

Retryable conditions (`429`, `503` pre-ready, connection reset, `5xx`) are handled inside the SDK with backoff and never surface unless the round timeout is exhausted.

## 3. Behavioral specification

**Destination `request()` lifecycle**

1. Acquire the peer's in-flight lock (fail fast with `ConcurrencyError`).
2. Encode the envelope; log `round.start {peer, operation, bytes}` (no params).
3. `POST /v1/request {payload}` → `correlationId` (retry `429`/`503`/connection errors with backoff).
4. Loop: `GET /v1/responses/{correlationId}` with HTTP timeout `FUSION_POLL_HTTP_TIMEOUT_S`. Empty hold → re-poll. `404` → the tunnel restarted: re-issue with the **same** `correlationId` (T1). Round timeout elapsed → re-issue likewise, up to `FUSION_ROUND_MAX_REISSUES`, then `RoundTimeoutError`. Terminal body (`LIMIT_EXCEEDED`, `SESSION_ERRORED`) → mark the peer terminal, raise.
5. Response arrives → decode envelope → `POST /v1/messages/{messageId}/ack` (even if decode failed) → update `budget` → release lock → return `Response` or raise `RemoteError` / `ProtocolError`.

**Source `serve()` loop**

1. Wait for `CHANNEL_UP` (readiness), read `guards`/`caps` from `/v1/info` (T3).
2. Loop: `GET /v1/messages/next` (long-poll). Terminal `STUDY_COMPLETE` → log `session.complete`, return. Terminal error → raise `SessionError` / `LimitExceededError`.
3. Message arrives → `POST /v1/messages/{messageId}/ack` immediately (stage two).
4. If `correlationId` is in the memo → re-post the memoized response (`inReplyTo`) and continue. If it is in the in-progress set (cannot happen under sequential processing, guarded anyway) → skip.
5. Decode envelope (`BAD_PARAMS` on failure) → look up operation (`UNKNOWN_OPERATION`) → run guards (`GUARD_REFUSED`) → run handler in `try` (`HANDLER_ERROR`) → encode response → `POST /v1/messages {inReplyTo, payload}`; on `LIMIT_EXCEEDED` from the tunnel, raise `LimitExceededError` (the tunnel has already told the destination and the BMA).
6. Store `correlationId → response` in the LRU memo; log `round.served {correlationId, operation, bytes, duration}`.

**HTTP status → behavior (both sides)**

| Status / condition | Behavior |
| :-- | :-- |
| `200` | proceed |
| `204` / empty hold on long-poll | re-poll |
| `401` | `ConfigError` (bad bearer token) — never retried |
| `403` | direction violation → `ConfigError` (programming error: wrong role) |
| `404` on responses poll | re-issue (destination tunnel restarted) |
| `409` | `ConcurrencyError` |
| `422` schema rejection | `ProtocolError` (SDK/tunnel contract drift) |
| `429` `BACKPRESSURE` | backoff + retry within round timeout |
| `503` not ready | readiness wait (before) / backoff + retry (during a round) |
| `5xx`, connection refused/reset, timeout on non-long-poll calls | backoff + retry, bounded |
| terminal JSON body `{code: STUDY_COMPLETE \| SESSION_ERRORED \| LIMIT_EXCEEDED}` | per §2.3 |

## 4. Cross-repo contract items (asks, with owners)

**To `fusion-tunnel-app` (`schemas/local-api.ts`, Phase 2/5/8):**

- **T1 — Re-issue with the same `correlationId`.** `POST /v1/request` must accept an optional `correlationId` (echoing one the tunnel previously minted) so the destination SDK can re-issue a timed-out or orphaned round (v2 §7.3). Idempotent: a re-issue for an in-flight `correlationId` returns `202` and does not create a second outbox entry. Alternative: `POST /v1/request/{correlationId}/reissue`.
- **T2 — Budget hints on the local API.** `GET /v1/responses/{id}` (destination) and `GET /v1/messages/next` (source, as the source's own view) carry `budget: {roundsUsed, roundsMax, responseBytesUsed, responseBytesMax, queryBytesUsed, queryBytesMax, roundsPerHourUsed?, roundsPerHourMax?}`; the source tunnel places them inside the AEAD-protected response header so the destination tunnel can expose them (security review §7.3 "piggybacked on responses").
- **T3 — Manifest guards on `/v1/info`.** Add `apiVersion`, `guards: {maxDistinctPersonIds?, minGroupSize?}`, `caps`, and `operations?: [{name, cardinality}]` (if the manifest carries the approved operation list, the SDK can refuse to serve an unregistered-but-approved or registered-but-unapproved operation at startup — a useful pre-flight, not a security boundary).
- **T4 — Payload representation.** Recommend `payload` is a **JSON value** passed through (bytes on the wire = canonical UTF-8 `JSON.stringify`), not a base64 string: no double encoding, simplest for `jsonlite`/`json`. The tunnel never interprets it. If binary is ever needed, the SDK's `encoding: "arrow"` base64s inside the envelope.
- **T5 — Terminal bodies and `apiVersion`.** Long-poll routes return terminal states as JSON `{terminal: true, code, message?}` with `200`, not as HTTP errors, so the SDK distinguishes "the study ended" from "the tunnel is broken". `/v1/info.apiVersion` (semver) lets the SDK refuse an incompatible tunnel with `ConfigError` instead of failing mid-study.
- **T6 — Message shape.** Delivered query/response objects: `{messageId, correlationId, payload, budget?, receivedAt}`; the source's `POST /v1/messages` body: `{inReplyTo, payload}`.

**To `setup-app` (env injection, tunnel plan Phase 9 / memo §3):**

- **S1 — Env contract (A10):** `FUSION_TUNNEL_ENDPOINT` + `FUSION_TUNNEL_TOKEN` for one leg; `FUSION_TUNNEL_ENDPOINTS` + `FUSION_TUNNEL_TOKENS` JSON maps keyed by the same labels for N legs. Tokens via `secrets`, not plaintext `environment`, on ECS (memo §5.2). Plus `FUSION_ROLE` (`source|destination`) so the SDK can fail fast on misuse before the tunnel's `403`.
- **S2 — Base-image cadence (decided: Data Partners rebuild, A11):** each Data Partner installs the SDK in its base image (`org_code_env.url`) and rebuilds on its own schedule. SafeInsights publishes every release with a changelog and a compatibility table against the tunnel's `apiVersion`, and offers `analysis/r/base-container` (plus a Python sibling) as recommended parents so partners deriving from them get the SDK for free. The hub's image is SafeInsights-built.

**To `management-app` (CRATE / starter code):**

- **B1 —** ship the `examples/` templates as fusion starter code (`org_code_env.starterCodeFileNames`) and wire the simulator (Phase 6) into the CRATE fusion IDE with both DPs' sample data.

---

## Phase 0 — Contract spec and decisions

**Goal:** `spec/` complete and reviewed with the tunnel-app owner; ADR 0001 (envelope), ADR 0002 (guards), ADR 0003 (distribution) recorded from the §11 decisions.

**Work:** write `spec/envelope.schema.json`, `spec/errors.*`, `spec/local-api.md` (mirroring the tunnel plan's Phase 2 route semantics and adding T1–T6 as marked proposals), `spec/env.md`, ~40 golden fixtures, the scenario list; name-collision check on PyPI/CRAN to feed the open A2 decision; open the T1–T6 / S1 asks as issues on the owning repos.

**Tests:** fixtures validate against the schema (a stdlib-only validator in `tools/`); CI runs schema validation on every push.

## Phase 1 — Scaffolding, CI, and the fake tunnel pair

**Goal:** both packages build, lint, type-check and run an empty test suite in CI; `fake-tunnel-pair` serves a full two-party round to a `curl` script.

**Work:** repo layout (§0.5); `checks.yml` matrix; Trivy + Sonar config; Dockerfiles for the smoke test (`python:3.12-slim`, `r-base`) that `pip install` / `R CMD INSTALL` the packages and run `doctor` against the fake; `tools/fake-tunnel-pair` implementing every §4.1 route with the tunnel plan's status semantics (`503` until a scripted `CHANNEL_UP`, `409`, `403` by role, long-poll holds, two-stage ACK bookkeeping, redelivery of un-ACKed messages, cached-response replay by `correlationId`, `429` injection, terminal bodies, `LIMIT_EXCEEDED` after configurable bytes/rounds, per-leg instances for the hub scenario) driven by `spec/scenarios/*.json`; `LICENSE` (AGPL-3.0-or-later, A14) and `CLA.md` following the TOA; README rewrite matching the sibling repos.

**Tests:** fake-tunnel-pair unit tests (Python); CI green on the matrix; both Docker smokes pass.

## Phase 2 — Python core: HTTP, envelope, destination

**Goal:** a Python destination completes N rounds against the fake with every §3 behavior.

**Work:** `_http.py` (bearer, timeouts, backoff, no content in exceptions), `_envelope.py` (+ fusion table helpers, optional pandas), `_tunnel.py`, `destination.py` (connect/readiness, peers from `/v1/info`, `request()` lifecycle, per-peer lock, re-issue on timeout and `404`, ACK-before-return, budget, `complete()` fan-out, context manager), `errors.py`, `_log.py`, `__main__.py doctor`.

**Tests:** all fixtures round-trip; scenario suite: happy N rounds (1 leg), backpressure, timeout → re-issue → cached replay, tunnel-restart `404` → re-issue, concurrency error, remote error envelope, limit exceeded terminal, session errored terminal, complete fan-out; content-free log audit (marker strings in params/bodies never appear in captured logs).

## Phase 3 — Python source: serve loop, memo, guards

**Goal:** a Python source serves a Python destination through the fake with full §3 semantics.

**Work:** `source.py` (registry/decorator, readiness, loop, ACK-first, memo LRU + in-progress set, error envelopes, `STUDY_COMPLETE` clean return, terminal raises), `guards.py`, `ctx` object.

**Tests:** redelivered `correlationId` → memo replay, handler not re-run; handler raises → `HANDLER_ERROR` envelope and loop continues (poison defense); unknown op; bad params; guards: distinct-ID breach and small-group breach → `GUARD_REFUSED`, no partial result; `STUDY_COMPLETE` ends `serve()` with exit 0 in a subprocess; `LIMIT_EXCEEDED` → typed raise → non-zero exit; content-free log audit at the source.

## Phase 4 — R package (mirror)

**Goal:** `sifusion` passes the same fixtures and scenarios as the Python package.

**Work:** `R/*.R` per §0.5, roxygen docs, `testthat` suites loading `spec/fixtures` and driving `fake-tunnel-pair` via `processx`-free `system2()` (dev-only dependency kept minimal); condition classes per §2.3; `fusion_as_data_frame()` / `fusion_table()` helpers with the type vocabulary (Date, POSIXct UTC, integer, double, logical, character, NA); `vendor.R`.

**Tests:** identical scenario list to Phases 2–3; `R CMD check --as-cran` clean; `jsonlite` determinism tests (digits, NA handling, `auto_unbox` pitfalls: a length-1 vector must not silently become a scalar where the schema says array).

## Phase 5 — Cross-language matrix and hub scenario

**Goal:** every combination talks; the two-leg hub works with mixed languages.

**Work:** integration job running {Py→Py, Py→R, R→Py, R→R} destination→source through the fake; hub scenario: one destination (Python, then R) with two legs, source A in R and source B in Python, queries to B derived from A's answers, `complete()` fan-out, one leg hitting `LIMIT_EXCEEDED` while the other continues; nightly chaos variant with randomized fault injection from `spec/scenarios`.

**Tests:** the matrix is the test; fixture parity gate (any fixture that passes in one language and fails in the other fails CI).

## Phase 6 — Simulator, examples, docs

**Sequencing (A15):** not required for the first fusion study; scheduled right after the first end-to-end run. Phases 2–4 must keep every HTTP call behind the internal `_tunnel` / `tunnel.R` interface so the simulator is an in-process implementation of that same interface — no second code path through the destination or source logic.

**Goal:** a researcher can run a fusion study end to end in the IDE with no tunnel.

**Work:** `simulate.py` / `simulate.R`: in-process wiring of a handler set and a destination function (no HTTP) with the same guards, memo, and error semantics, plus optional fault injection (timeouts, guard breaches) so researchers can test error handling; `examples/` two-party and hub templates in both languages; `pkgdown` and Python API docs; a "writing idempotent operations" guide; the content-free logging contract for reviewers.

**Tests:** examples run under the simulator in CI; examples also run against the fake pair.

## Phase 7 — Packaging and distribution

**Goal:** installable from public indexes, vendorable, and proven inside representative base images.

**Work:** PyPI release workflow (trusted publishing, signed tags); R: r-universe (`safeinsights.r-universe.dev`) and `remotes::install_github("safeinsights/fusion-sdk", subdir = "r")`, CRAN submission checklist as a later milestone; `python/vendor.sh` and `r/vendor.R` outputs tested by copying into a scratch `WORKDIR` and running the examples the way `crane mutate` would place them; Docker smoke extended to `public.ecr.aws/docker/library/r-base` and the SafeInsights `analysis/r/base-container` if pullable; SBOM in the release; Trivy license gate.

**Tests:** install-from-index in a clean container; vendored drop runs; SBOM generated.

## Phase 8 — Swap in the real tunnel

**Goal:** the SDK test suites run against `fusion-tunnel-app`'s Phase 9 harness (fake relay + fake BMA + two real tunnels) and later against the real relay.

**Work:** a `--tunnel real` switch in both test suites pointing at the tunnel harness's compose profiles; diff `spec/local-api.md` against a JSON-Schema export of the tunnel's zod schemas in CI; contribute the SDKs as the RC drivers for the tunnel harness's hub scenario (replacing `testing/rc-client.ts` where useful).

**Tests:** the Phase 5 matrix green against real tunnels; contract-diff job green.

---

## Risks and open items

1. **Two implementations drift** (highest). Mitigation: `spec/` fixtures + scenario parity gate (Phase 5); one behavioral spec (§3) both are written from; ADRs for every semantic choice.
2. **Local-API contract drift** with a tunnel that does not exist yet. Mitigation: T1–T6 filed before the tunnel's Phase 2; `spec/local-api.md` diffed against the tunnel's zod export (Phase 8); the fake pair encodes exactly the semantics the SDK relies on.
3. **Dependency footprint on Data Partner images.** `curl`/`jsonlite` and stdlib are conservative choices; the Docker smoke on `r-base` and `python:*-slim` is the tripwire; vendoring is the escape hatch.
4. **Researcher misuse**: non-idempotent handlers, ignoring `RoundTimeoutError`, calling `request()` from threads. Mitigation: `ctx.correlation_id`, the memo, fail-fast `ConcurrencyError`, the idempotency guide, simulator fault injection.
5. **JSON verbosity vs byte caps.** Typed-columnar tables cut overhead materially versus records; `arrow` encoding reserved; size logged per round.
6. **Guard semantics are new to the design** (memo §2.3 proposes them; the manifest fields do not exist yet). The SDK ships with guards optional (no `/v1/info.guards` → guards disabled with a startup warning) so it does not block on the BMA manifest work.
7. **R single-threaded long-poll** blocks the source script — intended; document that source scripts should do nothing but `fusion_serve()` after setup.
8. **Compatibility across independently rebuilt images.** Data Partners rebuild base images on their own schedule (A11), so a study may pair an older SDK on one side with a newer one on the other, and either with a newer tunnel. Mitigation: the envelope `v` and the tunnel `apiVersion` are checked at `connect()` / `serve()`; within a major version the SDK stays backward compatible with older envelopes and older tunnels; the compatibility table ships with every release; the cross-language matrix (Phase 5) also runs the previous minor against the current.
9. **All §0.3 numbers are provisional** until load-tested with the real tunnel and relay (v2 §15.6).

## 10. Hub topology (recorded 2026-09-17)

Adopted from the memo and the tunnel plan §10 as the SDK's day-one shape rather than a later extension: peer handles keyed by `peerOrgSlug`, one tunnel endpoint and one bearer token per leg (A10/S1), single in-flight **per peer**, `complete()` fan-out with per-leg outcomes, terminal errors scoped to a leg, and guards enforced at each **source** for its own data. The two-party study is the one-peer special case: `fusion.peer()` with no argument returns the only peer, so two-party researcher code stays one line shorter and otherwise identical. Nothing in the envelope or the source loop is topology-aware; a source never knows whether its destination is a Data Partner or the SafeInsights Fusion Enclave.

## 11. Decisions recorded 2026-09-17 (product owner)

| Q | Question | Answer | Applied in |
| :-- | :-- | :-- | :-- |
| 1 | Payload/table encoding | Typed-columnar JSON is acceptable for v1 | A3, §1 |
| 2 | Guard breach semantics | Refuse | A4, §2.2, §3 |
| 3 | Re-issue ownership: SDK or tunnel? | Discussed below — stays in the SDK, invisible to researchers | A6, T1 |
| 4 | Distribution | Data Partners rebuild their base images | A11, S2, Risk 8 |
| 5 | Repo shape and names | One repo; naming deferred | A1; A2 **open** |
| 6 | TOA helper | Keep it out of the SDK | A12 |
| 7 | Handler error detail | Full tracebacks — errors are reviewed in the Management App like results before release | A13, §1 |
| 8 | Python floor | ≥ 3.10 | A7 |
| 9 | License | AGPL-3.0-or-later | A14, §0.2 |
| 10 | Simulator priority | Not needed for the first study, but not far behind | A15, Phase 6 |

**Q3 — who owns round re-issue.** The product owner's concern: researchers use the SDK and should not be managing reliability; the Tunnel App should. The goal is shared, and the plan meets it — re-issue is SafeInsights' SDK code, never researcher code. `request()` returns a result or raises a typed error; researchers never see a `correlationId`, a timeout loop, or a resend. The *mechanism* nonetheless cannot move into the tunnel for one case: when the **destination tunnel itself restarts**, its memory-only outbox and every in-flight `correlationId` die with it (v2 §4.1, §7.3; read-only root filesystem at the hub). At that moment the research container is the only process that still holds the query, so only it can resend — and the SDK already holds it, because the researcher's parameters are in memory inside the blocked `request()` call. Everything else stays with the tunnel: outbox re-encryption and retransmission across epoch changes, the relay's redelivery within an epoch, and cached-response replay at the source so a resend never re-runs the operation and never spends a second round of the Data Partner's budget. That last property is exactly why the resend must carry the **same** `correlationId` (ask T1): a fresh id would recompute at the source and double-count against `maxRounds`. Net: liveness **policy** (timeout, resend, give up) lives in the SDK as v2 §7.3 specifies, with defaults the researcher never touches; every **transport** recovery lives in the tunnel. If the tunnel team prefers, T1 can be shaped as the SDK minting the `correlationId` up front (idempotency-key style), so a resend is byte-identical to the first submit and the tunnel's route stays a plain idempotent POST.

**Still open:** A2 package names (due before Phase 7 publishes anything).

## Key reference files

- `../SafeInsights Enclave Fusion Architecture Doc-v2.md` — §4.1 local API table, §4.5 SDK contracts, §7.1/§7.3 round lifecycle, §12 logging
- `../fusion-rc-querys.md`; `../drawings/fusion/FusionWithSafeInsightsEnclave-technical-phases/phase6-analysis-rounds.md` — the round as the SDK sees it (two-party and hub)
- `../fusion-tunnel-app/.claude/plans/2026-07-09-implementation-plan.md` — §0.3 contract ownership, Phase 2 route semantics, Phase 5 caps/replay, Phase 8 terminal errors, Phase 9 env contract, Risk 12
- `../.claude/fusion-hub-topology-review-2026-09-16.md` — §2.3 guards, §2.4 per-leg SDK items, §3 SDK row, §11 sequencing
- `../fusion_security_review.md` §7.3 — caps, budget hints, cardinality declarations
- `../iac/management-app/codebuild/scripts/containerizer.ts` — why the SDK must be pre-installed or vendorable (crane mutate, no build step)
- `../management-app/tests/fixtures/code-samples/main.r`, `main.py`; `../setup-app/local/main.r` — how researcher code looks today (the `osenclave` pattern; `httr`/`requests` + `TRUSTED_OUTPUT_ENDPOINT`)
- `../management-app/src/database/types.ts` (`OrgCodeEnv`, `Language = 'PYTHON' | 'R'`) — the base-image and language model the SDK slots into
- `../trusted-output-app/.github/workflows/checks.yml`, `package.json` — CI shape and repo hygiene to mirror
