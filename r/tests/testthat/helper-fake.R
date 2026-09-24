# Drive tools/fake_tunnel_pair (Python, stdlib) from R tests, plus child R processes for the other side.

start_fake <- function(scenario, longpoll_ms = 200, ready_delay_ms = NULL, api_version = NULL) {
  skip_without_fake()
  root <- fusion_sdk_root()
  announce <- tempfile(fileext = ".json")
  args <- c("-m", "fake_tunnel_pair", "--scenario", scenario, "--longpoll-ms", longpoll_ms, "--announce", announce, "--no-stdin-watch")
  if (!is.null(ready_delay_ms)) args <- c(args, "--ready-delay-ms", ready_delay_ms)
  if (!is.null(api_version)) args <- c(args, "--api-version", api_version)
  system2(python_bin(), args, stdout = FALSE, stderr = FALSE, wait = FALSE, env = paste0("PYTHONPATH=", shQuote(file.path(root, "tools"))))
  for (i in 1:200) {
    if (file.exists(announce)) break
    Sys.sleep(0.05)
  }
  if (!file.exists(announce)) stop("fake tunnel pair did not announce")
  ann <- read_json_file(announce)
  structure(list(pid = ann$pid, legs = ann$legs, env = ann$env, scenario = scenario), class = "fake_pair")
}

stop_fake <- function(fake) {
  if (!is.null(fake$pid)) tools::pskill(fake$pid, tools::SIGTERM)
  invisible(NULL)
}

# Evaluate `code` with `fake` bound to a running fake pair; the pair is stopped afterwards.
with_fake <- function(scenario, code, ...) {
  fake <- start_fake(scenario, ...)
  on.exit(stop_fake(fake), add = TRUE)
  eval(substitute(code), envir = list(fake = fake), enclos = parent.frame())
}

# Env vector for a role; single-leg style when possible, map style otherwise (or when forced).
fake_env <- function(fake, role, style = "auto") {
  e <- fake$env[[role]]
  chosen <- if (style == "map" || (style == "auto" && is.null(e$single))) e$map else e$single
  unlist(chosen)
}

leg_endpoint <- function(fake, role, leg = 1) fake$legs[[leg]][[role]]

raw_call <- function(endpoint, token, method, path, body = NULL, timeout = 10) {
  h <- curl::new_handle()
  curl::handle_setopt(h, customrequest = method, timeout_ms = as.integer(timeout * 1000), noproxy = "*")
  headers <- list(Authorization = paste("Bearer", token))
  if (!is.null(body)) {
    data <- charToRaw(jsonlite::toJSON(body, auto_unbox = TRUE, null = "null", digits = NA))
    curl::handle_setopt(h, postfields = data, postfieldsize = length(data))
    headers[["Content-Type"]] <- "application/json"
  } else if (method == "POST") {
    curl::handle_setopt(h, postfields = raw(0), postfieldsize = 0L)
  }
  curl::handle_setheaders(h, .list = headers)
  res <- curl::curl_fetch_memory(paste0(endpoint, path), handle = h)
  list(status = res$status_code, body = if (length(res$content)) jsonlite::fromJSON(rawToChar(res$content), simplifyVector = FALSE) else NULL)
}

fake_snapshot <- function(fake, leg = 1) {
  ep <- leg_endpoint(fake, "destination", leg)
  raw_call(ep$endpoint, ep$token, "GET", "/_fake/snapshot")$body
}

fake_log_events <- function(fake, leg = 1) vapply(fake_snapshot(fake, leg)$log, function(e) e$event, character(1))

# ---- child processes --------------------------------------------------------------------------------

rscript_bin <- function() file.path(R.home("bin"), "Rscript")

# How a child process gets sifusion: the installed package (R CMD check, CI) or, when this process
# loaded it with pkgload::load_all(), the same source tree.
sifusion_loader_line <- function() {
  if (requireNamespace("pkgload", quietly = TRUE) && !is.null(pkgload::dev_meta("sifusion"))) {
    src_dir <- getNamespaceInfo(asNamespace("sifusion"), "path")
    return(sprintf("suppressMessages(pkgload::load_all(\"%s\", quiet = TRUE))", src_dir))
  }
  "suppressPackageStartupMessages(library(sifusion))"
}

# Run an R script in a child process; returns a handle with output and exit-code files.
start_child <- function(code, env = character(0)) {
  script <- tempfile(fileext = ".R")
  writeLines(c(sifusion_loader_line(), code), script)
  out <- tempfile(fileext = ".log")
  exit_file <- tempfile(fileext = ".exit")
  env <- c(env, R_LIBS = paste(.libPaths(), collapse = .Platform$path.sep), FUSION_SDK_ROOT = fusion_sdk_root())
  assignments <- paste(paste0(names(env), "=", shQuote(env)), collapse = " ")
  cmd <- sprintf(
    "%s %s %s > %s 2>&1; echo $? > %s",
    assignments, shQuote(rscript_bin()), shQuote(script), shQuote(out), shQuote(exit_file)
  )
  system2("sh", c("-c", shQuote(cmd)), wait = FALSE)
  structure(list(out = out, exit_file = exit_file, script = script), class = "child")
}

