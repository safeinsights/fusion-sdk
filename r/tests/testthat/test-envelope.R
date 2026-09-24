enc <- function(x) sifusion:::encode_json(x)
dec <- function(s) sifusion:::decode_json(s)

test_that("every fixture in spec/fixtures round-trips", {
  skip_without_root()
  files <- sort(list.files(file.path(fusion_sdk_root(), "spec", "fixtures"), pattern = "\\.json$", full.names = TRUE))
  expect_gte(length(files), 40)
  for (f in files) {
    fx <- read_json_file(f)
    canonical <- if (is.null(fx$canonical)) fx$input else fx$canonical
    if (fx$kind == "table") {
      if (!isTRUE(fx$valid)) {
        expect_error(sifusion:::table_from_json(fx$input), class = "fusion_envelope_error", label = fx$id)
        next
      }
      df <- sifusion:::table_from_json(fx$input)
      expect_equal(nrow(df), fx$decoded$nrow, label = fx$id)
      expect_equal(ncol(df), fx$decoded$ncol, label = fx$id)
      expect_equal(names(df), as.character(unlist(fx$decoded$names)), label = fx$id)
      expect_equal(unname(attr(df, "fusion_types")), unlist(fx$decoded$types), label = fx$id)
      re <- wire_roundtrip(sifusion::fusion_table(df, types = attr(df, "fusion_types")))
      expect_true(json_equal(re, canonical), label = paste(fx$id, "canonical"))
      next
    }
    if (!isTRUE(fx$valid)) {
      expect_error(sifusion:::decode_envelope(fx$input), class = "fusion_envelope_error", label = fx$id)
      next
    }
    d <- sifusion:::decode_envelope(fx$input)
    ex <- fx$decoded
    if (d$kind == "query") {
      expect_equal(ex$kind, "query", label = fx$id)
      expect_equal(d$operation, ex$operation, label = fx$id)
      if (!is.null(ex$paramKeys)) expect_equal(sort(names(d$params)), unlist(ex$paramKeys), label = fx$id)
      re <- wire_roundtrip(sifusion:::encode_query(d$operation, d$params))
    } else {
      expect_equal(d$status, ex$status, label = fx$id)
      if (!is.null(ex$code)) expect_equal(d$error$code, ex$code, label = fx$id)
      if (!is.null(ex$bodyIsTable)) expect_equal(sifusion:::is_table_body(d$body), ex$bodyIsTable, label = fx$id)
      if (isTRUE(ex$bodyIsTable)) {
        t <- sifusion::fusion_as_data_frame(d$body)
        expect_equal(nrow(t), ex$table$nrow, label = fx$id)
        expect_equal(names(t), as.character(unlist(ex$table$names)), label = fx$id)
      }
      re <- if (!is.null(d$error)) {
        wire_roundtrip(sifusion:::encode_response_error(d$error$code, d$error$message, if (length(d$error$detail)) d$error$detail else NULL))
      } else if (sifusion:::is_table_body(d$body)) {
        df <- sifusion::fusion_as_data_frame(d$body)
        wire_roundtrip(sifusion:::encode_response_ok(sifusion::fusion_table(df, types = attr(df, "fusion_types"))))
      } else {
        wire_roundtrip(sifusion:::encode_response_ok(d$body))
      }
    }
    expect_true(json_equal(re, canonical), label = paste(fx$id, "canonical"))
  }
})

test_that("fusion_table infers types and encodes specials", {
  df <- data.frame(
    s = c("a", NA), i = c(1L, 2L), x = c(1.5, NaN), b = c(TRUE, NA), d = as.Date(c("2026-02-28", NA)),
    t = as.POSIXct(c("2026-09-18 12:00:00.1234", "2026-09-18 14:00:00"), tz = "UTC"), f = factor(c("u", "v")),
    stringsAsFactors = FALSE
  )
  t <- sifusion::fusion_table(df, types = c(i = "integer"))
  expect_equal(unname(t$types), c("string", "integer", "number", "boolean", "date", "datetime", "string"))
  j <- dec(enc(t))
  expect_true(json_equal(j$rows[[1]], list("a", 1, 1.5, TRUE, "2026-02-28", "2026-09-18T12:00:00.123Z", "u")))
  expect_true(json_equal(j$rows[[2]], list(NULL, 2, NULL, NULL, NULL, "2026-09-18T14:00:00Z", "v")))
  big <- data.frame(n = 2^60)
  expect_equal(sifusion::fusion_table(big, types = c(n = "integer64"))$json$rows[[1]][[1]], "1152921504606846976")
  expect_error(sifusion::fusion_table(big, types = c(n = "integer")), class = "fusion_envelope_error")
  expect_error(sifusion::fusion_table(data.frame(x = 1.5), types = c(x = "integer")), "whole")
  expect_equal(dec(enc(sifusion::fusion_table(data.frame(x = c(Inf, -Inf, NA)))))$rows, list(list(NULL), list(NULL), list(NULL)))
})

