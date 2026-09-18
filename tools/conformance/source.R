# Conformance source (R). See README.md for the operation contract.
src <- Sys.getenv("SIFUSION_SRC", "")
if (nzchar(src)) suppressMessages(pkgload::load_all(src, quiet = TRUE)) else suppressPackageStartupMessages(library(sifusion))

ops <- fusion_operations(
  counts_by_group = fusion_operation(function(params, ctx) {
    n <- length(params$person_ids)
    data.frame(grade = c("9", "10"), n = c(n, 2L * n + 1L), stringsAsFactors = FALSE)
  }, person_id_param = "person_ids", cardinality = "per-group"),
  total = fusion_operation(function(params, ctx) {
    list(total = 42L, correlation_id = ctx$correlation_id, peer = ctx$peer, operation = ctx$operation)
  }),
  echo = fusion_operation(function(params, ctx) params),
  table_types = fusion_operation(function(params, ctx) {
    fusion_table(data.frame(
      s = c("héllo ✓", NA),
      i = c(412L, NA),
      i64 = c("9007199254740993", NA),
      x = c(1.5, NA),
      b = c(TRUE, NA),
      d = as.Date(c("2024-02-29", NA)),
      t = as.POSIXct(c("2026-09-18 12:00:00.25", NA), tz = "UTC"),
      stringsAsFactors = FALSE
    ), types = c(i64 = "integer64"))
  }),
  boom = fusion_operation(function(params, ctx) stop("conformance boom"))
)
fusion_serve(ops)
quit(status = 0)
