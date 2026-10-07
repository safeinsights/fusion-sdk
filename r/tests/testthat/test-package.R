test_that("package metadata is consistent with the Python package", {
  expect_identical(as.character(utils::packageVersion("safeinsights.fusion")), "0.3.0")
})
