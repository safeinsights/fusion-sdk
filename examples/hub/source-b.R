# Hub fusion study, SOURCE B (Data Partner B, in R). Per-group outcomes and totals.
library(safeinsights.fusion)

sample_data <- data.frame(
  person_id = c("p-001", "p-002", "p-003", "p-004", "p-005"),
  cohort = c("2024", "2024", "2025", "2025", "2025"),
  outcome = c(1L, 0L, 1L, 1L, 0L),
  stringsAsFactors = FALSE
)

ops <- fusion_operations(
  outcomes_by_group = fusion_operation(function(params, ctx) {
    rows <- sample_data[sample_data$person_id %in% unlist(params$person_ids), ]
    agg <- aggregate(outcome ~ cohort, data = rows, FUN = function(x) c(n = length(x), rate = mean(x)))
    data.frame(cohort = agg$cohort, n = as.integer(agg$outcome[, "n"]), positive_rate = agg$outcome[, "rate"], stringsAsFactors = FALSE)
  }, person_id_param = "person_ids", cardinality = "per-group", count_column = "n"),
  totals = fusion_operation(function(params, ctx) {
    list(total = sum(sample_data$person_id %in% unlist(params$person_ids)))
  }, person_id_param = "person_ids", cardinality = "aggregate")
)

if (!exists("fusion_example_no_main")) fusion_serve(ops)
