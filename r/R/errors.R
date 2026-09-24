# Error taxonomy (spec/errors.md). Condition classes mirror the Python classes one to one:
#   fusion_error > {fusion_config_error, fusion_not_ready_error, fusion_concurrency_error,
#                   fusion_round_timeout_error, fusion_remote_error, fusion_protocol_error,
#                   fusion_terminal_error > {fusion_limit_exceeded_error, fusion_session_error}}
# Every message is content-free: it may name a peer, an operation, a correlationId, sizes and
# durations, never params or bodies.

fusion_condition <- function(class, message, peer = NULL, ..., call = NULL) {
  classes <- c(
    class, if (class %in% c("fusion_limit_exceeded_error", "fusion_session_error")) "fusion_terminal_error",
    "fusion_error", "error", "condition"
  )
  full <- if (is.null(peer)) message else sprintf("[peer=%s] %s", peer, message)
  structure(
    c(list(message = full, call = call, peer = peer), list(...)),
    class = unique(classes)
  )
}

fusion_abort <- function(class, message, peer = NULL, ...) {
  stop(fusion_condition(class, message, peer = peer, ...))
}

fusion_config_error <- function(message, peer = NULL) fusion_abort("fusion_config_error", message, peer = peer)
fusion_not_ready_error <- function(message, peer = NULL) fusion_abort("fusion_not_ready_error", message, peer = peer)
# `correlation_id` names the in-flight round when the tunnel reported one on a 409.
fusion_concurrency_error <- function(message, peer = NULL, correlation_id = NULL) {
  fusion_abort("fusion_concurrency_error", message, peer = peer, correlation_id = correlation_id)
}
fusion_protocol_error <- function(message, peer = NULL) fusion_abort("fusion_protocol_error", message, peer = peer)
fusion_round_timeout_error <- function(message, peer = NULL, correlation_id = NULL, reissues = 0L) {
  fusion_abort("fusion_round_timeout_error", message, peer = peer, correlation_id = correlation_id, reissues = reissues)
}
fusion_remote_error <- function(code, message, detail = list(), peer = NULL, operation = NULL) {
  fusion_abort("fusion_remote_error", sprintf("%s: %s", code, message),
    peer = peer,
    code = code, remote_message = message, detail = detail, operation = operation
  )
}

# Build (not signal) the terminal error for a terminal body; STUDY_COMPLETE is handled by callers.
# `cap` is the manifest name of the breached cap (`detail$cap`) on LIMIT_EXCEEDED.
fusion_terminal_condition <- function(code, message = "", detail = NULL, peer = NULL) {
  class <- if (identical(code, "LIMIT_EXCEEDED")) "fusion_limit_exceeded_error" else "fusion_session_error"
  text <- if (nzchar(message)) sprintf("%s: %s", code, message) else code
  fusion_condition(class, text,
    peer = peer, code = code, detail = if (is.null(detail)) list() else detail,
    cap = if (is.list(detail)) detail$cap else NULL
  )
}

#' Is this condition terminal for its peer?
#'
#' @param e A condition object.
#' @return `TRUE` for `fusion_config_error`, `fusion_not_ready_error`, `fusion_limit_exceeded_error`
#'   and `fusion_session_error`; `FALSE` otherwise.
#' @export
fusion_is_terminal <- function(e) {
  inherits(e, c("fusion_terminal_error", "fusion_config_error", "fusion_not_ready_error"))
}

# Internal control-flow conditions (never reach researcher code).
fusion_retryable <- function(code) {
  stop(structure(list(message = code, call = NULL, code = code), class = c("fusion_retryable", "condition")))
}
fusion_unknown_correlation <- function(cid) {
  stop(structure(list(message = "UNKNOWN_CORRELATION", call = NULL, correlation_id = cid),
    class = c("fusion_unknown_correlation", "condition")
  ))
}
