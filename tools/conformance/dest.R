# Conformance destination (R). See README.md for the round script and exit codes.
src <- Sys.getenv("SIFUSION_SRC", "")
if (nzchar(src)) suppressMessages(pkgload::load_all(src, quiet = TRUE)) else suppressPackageStartupMessages(library(sifusion))

out <- function(...) cat(jsonlite::toJSON(list(...), auto_unbox = TRUE, null = "null"), "\n")
check <- function(cond, what) if (!isTRUE(cond)) stop(paste("assertion failed:", what), call. = FALSE)
expected_echo <- list(s = "héllo ✓", n = 3L, x = 0.25, b = TRUE, z = NULL, list = list("a", 1L, NULL), obj = list(k = "v"))

json_equal <- function(a, b) {
  if (is.null(a) || is.null(b)) return(is.null(a) && is.null(b))
  if (is.list(a) || is.list(b)) {
    if (!is.list(a) || !is.list(b) || length(a) != length(b) || !setequal(names(a), names(b))) return(FALSE)
    idx <- if (is.null(names(a))) seq_along(a) else names(a)
    return(all(vapply(idx, function(k) json_equal(a[[k]], b[[k]]), logical(1))))
  }
  if (is.logical(a) || is.logical(b)) return(is.logical(a) && is.logical(b) && identical(a, b))
  if (is.numeric(a) && is.numeric(b)) return(length(a) == length(b) && all(a == b))
  identical(a, b)
}

round_counts <- function(peer) {
  r <- fusion_request(peer, "counts_by_group", list(person_ids = c("p1", "p2", "p3"), group_by = "grade"))
  df <- fusion_as_data_frame(r)
  check(identical(unname(attr(df, "fusion_types")), c("string", "integer")), "counts_by_group column types")
  check(identical(df$grade, c("9", "10")) && identical(df$n, c(3L, 7L)), "counts_by_group rows")
}

round_total <- function(peer) {
  r <- fusion_request(peer, "total")
  check(r$body$total == 42 && identical(r$body$correlation_id, r$correlation_id) && identical(r$body$peer, peer$name), "total body")
}

round_echo <- function(peer) {
  r <- fusion_request(peer, "echo", expected_echo)
  check(json_equal(r$body, expected_echo), paste("echo body", jsonlite::toJSON(r$body, auto_unbox = TRUE, null = "null")))
}

round_table_types <- function(peer) {
  df <- fusion_as_data_frame(fusion_request(peer, "table_types"))
  check(identical(unname(attr(df, "fusion_types")), c("string", "integer", "integer64", "number", "boolean", "date", "datetime")), "table_types types")
  check(df$s[1] == "héllo ✓" && df$i[1] == 412L && df$i64[1] == "9007199254740993" && df$x[1] == 1.5 && isTRUE(df$b[1]), "table_types row 1")
  check(df$d[1] == as.Date("2024-02-29") && abs(as.numeric(df$t[1]) - as.numeric(as.POSIXct("2026-09-18 12:00:00.25", tz = "UTC"))) < 1e-3, "table_types dates")
  check(all(is.na(df[2, ])), "table_types null row")
}

round_errors <- function(peer) {
  e <- tryCatch(fusion_request(peer, "boom"), fusion_remote_error = function(e) e)
  check(inherits(e, "fusion_remote_error") && e$code == "HANDLER_ERROR" && grepl("conformance boom", e$remote_message), "boom")
  e <- tryCatch(fusion_request(peer, "nope"), fusion_remote_error = function(e) e)
  check(inherits(e, "fusion_remote_error") && e$code == "UNKNOWN_OPERATION", "nope")
}

two_party <- function(fusion, rounds) {
  peer <- fusion_peer(fusion)
  for (i in seq_len(rounds)) {
    round_counts(peer)
    round_total(peer)
    round_echo(peer)
    round_table_types(peer)
    out(event = "round", i = i - 1L, ok = TRUE)
  }
  round_errors(peer)
  outcomes <- fusion_complete(fusion)
  check(identical(unname(outcomes), "OK"), "complete")
  out(event = "complete", outcomes = as.list(outcomes))
}

hub <- function(fusion) {
  a <- fusion_peer(fusion, "dp-a")
  b <- fusion_peer(fusion, "dp-b")
  ra <- fusion_request(a, "total")
  rb <- fusion_request(b, "echo", list(derived_from_a = ra$body$total))
  check(json_equal(rb$body, list(derived_from_a = 42L)), "hub echo")
  e <- tryCatch(fusion_request(b, "total"), fusion_terminal_error = function(e) e)
  check(inherits(e, "fusion_limit_exceeded_error"), "leg B capped")
  round_counts(a)
  round_table_types(a)
  outcomes <- fusion_complete(fusion)
  check(identical(outcomes, c(`dp-a` = "OK", `dp-b` = "LIMIT_EXCEEDED")), "complete fan-out")
  out(event = "complete", outcomes = as.list(outcomes))
}

chaos <- function(fusion, rounds) {
  peer <- fusion_peer(fusion)
  timeouts <- 0L
  for (i in seq_len(rounds)) {
    ok <- tryCatch({ round_total(peer); TRUE }, fusion_round_timeout_error = function(e) FALSE)
    if (!ok) timeouts <- timeouts + 1L
    out(event = "round", i = i - 1L, ok = ok)
  }
  outcomes <- fusion_complete(fusion)
  out(event = "complete", outcomes = as.list(outcomes), timeouts = timeouts)
}

mode <- Sys.getenv("CONF_MODE", "two-party")
rounds <- as.integer(Sys.getenv("CONF_ROUNDS", "2"))
status <- tryCatch({
  fusion <- fusion_connect()
  if (mode == "hub") hub(fusion) else if (mode == "chaos") chaos(fusion, rounds) else two_party(fusion, rounds)
  0L
}, fusion_terminal_error = function(e) {
  out(event = "terminal", code = e$code, peer = e$peer)
  3L
}, error = function(e) {
  out(event = "error", type = class(e)[1], message = conditionMessage(e))
  1L
})
quit(status = status)
