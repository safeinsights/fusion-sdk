# The internal transport interface: everything destination and source logic need from a tunnel.
# tunnel_transport() implements it over HTTP (spec/local-api.md); the simulator implements it in-process.
# Nothing above this layer knows about URLs, tokens or status codes.

ready_state <- "CHANNEL_UP"
terminal_states <- c(CLOSED = "STUDY_COMPLETE", ERRORED = "SESSION_ERRORED", LIMIT_EXCEEDED = "LIMIT_EXCEEDED")

budget_from_json <- function(obj) {
  if (!is_json_object(obj)) {
    return(NULL)
  }
  g <- function(key) {
    v <- obj[[key]]
    if (is.numeric(v) && !is.logical(v) && length(v) == 1) as.numeric(v) else NULL
  }
  structure(list(
    rounds_used = g("roundsUsed"), rounds_max = g("roundsMax"),
    response_bytes_used = g("responseBytesUsed"), response_bytes_max = g("responseBytesMax"),
    query_bytes_used = g("queryBytesUsed"), query_bytes_max = g("queryBytesMax"),
    rounds_per_hour_used = g("roundsPerHourUsed"), rounds_per_hour_max = g("roundsPerHourMax")
  ), class = "fusion_budget")
}

#' @export
print.fusion_budget <- function(x, ...) {
  show <- function(label, used, max) sprintf("%s %s/%s", label, if (is.null(used)) "?" else format(used), if (is.null(max)) "unlimited" else format(max))
  cat(
    "<fusion_budget", show("rounds", x$rounds_used, x$rounds_max), show("responseBytes", x$response_bytes_used, x$response_bytes_max),
    show("queryBytes", x$query_bytes_used, x$query_bytes_max), ">\n"
  )
  invisible(x)
}

# list of c(name, used, max) for every capped counter at or above `fraction` of its cap.
budget_near_limit <- function(b, fraction = 0.9) {
  out <- list()
  for (pair in list(
    c("rounds", "rounds_used", "rounds_max"), c("responseBytes", "response_bytes_used", "response_bytes_max"),
    c("queryBytes", "query_bytes_used", "query_bytes_max"), c("roundsPerHour", "rounds_per_hour_used", "rounds_per_hour_max")
  )) {
    used <- b[[pair[2]]]
    cap <- b[[pair[3]]]
    if (!is.null(used) && !is.null(cap) && cap > 0 && used >= fraction * cap) out[[length(out) + 1]] <- list(name = pair[1], used = used, max = cap)
  }
  out
}

terminal_result <- function(body) {
  code <- body$code
  if (!is.character(code)) fusion_protocol_error("terminal body without a code")
  detail <- body$detail
  list(
    kind = "terminal", code = code, message = if (is.character(body$message)) body$message else "",
    detail = if (is_json_object(detail)) detail else NULL
  )
}

delivered_result <- function(body, path, peer) {
  if (!is_json_object(body) || !is.character(body$messageId) || !is.character(body$correlationId) || !"payload" %in% names(body)) {
    fusion_protocol_error(sprintf("%s: delivered message is not {messageId, correlationId, payload}", path), peer = peer)
  }
  list(
    kind = "delivered", message_id = body$messageId, correlation_id = body$correlationId, payload = body$payload,
    budget = budget_from_json(body$budget), received_at = if (is.character(body$receivedAt)) body$receivedAt else NULL
  )
}

api_major <- function(version) {
  head <- strsplit(version, ".", fixed = TRUE)[[1]][1]
  if (grepl("^[0-9]+$", head)) as.integer(head) else NA_integer_
}

