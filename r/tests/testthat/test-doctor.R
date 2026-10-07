test_that("doctor reports each tunnel without secrets", {
  with_fake("hub-two-legs", {
    env <- fake_env(fake, "destination")
    out <- capture.output(ok <- safeinsights.fusion::fusion_doctor(env))
    expect_true(ok)
    text <- paste(out, collapse = "\n")
    expect_match(text, "leg=leg-a")
    expect_match(text, "leg=leg-b")
    expect_match(text, "result: ok")
    for (tok in jsonlite::fromJSON(env[["FUSION_TUNNEL_TOKENS"]])) expect_false(grepl(tok, text, fixed = TRUE))
  })
})

test_that("doctor failures, json output and wait", {
  with_fake("happy", {
    env <- fake_env(fake, "destination")
    env[["FUSION_TUNNEL_TOKEN"]] <- "wrong"
    out <- capture.output(ok <- safeinsights.fusion::fusion_doctor(env, json = TRUE))
    expect_false(ok)
    report <- jsonlite::fromJSON(paste(out, collapse = "\n"), simplifyVector = FALSE)
    expect_false(report$ok)
    expect_equal(report$tunnels[[1]]$error, "fusion_config_error")
    env <- fake_env(fake, "source")
    env[["FUSION_TUNNEL_ENDPOINT"]] <- leg_endpoint(fake, "destination")$endpoint
    env[["FUSION_TUNNEL_TOKEN"]] <- leg_endpoint(fake, "destination")$token
    out <- capture.output(ok <- safeinsights.fusion::fusion_doctor(env, json = TRUE))
    expect_false(ok)
    expect_false(jsonlite::fromJSON(paste(out, collapse = "\n"), simplifyVector = FALSE)$tunnels[[1]]$roleMatches)
    out <- capture.output(ok <- safeinsights.fusion::fusion_doctor(character(0)))
    expect_false(ok)
    expect_match(paste(out, collapse = "\n"), "config error")
  })
  with_fake("slow-ready",
    {
      env <- c(fake_env(fake, "destination"), FUSION_READY_TIMEOUT_S = "10", FUSION_READY_POLL_S = "0.1")
      out <- capture.output(ok <- safeinsights.fusion::fusion_doctor(env, wait = TRUE))
      expect_true(ok)
      expect_match(paste(out, collapse = "\n"), "state=CHANNEL_UP")
      env[["FUSION_TUNNEL_ENDPOINT"]] <- "http://127.0.0.1:9"
      env[["FUSION_HTTP_TIMEOUT_S"]] <- "1"
      out <- capture.output(ok <- safeinsights.fusion::fusion_doctor(env))
      expect_false(ok)
      expect_match(paste(out, collapse = "\n"), "unreachable")
    },
    ready_delay_ms = 300
  )
})
