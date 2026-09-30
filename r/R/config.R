# Environment contract (spec/env.md) and tuning defaults (plan section 0.3, all PROVISIONAL).

#' Tuning settings
#'
#' Reads the `FUSION_*` knobs from an environment (default: the process environment). Every
#' default is provisional until load-tested with the real tunnel.
#'
#' @param env A named character vector of environment variables; defaults to [Sys.getenv()].
#' @return A list of class `fusion_settings`.
#' @export
fusion_settings <- function(env = Sys.getenv()) {
  env <- as.list(env)
  num <- function(name, default) {
    raw <- env[[name]]
    if (is.null(raw) || !nzchar(raw)) {
      return(default)
    }
    value <- suppressWarnings(as.numeric(raw))
    if (is.na(value)) fusion_config_error(sprintf("%s must be a number", name))
    if (value < 0) fusion_config_error(sprintf("%s must be non-negative", name))
    value
  }
  level <- env[["FUSION_LOG_LEVEL"]]
  structure(list(
    ready_timeout_s = num("FUSION_READY_TIMEOUT_S", 900),
    ready_poll_s = num("FUSION_READY_POLL_S", 2),
    ready_poll_max_s = 15,
    poll_http_timeout_s = num("FUSION_POLL_HTTP_TIMEOUT_S", 40),
    http_timeout_s = num("FUSION_HTTP_TIMEOUT_S", 10),
    round_timeout_s = num("FUSION_ROUND_TIMEOUT_S", 600),
    round_max_reissues = as.integer(num("FUSION_ROUND_MAX_REISSUES", 3)),
    retry_base_ms = num("FUSION_RETRY_BASE_MS", 250),
    retry_max_ms = num("FUSION_RETRY_MAX_MS", 10000),
    memo_max_entries = as.integer(num("FUSION_MEMO_MAX_ENTRIES", 256)),
    warn_bytes = num("FUSION_WARN_BYTES", 8 * 1024 * 1024),
    log_level = toupper(if (is.null(level) || !nzchar(level)) "INFO" else level)
  ), class = "fusion_settings")
}

# Merge user overrides into settings (tests and advanced callers).
fusion_settings_with <- function(settings, ...) {
  overrides <- list(...)
  # Iterate by position so a later duplicate name (tests pass `...` after defaults) wins.
  for (i in seq_along(overrides)) settings[[names(overrides)[i]]] <- overrides[[i]]
  settings
}

read_role <- function(env) {
  role <- tolower(trimws(if (is.null(env[["FUSION_ROLE"]])) "" else env[["FUSION_ROLE"]]))
  if (!role %in% c("source", "destination")) fusion_config_error("FUSION_ROLE must be 'source' or 'destination'")
  role
}

local_hosts <- c("localhost", "localhost.localdomain", "host.docker.internal")

is_private_ip <- function(host) {
  h <- gsub("^\\[|\\]$", "", host)
  if (grepl("^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$", h)) {
    o <- as.integer(strsplit(h, ".", fixed = TRUE)[[1]])
    private_v4 <- o[1] == 127 || o[1] == 10 || (o[1] == 172 && o[2] >= 16 && o[2] <= 31)
    return(private_v4 || (o[1] == 192 && o[2] == 168) || (o[1] == 169 && o[2] == 254))
  }
  if (grepl(":", h, fixed = TRUE)) {
    return(h == "::1" || grepl("^(fc|fd|fe8|fe9|fea|feb)", tolower(h)))
  }
  NA
}

validate_endpoint <- function(url, label = "") {
  where <- if (nzchar(label)) sprintf(" (%s)", label) else ""
  url <- trimws(url)
  m <- regmatches(url, regexec("^([a-z]+)://(\\[[0-9a-fA-F:]+\\]|[^/:@?#]+)(:[0-9]+)?(/.*)?$", url))[[1]]
  if (length(m) == 0 || m[2] != "http") {
    fusion_config_error(sprintf("tunnel endpoint%s must use http:// (the tunnel is an enclave-local sidecar)", where))
  }
  host <- m[3]
  path <- m[5]
  if (!path %in% c("", "/") || grepl("[?#@]", url)) {
    fusion_config_error(sprintf("tunnel endpoint%s must be http://host[:port] with no path, query or credentials", where))
  }
  private <- is_private_ip(host)
  if (isFALSE(private)) {
    fusion_config_error(sprintf("tunnel endpoint%s must be a loopback, private or link-local address", where))
  }
  if (is.na(private) && !tolower(host) %in% local_hosts && grepl(".", host, fixed = TRUE)) {
    fusion_config_error(sprintf("tunnel endpoint%s must be a bare service name, localhost, or a private IP", where))
  }
  sub("/+$", "", url)
}

# One TunnelConfig per leg: list(label, endpoint, token), in env order.
read_tunnels <- function(env) {
  get <- function(name) {
    v <- env[[name]]
    if (is.null(v) || !nzchar(v)) NULL else v
  }
  single_ep <- get("FUSION_TUNNEL_ENDPOINT")
  single_tok <- get("FUSION_TUNNEL_TOKEN")
  map_ep <- get("FUSION_TUNNEL_ENDPOINTS")
  map_tok <- get("FUSION_TUNNEL_TOKENS")
  if (!is.null(single_ep) && !is.null(map_ep)) fusion_config_error("set FUSION_TUNNEL_ENDPOINT or FUSION_TUNNEL_ENDPOINTS, not both")
  if (!is.null(single_ep)) {
    if (is.null(single_tok)) fusion_config_error("FUSION_TUNNEL_TOKEN is required with FUSION_TUNNEL_ENDPOINT")
    return(list(list(label = "default", endpoint = validate_endpoint(single_ep), token = single_tok)))
  }
  if (is.null(map_ep)) fusion_config_error("FUSION_TUNNEL_ENDPOINT or FUSION_TUNNEL_ENDPOINTS is required")
  if (is.null(map_tok)) fusion_config_error("FUSION_TUNNEL_TOKENS is required with FUSION_TUNNEL_ENDPOINTS")
  parse <- function(s) tryCatch(jsonlite::fromJSON(s, simplifyVector = FALSE), error = function(e) NULL)
  endpoints <- parse(map_ep)
  tokens <- parse(map_tok)
  if (is.null(endpoints) || is.null(tokens)) fusion_config_error("FUSION_TUNNEL_ENDPOINTS and FUSION_TUNNEL_TOKENS must be JSON objects")
  if (!is.list(endpoints) || !is.list(tokens) || length(endpoints) == 0 || is.null(names(endpoints)) || is.null(names(tokens))) {
    fusion_config_error("FUSION_TUNNEL_ENDPOINTS and FUSION_TUNNEL_TOKENS must be non-empty JSON objects")
  }
  if (!setequal(names(endpoints), names(tokens))) fusion_config_error("FUSION_TUNNEL_ENDPOINTS and FUSION_TUNNEL_TOKENS must have the same keys")
  lapply(names(endpoints), function(label) {
    ep <- endpoints[[label]]
    tok <- tokens[[label]]
    if (!is.character(ep) || !is.character(tok) || !nzchar(tok)) {
      fusion_config_error(sprintf("FUSION_TUNNEL_ENDPOINTS/TOKENS entry '%s' must be strings", label))
    }
    list(label = label, endpoint = validate_endpoint(ep, label), token = tok)
  })
}
