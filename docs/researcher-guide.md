# Fusion SDK: researcher guide

A fusion study runs analysis code in two places at once. The **destination** research container asks
questions; each **source** research container (one per Data Partner) answers only the operations its
Data Partner approved. Your code never touches the transport: the SDK talks to the per-job Fusion Tunnel
App and hides retries and re-issues.

## Destination code

```python
from safeinsights_fusion import Fusion, RemoteError

with Fusion.connect() as fusion:                     # waits for every leg to be CHANNEL_UP
    peer = fusion.peer()                             # or fusion.peer("dp-a") in a hub study
    r = peer.request("counts_by_group", {"person_ids": ids, "group_by": "grade"})
    df = r.to_pandas()                               # r.body for the raw JSON; r.to_table() / r.to_records()
    print(r.budget)                                  # rounds and bytes used vs. the approved caps
    # ... more rounds; each may depend on the previous answer ...
# leaving the block cleanly sends CLOSE to every leg
```

```r
library(sifusion)
fusion <- fusion_connect()
peer <- fusion_peer(fusion)
r <- fusion_request(peer, "counts_by_group", list(person_ids = ids, group_by = "grade"))
df <- fusion_as_data_frame(r)
fusion_complete(fusion)
```

- One round at a time **per peer**; different peers may be in flight together.
- A round returns a `Response` or raises a typed error (`spec/errors.md`). After `LimitExceededError`
  or `SessionError` that peer is finished; `complete()` still closes the others.
- `RemoteError` means the source declined (`UNKNOWN_OPERATION`, `BAD_PARAMS`, `HANDLER_ERROR`,
  `GUARD_REFUSED`): fix the query, re-batch, and retry.
- Budget hints arrive with every response; the SDK also logs `budget.near_limit` at 90 %.
- In R, a length-1 vector becomes a JSON scalar; wrap with `I()` (or use a list) when the operation
  expects an array: `person_ids = I("p-1")`.

## Source code

```python
from safeinsights_fusion import OperationRegistry, serve

ops = OperationRegistry()

@ops.register("counts_by_group", person_id_param="person_ids", cardinality="per-group", count_column="n")
def counts_by_group(params, ctx):
    ...
    return Table.from_columns({"grade": grades, "n": counts})   # or a pandas DataFrame, a dict, a list, a scalar

serve(ops)          # blocks until the destination completes the study
```

```r
ops <- fusion_operations(
  counts_by_group = fusion_operation(function(params, ctx) { ... },
                                     person_id_param = "person_ids", cardinality = "per-group",
                                     count_column = "n")
)
fusion_serve(ops)
```

- Handlers must be **idempotent per `correlationId`** (`docs/idempotent-operations.md`).
- Declare `person_id_param` and `cardinality` so the source-side guards can be enforced. A per-group
  operation must name its `count_column`: `minGroupSize` checks that column only, it must be an
  integer column, and the SDK never guesses it from column types. The Person-ID parameter
  must arrive as a flat array of scalars: an object or a nested array is refused as `GUARD_REFUSED`
  before the handler runs, so the destination should always send `{"person_ids": ["p1", "p2"]}`.
- A handler exception never crashes the loop; it crosses as `HANDLER_ERROR` with the traceback unless
  the operation is registered with `sanitize`.
- The source script should do nothing but `serve()` after setup: the loop blocks the process.

## Tables

Tabular answers travel as the **fusion table** (typed columns: `string`, `integer`, `integer64`,
`number`, `boolean`, `date`, `datetime`; `null` anywhere), so an R data frame and a pandas DataFrame
round-trip with types intact. Integers beyond 2^53 travel as `integer64` and decode to Python `int` /
R character. Datetimes are UTC with millisecond precision.

## Developing without a tunnel

The simulator runs your source handlers and destination analysis in one process with the same guards,
memo and error semantics: `simulate(ops, analysis, guards=…, faults=…)` in Python,
`fusion_simulate(ops, analysis, guards = …, faults = …)` in R. `examples/` shows complete templates for
the two-party and hub shapes and how to run them under the simulator.

## Checking a container

`python -m safeinsights_fusion doctor [--wait]` / `sifusion::fusion_doctor(wait = TRUE)` prints each
tunnel's leg, peer, role, state and API version (never the token) and exits non-zero on a mismatch.