child_wait <- function(child, timeout = 30) {
  deadline <- Sys.time() + timeout
  while (!file.exists(child$exit_file) && Sys.time() < deadline) Sys.sleep(0.05)
  if (!file.exists(child$exit_file)) {
    return(NA_integer_)
  }
  as.integer(trimws(readLines(child$exit_file, warn = FALSE)[1]))
}

child_output <- function(child) if (file.exists(child$out)) paste(readLines(child$out, warn = FALSE), collapse = "\n") else ""

source_env <- function(fake, leg = 1, extra = character(0)) {
  ep <- leg_endpoint(fake, "source", leg)
  c(
    FUSION_ROLE = "source", FUSION_TUNNEL_ENDPOINT = ep$endpoint, FUSION_TUNNEL_TOKEN = ep$token,
    FUSION_READY_TIMEOUT_S = "20", FUSION_POLL_HTTP_TIMEOUT_S = "5", FUSION_HTTP_TIMEOUT_S = "5", FUSION_LOG_LEVEL = "DEBUG", extra
  )
}

# The default child source: an R fusion_serve() with a small registry. Handlers report a call counter.
default_ops_code <- '
calls <- 0L
ops <- fusion_operations(
  counts_by_group = fusion_operation(function(params, ctx) {
    calls <<- calls + 1L
    ids <- params$person_ids
    n <- if (is.null(ids)) 0L else length(ids)
    data.frame(grade = c("9", "10"), n = c(n, 2L * n + 1L), stringsAsFactors = FALSE)
  }, person_id_param = "person_ids", cardinality = "per-group"),
  total = fusion_operation(function(params, ctx) {
    calls <<- calls + 1L
    list(total = 42, calls = calls, correlation_id = ctx$correlation_id, peer = ctx$peer, operation = ctx$operation,
         pad = strrep("x", if (is.null(params$pad)) 0 else params$pad))
  }),
  boom = fusion_operation(function(params, ctx) { calls <<- calls + 1L; stop("division by zero MARKER_TRACE_11aa") }),
  boom_sanitized = fusion_operation(function(params, ctx) stop("bad value MARKER_SANITIZED_22bb"), sanitize = TRUE),
  unencodable = fusion_operation(function(params, ctx) list(fn = function() 1)),
  echo_table = fusion_operation(function(params, ctx) fusion_table(data.frame(k = c("a", "b"), v = c(1.5, 2.5))))
)
'

start_r_source <- function(fake, leg = 1, ops_code = default_ops_code, extra_env = character(0)) {
  start_child(c(ops_code, "fusion_serve(ops)"), env = source_env(fake, leg, extra_env))
}

# A raw-HTTP scripted source (no SDK) that answers every query with `reply_json` (a JSON string).
start_raw_source <- function(fake, reply_json, leg = 1) {
  ep <- leg_endpoint(fake, "source", leg)
  code <- sprintf('
endpoint <- "%s"; token <- "%s"
call <- function(method, path, body = NULL) {
  h <- curl::new_handle(); curl::handle_setopt(h, customrequest = method, timeout_ms = 15000L, noproxy = "*")
  headers <- list(Authorization = paste("Bearer", token))
  if (!is.null(body)) {
    d <- charToRaw(body)
    curl::handle_setopt(h, postfields = d, postfieldsize = length(d))
    headers[["Content-Type"]] <- "application/json"
  }
  else if (method == "POST") curl::handle_setopt(h, postfields = raw(0), postfieldsize = 0L)
  curl::handle_setheaders(h, .list = headers)
  res <- curl::curl_fetch_memory(paste0(endpoint, path), handle = h)
  list(status = res$status_code, body = if (length(res$content)) jsonlite::fromJSON(rawToChar(res$content), simplifyVector = FALSE) else NULL)
}
repeat {
  r <- tryCatch(call("GET", "/v1/messages/next"), error = function(e) NULL)
  if (is.null(r) || r$status == 204 || r$status == 503) next
  if (r$status != 200) break
  if (isTRUE(r$body$terminal)) break
  reply <- sprintf(\'{"inReplyTo": "%%s", "payload": %s}\', r$body$correlationId)
  for (i in 1:20) { p <- call("POST", "/v1/messages", reply); if (p$status != 429) break; Sys.sleep(0.05) }
}
', ep$endpoint, ep$token, reply_json)
  start_child(code)
}
