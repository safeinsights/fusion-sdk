# Run the hub example in this process with two simulated Data Partners; leg B is capped at one round.
library(safeinsights.fusion)
here <- dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)[1])))
fusion_example_no_main <- TRUE
env_a <- new.env()
env_a$fusion_example_no_main <- TRUE
sys.source(file.path(here, "hub", "source-a.R"), envir = env_a)
env_b <- new.env()
env_b$fusion_example_no_main <- TRUE
sys.source(file.path(here, "hub", "source-b.R"), envir = env_b)
source(file.path(here, "hub", "destination.R"), local = TRUE)
result <- fusion_simulate(
  list(`dp-a` = env_a$ops, `dp-b` = env_b$ops), analysis,
  guards = list(maxDistinctPersonIds = 100, minGroupSize = 2),
  faults = list(`dp-b` = fusion_sim_faults(max_rounds = 1))
)
str(result)
quit(status = if (result$known_by_a == 4 && result$b_groups == 2 && is.null(result$b_total)) 0 else 1)
