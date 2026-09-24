# Source behavior: fusion_serve() runs in an R child; the destination in this process drives it.

connect_to <- function(fake, settings = fast_settings(), ...) {
  sifusion::fusion_connect(fake_env(fake, "destination"), settings = settings, ...)
}

test_that("serve handles every body kind, guards are disabled without info.guards, and STUDY_COMPLETE exits 0", {
  with_fake("happy", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    r <- sifusion::fusion_request(peer, "counts_by_group", list(person_ids = as.character(1:1000)))
    expect_equal(sifusion::fusion_as_data_frame(r)$n, c(1000L, 2001L))
    r <- sifusion::fusion_request(peer, "echo_table")
    expect_equal(sifusion::fusion_as_data_frame(r)$v, c(1.5, 2.5))
    expect_equal(sifusion::fusion_request(peer, "total")$body$operation, "total")
    sifusion::fusion_complete(fusion)
    expect_equal(child_wait(src), 0L)
    out <- child_output(src)
    expect_match(out, "guards.disabled")
    expect_match(out, "serve.start peer=dp-a operations=counts_by_group,total,boom,boom_sanitized,unencodable,echo_table")
    expect_match(out, "round.served")
    expect_false(grepl("MARKER_TRACE", out, fixed = TRUE))
  })
})

test_that("a redelivered query is answered from the memo without re-running the handler", {
  with_fake("redeliver-query", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    sifusion::fusion_request(peer, "total")
    sifusion::fusion_request(peer, "total")
    Sys.sleep(0.5)
    expect_equal(sifusion::fusion_request(peer, "total")$body$calls, 3)
    sifusion::fusion_complete(fusion)
    expect_equal(child_wait(src), 0L)
    snap <- fake_snapshot(fake)
    expect_equal(snap$respondCalls, 4)
    expect_equal(snap$budget$roundsUsed, 3)
    expect_equal(snap$rounds[[2]]$queryDeliveries, 2)
    expect_match(child_output(src), "serve.memo_replay")
  })
})

test_that("guards refuse loudly without partial results", {
  ops <- '
ops <- fusion_operations(
  counts_by_group = fusion_operation(function(params, ctx) {
    small <- isTRUE(params$small)
    data.frame(grade = c("9", "10"), n = c(5L, if (small) 2L else 4L))
  }, person_id_param = "person_ids", cardinality = "per-group"),
  two_counts = fusion_operation(function(params, ctx) data.frame(a = 5L, b = 6L), cardinality = "per-group"),
  two_counts_declared = fusion_operation(function(params, ctx) data.frame(a = 1L, b = 6L), cardinality = "per-group", count_column = "b"),
  per_group_scalar = fusion_operation(function(params, ctx) 7, cardinality = "per-group"),
  aggregate_small = fusion_operation(function(params, ctx) data.frame(n = 1L))
)
'
  with_fake("guards", {
    src <- start_r_source(fake, ops_code = ops)
    fusion <- connect_to(fake)
    peer <- sifusion::fusion_peer(fusion)
    five_distinct <- sifusion::fusion_request(peer, "counts_by_group", list(person_ids = c("a", "b", "c", "d", "e", "a")))
    expect_s3_class(sifusion::fusion_as_data_frame(five_distinct), "data.frame")
    e <- tryCatch(sifusion::fusion_request(peer, "counts_by_group", list(person_ids = c("a", "b", "c", "d", "e", "f"))), fusion_remote_error = function(e) e)
    expect_equal(e$code, "GUARD_REFUSED")
    expect_equal(e$detail, list(guard = "maxDistinctPersonIds", limit = 5L, observed = 6L))
    as_object <- as.list(setNames(paste0("p", 1:6), paste0("p", 1:6)))
    nested <- list(as.list(paste0("p", 1:6)))
    for (shape in list(as_object, nested)) {
      e <- tryCatch(sifusion::fusion_request(peer, "counts_by_group", list(person_ids = shape)), fusion_remote_error = function(e) e)
      expect_equal(e$code, "GUARD_REFUSED")
      expect_equal(e$detail, list(guard = "maxDistinctPersonIds", limit = 5L))
    }
    e <- tryCatch(sifusion::fusion_request(peer, "counts_by_group", list(person_ids = I("a"), small = TRUE)), fusion_remote_error = function(e) e)
    expect_equal(e$detail, list(guard = "minGroupSize", limit = 3L))
    e <- tryCatch(sifusion::fusion_request(peer, "two_counts"), fusion_remote_error = function(e) e)
    expect_match(e$remote_message, "count_column")
    expect_s3_class(sifusion::fusion_request(peer, "two_counts_declared"), "fusion_response")
    e <- tryCatch(sifusion::fusion_request(peer, "per_group_scalar"), fusion_remote_error = function(e) e)
    expect_match(e$remote_message, "fusion table")
    expect_s3_class(sifusion::fusion_request(peer, "aggregate_small"), "fusion_response")
    sifusion::fusion_complete(fusion)
    expect_equal(child_wait(src), 0L)
    out <- child_output(src)
    expect_match(out, "guards.loaded peer=dp-a maxDistinctPersonIds=5 minGroupSize=3")
    expect_match(out, "serve.guard_refused")
  })
})

