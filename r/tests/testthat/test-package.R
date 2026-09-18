test_that("package metadata is consistent with the Python package", {
  expect_identical(as.character(utils::packageVersion("sifusion")), "0.1.0")
})
