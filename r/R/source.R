# Source side: operation registry and fusion_serve().
# A handler error never crashes the loop: it becomes a HANDLER_ERROR envelope (ADR 0005). A query the
# tunnel delivers again is answered from the in-memory memo without re-running the handler; guards run
# around every handler call (ADR 0002). Receiving a query is its acknowledgement.

#' Declare an operation handler
#'
#' @param handler A function `function(params, ctx)` returning a JSON-able value, a data frame,
#'   or a [fusion_table()]. `params` is the query's named list; `ctx` has `correlation_id`,
#'   `message_id`, `peer`, `operation`, `budget`.
#' @param person_id_param Name of the parameter whose distinct values `maxDistinctPersonIds` bounds.
#'   Its value must be absent, one scalar, or a flat array of scalars; other shapes are refused with
#'   `GUARD_REFUSED` before the handler runs.
#' @param cardinality One of `"aggregate"`, `"per-group"`, `"per-record"` (the manifest's class).
#' @param count_column For `per-group` operations, the result column holding group sizes; inferred
#'   as the single integer column when omitted.
#' @param sanitize If `TRUE`, a handler error crosses to the destination as class and one-line
#'   message only, instead of the full traceback (ADR 0005).
#' @return An object of class `fusion_operation`.
#' @export
fusion_operation <- function(handler, person_id_param = NULL, cardinality = "aggregate", count_column = NULL, sanitize = FALSE) {
  if (!is.function(handler)) stop("handler must be a function(params, ctx)")
  if (!cardinality %in% cardinalities) stop(sprintf("cardinality must be one of: %s", paste(cardinalities, collapse = ", ")))
  structure(list(
    handler = handler, person_id_param = person_id_param, cardinality = cardinality,
    count_column = count_column, sanitize = isTRUE(sanitize)
  ), class = "fusion_operation")
}

#' Build the operation registry
#'
#' @param ... Named [fusion_operation()] objects (bare functions are wrapped as aggregate operations).
#' @return An object of class `fusion_operations`.
#' @export
fusion_operations <- function(...) {
  ops <- list(...)
  if (length(ops) == 0 || is.null(names(ops)) || any(!nzchar(names(ops)))) stop("every operation needs a name")
  if (anyDuplicated(names(ops))) stop("operation names must be unique")
  for (n in names(ops)) {
    if (is.function(ops[[n]])) ops[[n]] <- fusion_operation(ops[[n]])
    if (!inherits(ops[[n]], "fusion_operation")) stop(sprintf("'%s' is not a fusion_operation", n))
    if (!grepl(operation_re, n)) stop(sprintf("operation name '%s' is invalid", n))
  }
  structure(ops, class = c("fusion_operations", "list"))
}

#' @export
print.fusion_operations <- function(x, ...) {
  cat(sprintf("<fusion_operations: %s>\n", paste(names(x), collapse = ", ")))
  invisible(x)
}

# LRU of correlationId -> response payload.
new_memo <- function(max_entries) {
  m <- new.env(parent = emptyenv())
  m$max <- max(1L, as.integer(max_entries))
  m$data <- list()
  m
}
memo_get <- function(m, cid) {
  v <- m$data[[cid]]
  if (!is.null(v)) {
    m$data[[cid]] <- NULL
    m$data[[cid]] <- v
  }
  v
}
memo_put <- function(m, cid, payload) {
  m$data[[cid]] <- NULL
  m$data[[cid]] <- payload
  while (length(m$data) > m$max) m$data[[1]] <- NULL
  invisible(NULL)
}

format_traceback <- function(e, calls) {
  lines <- c(sprintf("%s: %s", class(e)[1], conditionMessage(e)), "Traceback (most recent call last):")
  if (length(calls) > 0) {
    shown <- utils::tail(calls, 30)
    lines <- c(lines, vapply(shown, function(cl) paste0("  ", paste(deparse(cl, width.cutoff = 120)[1], collapse = "")), character(1)))
  }
  paste(lines, collapse = "\n")
}

# Run the handler, capturing the call stack at the error for the traceback.
run_handler <- function(spec, params, ctx) {
  calls <- NULL
  result <- tryCatch(
    withCallingHandlers(list(ok = TRUE, value = spec$handler(params, ctx)),
      error = function(e) calls <<- sys.calls()
    ),
    error = function(e) list(ok = FALSE, error = e, calls = calls)
  )
  result
}

new_server <- function(transport, operations, settings, label) {
  srv <- new.env(parent = emptyenv())
  srv$transport <- transport
  srv$operations <- operations
  srv$settings <- settings
  srv$label <- label
  srv$peer <- label
  srv$info <- NULL
  srv$guards <- NULL
  srv$memo <- new_memo(settings$memo_max_entries)
  srv$budget <- NULL
  srv$rounds_served <- 0L
  srv
}

