g <- function(...) structure(list(...), class = "fusion_guards")

test_that("distinct_count canonicalises values", {
  dc <- sifusion:::distinct_count
  expect_equal(dc(NULL), 0L)
  expect_equal(dc("x"), 1L)
  expect_equal(dc(list("a", "a", "b", 1, 1.0, list(k = 1), list(k = 1))), 4L)
  expect_equal(dc(c("a", "b", "a")), 2L)
})

test_that("check_query and check_result", {
  spec <- sifusion::fusion_operation(function(p, c) 1, person_id_param = "ids", cardinality = "per-group")
  expect_null(sifusion:::check_query(list(ids = list("a", "b")), spec, g(max_distinct_person_ids = 2L)))
  expect_null(sifusion:::check_query(list(ids = list("a", "b", "c")), spec, NULL))
  e <- tryCatch(sifusion:::check_query(list(ids = list("a", "b", "c")), spec, g(max_distinct_person_ids = 2L)), fusion_guard_refused = function(e) e)
  expect_equal(sifusion:::guard_detail(e), list(guard = "maxDistinctPersonIds", limit = 2L, observed = 3L))
  # Shapes the guard cannot count are refused, not counted as one value (unlist() would hand the handler N ids).
  for (shape in list(list(a = "a", b = "b", c = "c"), list(list("a", "b", "c")), list("a", list("b", "c")), list("a", list(k = "b")), list(NULL))) {
    e <- tryCatch(sifusion:::check_query(list(ids = shape), spec, g(max_distinct_person_ids = 100L)), fusion_guard_refused = function(e) e)
    expect_s3_class(e, "fusion_guard_refused")
    expect_match(conditionMessage(e), "flat array")
    expect_equal(sifusion:::guard_detail(e), list(guard = "maxDistinctPersonIds", limit = 100L))
  }
  expect_null(sifusion:::check_query(list(ids = c("a", "b")), spec, g(max_distinct_person_ids = 2L)))
  expect_null(sifusion:::check_query(list(ids = list(a = 1)), spec, NULL)) # not enforced when the guard is disabled
  tbl <- sifusion::fusion_table(data.frame(grp = "x", n = 3L))$json
  expect_null(sifusion:::check_result(tbl, spec, g(min_group_size = 3L)))
  e <- tryCatch(sifusion:::check_result(tbl, spec, g(min_group_size = 4L)), fusion_guard_refused = function(e) e)
  expect_equal(sifusion:::guard_detail(e), list(guard = "minGroupSize", limit = 4L))
  expect_match(conditionMessage(e), "smaller")
  two <- sifusion::fusion_table(data.frame(a = 5L, b = 6L))$json
  expect_error(sifusion:::check_result(two, spec, g(min_group_size = 1L)), "count_column")
  declared <- sifusion::fusion_operation(function(p, c) 1, cardinality = "per-group", count_column = "b")
  expect_null(sifusion:::check_result(two, declared, g(min_group_size = 1L)))
  expect_error(sifusion:::check_result(7, spec, g(min_group_size = 1L)), "fusion table")
  null_count <- sifusion::fusion_table(data.frame(grp = "x", n = NA_integer_))$json
  expect_error(sifusion:::check_result(null_count, spec, g(min_group_size = 1L)), "smaller")
  agg <- sifusion::fusion_operation(function(p, c) 1)
  expect_null(sifusion:::check_result(sifusion::fusion_table(data.frame(n = 1L))$json, agg, g(min_group_size = 5L)))
})

test_that("guards_from_info and preflight", {
  expect_null(sifusion:::guards_from_info(NULL))
  expect_false(sifusion:::guards_enabled(sifusion:::guards_from_info(structure(list(), names = character(0)))))
  gg <- sifusion:::guards_from_info(list(maxDistinctPersonIds = 10, minGroupSize = TRUE))
  expect_equal(gg$max_distinct_person_ids, 10L)
  expect_null(gg$min_group_size)
  ops <- sifusion::fusion_operations(op = sifusion::fusion_operation(function(p, c) 1, cardinality = "per-group"))
  expect_length(sifusion:::preflight(ops, NULL), 0)
  expect_length(sifusion:::preflight(ops, list(list(name = "op", cardinality = "per-group"))), 0)
  expect_length(sifusion:::preflight(ops, list(list(name = "op"))), 0)
  expect_match(sifusion:::preflight(ops, list(list(name = "op", cardinality = "aggregate"))), "approved as aggregate")
  expect_match(sifusion:::preflight(ops, list(list(name = "other", cardinality = "aggregate")))[1], "not in the approved list")
})

test_that("fusion_operations validates its inputs", {
  expect_error(sifusion::fusion_operations(), "name")
  expect_error(sifusion::fusion_operations(a = 1), "not a fusion_operation")
  expect_error(sifusion::fusion_operation("nope"), "function")
  expect_error(sifusion::fusion_operation(function(p, c) 1, cardinality = "per-thing"), "cardinality")
  ops <- sifusion::fusion_operations(a = function(p, c) 1, b = sifusion::fusion_operation(function(p, c) 2))
  expect_s3_class(ops$a, "fusion_operation")
  expect_equal(ops$a$cardinality, "aggregate")
  expect_output(print(ops), "a, b")
  m <- sifusion:::new_memo(2L)
  sifusion:::memo_put(m, "a", 1)
  sifusion:::memo_put(m, "b", 2)
  expect_equal(sifusion:::memo_get(m, "a"), 1)
  sifusion:::memo_put(m, "c", 3)
  expect_null(sifusion:::memo_get(m, "b"))
  expect_equal(names(m$data), c("a", "c"))
})

test_that("budget parsing and near-limit detection", {
  b <- sifusion:::budget_from_json(list(
    roundsUsed = 9, roundsMax = 10, responseBytesUsed = 1, responseBytesMax = NULL, queryBytesUsed = 5, queryBytesMax = 100
  ))
  expect_equal(b$rounds_used, 9)
  expect_null(b$response_bytes_max)
  nl <- sifusion:::budget_near_limit(b)
  expect_length(nl, 1)
  expect_equal(nl[[1]]$name, "rounds")
  expect_null(sifusion:::budget_from_json(NULL))
  expect_output(print(b), "rounds 9/10")
  expect_equal(sifusion:::api_major("1.4.2"), 1L)
  expect_true(is.na(sifusion:::api_major("x")))
})
