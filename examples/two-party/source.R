# Two-party fusion study, SOURCE side (Data Partner).
#
# Registers the operations the Data Partner approved at review and serves them until the
# destination completes the study. Replace the sample data with your Data Partner package's
# data access.
library(sifusion)

# Sample data standing in for the Data Partner's Person-ID -> record lookup.
sample_data <- data.frame(
  person_id = c("p-001", "p-002", "p-003", "p-004", "p-005"),
  grade = c("9", "9", "10", "10", "10"),
  score = c(71.0, 64.5, 88.0, 79.5, 91.0),
  stringsAsFactors = FALSE
)

ops <- fusion_operations(
  # How many of the requested people fall in each group. Pure function of params: idempotent.
  counts_by_group = fusion_operation(function(params, ctx) {
    group_by <- if (is.null(params$group_by)) "grade" else params$group_by
    rows <- sample_data[sample_data$person_id %in% unlist(params$person_ids), ]
    counts <- table(rows[[group_by]])
    out <- data.frame(group = names(counts), n = as.integer(counts), stringsAsFactors = FALSE)
    names(out)[1] <- group_by
    out
  }, person_id_param = "person_ids", cardinality = "per-group"),
  mean_score = fusion_operation(function(params, ctx) {
    scores <- sample_data$score[sample_data$person_id %in% unlist(params$person_ids)]
    list(n = length(scores), mean = if (length(scores)) mean(scores) else NULL)
  }, person_id_param = "person_ids", cardinality = "aggregate")
)

if (!exists("fusion_example_no_main")) fusion_serve(ops)  # returns when the destination completes the study
