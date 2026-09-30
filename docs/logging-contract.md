# The content-free logging contract (for reviewers)

Architecture Doc v2 §12: logging is **event-level, never content-level**; a content-recording log would
itself be a leak outliving the study. This document states what the SDK guarantees so a Data Partner
reviewer can rely on it without reading the code.

## What the SDK logs

One line per event, `fusion <event> key=value …`, with a **closed field vocabulary** listed in
`spec/log-events.md`. The field names are enforced in code: an event carrying a field outside the
vocabulary raises an error in the SDK's own tests. The allowed fields are identifiers and measurements
only: peer slug, leg id, operation *name*, `correlationId`, `messageId`, byte counts, durations,
attempt counters, state and error *codes*, guard *names and limits*, budget counters.

## What the SDK never logs

- Query parameters, response bodies, table cells, or any value derived from them.
- Bearer tokens or endpoints.
- Handler exception messages or tracebacks in event lines. The traceback goes to a separate
  local-only logger at `DEBUG` (`safeinsights_fusion.handler_trace` in Python; a `fusion-trace`
  message in R), and — per ADR 0005 — crosses to the destination inside the error envelope, where
  output review gates it like any result.
- The size of a group refused by `minGroupSize`. The refusal names the guard and the limit, not the
  observed count, so the small cell does not leak through the log or the error.

## How this is tested

Both packages run a **content-free log audit**: marker strings are placed in params, bodies and handler
error messages, logs are captured at `DEBUG`, and the test fails if any marker (or a bearer token)
appears in an event line. See `python/tests/test_log_audit.py`, `test_source.py`, and
`r/tests/testthat/test-destination.R` ("destination logs are content-free").

## What researchers add

Researcher code can log whatever it likes; the enclave's log posture (no log driver by default at the
hub, Data-Partner-controlled elsewhere) is outside the SDK. The examples print summaries and counts only.
`ctx.logger` (Python) is a child of the SDK logger for handler-side messages and is *not* content-checked:
treat it as your own log.

## Reading a study from the logs

`messageId` is the audit join key across the source's, destination's and relay's event logs (v2 §12);
`correlationId` groups a round's query and response. `round.complete` and `round.served` carry the
budget counters, so consumption against the approved caps can be read off the log without the tunnel's
status reports.
