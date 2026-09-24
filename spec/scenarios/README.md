# Scenarios

Fault-injection definitions for `tools/fake_tunnel_pair`, consumed by both test suites, the cross-language matrix and the nightly chaos job. Validated against `scenario.schema.json` by `tools/validate_spec.py`.

Rounds are numbered from 1 **per leg**, counting `POST /v1/request` submissions that mint a new `correlationId` (re-issues do not start a new round). Top-level `caps`, `guards`, `operations` and `faults` apply to every leg unless a leg overrides the same key.

| Key | Meaning |
| :-- | :-- |
| `readyDelayMs` | both tunnels of every leg report `RELAY_ATTACHED` and answer `503 NOT_READY` until this many ms after start, then `CHANNEL_UP` |
| `longpollMs` | hold time on the two long-poll routes (tests use short holds; the real tunnel uses 25 000) |
| `maxBodyBytes` | request bodies larger than this get `413 TOO_LARGE` (default 64 MiB) |
| `caps` | source-side caps metered at the source tunnel, by manifest name (`maxRounds`, `maxRoundsPerHour`, `maxResponsePlaintextBytesPerRound`, `maxCumulativeResponsePlaintextBytes`, `maxQueryPlaintextBytesPerRound`, `maxCumulativeQueryPlaintextBytes`); absent or `null` = unlimited. Breach → `LIMIT_EXCEEDED` on that leg with `detail: {cap, limit, observed}` |
| `guards`, `operations` | exposed verbatim on the source tunnel's `/v1/info` |
| `faults.backpressureOnRequest` | the listed `POST /v1/request` calls (counted per leg, all submissions including re-issues) get one `429 BACKPRESSURE` |
| `faults.backpressureOnRespond` | same for `POST /v1/messages` |
| `faults.notReadyOnRequest` | the listed `POST /v1/request` calls get one `503 NOT_READY` although the channel is up |
| `faults.serverErrorOnRequest` | the listed `POST /v1/request` calls get one `500 INTERNAL` |
| `faults.dropResponseRounds` | the source tunnel accepts and caches round *n*'s response but never delivers it; a re-issue with the same `correlationId` triggers the cached replay |
| `faults.forgetCorrelationRounds` | the destination tunnel "restarts" on round *n*: the first response poll returns `404 NOT_FOUND` and the in-flight entry is forgotten; the re-issue is accepted with the same id |
| `faults.redeliverRounds` | the source tunnel delivers round *n*'s query to the RC once more on the poll after its first delivery, whether or not a response arrived in between (RC-crash simulation); exercises the SDK memo |
| `faults.delayDeliveryMs` | `{ "<round>": ms }` — hold round *n*'s query at the source tunnel before it becomes deliverable |
| `faults.errorAfterRound` | after round *n* completes (response delivered), both tunnels of the leg go `SESSION_ERRORED`; `0` = immediately after `CHANNEL_UP` |

## Files

| Scenario | Exercises |
| :-- | :-- |
| `happy.json` | N rounds, one leg, no faults |
| `slow-ready.json` | `503` for 1.5 s before `CHANNEL_UP`; readiness wait and `ready.wait` logging |
| `never-ready.json` | never `CHANNEL_UP`; `NotReadyError` with a short `FUSION_READY_TIMEOUT_S` |
| `backpressure.json` | `429` on the first request and first response; invisible retry |
| `transient-errors.json` | one `503` and one `500` mid-study on submit; bounded retry |
| `timeout-reissue.json` | round 2's response is dropped; round timeout → re-issue → cached replay, handler not re-run |
| `restart-404.json` | destination tunnel forgets round 2; `404` → re-issue with the same id |
| `redeliver-query.json` | query 2 is delivered twice; the source memo replays without re-running the handler |
| `slow-source.json` | query 2 held 3 s; a per-request timeout shorter than that yields `RoundTimeoutError`, the peer stays usable |
| `limit-exceeded-rounds.json` | `maxRounds: 2`; round 3 → `LIMIT_EXCEEDED`, terminal on the leg |
| `limit-exceeded-bytes.json` | `maxResponsePlaintextBytesPerRound: 64`; a large response → `LIMIT_EXCEEDED` at `POST /v1/messages` |
| `session-errored.json` | `errorAfterRound: 1`; `SessionError` on both sides |
| `guards.json` | `guards` on `/v1/info`; the test's params breach them → `GUARD_REFUSED` |
| `operations-declared.json` | `operations` on `/v1/info` for the registration pre-flight |
| `hub-two-legs.json` | two legs; leg B has `maxRounds: 1` so B goes `LIMIT_EXCEEDED` while A continues; `complete()` fan-out |
| `hub-two-legs-open.json` | two legs, no caps; the `examples/hub` run |
| `hub-three-legs.json` | three legs, no faults; peers keyed by `peerOrgSlug` |

Handler-side behaviors (handler raises, unknown operation, bad params, `STUDY_COMPLETE`) need no fault: the tests drive them from the source handler set and `complete()`.
