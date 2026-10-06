# Rprofile used by tools/run_examples.py when --r-src is given: make `library(safeinsights.fusion)` in the
# example scripts load the package from source, so the examples stay unchanged between dev and
# an image with the package installed.
local({
  src <- Sys.getenv("SAFEINSIGHTS_FUSION_SRC", "")
  if (nzchar(src) && requireNamespace("pkgload", quietly = TRUE)) {
    suppressMessages(pkgload::load_all(src, quiet = TRUE, export_all = FALSE))
    library <- function(package, ...) {
      pkg <- as.character(substitute(package))
      if (identical(pkg, "safeinsights.fusion")) return(invisible(TRUE))
      base::library(package, ..., character.only = TRUE)
    }
    assign("library", library, envir = globalenv())
  }
})
