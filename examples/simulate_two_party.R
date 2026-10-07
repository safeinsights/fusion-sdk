# Run the two-party example end to end in this process (no tunnel): what the IDE does.
library(safeinsights.fusion)
here <- dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)[1])))
fusion_example_no_main <- TRUE
source(file.path(here, "two-party", "source.R"), local = TRUE)
source(file.path(here, "two-party", "destination.R"), local = TRUE)
result <- fusion_simulate(ops, analysis, guards = list(maxDistinctPersonIds = 5000, minGroupSize = 2))
cat("simulated result: largest grade", result$largest, "mean", result$mean, "\n")
quit(status = if (identical(result$by_grade$n, c(3L, 2L)) || identical(result$by_grade$n, c(2L, 3L))) 0 else 1)
