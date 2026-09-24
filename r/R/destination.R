# Destination side: fusion_connect(), one peer per leg, fusion_request() and fusion_complete().
# Liveness policy: ADR 0004. A round is submit -> long-poll; the delivered response is the acknowledgement.

sdk_api_major <- 2L

new_peer <- function(transport, info, settings, label) {
  p <- new.env(parent = emptyenv())
  p$transport <- transport
  p$settings <- settings
  p$label <- label
  p$name <- info$peer_org_slug
  p$leg_id <- info$leg_id
  p$direction <- info$direction
  p$state <- info$state
  p$in_flight <- FALSE
  p$terminal <- NULL
  p$budget <- NULL
  p$closed <- FALSE
  class(p) <- "fusion_peer"
  p
}

peer_state <- function(p) {
  if (!is.null(p$terminal)) {
    return(paste0("TERMINAL:", p$terminal$code))
  }
  if (p$closed) {
    return("CLOSED")
  }
  p$state
}

#' @export
print.fusion_peer <- function(x, ...) {
  cat(sprintf("<fusion_peer %s leg=%s state=%s>\n", x$name, x$leg_id, peer_state(x)))
  invisible(x)
}

check_usable <- function(p) {
  if (!is.null(p$terminal)) stop(p$terminal)
  if (p$closed) fusion_concurrency_error("fusion_complete() was already called on this peer", peer = p$name)
}

go_terminal <- function(p, t) {
  p$terminal <- terminal_failure(t, p$name)
  p$terminal
}

#' Connect to the study's tunnel(s) as the destination
#'
#' Reads the environment contract (`FUSION_ROLE`, `FUSION_TUNNEL_ENDPOINT(S)`,
#' `FUSION_TUNNEL_TOKEN(S)`), waits for every leg to reach `CHANNEL_UP`, and returns a handle
#' whose peers are keyed by the peer organization slug reported by each tunnel.
#'
#' @param env Environment variables; defaults to [Sys.getenv()].
#' @param settings A [fusion_settings()] object; defaults to reading the `FUSION_*` knobs from `env`.
#' @param ready_timeout Seconds to wait for `CHANNEL_UP` on all legs (default `FUSION_READY_TIMEOUT_S`).
#' @param transports Advanced: a named list of transports to use instead of the environment
#'   (the simulator and tests).
#' @return An object of class `fusion`.
#' @export
fusion_connect <- function(env = Sys.getenv(), settings = NULL, ready_timeout = NULL, transports = NULL) {
  env <- as.list(env)
  s <- if (is.null(settings)) fusion_settings(env) else settings
  fusion_log_configure(s$log_level)
  if (is.null(transports)) {
    if (read_role(env) != "destination") fusion_config_error("fusion_connect() is for FUSION_ROLE=destination; a source calls fusion_serve()")
    tunnels <- read_tunnels(env)
    transports <- lapply(tunnels, function(t) tunnel_transport(t$endpoint, t$token, t$label, s$http_timeout_s))
    names(transports) <- vapply(tunnels, function(t) t$label, character(1))
  }
  fusion_log("connect.start", "INFO", endpointCount = length(transports))
  deadline <- now_s() + (if (is.null(ready_timeout)) s$ready_timeout_s else ready_timeout)
  peers <- list()
  for (label in names(transports)) {
    info <- wait_ready(transports[[label]], deadline, s, label)
    if (info$role != "destination") fusion_config_error(sprintf("tunnel '%s' reports role '%s'; this process is a destination", label, info$role), peer = label)
    if (!identical(api_major(info$api_version), sdk_api_major)) {
      fusion_config_error(sprintf("tunnel '%s' speaks local API %s; this SDK requires major %d", label, info$api_version, sdk_api_major), peer = label)
    }
    if (!is.null(peers[[info$peer_org_slug]])) fusion_config_error(sprintf("two tunnels report the same peerOrgSlug '%s'", info$peer_org_slug))
    peers[[info$peer_org_slug]] <- new_peer(transports[[label]], info, s, label)
  }
  f <- new.env(parent = emptyenv())
  f$peers <- peers
  f$settings <- s
  f$completed <- FALSE
  class(f) <- "fusion"
  f
}

#' @export
print.fusion <- function(x, ...) {
  cat(sprintf("<fusion %d peer(s): %s>\n", length(x$peers), paste(names(x$peers), collapse = ", ")))
  invisible(x)
}

