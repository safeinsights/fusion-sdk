# Two-party fusion study, DESTINATION side (the researcher's analysis).
#
# `analysis(fusion)` is the whole study. Inside the enclave it connects through the env the Setup
# App injects; under the simulator it receives a simulated fusion object.
library(safeinsights.fusion)

# Person IDs the destination already holds (from its own data); the source answers about them.
my_person_ids <- c("p-001", "p-002", "p-003", "p-004", "p-005", "p-999")

analysis <- function(fusion) {
  peer <- fusion_peer(fusion) # the only peer of a two-party study
  counts <- fusion_request(peer, "counts_by_group", list(person_ids = my_person_ids, group_by = "grade"))
  by_grade <- fusion_as_data_frame(counts)
  cat("counts by grade:", paste(by_grade$grade, by_grade$n, sep = "=", collapse = ", "),
      "; budget rounds", counts$budget$rounds_used, "\n")

  # Round 2 depends on round 1: ask about the largest group only.
  largest <- by_grade$grade[which.max(by_grade$n)]
  ids_in_largest <- my_person_ids[seq_len(max(by_grade$n))] # in a real study: the IDs you know are in that grade
  mean <- tryCatch(
    fusion_request(peer, "mean_score", list(person_ids = I(ids_in_largest))),
    fusion_remote_error = function(e) {
      cat("source declined:", e$code, "\n") # e.g. GUARD_REFUSED: re-batch and retry
      stop(e)
    }
  )
  cat(sprintf("mean score in grade %s: %.2f over %d people\n", largest, mean$body$mean, mean$body$n))
  list(by_grade = by_grade, largest = largest, mean = mean$body$mean)
}

if (!exists("fusion_example_no_main")) {
  fusion <- fusion_connect()
  result <- analysis(fusion)
  fusion_complete(fusion) # explicit CLOSE; never implicit
  # Result release stays with your Data Partner package (e.g. osenclave::toa_results_upload()), not the SDK.
  print(result)
}
