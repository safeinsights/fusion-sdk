sim_ops <- function() {
  calls <- 0L
  sifusion::fusion_operations(
    counts_by_group = sifusion::fusion_operation(function(params, ctx) {
      calls <<- calls + 1L
      n <- length(params$person_ids)
      data.frame(grade = c("9", "10"), n = c(n, n + 5L))
    }, person_id_param = "person_ids", cardinality = "per-group"),
    total = sifusion::fusion_operation(function(params, ctx) {
      calls <<- calls + 1L
      list(total = 42L, calls = calls, peer = ctx$peer, cid = ctx$correlation_id)
    }),
    boom = sifusion::fusion_operation(function(params, ctx) stop("nope"))
  )
}

test_that("two-party simulation returns the analysis result and runs the real error paths", {
  result <- sifusion::fusion_simulate(sim_ops(), function(fusion) {
    peer <- sifusion::fusion_peer(fusion)
    expect_equal(peer$name, "dp-sim")
    r <- sifusion::fusion_request(peer, "counts_by_group", list(person_ids = c("a", "b", "c")))
    expect_equal(r$budget$rounds_used, 1)
    t <- sifusion::fusion_request(peer, "total")
    expect_equal(t$body$peer, "dp-sim")
    expect_equal(t$body$cid, t$correlation_id)
    e <- tryCatch(sifusion::fusion_request(peer, "boom"), fusion_remote_error = function(e) e)
    expect_equal(e$code, "HANDLER_ERROR")
    expect_match(e$remote_message, "nope")
    e <- tryCatch(sifusion::fusion_request(peer, "nope"), fusion_remote_error = function(e) e)
    expect_equal(e$code, "UNKNOWN_OPERATION")
    sifusion::fusion_as_data_frame(r)
  })
  expect_equal(result$n, c(3L, 8L))
})

test_that("guards apply in simulation", {
  sifusion::fusion_simulate(sim_ops(), function(fusion) {
    peer <- sifusion::fusion_peer(fusion)
    e <- tryCatch(sifusion::fusion_request(peer, "counts_by_group", list(person_ids = as.character(1:6))), fusion_remote_error = function(e) e)
    expect_equal(e$detail$guard, "maxDistinctPersonIds")
    e <- tryCatch(sifusion::fusion_request(peer, "counts_by_group", list(person_ids = I("a"))), fusion_remote_error = function(e) e)
    expect_equal(e$detail, list(guard = "minGroupSize", limit = 3L))
    expect_s3_class(sifusion::fusion_request(peer, "counts_by_group", list(person_ids = c("a", "b", "c"))), "fusion_response")
  }, guards = list(maxDistinctPersonIds = 5, minGroupSize = 3))
})

test_that("faults: dropped response is replayed on re-issue, session error and caps are terminal", {
  calls <- sifusion::fusion_simulate(sim_ops(), function(fusion) {
    sifusion::fusion_request(sifusion::fusion_peer(fusion), "total", timeout = 0.3)$body$calls
  }, faults = sifusion::fusion_sim_faults(drop_response_rounds = 1))
  expect_equal(calls, 1)
  sifusion::fusion_simulate(sim_ops(), function(fusion) {
    sifusion::fusion_request(fusion, "total")
    expect_error(sifusion::fusion_request(fusion, "total"), class = "fusion_session_error")
  }, faults = sifusion::fusion_sim_faults(error_after_round = 1))
  outcomes <- sifusion::fusion_simulate(sim_ops(), function(fusion) {
    sifusion::fusion_request(fusion, "total")
    e <- tryCatch(sifusion::fusion_request(fusion, "total"), fusion_limit_exceeded_error = function(e) e)
    expect_equal(e$cap, "maxRounds")
    sifusion::fusion_complete(fusion)
  }, faults = sifusion::fusion_sim_faults(max_rounds = 1))
  expect_equal(outcomes, c(`dp-sim` = "LIMIT_EXCEEDED"))
  sifusion::fusion_simulate(sim_ops(), function(fusion) {
    e <- tryCatch(sifusion::fusion_request(fusion, "counts_by_group", list(person_ids = I("a"))), fusion_limit_exceeded_error = function(e) e)
    expect_equal(e$cap, "maxResponsePlaintextBytesPerRound")
  }, faults = sifusion::fusion_sim_faults(max_response_bytes_per_round = 10))
})

test_that("hub simulation with two peers", {
  outcomes <- sifusion::fusion_simulate(list(`dp-a` = sim_ops(), `dp-b` = sim_ops()), function(fusion) {
    a <- sifusion::fusion_peer(fusion, "dp-a")
    b <- sifusion::fusion_peer(fusion, "dp-b")
    ra <- sifusion::fusion_request(a, "total")
    rb <- sifusion::fusion_request(b, "counts_by_group", list(person_ids = rep("x", ra$body$total)))
    expect_equal(sifusion::fusion_as_data_frame(rb)$n[1], 42L)
    expect_error(sifusion::fusion_request(b, "total"), class = "fusion_limit_exceeded_error")
    expect_equal(sifusion::fusion_request(a, "total")$body$total, 42)
    sifusion::fusion_complete(fusion)
  }, faults = list(`dp-b` = sifusion::fusion_sim_faults(max_rounds = 1)))
  expect_equal(outcomes, c(`dp-a` = "OK", `dp-b` = "LIMIT_EXCEEDED"))
})

test_that("preflight applies and legs close on a clean return", {
  expect_error(sifusion::fusion_simulate(sim_ops(), function(f) NULL, approved_operations = list(list(name = "total", cardinality = "aggregate"))),
               class = "fusion_config_error")
  expect_error(sifusion::fusion_simulate(list(sim_ops()), function(f) NULL), "named list")
  lines <- capture_fusion_log(sifusion::fusion_simulate(sim_ops(), function(f) sifusion::fusion_request(f, "total")))
  expect_true(any(grepl("^fusion complete.leg peer=dp-sim code=OK$", lines)))
})
