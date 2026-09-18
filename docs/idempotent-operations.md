# Writing idempotent operations

Architecture Doc v2 §4.5 makes idempotency a contract the SDK enforces and the review process checks:
*source-side operations must be idempotent per `correlationId`*. This guide says what that means for a
handler and what the SDK already does for you.

## Why a handler can run twice

A round can be redelivered to your handler when the destination re-issues it after a timeout, when a
tunnel restarts mid-round, or when your research container crashes after acknowledging the query but
before answering it (v2 §8). The SDK removes almost all of these cases before they reach you:

| Situation | Who handles it | Handler runs again? |
| :-- | :-- | :-- |
| Destination re-issues the same `correlationId` after a timeout | source **tunnel** replays its cached response | no |
| Query redelivered to the RC after the SDK already answered it | SDK **memo** (`correlationId → response`, in memory) replays | no |
| RC process restarted between ACK and response | nobody can replay: the memo died with the process | **yes** |

The last row is why the contract exists. The SDK hands your handler `ctx.correlation_id`
(`ctx$correlation_id` in R) so you can make the rerun harmless.

## Rules

1. **Prefer pure functions.** A handler that only reads data and returns a value is idempotent by
   construction. Every operation in `examples/` is pure.
2. **Key side effects by `correlation_id`.** If an operation must write (a scratch table, a cache, an
   audit row), use the `correlation_id` as the key and make the write an upsert. A second run then
   overwrites the same row instead of adding one.
3. **Never count rounds inside the handler.** Budget accounting belongs to the tunnel; the SDK exposes
   the tunnel's counters as `ctx.budget` / `ctx$budget`. A handler that increments its own counter
   double-counts on a rerun.
4. **Do not depend on wall-clock time or randomness** for the result. Two runs of the same query must
   produce the same answer, or the destination may see two different answers for one round.
5. **Let exceptions propagate.** Do not catch errors to return a partial result: the SDK turns the
   exception into a `HANDLER_ERROR` envelope, the round completes, and the destination decides what to
   do. A partial result would cross the channel as if it were the answer.
6. **Return the whole answer in one value.** Streaming or chunking at the handler level is not
   supported; the tunnel chunks below the SDK. Throughput comes from batching within a query.

## Guards and idempotency

Guards (`maxDistinctPersonIds`, `minGroupSize`) run around the handler on every delivery, so a query
refused once is refused identically on a rerun, and a rerun after a refusal never spends a round
(the refusal envelope is what the memo replays).

## Testing it

The simulator injects the redelivery cases so you can prove a handler is idempotent without a tunnel:

```python
from safeinsights_fusion import SimFaults, simulate
simulate(ops, analysis, faults=SimFaults(drop_response_rounds={1}))   # round 1 is answered from the memo on re-issue
```

```r
fusion_simulate(ops, analysis, faults = fusion_sim_faults(drop_response_rounds = 1))
```

Count your handler's invocations in the test; it should equal the number of *distinct* rounds.
