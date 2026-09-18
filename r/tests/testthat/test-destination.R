# Destination behavior (plan section 3) against the fake, with an R child process as the source.

connect_to <- function(fake, settings = fast_settings(), ...) {
  sifusion::fusion_connect(fake_env(fake, "destination"), settings = settings, ...)
}

test_that("happy rounds, budget, data frames and complete", {
  with_fake("happy", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    expect_equal(sifusion::fusion_peers(fusion)$peer, "dp-a")
    peer <- sifusion::fusion_peer(fusion)
    expect_identical(sifusion::fusion_peer(fusion, "dp-a"), peer)
    for (i in 1:3) {
      r <- sifusion::fusion_request(peer, "counts_by_group", list(person_ids = c("p1", "p2"), group_by = "grade"))
      df <- sifusion::fusion_as_data_frame(r)
      expect_equal(df$grade, c("9", "10"))
      expect_equal(df$n, c(2L, 5L))
      expect_equal(r$budget$rounds_used, i)
      expect_null(r$budget$rounds_max)
      expect_equal(r$peer, "dp-a")
    }
    expect_equal(sifusion::fusion_budget(peer)$rounds_used, 3)
    r <- sifusion::fusion_request(fusion, "total")
    expect_equal(r$body$total, 42)
    expect_equal(r$body$correlation_id, r$correlation_id)
    expect_equal(r$body$peer, "dp-a")
    expect_output(print(r), "total from dp-a")
    expect_output(print(peer), "dp-a")
    expect_equal(sifusion::fusion_complete(fusion), c(`dp-a` = "OK"))
    expect_error(sifusion::fusion_request(peer, "total"), class = "fusion_concurrency_error")
    expect_equal(child_wait(src), 0L)
    expect_match(child_output(src), "session.complete")
    expect_equal(fake_snapshot(fake)$state, "CLOSED")
  })
})

test_that("unknown peer and multi-peer lookups", {
  with_fake("hub-two-legs", {
    fusion <- connect_to(fake)
    expect_error(sifusion::fusion_peer(fusion), "2 peers")
    expect_error(sifusion::fusion_peer(fusion, "dp-z"), "unknown peer")
    expect_setequal(sifusion::fusion_peers(fusion)$peer, c("dp-a", "dp-b"))
    expect_equal(sifusion::fusion_complete(fusion), c(`dp-a` = "OK", `dp-b` = "OK"))
  })
})

test_that("backpressure and transient errors are invisible", {
  for (scenario in c("backpressure", "transient-errors")) {
    with_fake(scenario, {
      src <- start_r_source(fake)
      fusion <- connect_to(fake)
      for (i in 1:3) expect_equal(sifusion::fusion_request(fusion, "total")$body$total, 42)
      snap <- fake_snapshot(fake)
      expect_equal(snap$budget$roundsUsed, 3)
      expect_gt(snap$requestCalls, 3)
      sifusion::fusion_complete(fusion)
      expect_equal(child_wait(src), 0L)
    })
  }
})

test_that("round timeout re-issues the same correlationId and the cached response is replayed", {
  with_fake("timeout-reissue", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    sifusion::fusion_request(peer, "total")
    t0 <- Sys.time()
    r <- sifusion::fusion_request(peer, "total", list(round = 2), timeout = 0.7)
    elapsed <- as.numeric(Sys.time() - t0, units = "secs")
    expect_gte(elapsed, 0.7)
    expect_lt(elapsed, 5)
    expect_equal(r$body$calls, 2) # the handler ran once for round 2
    expect_equal(sifusion::fusion_request(peer, "total")$body$calls, 3)
    events <- fake_log_events(fake)
    expect_true("reissue" %in% events && "cached_replay" %in% events)
    sifusion::fusion_complete(fusion)
    expect_equal(child_wait(src), 0L)
  })
})

test_that("a 404 on the poll re-issues with the same id", {
  with_fake("restart-404", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    sifusion::fusion_request(peer, "total")
    r <- sifusion::fusion_request(peer, "total")
    expect_equal(r$body$total, 42)
    events <- fake_log_events(fake)
    expect_equal(events[events %in% c("forget", "reissue")], c("forget", "reissue"))
    sifusion::fusion_complete(fusion)
    expect_equal(child_wait(src), 0L)
  })
})

