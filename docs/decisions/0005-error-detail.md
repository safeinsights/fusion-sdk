# ADR 0005 — Handler errors carry the full traceback by default, with per-operation `sanitize`

**Status:** accepted for now 2026-09-17 (product owner, plan §11 Q7) · **Date recorded:** 2026-09-18

## Context

When a source handler raises, the SDK must not let the exception escape the serve loop (a deterministic bug would become a poison message: `MAX_DELIVERIES` redeliveries → dead-letter → session errored). It sends an error envelope instead. The question was how much of the exception crosses to the destination.

## Decision

1. `HANDLER_ERROR.message` carries the **full traceback** by default. The destination enclave is the approved channel; everything reaching it is gated by output review before release; error envelopes are metered against the source's response-byte caps like any response.
2. Operations may be registered with `sanitize = True`, in which case `message` is the exception class name plus its one-line message, and `detail.type` names the class. The full traceback always goes to the source's **local** log at DEBUG level and never to the content-free event line.
3. **Revisit triggers:** a Data Partner objects at agreement review, or traceback bytes become a material share of a study's response budget.

## Alternatives rejected

- **Sanitized by default.** Slows researcher debugging across the channel for little gain, since output review already gates release.
- **No error envelope (let the tunnel dead-letter).** Turns a handler bug into a session-fatal event for both parties.

## Consequences

- The source-side content-free log audit tests that the traceback appears only in the DEBUG local log, never in event lines.
- `ProtocolError` at the destination is raised when a message cannot be decoded, but the message is **still ACKed** so it is not redelivered.
