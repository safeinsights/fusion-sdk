test_that("log lines are content-free and the vocabulary is closed", {
  sifusion:::fusion_log_configure("DEBUG")
  lines <- capture_fusion_log({
    sifusion:::fusion_log("round.start", "INFO", peer = "dp-a", operation = "op", bytes = 12L)
    sifusion:::fusion_log("round.retry", "DEBUG", peer = "dp-a", attempt = 0L, code = "NOT_READY")
    sifusion:::fusion_log("complete.leg", "INFO", peer = "dp a", code = "OK", operations = c("a", "b"), state = NULL)
  })
  expect_equal(lines[1], "fusion round.start peer=dp-a operation=op bytes=12")
  expect_equal(lines[2], "fusion round.retry peer=dp-a attempt=0 code=NOT_READY")
  expect_equal(lines[3], "fusion complete.leg peer=\"dp a\" code=OK operations=a,b")
  expect_error(sifusion:::fusion_log("round.start", "INFO", params = list(secret = 1)), "content-free")
  sifusion:::fusion_log_configure("WARNING")
  quiet <- capture_fusion_log(sifusion:::fusion_log("round.start", "INFO", peer = "x"))
  expect_length(quiet, 0)
  sifusion:::fusion_log_configure("INFO")
  expect_message(sifusion:::fusion_log("round.start", "INFO", peer = "x"), "^fusion round.start peer=x")
})

test_that("the allowed log fields are exactly spec/log-events.md", {
  skip_without_root()
  text <- paste(readLines(file.path(fusion_sdk_root(), "spec", "log-events.md"), warn = FALSE), collapse = "\n")
  section <- strsplit(strsplit(text, "## Allowed fields", fixed = TRUE)[[1]][2], "\n## ", fixed = TRUE)[[1]][1]
  names <- unique(regmatches(section, gregexpr("`[A-Za-z]+`", section))[[1]])
  expect_setequal(gsub("`", "", names), sifusion:::allowed_log_fields)
})

test_that("budget.near_limit uses the manifest cap names", {
  b <- sifusion:::budget_from_json(list(roundsUsed = 9, roundsMax = 10, responseBytesUsed = 1, queryBytesUsed = 95, queryBytesMax = 100))
  lines <- capture_fusion_log(sifusion:::log_budget("dp-a", b))
  expect_equal(lines, c(
    "fusion budget.near_limit peer=dp-a cap=maxRounds limit=10 observed=9",
    "fusion budget.near_limit peer=dp-a cap=maxCumulativeQueryPlaintextBytes limit=100 observed=95"
  ))
  expect_length(capture_fusion_log(sifusion:::log_budget("dp-a", NULL)), 0)
})

test_that("wait_ready treats CLOSING as STUDY_COMPLETE", {
  closing <- list(info = function() list(api_version = "2.0.0", leg_id = "leg-a", peer_org_slug = "dp-a", role = "destination", state = "CLOSING"))
  e <- tryCatch(sifusion:::wait_ready(closing, sifusion:::now_s() + 1, fast_settings(), "x"), fusion_session_error = function(e) e)
  expect_equal(e$code, "STUDY_COMPLETE")
})
