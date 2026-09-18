test_that("the vendored drop loads with source() and round-trips an envelope", {
  skip_without_root()
  root <- fusion_sdk_root()
  out <- tempfile("vendored")
  res <- system2(rscript_bin(), c(shQuote(file.path(root, "r", "vendor.R")), shQuote(out)), stdout = TRUE, stderr = TRUE)
  expect_match(paste(res, collapse = "\n"), "vendored sifusion")
  expect_true(file.exists(file.path(out, "sifusion.R")))
  expect_true(file.exists(file.path(out, "envelope.R")))
  script <- tempfile(fileext = ".R")
  writeLines(c(
    sprintf('source("%s")', file.path(out, "sifusion.R")),
    "q <- encode_query('counts_by_group', list(person_ids = c('a', 'b')))",
    "d <- decode_envelope(decode_json(encode_json(q)))",
    "stopifnot(d$kind == 'query', d$operation == 'counts_by_group', length(d$params$person_ids) == 2)",
    "df <- fusion_as_data_frame(decode_json(encode_json(fusion_table(data.frame(g = '9', n = 1L)))))",
    "stopifnot(nrow(df) == 1, df$n == 1L)",
    "cat('vendored ok', .sifusion_vendored_version, '\\n')"
  ), script)
  res <- system2(rscript_bin(), shQuote(script), stdout = TRUE, stderr = TRUE)
  expect_match(paste(res, collapse = "\n"), "vendored ok")
})