# Typed wrapper of the local API. Statuses are mapped once, here, per spec/errors.json.
tunnel_transport <- function(endpoint, token, peer, http_timeout_s = 10) {
  http <- fusion_http_client(endpoint, token, default_timeout_s = http_timeout_s)

  call <- function(method, path, body = NULL, timeout_s = NULL) {
    resp <- tryCatch(http$request(method, path, if (is.null(body)) NULL else encode_json(body), timeout_s = timeout_s),
      fusion_transport_error = function(e) fusion_retryable("TRANSPORT")
    )
    if (is_invalid_json(resp$body)) fusion_protocol_error(sprintf("%s %s: tunnel returned a non-JSON body (status %d)", method, path, resp$status), peer = peer)
    s <- resp$status
    if (s == 401L) fusion_config_error("bearer token rejected by the tunnel (401)", peer = peer)
    if (s == 403L) fusion_config_error(sprintf("%s %s is not allowed for this role (403): check FUSION_ROLE", method, path), peer = peer)
    if (s == 409L) {
      fusion_concurrency_error("another round is in flight at the tunnel (409)",
        peer = peer,
        correlation_id = if (is_json_object(resp$body)) resp$body$correlationId else NULL
      )
    }
    if (s == 422L) fusion_protocol_error(sprintf("%s %s: tunnel rejected the request schema (422)", method, path), peer = peer)
    if (s == 429L) fusion_retryable("BACKPRESSURE")
    if (s == 503L) fusion_retryable("NOT_READY")
    if (s >= 500L) fusion_retryable("SERVER_ERROR")
    resp
  }

  info <- function() {
    resp <- call("GET", "/v1/info")
    b <- resp$body
    if (resp$status != 200L || !is_json_object(b)) fusion_protocol_error(sprintf("GET /v1/info returned status %d", resp$status), peer = peer)
    for (k in c("legId", "peerOrgSlug", "role", "state")) {
      if (!is.character(b[[k]])) fusion_protocol_error("GET /v1/info body is missing legId/peerOrgSlug/role/state", peer = peer)
    }
    list(
      api_version = if (is.character(b$apiVersion)) b$apiVersion else "0.0.0", leg_id = b$legId, peer_org_slug = b$peerOrgSlug,
      role = b$role, direction = if (is.character(b$direction)) b$direction else "", state = b$state,
      guards = if (is_json_object(b$guards)) b$guards else NULL, caps = if (is_json_object(b$caps)) b$caps else NULL,
      operations = if (is_json_array(b$operations)) b$operations else NULL
    )
  }

  submit <- function(payload, correlation_id = NULL) {
    body <- list(payload = payload)
    if (!is.null(correlation_id)) body$correlationId <- correlation_id
    resp <- call("POST", "/v1/request", body)
    if (is_terminal_body(resp$body)) {
      return(terminal_result(resp$body))
    }
    cid <- if (is_json_object(resp$body)) resp$body$correlationId else NULL
    if (!resp$status %in% c(200L, 202L) || !is.character(cid)) {
      fusion_protocol_error(sprintf("POST /v1/request returned status %d without a correlationId", resp$status), peer = peer)
    }
    cid
  }

  poll_response <- function(correlation_id, timeout_s) {
    path <- paste0("/v1/responses/", correlation_id)
    resp <- call("GET", path, timeout_s = timeout_s)
    if (resp$status == 204L) {
      return(list(kind = "empty"))
    }
    if (resp$status == 404L) fusion_unknown_correlation(correlation_id)
    if (is_terminal_body(resp$body)) {
      return(terminal_result(resp$body))
    }
    if (resp$status != 200L) fusion_protocol_error(sprintf("GET %s returned status %d", path, resp$status), peer = peer)
    delivered_result(resp$body, path, peer)
  }

  abandon <- function(correlation_id) {
    resp <- tryCatch(call("DELETE", paste0("/v1/request/", correlation_id)), fusion_retryable = function(e) NULL)
    !is.null(resp) && resp$status %in% c(200L, 202L, 204L)
  }

  next_message <- function(timeout_s) {
    resp <- call("GET", "/v1/messages/next", timeout_s = timeout_s)
    if (resp$status == 204L) {
      return(list(kind = "empty"))
    }
    if (is_terminal_body(resp$body)) {
      return(terminal_result(resp$body))
    }
    if (resp$status != 200L) fusion_protocol_error(sprintf("GET /v1/messages/next returned status %d", resp$status), peer = peer)
    delivered_result(resp$body, "/v1/messages/next", peer)
  }

  post_response <- function(in_reply_to, payload) {
    resp <- call("POST", "/v1/messages", list(inReplyTo = in_reply_to, payload = payload))
    if (is_terminal_body(resp$body)) {
      return(terminal_result(resp$body))
    }
    if (resp$status == 400L) fusion_protocol_error("POST /v1/messages: inReplyTo does not match a delivered query", peer = peer)
    if (!resp$status %in% c(200L, 202L)) fusion_protocol_error(sprintf("POST /v1/messages returned status %d", resp$status), peer = peer)
    if (is_json_object(resp$body)) budget_from_json(resp$body$budget) else NULL
  }

  ack <- function(message_id) {
    resp <- call("POST", paste0("/v1/messages/", message_id, "/ack"))
    if (resp$status == 404L) {
      fusion_log("ack.unknown", "WARNING", peer = peer, messageId = message_id)
      return(invisible(NULL))
    }
    if (is_terminal_body(resp$body)) {
      return(invisible(NULL))
    }
    if (!resp$status %in% c(200L, 202L, 204L)) fusion_protocol_error(sprintf("ack returned status %d", resp$status), peer = peer)
    invisible(NULL)
  }

  complete <- function() {
    resp <- call("POST", "/v1/complete", empty_object())
    if (is_terminal_body(resp$body)) {
      return(terminal_result(resp$body))
    }
    if (!resp$status %in% c(200L, 202L, 204L)) fusion_protocol_error(sprintf("POST /v1/complete returned status %d", resp$status), peer = peer)
    NULL
  }

  structure(
    list(
      endpoint = endpoint, peer = peer, info = info, submit = submit, poll_response = poll_response, abandon = abandon,
      next_message = next_message, post_response = post_response, ack = ack, complete = complete
    ),
    class = "fusion_transport"
  )
}

