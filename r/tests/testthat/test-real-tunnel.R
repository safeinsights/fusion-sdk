# Phase 8 switch: run the happy path against REAL tunnels when FUSION_TEST_ANNOUNCE points at an announce
# file in the fake pair's format (produced by the tunnel repo's harness). Skipped otherwise.

test_that("happy path against real tunnels", {
  announce <- Sys.getenv("FUSION_TEST_ANNOUNCE", "")
  skip_if(!nzchar(announce), "set FUSION_TEST_ANNOUNCE=<announce.json> to run against real tunnels")
  ann <- read_json_file(announce)
  fake <- structure(list(pid = NULL, legs = ann$legs, env = ann$env), class = "fake_pair")
  src <- start_r_source(fake, extra_env = c(FUSION_READY_TIMEOUT_S = "900", FUSION_POLL_HTTP_TIMEOUT_S = "40"))
  fusion <- sifusion::fusion_connect(fake_env(fake, "destination"))
  peer <- sifusion::fusion_peer(fusion)
  for (i in 1:3) {
    r <- sifusion::fusion_request(peer, "counts_by_group", list(person_ids = c("p1", "p2")))
    expect_equal(sifusion::fusion_as_data_frame(r)$n, c(2L, 5L))
  }
  e <- tryCatch(sifusion::fusion_request(peer, "boom"), fusion_remote_error = function(e) e)
  expect_equal(e$code, "HANDLER_ERROR")
  expect_equal(sifusion::fusion_complete(fusion), c(`dp-a` = "OK")[names(sifusion::fusion_complete)])
  expect_equal(child_wait(src, 300), 0L)
})
