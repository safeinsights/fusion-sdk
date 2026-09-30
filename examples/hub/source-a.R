# Hub fusion study, SOURCE A (Data Partner A, in R). Answers which of the requested people it knows.
library(sifusion)

known <- c("p-001", "p-002", "p-003", "p-004")

ops <- fusion_operations(
  enrolled_ids = fusion_operation(function(params, ctx) {
    ids <- unlist(params$person_ids)
    list(person_ids = I(ids[ids %in% known]))
  }, person_id_param = "person_ids", cardinality = "per-record")
)

if (!exists("fusion_example_no_main")) fusion_serve(ops)