test_that("one-row, zero-row and zero-column tables keep their shape (auto_unbox pitfall)", {
  one <- dec(enc(sifusion::fusion_table(data.frame(grade = "9", n = 412L))))
  expect_true(json_equal(one$rows, list(list("9", 412))))
  none <- sifusion::fusion_table(data.frame(grade = character(0), n = integer(0)))
  expect_true(json_equal(dec(enc(none))$rows, list()))
  df0 <- sifusion::fusion_as_data_frame(dec(enc(none)))
  expect_equal(nrow(df0), 0)
  expect_equal(class(df0$n), "integer")
  expect_true(json_equal(dec(enc(sifusion::fusion_table(data.frame())))$columns, list()))
  expect_true(json_equal(dec(enc(list(single = I("s"), scalar = "s", many = c("a", "b"), empty = list(), n = 1L)))$single, list("s")))
  expect_true(json_equal(dec(enc(sifusion:::encode_query("op", list())))$params, structure(list(), names = character(0))))
})

test_that("datetime parsing normalises offsets and truncates to milliseconds", {
  p <- sifusion:::parse_datetime
  f <- sifusion:::format_datetime
  expect_equal(f(p("2026-09-18T12:00:00.123456789+02:00")), "2026-09-18T10:00:00.123Z")
  expect_equal(f(p("2026-09-18T12:00:00Z")), "2026-09-18T12:00:00Z")
  expect_equal(f(p("2026-09-18T12:00:00.5-05:30")), "2026-09-18T17:30:00.500Z")
  for (bad in c("2026-09-18T12:00:00", "2026-09-18 12:00:00Z", "2026-13-01T00:00:00Z", "yesterday")) {
    expect_error(p(bad), class = "fusion_envelope_error")
  }
  expect_error(sifusion:::parse_date("2026-02-30"), class = "fusion_envelope_error")
  expect_equal(sifusion:::parse_date("2024-02-29"), as.Date("2024-02-29"))
})

test_that("decoded tables have the documented R classes", {
  json <- paste0(
    '{"__table__":1,"columns":[{"name":"s","type":"string"},{"name":"i","type":"integer"},{"name":"big","type":"integer"},',
    '{"name":"i64","type":"integer64"},{"name":"x","type":"number"},{"name":"b","type":"boolean"},{"name":"d","type":"date"},',
    '{"name":"t","type":"datetime"}],"rows":[["a",1,9007199254740991,"123",1.5,true,"2024-02-29","2026-09-18T12:00:00.250Z"],',
    "[null,null,null,null,null,null,null,null]]}"
  )
  df <- sifusion::fusion_as_data_frame(dec(json))
  expect_type(df$s, "character")
  expect_type(df$i, "integer")
  expect_type(df$big, "double")
  expect_type(df$i64, "character")
  expect_type(df$x, "double")
  expect_type(df$b, "logical")
  expect_s3_class(df$d, "Date")
  expect_s3_class(df$t, "POSIXct")
  expect_equal(attr(df$t, "tzone"), "UTC")
  expect_equal(as.numeric(df$t[1]) %% 1, 0.25)
  expect_true(all(is.na(df[2, ])))
  expect_error(sifusion::fusion_as_data_frame(list(a = 1)), "not a fusion table")
})

test_that("query and error encoding validate their inputs", {
  q <- wire_roundtrip(sifusion:::encode_query("counts_by_group", list(person_ids = c("a", "b"), n = 1L)))
  expect_true(json_equal(q, list(v = 1, kind = "query", operation = "counts_by_group", params = list(person_ids = list("a", "b"), n = 1), encoding = "json")))
  expect_error(sifusion:::encode_query("9bad", list()), class = "fusion_envelope_error")
  expect_error(sifusion:::encode_query("ok", list(1, 2)), "named")
  expect_error(sifusion:::encode_response_error("NOPE", "x"), class = "fusion_envelope_error")
  expect_error(enc(list(f = function() 1)), class = "fusion_envelope_error")
  expect_equal(sifusion:::canonical_bytes(list(a = "é")), nchar('{"a":"é"}', type = "bytes"))
  expect_equal(sifusion:::shortest_number(0.1 + 0.2), "0.30000000000000004")
  expect_equal(sifusion:::shortest_number(9007199254740991), "9007199254740991")
  expect_equal(sifusion:::shortest_number(1e308), "1e+308")
})

test_that("tibbles are accepted when available", {
  skip_if_not_installed("tibble")
  tb <- tibble::tibble(k = c("a", "b"), v = c(1L, 2L))
  t <- sifusion::fusion_table(tb)
  expect_equal(unname(t$types), c("string", "integer"))
  expect_s3_class(sifusion::fusion_as_data_frame(dec(enc(t))), "data.frame")
})

test_that("repeated member names are refused before the guards see them", {
  # jsonlite keeps both entries and `$` returns the first; Python's parser collapses to one.
  dup_params <- sifusion:::decode_json('{"v":1,"kind":"query","operation":"op","params":{"ids":["a"],"ids":["b","c"]}}')
  expect_error(sifusion:::decode_envelope(dup_params), "repeats a member name", class = "fusion_envelope_error")
  dup_top <- sifusion:::decode_json('{"v":1,"kind":"query","operation":"op","operation":"other","params":{"ids":["a"]}}')
  expect_error(sifusion:::decode_envelope(dup_top), "repeats a member name", class = "fusion_envelope_error")
  ok <- sifusion:::decode_json('{"v":1,"kind":"query","operation":"op","params":{"ids":["a"],"other":["b"]}}')
  expect_equal(sifusion:::decode_envelope(ok)$params$ids, list("a"))
})