#' Get a peer handle
#'
#' @param fusion A `fusion` object from [fusion_connect()].
#' @param name The peer organization slug. With one leg it may be omitted.
#' @return A `fusion_peer` handle for [fusion_request()].
#' @export
fusion_peer <- function(fusion, name = NULL) {
  if (is.null(name)) {
    if (length(fusion$peers) != 1) stop(sprintf("this study has %d peers; name one of: %s", length(fusion$peers), paste(names(fusion$peers), collapse = ", ")))
    return(fusion$peers[[1]])
  }
  p <- fusion$peers[[name]]
  if (is.null(p)) stop(sprintf("unknown peer '%s'; known peers: %s", name, paste(names(fusion$peers), collapse = ", ")))
  p
}

#' List peers
#'
#' @param fusion A `fusion` object.
#' @return A data frame with columns `peer`, `legId`, `direction`, `state`.
#' @export
fusion_peers <- function(fusion) {
  data.frame(
    peer = vapply(fusion$peers, function(p) p$name, character(1)),
    legId = vapply(fusion$peers, function(p) p$leg_id, character(1)),
    direction = vapply(fusion$peers, function(p) p$direction, character(1)),
    state = vapply(fusion$peers, peer_state, character(1)),
    stringsAsFactors = FALSE, row.names = NULL
  )
}

#' Last budget hint seen on a peer
#'
#' @param peer A `fusion_peer`.
#' @return A `fusion_budget` (rounds and bytes used vs. max) or `NULL` before the first response.
#' @export
fusion_budget <- function(peer) peer$budget

#' @export
print.fusion_response <- function(x, ...) {
  cat(sprintf("<fusion_response %s from %s: %d bytes%s>\n", x$operation, x$peer, x$bytes, if (is_table_body(x$body)) ", fusion table" else ""))
  invisible(x)
}

#' Run one round: request an operation from a peer
#'
#' Blocks until the source's response arrives, a typed error is raised, or the round timeout
#' (with the SDK's same-`correlationId` re-issues) is exhausted. Researcher code never sees a
#' `correlationId` or a retransmission.
#'
#' @param peer A `fusion_peer` from [fusion_peer()] (or a `fusion` object with a single peer).
#' @param operation The Data-Partner-approved operation name.
#' @param params A named list of parameters. Use `I()` or `list()` to force a length-1 vector to
#'   travel as a JSON array (for example `person_ids = I("p-1")`).
#' @param timeout Per-attempt round timeout in seconds (default `FUSION_ROUND_TIMEOUT_S`).
#' @return A `fusion_response` with `$body` (raw JSON as nested lists), `$budget`, `$operation`,
#'   `$peer`, `$correlation_id`, `$message_id`, `$bytes`, `$duration_s`. Decode a table body with
#'   [fusion_as_data_frame()].
#' @export
fusion_request <- function(peer, operation, params = list(), timeout = NULL) {
  if (inherits(peer, "fusion")) peer <- fusion_peer(peer)
  if (!inherits(peer, "fusion_peer")) stop("first argument must be a fusion_peer")
  check_usable(peer)
  if (isTRUE(peer$in_flight)) fusion_concurrency_error("a request is already in flight on this peer", peer = peer$name)
  peer$in_flight <- TRUE
  on.exit(peer$in_flight <- FALSE, add = TRUE)
  request_locked(peer, operation, params, timeout)
}

request_locked <- function(p, operation, params, timeout) {
  s <- p$settings
  payload <- encode_query(operation, params)
  nbytes <- canonical_bytes(payload)
  if (nbytes > s$warn_bytes) fusion_log("envelope.large", "WARNING", peer = p$name, bytes = nbytes, limit = s$warn_bytes)
  fusion_log("round.start", "INFO", peer = p$name, operation = operation, bytes = nbytes)
  per_attempt <- if (is.null(timeout)) s$round_timeout_s else as.numeric(timeout)
  started <- now_s()
  cid <- NULL
  code <- NULL # why the previous attempt ended: TIMEOUT | UNKNOWN_CORRELATION
  for (reissue in seq.int(0L, s$round_max_reissues)) {
    if (!is.null(code)) fusion_log("round.reissue", "WARNING", peer = p$name, correlationId = cid, reissue = reissue, code = code)
    deadline <- now_s() + per_attempt
    cid <- submit_round(p, payload, cid, deadline)
    result <- poll_round(p, cid, deadline)
    if (is.list(result)) {
      return(finish_round(p, result, operation, started))
    }
    code <- result
  }
  fusion_log("round.timeout", "ERROR", peer = p$name, operation = operation, correlationId = cid, durationMs = round((now_s() - started) * 1000))
  p$transport$abandon(cid)
  fusion_round_timeout_error(sprintf("round did not complete within %g s after %d re-issue(s)", per_attempt, s$round_max_reissues),
    peer = p$name, correlation_id = cid, reissues = s$round_max_reissues
  )
}

