# sifusion

The SafeInsights **Fusion SDK** for R. Imports: `curl`, `jsonlite` only.

```r
# destination
fusion <- fusion_connect()
a <- fusion_peer(fusion, "dp-a")
r <- fusion_request(a, "counts_by_group", list(person_ids = ids, group_by = "grade"))
df <- fusion_as_data_frame(r)
fusion_complete(fusion)

# source
ops <- fusion_operations(
  counts_by_group = fusion_operation(function(params, ctx) { ... },
                                     person_id_param = "person_ids", cardinality = "per-group",
                                     count_column = "n")
)
fusion_serve(ops)
```

Install from r-universe (`install.packages("sifusion", repos = "https://safeinsights.r-universe.dev")`) or `remotes::install_github("safeinsights/fusion-sdk", subdir = "r")`. See the repository README for the full contract. License: AGPL-3.0-or-later.
