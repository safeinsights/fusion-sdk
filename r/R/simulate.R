# In-process simulator for the SafeInsights IDE / CRATE.
#
# An implementation of the same internal transport interface `tunnel_transport()` provides, so
# fusion_connect(), fusion_request() and the source server run their real code paths (envelopes,
# guards, memo, error envelopes, budget hints, typed errors) with no tunnel and no HTTP. It is
# synchronous: a destination submit() runs the source pipeline inline, which is what makes it work
# in single-threaded R.

sim_api_version <- "2.0.0"

#' Fault injection for [fusion_simulate()]
#'
#' Rounds are numbered from 1 per peer, as in the fake tunnel scenarios.
#'
#' @param drop_response_rounds Rounds whose response is withheld until the SDK re-issues the same
#'   `correlationId` (exercises round-timeout handling with a short `timeout`).
#' @param error_after_round The leg becomes `SESSION_ERRORED` after this round completes.
#' @param max_rounds Cap on rounds enforced by the simulated source tunnel (`LIMIT_EXCEEDED`).
#' @param max_response_bytes_per_round Cap on response bytes per round (`LIMIT_EXCEEDED` with
#'   `cap = "maxResponsePlaintextBytesPerRound"`).
#' @return An object of class `fusion_sim_faults`.
#' @export
fusion_sim_faults <- function(drop_response_rounds = integer(0), error_after_round = NULL, max_rounds = NULL,
                              max_response_bytes_per_round = NULL) {
  structure(list(drop_response_rounds = as.integer(drop_response_rounds), error_after_round = error_after_round,
                 max_rounds = max_rounds, max_response_bytes_per_round = max_response_bytes_per_round),
            class = "fusion_sim_faults")
}

new_sim_leg <- function(peer, leg_id, guards, caps, approved, faults) {
  leg <- new.env(parent = emptyenv())
  leg$peer <- peer
  leg$leg_id <- leg_id
  leg$guards <- guards
  leg$caps <- caps
  leg$approved <- approved
  leg$faults <- faults
  leg$rounds <- list()
  leg$round_counter <- 0L
  leg$rounds_used <- 0L
  leg$response_bytes_used <- 0
  leg$query_bytes_used <- 0
  leg$terminal <- NULL
  leg$closed <- FALSE
  leg
}

sim_budget <- function(leg) {
  structure(list(rounds_used = leg$rounds_used, rounds_max = leg$faults$max_rounds, response_bytes_used = leg$response_bytes_used,
                 response_bytes_max = NULL, query_bytes_used = leg$query_bytes_used, query_bytes_max = NULL,
                 rounds_per_hour_used = NULL, rounds_per_hour_max = NULL), class = "fusion_budget")
}

sim_info <- function(leg, role) {
  state <- ready_state
  if (leg$closed) {
    state <- "CLOSED"
  } else if (!is.null(leg$terminal)) {
    state <- if (leg$terminal$code == "LIMIT_EXCEEDED") "LIMIT_EXCEEDED" else "ERRORED"
  }
  list(api_version = sim_api_version, leg_id = leg$leg_id, peer_org_slug = leg$peer, role = role, direction = "dst_to_src",
       state = state, guards = if (role == "source") leg$guards else NULL, caps = if (role == "source") leg$caps else NULL,
       operations = if (role == "source") leg$approved else NULL)
}

sim_terminal <- function(leg, code, message, detail = NULL) {
  leg$terminal <- list(kind = "terminal", code = code, message = message, detail = detail)
  leg$terminal
}

# Every payload crosses the simulated wire as JSON text, exactly as it would through the tunnel.
sim_wire <- function(x) decode_json(encode_json(x))

# What the simulated source server sees.
sim_source_view <- function(leg) {
  force(leg) # closures below must bind this leg, not the caller's loop variable (lazy evaluation)
  structure(list(
    endpoint = "sim", peer = leg$peer,
    info = function() sim_info(leg, "source"),
    submit = function(payload, correlation_id = NULL) stop("a source never submits"),
    poll_response = function(correlation_id, timeout_s) stop("a source never polls responses"),
    abandon = function(correlation_id) stop("a source never abandons"),
    next_message = function(timeout_s) if (leg$closed) list(kind = "terminal", code = "STUDY_COMPLETE", message = "", detail = NULL) else list(kind = "empty"),
    post_response = function(in_reply_to, payload) {
      rnd <- leg$rounds[[in_reply_to]]
      if (!is.null(rnd$response_message_id)) return(sim_budget(leg))
      nbytes <- canonical_bytes(payload)
      cap <- leg$faults$max_response_bytes_per_round
      if (!is.null(cap) && nbytes > cap) {
        return(sim_terminal(leg, "LIMIT_EXCEEDED", "response exceeds maxResponsePlaintextBytesPerRound",
                            list(cap = "maxResponsePlaintextBytesPerRound", limit = cap, observed = nbytes)))
      }
      rnd$response <- sim_wire(payload)
      rnd$response_message_id <- sim_id()
      rnd$deliverable <- !(rnd$round_no %in% leg$faults$drop_response_rounds)
      leg$rounds[[in_reply_to]] <- rnd
      leg$response_bytes_used <- leg$response_bytes_used + nbytes
      leg$rounds_used <- leg$rounds_used + 1L
      sim_budget(leg)
    },
    complete = function() stop("a source never completes")
  ), class = "fusion_transport")
}

sim_id <- function() paste(sprintf("%08x", sample.int(.Machine$integer.max, 4)), collapse = "")

