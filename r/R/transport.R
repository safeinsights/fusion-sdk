# The internal transport interface: everything destination and source logic need from a tunnel.
# tunnel_transport() implements it over HTTP (spec/local-api.md); the simulator implements it in-process.
# Nothing above this layer knows about URLs, tokens or status codes.

ready_state <- "CHANNEL_UP"
# Tunnel states that end the leg, mapped to the terminal code the SDK reports for them.
terminal_states <- c(CLOSING = "STUDY_COMPLETE", CLOSED = "STUDY_COMPLETE", ERRORED = "SESSION_ERRORED", LIMIT_EXCEEDED = "LIMIT_EXCEEDED")

# ---- budget ----------------------------------------------------------------------------------------

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

# list(cap, observed, limit) for every capped counter at or above `fraction` of its cap; caps carry the
# manifest names the tunnel uses in LIMIT_EXCEEDED details.
budget_near_limit <- function(b, fraction = 0.9) {
  out <- list()
  for (row in list(
    c("maxRounds", "rounds_used", "rounds_max"),
    c("maxCumulativeResponsePlaintextBytes", "response_bytes_used", "response_bytes_max"),
    c("maxCumulativeQueryPlaintextBytes", "query_bytes_used", "query_bytes_max"),
    c("maxRoundsPerHour", "rounds_per_hour_used", "rounds_per_hour_max")
  )) {
    used <- b[[row[2]]]
    limit <- b[[row[3]]]
    if (!is.null(used) && !is.null(limit) && limit > 0 && used >= fraction * limit) {
      out[[length(out) + 1]] <- list(cap = row[1], observed = used, limit = limit)
    }
  }
  out
}

# Warn once per capped counter that has passed 90 % of its cap.
log_budget <- function(peer, b) {
  if (is.null(b)) {
    return(invisible(NULL))
  }
  for (n in budget_near_limit(b)) fusion_log("budget.near_limit", "WARNING", peer = peer, cap = n$cap, limit = n$limit, observed = n$observed)
  invisible(NULL)
}

# ---- results ---------------------------------------------------------------------------------------

terminal_result <- function(body) {
  code <- body$code
  if (!is.character(code)) fusion_protocol_error("terminal body without a code")
  detail <- body$detail
  list(
    kind = "terminal", code = code, message = if (is.character(body$message)) body$message else "",
    detail = if (is_json_object(detail)) detail else NULL
  )
}

