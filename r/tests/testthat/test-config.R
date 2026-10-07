rt <- function(...) safeinsights.fusion:::read_tunnels(list(...))

test_that("single-leg shorthand and map form", {
  t <- rt(FUSION_TUNNEL_ENDPOINT = "http://127.0.0.1:8471/", FUSION_TUNNEL_TOKEN = "t")
  expect_equal(t[[1]], list(label = "default", endpoint = "http://127.0.0.1:8471", token = "t"))
  m <- rt(FUSION_TUNNEL_ENDPOINTS = '{"b":"http://tunnel-b:8471","a":"http://tunnel-a:8471"}', FUSION_TUNNEL_TOKENS = '{"a":"ta","b":"tb"}')
  expect_equal(vapply(m, function(x) x$label, character(1)), c("b", "a"))
  expect_equal(m[[2]]$token, "ta")
})

test_that("malformed env is a fusion_config_error", {
  expect_error(rt(), class = "fusion_config_error")
  expect_error(rt(FUSION_TUNNEL_ENDPOINT = "http://127.0.0.1:1"), "FUSION_TUNNEL_TOKEN")
  expect_error(rt(FUSION_TUNNEL_ENDPOINT = "http://127.0.0.1:1", FUSION_TUNNEL_TOKEN = "t", FUSION_TUNNEL_ENDPOINTS = "{}"), "not both")
  expect_error(rt(FUSION_TUNNEL_ENDPOINTS = "{}", FUSION_TUNNEL_TOKENS = "{}"), "non-empty")
  expect_error(rt(FUSION_TUNNEL_ENDPOINTS = "nope", FUSION_TUNNEL_TOKENS = "{}"), "JSON")
  expect_error(rt(FUSION_TUNNEL_ENDPOINTS = '{"a":"http://x:1"}', FUSION_TUNNEL_TOKENS = '{"b":"t"}'), "same keys")
  expect_error(rt(FUSION_TUNNEL_ENDPOINTS = '{"a":"http://x:1"}', FUSION_TUNNEL_TOKENS = '{"a":""}'), "strings")
})

test_that("only enclave-local endpoints are accepted", {
  ok <- c("http://127.0.0.1:8471", "http://localhost:8471", "http://tunnel-a:8471", "http://10.0.1.5:80", "http://[::1]:8471", "http://169.254.1.1")
  for (u in ok) expect_equal(safeinsights.fusion:::validate_endpoint(u), u)
  bad <- c(
    "https://127.0.0.1:8471", "http://relay.safeinsights.org", "http://8.8.8.8", "http://127.0.0.1:8471/v1",
    "http://user:pw@127.0.0.1", "ftp://x", "http://127.0.0.1?x=1"
  )
  for (u in bad) expect_error(safeinsights.fusion:::validate_endpoint(u), class = "fusion_config_error")
})

test_that("role and settings", {
  expect_equal(safeinsights.fusion:::read_role(list(FUSION_ROLE = " Destination ")), "destination")
  expect_error(safeinsights.fusion:::read_role(list()), class = "fusion_config_error")
  expect_error(safeinsights.fusion:::read_role(list(FUSION_ROLE = "hub")), class = "fusion_config_error")
  s <- safeinsights.fusion::fusion_settings(character(0))
  expect_equal(c(s$ready_timeout_s, s$round_timeout_s, s$round_max_reissues, s$poll_http_timeout_s), c(900, 600, 3, 40))
  s <- safeinsights.fusion::fusion_settings(
    c(FUSION_ROUND_TIMEOUT_S = "30", FUSION_ROUND_MAX_REISSUES = "1", FUSION_LOG_LEVEL = "debug", FUSION_READY_POLL_S = "")
  )
  expect_equal(c(s$round_timeout_s, s$round_max_reissues, s$ready_poll_s), c(30, 1, 2))
  expect_equal(s$log_level, "DEBUG")
  expect_error(safeinsights.fusion::fusion_settings(c(FUSION_ROUND_TIMEOUT_S = "soon")), class = "fusion_config_error")
  expect_error(safeinsights.fusion::fusion_settings(c(FUSION_ROUND_TIMEOUT_S = "-1")), class = "fusion_config_error")
})

test_that("condition classes match spec/errors.json", {
  skip_without_root()
  errors <- read_json_file(file.path(fusion_sdk_root(), "spec", "errors.json"))
  r_classes <- vapply(errors$localClasses, function(c) c$r, character(1))
  e <- tryCatch(safeinsights.fusion:::fusion_remote_error("BAD_PARAMS", "x", peer = "p"), error = function(e) e)
  expect_s3_class(e, c("fusion_remote_error", "fusion_error", "error"))
  expected <- c("fusion_error", "fusion_config_error", "fusion_remote_error", "fusion_terminal_error", "fusion_limit_exceeded_error", "fusion_session_error")
  expect_true(all(expected %in% r_classes))
  t <- safeinsights.fusion:::fusion_terminal_condition("LIMIT_EXCEEDED", "cap", list(cap = "maxRounds"), peer = "p")
  expect_s3_class(t, c("fusion_limit_exceeded_error", "fusion_terminal_error", "fusion_error"))
  expect_equal(t$cap, "maxRounds")
  expect_true(safeinsights.fusion::fusion_is_terminal(t))
  expect_false(safeinsights.fusion::fusion_is_terminal(e))
  expect_match(conditionMessage(e), "^\\[peer=p\\] BAD_PARAMS: x$")
})
