# safeinsights.fusion

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

Install from r-universe:

```r
install.packages("safeinsights.fusion",
  repos = c("https://safeinsights.r-universe.dev", "https://cloud.r-project.org"))
```

or from the source tarball attached to a GitHub release (`safeinsights.fusion_<version>.tar.gz`, pinned):

```r
install.packages("https://github.com/safeinsights/fusion-sdk/releases/download/v0.3.0/safeinsights.fusion_0.3.0.tar.gz",
  repos = NULL, type = "source")
```

or `remotes::install_github("safeinsights/fusion-sdk", subdir = "r")` for the development head. See the repository README for the full contract. License: AGPL-3.0-or-later.