# Log `session.terminal` and build (not signal) the typed error for a terminal result. STUDY_COMPLETE is
# not an error and is handled by the callers before they get here.
terminal_failure <- function(t, peer) {
  fusion_log("session.terminal", "ERROR", peer = peer, code = t$code)
  fusion_terminal_condition(t$code, t$message, t$detail, peer = peer)
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

# ---- HTTP ------------------------------------------------------------------------------------------

invalid_json <- structure(list(), class = "fusion_invalid_json")

parse_json_body <- function(raw) {
  if (length(raw) == 0) {
    return(NULL)
  }
  text <- rawToChar(raw)
  Encoding(text) <- "UTF-8"
  tryCatch(jsonlite::fromJSON(text, simplifyVector = FALSE), error = function(e) invalid_json)
}

is_invalid_json <- function(x) inherits(x, "fusion_invalid_json")

is_terminal_body <- function(body) is.list(body) && isTRUE(body$terminal)

# Minimal HTTP client over curl: bearer auth, explicit per-call timeouts, JSON both ways, no proxies (the
# tunnel is a sidecar) and content-free errors. `request(method, path, body, timeout_s)` -> list(status, body).
fusion_http_client <- function(endpoint, token, default_timeout_s = 10) {
  endpoint <- sub("/+$", "", endpoint)
  request <- function(method, path, body = NULL, timeout_s = NULL) {
    timeout <- if (is.null(timeout_s)) default_timeout_s else timeout_s
    h <- curl::new_handle()
    headers <- c(Authorization = paste("Bearer", token), Accept = "application/json")
    curl::handle_setopt(h,
      customrequest = method, timeout_ms = as.integer(timeout * 1000),
      connecttimeout_ms = as.integer(min(timeout, 10) * 1000), noproxy = "*"
    )
    if (!is.null(body)) {
      data <- charToRaw(as.character(body))
      headers <- c(headers, `Content-Type` = "application/json; charset=utf-8")
      curl::handle_setopt(h, postfields = data, postfieldsize = length(data))
    } else if (method %in% c("POST", "PUT")) {
      curl::handle_setopt(h, postfields = raw(0), postfieldsize = 0L)
    }
    curl::handle_setheaders(h, .list = as.list(headers))
    res <- tryCatch(curl::curl_fetch_memory(paste0(endpoint, path), handle = h),
      error = function(e) NULL
    )
    if (is.null(res)) fusion_retryable("TRANSPORT")
    list(status = as.integer(res$status_code), body = parse_json_body(res$content))
  }
  list(endpoint = endpoint, request = request)
}

# Typed wrapper of the local API. Statuses are mapped once, here, per spec/errors.json.
tunnel_transport <- function(endpoint, token, peer, http_timeout_s = 10) {
  http <- fusion_http_client(endpoint, token, default_timeout_s = http_timeout_s)

  call <- function(method, path, body = NULL, timeout_s = NULL) {
    resp <- http$request(method, path, if (is.null(body)) NULL else encode_json(body), timeout_s = timeout_s)
    if (is_invalid_json(resp$body)) fusion_protocol_error(sprintf("%s %s: tunnel returned a non-JSON body (status %d)", method, path, resp$status), peer = peer)
    s <- resp$status
    code <- if (is_json_object(resp$body) && is.character(resp$body$code)) resp$body$code else "?"
    if (s == 401L) fusion_config_error("bearer token rejected by the tunnel (401)", peer = peer)
    if (s == 403L) fusion_config_error(sprintf("%s %s is not allowed for this role (403): check FUSION_ROLE", method, path), peer = peer)
    if (s == 409L) {
      cid <- if (is_json_object(resp$body) && is.character(resp$body$correlationId)) resp$body$correlationId else NULL
      fusion_concurrency_error(sprintf("%s %s: 409 CONFLICT (another round is in flight, or the round is unknown)", method, path),
        peer = peer, correlation_id = cid
      )
    }
    if (s %in% c(400L, 413L, 422L)) fusion_protocol_error(sprintf("%s %s: tunnel rejected the request (%d %s)", method, path, s, code), peer = peer)
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
    if (resp$status != 202L || !is.character(cid)) {
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

  # Best effort: one DELETE, any failure ignored (the round is already lost to the caller).
  abandon <- function(correlation_id) {
    tryCatch(call("DELETE", paste0("/v1/request/", correlation_id)), condition = function(e) NULL)
    invisible(NULL)
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
    if (resp$status != 202L) fusion_protocol_error(sprintf("POST /v1/messages returned status %d", resp$status), peer = peer)
    if (is_json_object(resp$body)) budget_from_json(resp$body$budget) else NULL
  }

  complete <- function() {
    resp <- call("POST", "/v1/complete", empty_object())
    if (is_terminal_body(resp$body)) {
      return(terminal_result(resp$body))
    }
    if (resp$status != 202L) fusion_protocol_error(sprintf("POST /v1/complete returned status %d", resp$status), peer = peer)
    NULL
  }

  structure(
    list(
      endpoint = endpoint, peer = peer, info = info, submit = submit, poll_response = poll_response, abandon = abandon,
      next_message = next_message, post_response = post_response, complete = complete
    ),
    class = "fusion_transport"
  )
}

# ---- retry and readiness -----------------------------------------------------------------------------

now_s <- function() as.numeric(proc.time()[["elapsed"]])

# Exponential backoff with full jitter, in seconds.
backoff_delay <- function(attempt, base_ms, max_ms) {
  cap <- min(max_ms, base_ms * 2^min(max(0, attempt), 30))
  stats::runif(1, 0, cap) / 1000
}

# Call `fn` until it stops signalling fusion_retryable or the `deadline` (now_s() clock; Inf = never) passes.
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
        t <- list(code = terminal_states[[state]], message = sprintf("tunnel is already %s", state), detail = NULL)
        stop(terminal_failure(t, label))
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
