# Content-free event logging (spec/log-events.md). One line per event: `fusion <event> key=value ...`.
# Field names are whitelisted so a params or body value can never reach a log line by accident.

.fusion_log <- new.env(parent = emptyenv())
.fusion_log$level <- 20L
.fusion_log$sink <- NULL

log_levels <- c(DEBUG = 10L, INFO = 20L, WARNING = 30L, WARN = 30L, ERROR = 40L)

# Exactly spec/log-events.md "Allowed fields" (a test checks).
allowed_log_fields <- c(
  "peer", "legId", "role", "operation", "operations", "correlationId", "messageId", "bytes", "durationMs",
  "attempt", "reissue", "state", "code", "guard", "limit", "observed", "cap", "roundsUsed", "roundsMax",
  "maxDistinctPersonIds", "minGroupSize", "endpointCount", "apiVersion"
)

fusion_log_configure <- function(level = "INFO") {
  lv <- log_levels[[toupper(level)]]
  .fusion_log$level <- if (is.null(lv)) 20L else lv
  invisible(NULL)
}

#' Redirect SDK log lines
#'
#' By default the SDK writes one `message()` per event to stderr. Tests and embedding
#' applications can capture the lines instead.
#'
#' @param sink A function of one character argument, or `NULL` to restore `message()`.
#' @return The previous sink, invisibly.
#' @export
fusion_log_sink <- function(sink = NULL) {
  old <- .fusion_log$sink
  .fusion_log$sink <- sink
  invisible(old)
}

fmt_log_value <- function(v) {
  if (is.null(v)) {
    return("null")
  }
  if (is.logical(v) && length(v) == 1) {
    return(if (isTRUE(v)) "true" else "false")
  }
  if (is.numeric(v) && length(v) == 1) {
    return(format(v, scientific = FALSE, trim = TRUE))
  }
  if (length(v) > 1) {
    return(paste(vapply(v, fmt_log_value, character(1)), collapse = ","))
  }
  text <- as.character(v)
  if (!nzchar(text) || grepl(" ", text, fixed = TRUE)) sprintf("\"%s\"", text) else text
}

fusion_log <- function(event, level = "INFO", ...) {
  fields <- list(...)
  bad <- setdiff(names(fields), allowed_log_fields)
  if (length(bad) > 0) stop(sprintf("log field(s) not in the content-free vocabulary: %s", paste(bad, collapse = ", ")))
  if (log_levels[[level]] < .fusion_log$level) {
    return(invisible(NULL))
  }
  fields <- fields[!vapply(fields, is.null, logical(1))]
  parts <- vapply(names(fields), function(k) sprintf("%s=%s", k, fmt_log_value(fields[[k]])), character(1))
  line <- paste(c("fusion", event, parts), collapse = " ")
  if (is.function(.fusion_log$sink)) .fusion_log$sink(line) else message(line)
  invisible(line)
}