submit_round <- function(p, payload, correlation_id, deadline) {
  result <- retry_until(function() p$transport$submit(payload, correlation_id), deadline, p$settings, p$name, "submit")
  if (is.list(result) && identical(result$kind, "terminal")) stop(go_terminal(p, result))
  result
}

# Long-poll until the response arrives (a delivered result), or the attempt ends with "TIMEOUT" or
# "UNKNOWN_CORRELATION".
poll_round <- function(p, cid, deadline) {
  s <- p$settings
  once <- function() p$transport$poll_response(cid, min(s$poll_http_timeout_s, max(deadline - now_s(), 1)))
  while (now_s() < deadline) {
    result <- tryCatch(retry_until(once, deadline, s, p$name, "poll"),
      fusion_round_timeout_error = function(e) "TIMEOUT",
      fusion_unknown_correlation = function(e) "UNKNOWN_CORRELATION"
    )
    if (is.character(result)) {
      return(result)
    }
    if (identical(result$kind, "terminal")) stop(go_terminal(p, result))
    if (identical(result$kind, "delivered")) {
      return(result)
    }
  }
  "TIMEOUT"
}

finish_round <- function(p, delivered, operation, started) {
  if (!is.null(delivered$budget)) p$budget <- delivered$budget
  env <- tryCatch(decode_envelope(delivered$payload), fusion_envelope_error = function(e) e)
  if (inherits(env, "fusion_envelope_error") || !identical(env$kind, "response")) {
    fusion_log("round.protocol_error", "ERROR", peer = p$name, correlationId = delivered$correlation_id, messageId = delivered$message_id)
    reason <- if (inherits(env, "condition")) conditionMessage(env) else "expected a response envelope, got a query"
    fusion_protocol_error(sprintf("response envelope is invalid: %s", reason), peer = p$name)
  }
  duration <- now_s() - started
  if (!is.null(env$error)) {
    fusion_log("round.remote_error", "WARNING", peer = p$name, operation = operation, correlationId = delivered$correlation_id, code = env$error$code)
    fusion_remote_error(env$error$code, env$error$message, env$error$detail, peer = p$name, operation = operation)
  }
  resp_bytes <- canonical_bytes(delivered$payload)
  fusion_log("round.complete", "INFO",
    peer = p$name, operation = operation, correlationId = delivered$correlation_id,
    messageId = delivered$message_id, bytes = resp_bytes, durationMs = round(duration * 1000),
    roundsUsed = p$budget$rounds_used, roundsMax = p$budget$rounds_max
  )
  log_budget(p$name, p$budget)
  structure(
    list(
      body = env$body, operation = operation, peer = p$name, correlation_id = delivered$correlation_id,
      message_id = delivered$message_id, budget = p$budget, bytes = resp_bytes, duration_s = duration
    ),
    class = "fusion_response"
  )
}

complete_peer <- function(p) {
  if (!is.null(p$terminal)) {
    return(p$terminal$code)
  }
  if (p$closed) {
    return("OK")
  }
  if (isTRUE(p$in_flight)) fusion_concurrency_error("cannot complete while a request is in flight", peer = p$name)
  deadline <- now_s() + max(10, p$settings$http_timeout_s * 3)
  t <- retry_until(function() p$transport$complete(), deadline, p$settings, p$name, "complete")
  if (!is.null(t) && !identical(t$code, "STUDY_COMPLETE")) {
    go_terminal(p, t)
    return(t$code)
  }
  p$closed <- TRUE
  "OK"
}

#' Finish the analysis: send CLOSE to every leg
#'
#' Every leg is attempted. Legs that already ended report their terminal code.
#'
#' @param fusion A `fusion` object.
#' @return A named character vector of per-leg outcomes: `"OK"`, a terminal code, or `"ERROR"`.
#'   If any leg failed with a non-terminal error, that error is raised after all legs were tried.
#' @export
fusion_complete <- function(fusion) {
  fusion_log("complete.start", "INFO", endpointCount = length(fusion$peers))
  results <- character(0)
  errors <- list()
  for (name in names(fusion$peers)) {
    outcome <- tryCatch(complete_peer(fusion$peers[[name]]), fusion_error = function(e) {
      errors[[length(errors) + 1]] <<- e
      "ERROR"
    })
    results[[name]] <- outcome
    fusion_log("complete.leg", "INFO", peer = name, code = outcome)
  }
  fusion$completed <- TRUE
  if (length(errors) > 0) stop(errors[[1]])
  results
}