# What the destination peer sees. submit() runs the source pipeline inline.
sim_destination_view <- function(leg, srv) {
  force(leg)
  force(srv)
  structure(list(
    endpoint = "sim", peer = leg$peer,
    info = function() sim_info(leg, "destination"),
    submit = function(payload, correlation_id = NULL) {
      if (!is.null(leg$terminal)) return(leg$terminal)
      if (leg$closed) return(list(kind = "terminal", code = "STUDY_COMPLETE", message = "", detail = NULL))
      if (!is.null(correlation_id) && !is.null(leg$rounds[[correlation_id]])) {
        rnd <- leg$rounds[[correlation_id]]
        if (!is.null(rnd$response_message_id)) {
          rnd$deliverable <- TRUE  # the source tunnel replays its cached response
          leg$rounds[[correlation_id]] <- rnd
        }
        return(correlation_id)
      }
      leg$round_counter <- leg$round_counter + 1L
      if (!is.null(leg$faults$max_rounds) && leg$rounds_used + 1L > leg$faults$max_rounds) {
        return(sim_terminal(leg, "LIMIT_EXCEEDED", "round count exceeds maxRounds",
                            list(cap = "maxRounds", limit = leg$faults$max_rounds, observed = leg$rounds_used + 1L)))
      }
      cid <- if (is.null(correlation_id)) sim_id() else correlation_id
      leg$rounds[[cid]] <- list(correlation_id = cid, round_no = leg$round_counter, query = payload, response = NULL,
                                response_message_id = NULL, deliverable = FALSE, delivered = FALSE)
      leg$query_bytes_used <- leg$query_bytes_used + canonical_bytes(payload)
      msg <- list(kind = "delivered", message_id = sim_id(), correlation_id = cid, payload = sim_wire(payload), budget = sim_budget(leg), received_at = NULL)
      outcome <- server_step(srv, msg)
      if (identical(outcome, "STUDY_COMPLETE")) leg$closed <- TRUE
      cid
    },
    poll_response = function(correlation_id, timeout_s) {
      if (!is.null(leg$terminal)) return(leg$terminal)
      rnd <- leg$rounds[[correlation_id]]
      if (!is.null(rnd) && isTRUE(rnd$deliverable) && !is.null(rnd$response_message_id) && !isTRUE(rnd$delivered)) {
        rnd$delivered <- TRUE # delivery is the acknowledgement
        leg$rounds[[correlation_id]] <- rnd
        if (isTRUE(leg$faults$error_after_round == rnd$round_no)) sim_terminal(leg, "SESSION_ERRORED", "simulated session error")
        return(list(kind = "delivered", message_id = rnd$response_message_id, correlation_id = correlation_id,
                    payload = rnd$response, budget = sim_budget(leg), received_at = NULL))
      }
      Sys.sleep(min(timeout_s, 0.02))
      list(kind = "empty")
    },
    abandon = function(correlation_id) {
      rnd <- leg$rounds[[correlation_id]]
      if (!is.null(rnd)) {
        rnd$delivered <- TRUE
        leg$rounds[[correlation_id]] <- rnd
      }
      invisible(NULL)
    },
    next_message = function(timeout_s) stop("a destination never polls messages"),
    post_response = function(in_reply_to, payload) stop("a destination never posts responses"),
    complete = function() {
      if (!is.null(leg$terminal)) return(leg$terminal)
      leg$closed <- TRUE
      NULL
    }
  ), class = "fusion_transport")
}

#' Run a fusion analysis in this process, with no tunnel
#'
#' Wires a source handler set and a destination analysis function together through an in-process
#' transport, so researchers can develop and test fusion code against sample data in the IDE. The
#' same guards, memo, error envelopes and typed errors apply as in a real study.
#'
#' @param operations A [fusion_operations()] registry (two-party; the peer is named `"dp-sim"`) or a
#'   named list of registries, one per peer (hub).
#' @param analysis A function of one argument, the connected `fusion` object, exactly as in a study.
#' @param guards Optional list like `list(maxDistinctPersonIds = 5000, minGroupSize = 11)`.
#' @param caps Optional caps object as `/v1/info` would carry (informational).
#' @param approved_operations Optional approved operation list for the registry pre-flight.
#' @param faults A [fusion_sim_faults()] object, or a named list of them per peer.
#' @param settings A [fusion_settings()] object; defaults to fast simulator settings.
#' @return Whatever `analysis` returns. Every leg is closed on a clean return.
#' @export
fusion_simulate <- function(operations, analysis, guards = NULL, caps = NULL, approved_operations = NULL, faults = NULL, settings = NULL) {
  regs <- if (inherits(operations, "fusion_operations")) list(`dp-sim` = operations) else operations
  if (!is.list(regs) || is.null(names(regs))) stop("operations must be a fusion_operations() registry or a named list of them")
  s <- settings
  if (is.null(s)) {
    s <- fusion_settings_with(fusion_settings(character(0)), ready_timeout_s = 5, ready_poll_s = 0.01, poll_http_timeout_s = 0.1,
                              http_timeout_s = 1, round_timeout_s = 30, retry_base_ms = 1, retry_max_ms = 5)
  }
  fusion_log_configure(s$log_level)
  transports <- list()
  i <- 0L
  for (peer in names(regs)) {
    i <- i + 1L
    f <- if (inherits(faults, "fusion_sim_faults")) faults else if (is.list(faults) && !is.null(faults[[peer]])) faults[[peer]] else fusion_sim_faults()
    leg <- new_sim_leg(peer, sprintf("leg-%d", i), guards, caps, approved_operations, f)
    srv <- new_server(sim_source_view(leg), regs[[peer]], s, peer)
    server_start(srv, now_s() + s$ready_timeout_s)
    transports[[peer]] <- sim_destination_view(leg, srv)
  }
  fusion <- fusion_connect(settings = s, transports = transports)
  result <- analysis(fusion)
  if (!isTRUE(fusion$completed)) tryCatch(fusion_complete(fusion), fusion_terminal_error = function(e) NULL)
  result
}