test_that("RoundTimeoutError abandons the round and the peer stays usable", {
  with_fake("slow-source", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake, fast_settings(round_max_reissues = 1L))
    peer <- sifusion::fusion_peer(fusion)
    sifusion::fusion_request(peer, "total")
    t0 <- Sys.time()
    e <- tryCatch(sifusion::fusion_request(peer, "total", timeout = 0.4), fusion_round_timeout_error = function(e) e)
    expect_s3_class(e, "fusion_round_timeout_error")
    expect_equal(e$reissues, 1L)
    expect_false(sifusion::fusion_is_terminal(e))
    elapsed <- as.numeric(Sys.time() - t0, units = "secs")
    expect_gte(elapsed, 0.8)
    expect_lt(elapsed, 3)
    expect_false(peer$in_flight)
    expect_equal(sifusion::fusion_peers(fusion)$state, "CHANNEL_UP")
    expect_equal(sifusion::fusion_request(peer, "total")$body$total, 42)
    expect_true("abandon" %in% fake_log_events(fake))
    Sys.sleep(3.2)
    expect_equal(sifusion::fusion_complete(fusion), c(`dp-a` = "OK"))
    expect_equal(child_wait(src), 0L)
  })
})

test_that("remote error envelopes raise fusion_remote_error and the peer stays usable", {
  with_fake("happy", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    e <- tryCatch(sifusion::fusion_request(peer, "boom"), fusion_remote_error = function(e) e)
    expect_equal(e$code, "HANDLER_ERROR")
    expect_equal(e$detail$type, "simpleError")
    expect_match(e$remote_message, "Traceback")
    expect_match(e$remote_message, "MARKER_TRACE_11aa")
    expect_match(conditionMessage(e), "^\\[peer=dp-a\\] HANDLER_ERROR")
    e <- tryCatch(sifusion::fusion_request(peer, "boom_sanitized"), fusion_remote_error = function(e) e)
    expect_equal(e$remote_message, "simpleError: bad value MARKER_SANITIZED_22bb")
    e <- tryCatch(sifusion::fusion_request(peer, "nope"), fusion_remote_error = function(e) e)
    expect_equal(e$code, "UNKNOWN_OPERATION")
    expect_equal(e$detail$operation, "nope")
    e <- tryCatch(sifusion::fusion_request(peer, "unencodable"), fusion_remote_error = function(e) e)
    expect_equal(e$code, "HANDLER_ERROR")
    expect_equal(sifusion::fusion_request(peer, "total")$body$total, 42)
    sifusion::fusion_complete(fusion)
    expect_equal(child_wait(src), 0L)
  })
})

test_that("an undecodable response is ACKed and raises fusion_protocol_error", {
  with_fake("happy", {
    src <- start_raw_source(fake, '{"not": "an envelope"}')
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    expect_error(sifusion::fusion_request(peer, "total"), class = "fusion_protocol_error")
    expect_error(sifusion::fusion_request(peer, "total"), class = "fusion_protocol_error")
    snap <- fake_snapshot(fake)
    expect_true(all(vapply(snap$rounds, function(r) isTRUE(r$responseAcked), logical(1))))
    expect_equal(sifusion::fusion_peers(fusion)$state, "CHANNEL_UP")
    sifusion::fusion_complete(fusion)
    child_wait(src, 10)
  })
})

test_that("LIMIT_EXCEEDED and SESSION_ERRORED are terminal for the peer", {
  with_fake("limit-exceeded-rounds", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    sifusion::fusion_request(peer, "total")
    r <- sifusion::fusion_request(peer, "total")
    expect_equal(c(r$budget$rounds_used, r$budget$rounds_max), c(2, 2))
    e <- tryCatch(sifusion::fusion_request(peer, "total"), fusion_limit_exceeded_error = function(e) e)
    expect_equal(e$cap, "maxRounds")
    expect_true(sifusion::fusion_is_terminal(e))
    calls <- fake_snapshot(fake)$requestCalls
    expect_error(sifusion::fusion_request(peer, "total"), class = "fusion_limit_exceeded_error")
    expect_equal(fake_snapshot(fake)$requestCalls, calls) # raised locally
    expect_equal(sifusion::fusion_peers(fusion)$state, "TERMINAL:LIMIT_EXCEEDED")
    expect_equal(sifusion::fusion_complete(fusion), c(`dp-a` = "LIMIT_EXCEEDED"))
    expect_true(child_wait(src) != 0L)
    expect_match(child_output(src), "LIMIT_EXCEEDED")
  })
  with_fake("session-errored", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    sifusion::fusion_request(peer, "total")
    expect_error(sifusion::fusion_request(peer, "total"), class = "fusion_session_error")
    expect_error(sifusion::fusion_request(peer, "total"), class = "fusion_session_error")
    expect_true(child_wait(src) != 0L)
  })
})

