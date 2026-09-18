# Hub fusion study, DESTINATION side: a SafeInsights-hosted enclave with two Data Partner sources.
#
# Peers are keyed by the Data Partner's organization slug. One round at a time per peer. The query
# to B is derived from A's answer: exactly the information path the source-side caps and guards bound.
library(sifusion)

my_person_ids <- c("p-001", "p-002", "p-003", "p-004", "p-005")

analysis <- function(fusion) {
  a <- fusion_peer(fusion, "dp-a")
  b <- fusion_peer(fusion, "dp-b")
  enrolled <- fusion_request(a, "enrolled_ids", list(person_ids = my_person_ids))
  ids <- unlist(enrolled$body$person_ids) # A tells us which of our people it knows
  cat("dp-a knows", length(ids), "of", length(my_person_ids), "; rounds used on A:", enrolled$budget$rounds_used, "\n")

  # The query to B depends on A's answer (bounded by B's query-side caps and guards).
  outcomes <- fusion_request(b, "outcomes_by_group", list(person_ids = I(ids), group_by = "cohort"))
  table <- fusion_as_data_frame(outcomes)
  print(table)

  b_total <- tryCatch(
    fusion_request(b, "totals", list(person_ids = I(ids)))$body$total,
    fusion_limit_exceeded_error = function(e) {
      # Leg B ended (a cap was reached); leg A is unaffected and fusion_complete() will still CLOSE it.
      cat("dp-b ended the leg:", e$cap, "\n")
      NULL
    }
  )
  list(known_by_a = length(ids), b_groups = nrow(table), b_total = b_total)
}

if (!exists("fusion_example_no_main")) {
  fusion <- fusion_connect()
  result <- analysis(fusion)
  print(fusion_complete(fusion)) # per-leg outcomes
  print(result)
}