test_that("the registry is pre-flighted against the approved operation list", {
  with_fake("operations-declared", {
    extra <- start_child(c(default_ops_code, "fusion_serve(ops)"), env = source_env(fake))
    expect_true(child_wait(extra) != 0L)
    expect_match(child_output(extra), "registered but not in the approved list")
    ok_ops <- '
ops <- fusion_operations(
  counts_by_group = fusion_operation(function(params, ctx) data.frame(g = "x", n = 1L), cardinality = "per-group"),
  total = fusion_operation(function(params, ctx) list(ok = TRUE))
)
'
    src <- start_r_source(fake, ops_code = ok_ops)
    fusion <- connect_to(fake)
    expect_true(sifusion::fusion_request(fusion, "total")$body$ok)
    sifusion::fusion_complete(fusion)
    expect_equal(child_wait(src), 0L)
  })
})

test_that("bad params for a non-envelope query", {
  with_fake("happy", {
    src <- start_r_source(fake)
    dst <- leg_endpoint(fake, "destination")
    Sys.sleep(1)
    for (payload in list(list(hello = "world"), list(v = 1, kind = "response", status = "ok", body = 1))) {
      cid <- raw_call(dst$endpoint, dst$token, "POST", "/v1/request", list(payload = payload))$body$correlationId
      msg <- raw_call(dst$endpoint, dst$token, "GET", paste0("/v1/responses/", cid), timeout = 20)$body
      expect_equal(msg$payload$status, "error")
      expect_equal(msg$payload$error$code, "BAD_PARAMS")
    }
    raw_call(dst$endpoint, dst$token, "POST", "/v1/complete", structure(list(), names = character(0)))
    expect_equal(child_wait(src), 0L)
  })
})

test_that("LIMIT_EXCEEDED at POST /v1/messages raises from fusion_serve and exits non-zero", {
  with_fake("limit-exceeded-bytes", {
    src <- start_r_source(fake)
    fusion <- connect_to(fake)
    e <- tryCatch(sifusion::fusion_request(fusion, "total", list(pad = 500)), fusion_limit_exceeded_error = function(e) e)
    expect_equal(e$cap, "maxResponsePlaintextBytesPerRound")
    expect_true(child_wait(src) != 0L)
    expect_match(child_output(src), "fusion_limit_exceeded_error|LIMIT_EXCEEDED")
  })
})

# A source-side transport whose POST /v1/messages outcome is scripted.
stub_source_transport <- function(outcome) {
  posted <- character(0)
  structure(list(
    endpoint = "stub", peer = "dp-a",
    info = function() list(api_version = "2.0.0", leg_id = "leg-a", peer_org_slug = "dp-a", role = "source", direction = "dst_to_src",
                           state = "CHANNEL_UP", guards = NULL, caps = NULL, operations = NULL),
    submit = function(...) stop("unused"), poll_response = function(...) stop("unused"), abandon = function(...) stop("unused"),
    next_message = function(timeout_s) list(kind = "empty"),
    post_response = function(in_reply_to, payload) {
      posted <<- c(posted, in_reply_to)
      if (is.function(outcome)) outcome() else outcome
    },
    complete = function() stop("unused"),
    posted = function() posted
  ), class = "fusion_transport")
}

test_that("a 409 on the response post drops the round without raising", {
  ops <- sifusion::fusion_operations(total = function(p, c) list(total = 42L))
  query <- list(v = 1L, kind = "query", operation = "total", params = structure(list(), names = character(0)), encoding = "json")
  msg <- list(kind = "delivered", message_id = "m1", correlation_id = "c1", payload = query, budget = NULL, received_at = NULL)
  transport <- stub_source_transport(function() sifusion:::fusion_concurrency_error("409", peer = "dp-a", correlation_id = "c1"))
  srv <- sifusion:::new_server(transport, ops, fast_settings(), "x")
  sifusion:::server_start(srv, sifusion:::now_s() + 1)
  lines <- capture_fusion_log(expect_null(sifusion:::server_step(srv, msg)))
  expect_equal(transport$posted(), "c1")
  expect_null(sifusion:::memo_get(srv$memo, "c1"))
  expect_equal(srv$rounds_served, 0L)
  expect_true(any(grepl("^fusion round.protocol_error peer=dp-a correlationId=c1 messageId=m1$", lines)))
  # A terminal body on the post ends the leg like one on the poll.
  transport <- stub_source_transport(list(kind = "terminal", code = "LIMIT_EXCEEDED", message = "cap", detail = list(cap = "maxRounds", limit = 1L, observed = 2L)))
  srv <- sifusion:::new_server(transport, ops, fast_settings(), "x")
  sifusion:::server_start(srv, sifusion:::now_s() + 1)
  e <- tryCatch(sifusion:::server_step(srv, msg), fusion_limit_exceeded_error = function(e) e)
  expect_equal(e$cap, "maxRounds")
})

test_that("source config errors", {
  ops <- sifusion::fusion_operations(total = function(p, c) 1)
  with_fake("hub-two-legs", {
    expect_error(sifusion::fusion_serve(ops, fake_env(fake, "source"), settings = fast_settings()), "exactly one tunnel", class = "fusion_config_error")
  })
  with_fake("happy", {
    expect_error(sifusion::fusion_serve(ops, fake_env(fake, "destination"), settings = fast_settings()), "FUSION_ROLE", class = "fusion_config_error")
    env <- fake_env(fake, "source")
    env[["FUSION_TUNNEL_ENDPOINT"]] <- leg_endpoint(fake, "destination")$endpoint
    env[["FUSION_TUNNEL_TOKEN"]] <- leg_endpoint(fake, "destination")$token
    expect_error(sifusion::fusion_serve(ops, env, settings = fast_settings()), "role", class = "fusion_config_error")
    expect_error(sifusion::fusion_serve(list(), fake_env(fake, "source"), settings = fast_settings()), "fusion_operations")
  })
})
