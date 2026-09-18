# Minimal HTTP client over curl: bearer auth, explicit per-call timeouts, JSON both ways, no proxies
# (the tunnel is a sidecar), and content-free errors.

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

# `list(request = function(method, path, body = NULL, timeout_s = NULL))` returning list(status, body).
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
    if (is.null(res)) stop(structure(list(message = "TRANSPORT", call = NULL), class = c("fusion_transport_error", "condition")))
    list(status = as.integer(res$status_code), body = parse_json_body(res$content))
  }
  list(endpoint = endpoint, request = request)
}

is_terminal_body <- function(body) is.list(body) && isTRUE(body$terminal)

# Exponential backoff with full jitter, in seconds.
backoff_delay <- function(attempt, base_ms, max_ms) {
  cap <- min(max_ms, base_ms * 2^max(0, attempt))
  stats::runif(1, 0, cap) / 1000
}