server_start <- function(srv, deadline) {
  info <- wait_ready(srv$transport, deadline, srv$settings, srv$label)
  if (info$role != "source") fusion_config_error(sprintf("tunnel reports role '%s'; fusion_serve() is for a source", info$role), peer = srv$label)
  if (!identical(api_major(info$api_version), sdk_api_major)) {
    fusion_config_error(sprintf("tunnel speaks local API %s; this SDK requires major %d", info$api_version, sdk_api_major), peer = srv$label)
  }
  srv$info <- info
  srv$peer <- info$peer_org_slug
  srv$guards <- guards_from_info(info$guards)
  if (!guards_enabled(srv$guards)) {
    fusion_log("guards.disabled", "WARNING", peer = srv$peer)
  } else {
    fusion_log("guards.loaded", "INFO", peer = srv$peer, maxDistinctPersonIds = srv$guards$max_distinct_person_ids, minGroupSize = srv$guards$min_group_size)
  }
  problems <- preflight(srv$operations, info$operations)
  if (length(problems) > 0) {
    fusion_config_error(paste("operation registry does not match the approved list:", paste(problems, collapse = "; ")), peer = srv$peer)
  }
  fusion_log("serve.start", "INFO", peer = srv$peer, operations = names(srv$operations))
  info
}

server_run <- function(srv) {
  s <- srv$settings
  repeat {
    # The source has no round timeout: keep polling with capped backoff, forever.
    msg <- retry_until(function() srv$transport$next_message(s$poll_http_timeout_s), Inf, s, srv$peer, "poll")
    if (identical(msg$kind, "empty")) next
    if (identical(msg$kind, "terminal")) {
      server_ended(srv, msg)
      return(invisible(NULL))
    }
    if (identical(server_step(srv, msg), "STUDY_COMPLETE")) {
      return(invisible(NULL))
    }
  }
}

# The leg ended: STUDY_COMPLETE returns its code; anything else signals the typed error.
server_ended <- function(srv, t) {
  if (identical(t$code, "STUDY_COMPLETE")) {
    fusion_log("session.complete", "INFO", peer = srv$peer, roundsUsed = srv$rounds_served)
    return(t$code)
  }
  stop(terminal_failure(t, srv$peer))
}

server_step <- function(srv, msg) {
  started <- now_s()
  if (!is.null(msg$budget)) srv$budget <- msg$budget
  fusion_log("serve.received", "INFO", peer = srv$peer, correlationId = msg$correlation_id, messageId = msg$message_id, bytes = canonical_bytes(msg$payload))
  cached <- memo_get(srv$memo, msg$correlation_id)
  if (!is.null(cached)) {
    fusion_log("serve.memo_replay", "INFO", peer = srv$peer, correlationId = msg$correlation_id)
    return(server_respond(srv, msg, cached, started, NULL))
  }
  handled <- server_handle(srv, msg)
  server_respond(srv, msg, handled$payload, started, handled$operation)
}

refuse <- function(srv, msg, operation, e) {
  fusion_log("serve.guard_refused", "WARNING",
    peer = srv$peer, correlationId = msg$correlation_id, operation = operation,
    guard = e$guard, limit = e$limit, observed = e$observed
  )
  encode_response_error("GUARD_REFUSED", conditionMessage(e), guard_detail(e))
}

server_handle <- function(srv, msg) {
  env <- tryCatch(decode_envelope(msg$payload), fusion_envelope_error = function(e) e)
  if (inherits(env, "fusion_envelope_error")) {
    fusion_log("serve.bad_params", "WARNING", peer = srv$peer, correlationId = msg$correlation_id)
    reason <- conditionMessage(env)
    return(list(operation = NULL, payload = encode_response_error("BAD_PARAMS", paste("query envelope is invalid:", reason), list(reason = reason))))
  }
  if (!identical(env$kind, "query")) {
    fusion_log("serve.bad_params", "WARNING", peer = srv$peer, correlationId = msg$correlation_id)
    return(list(operation = NULL, payload = encode_response_error("BAD_PARAMS", "expected a query envelope", list(reason = "not a query"))))
  }
  op <- env$operation
  spec <- srv$operations[[op]]
  if (is.null(spec)) {
    fusion_log("serve.unknown_operation", "WARNING", peer = srv$peer, correlationId = msg$correlation_id, operation = op)
    text <- sprintf("no handler registered for operation '%s'", op)
    return(list(operation = op, payload = encode_response_error("UNKNOWN_OPERATION", text, list(operation = op))))
  }
  refused <- tryCatch(
    {
      check_query(env$params, spec, srv$guards)
      NULL
    },
    fusion_guard_refused = function(e) e
  )
  if (!is.null(refused)) {
    return(list(operation = op, payload = refuse(srv, msg, op, refused)))
  }
  ctx <- list(correlation_id = msg$correlation_id, message_id = msg$message_id, peer = srv$peer, operation = op, budget = srv$budget)
  t0 <- now_s()
  run <- run_handler(spec, env$params, ctx)
  body <- if (run$ok) tryCatch(list(ok = TRUE, value = encode_body(run$value)), error = function(e) list(ok = FALSE, error = e, calls = NULL)) else run
  if (!body$ok) {
    e <- body$error
    fusion_log("serve.handler_error", "ERROR", peer = srv$peer, correlationId = msg$correlation_id, operation = op, durationMs = round((now_s() - t0) * 1000))
    tb <- format_traceback(e, body$calls)
    if (.fusion_log$level <= 10L) message(sprintf("fusion-trace handler traceback correlationId=%s\n%s", msg$correlation_id, tb))
    message_text <- if (spec$sanitize) strsplit(sprintf("%s: %s", class(e)[1], conditionMessage(e)), "\n")[[1]][1] else tb
    return(list(operation = op, payload = encode_response_error("HANDLER_ERROR", message_text, list(type = class(e)[1]))))
  }
  refused <- tryCatch(
    {
      check_result(body$value, spec, srv$guards)
      NULL
    },
    fusion_guard_refused = function(e) e
  )
  if (!is.null(refused)) {
    return(list(operation = op, payload = refuse(srv, msg, op, refused)))
  }
  list(operation = op, payload = list(v = envelope_version, kind = "response", status = "ok", body = body$value, encoding = "json"))
}

