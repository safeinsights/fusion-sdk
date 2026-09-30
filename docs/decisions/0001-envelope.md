# ADR 0001 — The fusion envelope is SDK-owned, JSON, typed-columnar

**Status:** accepted 2026-09-17 (product owner, plan §11 Q1) · **Date recorded:** 2026-09-18

## Context

The tunnel carries an opaque payload (Architecture Doc v2 §4.1: "payloads on this API are plaintext … the Tunnel App encrypts before anything touches the relay"). What is inside is the only cross-language contract in the framework not owned by a TypeScript service: an R source must answer a Python destination and vice versa. Every byte is metered against Data-Partner-approved caps (security review §7.3), and both packages must stay pure-language with a minimal dependency set because Data Partner base images are assembled with `crane mutate` and the SDK is pre-installed or vendored (plan constraint 2).

## Decision

1. The envelope is versioned JSON (`v: 1`) with three shapes: `query`, `response/ok`, `response/error`, defined once in `spec/envelope.schema.json`.
2. Tabular bodies use the **fusion table** convention: `{"__table__": 1, "columns": [{name, type}], "rows": [[…]]}` with a closed type vocabulary (`string`, `integer`, `integer64`, `number`, `boolean`, `date`, `datetime`), null in any cell. Bodies are otherwise arbitrary JSON; the table is recognised only at the top level of `body`.
3. `encoding` is `json` in v1. `arrow` is reserved (not accepted) so v1.1 can add base64 Arrow IPC without changing the envelope shape.
4. Cell rules that both encoders and decoders follow are normative in `spec/fixtures/README.md`; the fixtures are the executable statement.

## Alternatives rejected

- **Apache Arrow IPC as the default.** Would add `pyarrow` / `arrow` as hard dependencies on every base image; both are large and compiled (constraint 2).
- **Records (array of objects).** Repeats every key per row; against per-study byte caps the overhead is material. Typed-columnar carries types, which records cannot.
- **CSV in a string.** No types, no nulls vs empty strings, quoting ambiguity across languages.

## Consequences

- R and Python must agree on every edge case; the 71 fixtures in `spec/fixtures/` are the parity gate (Phase 5).
- Integers beyond ±2^53 travel as `integer64` strings; without `bit64` R decodes them as character and says so in the docs.
- Datetimes are truncated to milliseconds on encode; sub-millisecond precision is not a supported use.