test_that("hub: two legs are independent and complete fans out", {
  with_fake("hub-two-legs", {
    src_a <- start_r_source(fake, 1)
    src_b <- start_r_source(fake, 2)
    fusion <- connect_to(fake)
    a <- sifusion::fusion_peer(fusion, "dp-a")
    b <- sifusion::fusion_peer(fusion, "dp-b")
    ra <- sifusion::fusion_request(a, "total", list(from = "a"))
    rb <- sifusion::fusion_request(b, "total", list(from = "b"))
    expect_equal(c(ra$peer, rb$peer), c("dp-a", "dp-b"))
    expect_error(sifusion::fusion_request(b, "total"), class = "fusion_limit_exceeded_error")
    expect_equal(sifusion::fusion_request(a, "total", list(derived_from = rb$body$total))$body$total, 42)
    expect_equal(sifusion::fusion_complete(fusion), c(`dp-a` = "OK", `dp-b` = "LIMIT_EXCEEDED"))
    expect_equal(child_wait(src_a), 0L)
    expect_true(child_wait(src_b) != 0L)
  })
})

test_that("readiness wait, readiness timeout and config errors", {
  with_fake("slow-ready",
    {
      t0 <- Sys.time()
      fusion <- connect_to(fake)
      expect_gte(as.numeric(Sys.time() - t0, units = "secs"), 0.7)
      expect_equal(sifusion::fusion_peers(fusion)$state, "CHANNEL_UP")
    },
    ready_delay_ms = 800
  )
  with_fake("never-ready", {
    e <- tryCatch(connect_to(fake, ready_timeout = 0.5), fusion_not_ready_error = function(e) e)
    expect_match(conditionMessage(e), "RELAY_ATTACHED")
  })
  with_fake("happy", {
    expect_error(sifusion::fusion_connect(fake_env(fake, "source"), settings = fast_settings()), "FUSION_ROLE", class = "fusion_config_error")
    env <- fake_env(fake, "destination")
    env[["FUSION_TUNNEL_TOKEN"]] <- "wrong"
    expect_error(sifusion::fusion_connect(env, settings = fast_settings()), "401", class = "fusion_config_error")
    env <- fake_env(fake, "destination")
    env[["FUSION_TUNNEL_ENDPOINT"]] <- leg_endpoint(fake, "source")$endpoint
    env[["FUSION_TUNNEL_TOKEN"]] <- leg_endpoint(fake, "source")$token
    expect_error(sifusion::fusion_connect(env, settings = fast_settings()), "role", class = "fusion_config_error")
    lines <- capture_fusion_log(connect_to(fake))
    expect_true(any(grepl("^fusion connect.start endpointCount=1$", lines)))
    expect_true(any(grepl("^fusion ready.ok peer=default legId=leg-a role=destination", lines)))
  })
  with_fake("happy",
    {
      expect_error(connect_to(fake), "major", class = "fusion_config_error")
    },
    api_version = "2.1.0"
  )
})

test_that("destination logs are content-free", {
  with_fake("happy", {
    src <- start_r_source(fake)
    lines <- capture_fusion_log({
      fusion <- connect_to(fake)
      peer <- sifusion::fusion_peer(fusion)
      r <- sifusion::fusion_request(peer, "counts_by_group", list(person_ids = c("MARKER_PARAM_7f3a"), group_by = "MARKER_PARAM_7f3a"))
      e <- tryCatch(sifusion::fusion_request(peer, "boom", list(x = "MARKER_PARAM_9c1d")), fusion_remote_error = function(e) e)
      expect_match(e$remote_message, "MARKER_TRACE_11aa")
      sifusion::fusion_complete(fusion)
    })
    text <- paste(lines, collapse = "\n")
    for (marker in c("MARKER_PARAM_7f3a", "MARKER_PARAM_9c1d", "MARKER_TRACE_11aa", leg_endpoint(fake, "destination")$token)) {
      expect_false(grepl(marker, text, fixed = TRUE), label = marker)
    }
    events <- vapply(strsplit(lines, " "), `[`, character(1), 2)
    expect_true(all(c("connect.start", "ready.ok", "round.start", "round.complete", "round.remote_error", "complete.start", "complete.leg") %in% events))
    expect_true(all(startsWith(lines, "fusion ")))
    expect_equal(child_wait(src), 0L)
  })
})