server_respond <- function(srv, msg, payload, started, operation) {
  nbytes <- canonical_bytes(payload)
  if (nbytes > srv$settings$warn_bytes) fusion_log("envelope.large", "WARNING", peer = srv$peer, bytes = nbytes, limit = srv$settings$warn_bytes)
  deadline <- now_s() + max(30, srv$settings$round_timeout_s)
  result <- tryCatch(
    retry_until(function() srv$transport$post_response(msg$correlation_id, payload), deadline, srv$settings, srv$peer, "respond"),
    fusion_round_timeout_error = function(e) {
      # The tunnel was unreachable for too long. It still holds the query and delivers it again on a later
      # poll; the memo then answers it without re-running the handler.
      memo_put(srv$memo, msg$correlation_id, payload)
      list(kind = "dropped")
    },
    # 409: the tunnel no longer knows the round (it restarted, or the round was abandoned): nothing to memoise.
    fusion_concurrency_error = function(e) list(kind = "dropped")
  )
  if (is.list(result) && identical(result$kind, "dropped")) {
    fusion_log("round.protocol_error", "ERROR", peer = srv$peer, correlationId = msg$correlation_id, messageId = msg$message_id)
    return(NULL)
  }
  if (is.list(result) && identical(result$kind, "terminal")) {
    return(server_ended(srv, result))
  }
  if (inherits(result, "fusion_budget")) srv$budget <- result
  memo_put(srv$memo, msg$correlation_id, payload)
  srv$rounds_served <- srv$rounds_served + 1L
  fusion_log("round.served", "INFO",
    peer = srv$peer, correlationId = msg$correlation_id, operation = operation, bytes = nbytes,
    durationMs = round((now_s() - started) * 1000), roundsUsed = srv$budget$rounds_used, roundsMax = srv$budget$rounds_max
  )
  log_budget(srv$peer, srv$budget)
  NULL
}

#' Serve registered operations until the study completes
#'
#' Blocks. Returns invisibly on `STUDY_COMPLETE`; signals `fusion_limit_exceeded_error` /
#' `fusion_session_error` on a terminal error and `fusion_config_error` on a misconfigured
#' environment or registry. A source serves exactly one tunnel. Source scripts should do nothing
#' but `fusion_serve()` after setup: the long-poll loop blocks the R process by design.
#'
#' @param operations A [fusion_operations()] registry.
#' @param env Environment variables; defaults to [Sys.getenv()].
#' @param settings A [fusion_settings()] object.
#' @param ready_timeout Seconds to wait for `CHANNEL_UP`.
#' @param transport Advanced: a transport to use instead of the environment (simulator, tests).
#' @return `NULL`, invisibly.
#' @export
fusion_serve <- function(operations, env = Sys.getenv(), settings = NULL, ready_timeout = NULL, transport = NULL) {
  env <- as.list(env)
  s <- if (is.null(settings)) fusion_settings(env) else settings
  fusion_log_configure(s$log_level)
  if (!inherits(operations, "fusion_operations")) stop("operations must come from fusion_operations()")
  label <- "default"
  if (is.null(transport)) {
    if (read_role(env) != "source") fusion_config_error("fusion_serve() is for FUSION_ROLE=source; a destination calls fusion_connect()")
    tunnels <- read_tunnels(env)
    if (length(tunnels) != 1) fusion_config_error(sprintf("a source serves exactly one tunnel; %d configured", length(tunnels)))
    label <- tunnels[[1]]$label
    transport <- tunnel_transport(tunnels[[1]]$endpoint, tunnels[[1]]$token, label, s$http_timeout_s)
  }
  srv <- new_server(transport, operations, s, label)
  deadline <- now_s() + (if (is.null(ready_timeout)) s$ready_timeout_s else ready_timeout)
  server_start(srv, deadline)
  server_run(srv)
}