now_s <- function() as.numeric(proc.time()[["elapsed"]])

# Call `fn` until it stops signalling fusion_retryable or the `deadline` (now_s() clock) passes.
retry_until <- function(fn, deadline, settings, peer, what) {
  attempt <- 0L
  repeat {
    result <- tryCatch(list(ok = TRUE, value = fn()), fusion_retryable = function(e) list(ok = FALSE, code = e$code))
    if (result$ok) {
      return(result$value)
    }
    now <- now_s()
    if (now >= deadline) fusion_round_timeout_error(sprintf("%s: gave up retrying after %s", what, result$code), peer = peer)
    if (result$code == "BACKPRESSURE") {
      fusion_log("round.backpressure", "WARNING", peer = peer, attempt = attempt)
    } else {
      fusion_log("round.retry", "DEBUG", peer = peer, attempt = attempt, code = result$code)
    }
    Sys.sleep(min(backoff_delay(attempt, settings$retry_base_ms, settings$retry_max_ms), max(0, deadline - now)))
    attempt <- attempt + 1L
  }
}

# Poll GET /v1/info until CHANNEL_UP (content-free progress log), a terminal state, or the deadline.
wait_ready <- function(transport, deadline, settings, label) {
  start <- now_s()
  interval <- settings$ready_poll_s
  attempt <- 0L
  repeat {
    info <- tryCatch(transport$info(), fusion_retryable = function(e) NULL)
    state <- if (is.null(info)) "UNREACHABLE" else info$state
    if (!is.null(info)) {
      if (state == ready_state) {
        fusion_log("ready.ok", "INFO", peer = label, legId = info$leg_id, role = info$role, durationMs = round((now_s() - start) * 1000))
        return(info)
      }
      if (state %in% names(terminal_states)) {
        code <- terminal_states[[state]]
        fusion_log("session.terminal", "ERROR", peer = label, code = code)
        stop(fusion_terminal_condition(code, sprintf("tunnel is already %s", state), peer = label))
      }
    }
    fusion_log("ready.wait", "INFO", peer = label, state = state, attempt = attempt)
    now <- now_s()
    if (now >= deadline) {
      fusion_log("ready.timeout", "ERROR", peer = label, state = state, durationMs = round((now - start) * 1000))
      fusion_not_ready_error(sprintf("tunnel not CHANNEL_UP after %d s (state %s)", as.integer(now - start), state), peer = label)
    }
    Sys.sleep(min(interval, max(0, deadline - now)))
    interval <- min(interval * 2, settings$ready_poll_max_s)
    attempt <- attempt + 1L
  }
}
