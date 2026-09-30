# The fusion envelope and the typed-columnar fusion table (spec/envelope.schema.json, ADR 0001).
# Cell rules are normative in spec/fixtures/README.md; every branch here has a fixture.

envelope_version <- 1L
remote_codes <- c("UNKNOWN_OPERATION", "BAD_PARAMS", "HANDLER_ERROR", "GUARD_REFUSED")
column_types <- c("string", "integer", "integer64", "number", "boolean", "date", "datetime")
safe_int <- 2^53 - 1
operation_re <- "^[A-Za-z_][A-Za-z0-9_.-]*$"
int64_re <- "^-?[0-9]+$"
date_re <- "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
datetime_re <- "^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$"

envelope_error <- function(message) {
  stop(structure(list(message = message, call = NULL), class = c("fusion_envelope_error", "error", "condition")))
}

is_json_object <- function(x) is.list(x) && !is.null(names(x))
is_json_array <- function(x) is.list(x) && is.null(names(x))
is_scalar <- function(x) is.atomic(x) && length(x) == 1 && !is.na(x)

# ---- JSON emission -------------------------------------------------------------------------------

# Shortest decimal string that round-trips to the same double (Python repr semantics).
shortest_number <- function(x) {
  for (d in 15:17) {
    s <- formatC(x, digits = d, format = "g", width = 1)
    if (identical(as.numeric(s), x)) {
      return(s)
    }
  }
  s
}

json_verbatim <- function(s) structure(s, class = "json")

json_number <- function(x) {
  if (is.na(x) || !is.finite(x)) {
    return(NULL)
  }
  json_verbatim(shortest_number(x))
}

json_integer <- function(x) {
  if (is.na(x)) {
    return(NULL)
  }
  json_verbatim(sprintf("%.0f", x))
}

empty_object <- function() structure(list(), names = character(0))

# Convert an R value into a structure jsonlite emits deterministically with
# toJSON(auto_unbox = TRUE, json_verbatim = TRUE, null = "null", na = "null").
# Length-1 vectors become scalars unless wrapped in I(); data frames become fusion tables.
to_json_value <- function(x) {
  if (is.null(x)) {
    return(NULL)
  }
  if (inherits(x, "json")) {
    return(x)
  } # already verbatim (a second pass over an encoded structure)
  if (inherits(x, "fusion_table")) {
    return(to_json_value(x$json))
  }
  if (is.data.frame(x)) {
    return(to_json_value(fusion_table(x)$json))
  }
  if (is.list(x)) {
    if (length(x) == 0) {
      return(if (is.null(names(x))) list() else empty_object())
    }
    out <- lapply(x, to_json_value)
    names(out) <- names(x)
    return(out)
  }
  as_is <- inherits(x, "AsIs")
  if (is.factor(x)) x <- as.character(x)
  if (inherits(x, "Date")) x <- format_date(x)
  if (inherits(x, "POSIXt")) x <- format_datetime(x)
  if (is.numeric(x)) {
    vals <- if (is.integer(x)) lapply(x, json_integer) else lapply(x, json_number)
    if (length(vals) == 1 && !as_is) {
      return(vals[[1]])
    }
    return(vals)
  }
  if (is.character(x) || is.logical(x)) {
    if (as_is) {
      return(as.list(unclass(x)))
    }
    if (length(x) == 1) {
      return(if (is.na(x)) NULL else unclass(x))
    }
    return(as.list(x))
  }
  envelope_error(sprintf("value of class %s is not JSON-serializable", class(x)[1]))
}

encode_json <- function(x) {
  as.character(jsonlite::toJSON(to_json_value(x),
    auto_unbox = TRUE, json_verbatim = TRUE, null = "null",
    na = "null", digits = NA
  ))
}

canonical_bytes <- function(x) nchar(encode_json(x), type = "bytes")

decode_json <- function(text) jsonlite::fromJSON(text, simplifyVector = FALSE)

# ---- dates and datetimes ---------------------------------------------------------------------------

# ISO 8601 with a zero-padded four-digit year (format() prints year 1 as "1"). NA stays NA.
format_date <- function(x) {
  lt <- as.POSIXlt(x)
  ifelse(is.na(x), NA_character_, sprintf("%04d-%02d-%02d", lt$year + 1900L, lt$mon + 1L, lt$mday))
}

# UTC, `T` separator, 0 or exactly 3 fractional digits (truncated), `Z`.
# Milliseconds are taken with a 1 us tolerance so 0.123 stored as 0.12299999 still prints .123.
format_datetime <- function(x) {
  secs <- as.numeric(x)
  whole <- floor(secs)
  ms <- floor((secs - whole) * 1000 + 1e-3) # 1 us tolerance: doubles near 1.7e9 carry ~0.2 us of error
  whole <- ifelse(ms >= 1000, whole + 1, whole)
  ms <- ifelse(ms >= 1000, 0, ms)
  lt <- as.POSIXlt(whole, origin = "1970-01-01", tz = "UTC")
  base <- sprintf("%04d-%02d-%02dT%02d:%02d:%02d", lt$year + 1900L, lt$mon + 1L, lt$mday, lt$hour, lt$min, as.integer(lt$sec))
  ifelse(is.na(secs), NA_character_, paste0(base, ifelse(ms > 0, sprintf(".%03d", ms), ""), "Z"))
}

parse_datetime <- function(s) {
  m <- regmatches(s, regexec(datetime_re, s))[[1]]
  if (length(m) == 0) envelope_error("datetime must be RFC 3339 with a T separator and a zone")
  y <- as.integer(m[2])
  mo <- as.integer(m[3])
  d <- as.integer(m[4])
  hh <- as.integer(m[5])
  mi <- as.integer(m[6])
  ss <- as.integer(m[7])
  frac <- m[8]
  zone <- m[9]
  if (mo < 1 || mo > 12 || hh > 23 || mi > 59 || ss > 60) envelope_error("datetime is out of range")
  base <- ISOdatetime(y, mo, d, hh, mi, ss, tz = "UTC")
  if (is.na(base)) envelope_error("datetime is out of range")
  digits <- if (nzchar(frac)) substr(frac, 2, 4) else ""
  ms <- if (nzchar(digits)) as.integer(paste0(digits, strrep("0", 3 - nchar(digits)))) / 1000 else 0
  offset <- 0
  if (zone != "Z") {
    sign <- if (substr(zone, 1, 1) == "+") 1 else -1
    offset <- sign * (as.integer(substr(zone, 2, 3)) * 3600 + as.integer(substr(zone, 5, 6)) * 60)
  }
  as.POSIXct(as.numeric(base) + ms - offset, origin = "1970-01-01", tz = "UTC")
}

parse_date <- function(s) {
  if (!grepl(date_re, s)) envelope_error("date must be YYYY-MM-DD")
  d <- as.Date(s, format = "%Y-%m-%d")
  if (is.na(d)) envelope_error("date is not a real calendar date")
  d
}

# ---- table: R -> JSON -----------------------------------------------------------------------------

infer_column_type <- function(x) {
  if (inherits(x, "integer64")) {
    return("integer64")
  }
  if (inherits(x, "Date")) {
    return("date")
  }
  if (inherits(x, "POSIXt")) {
    return("datetime")
  }
  if (is.factor(x) || is.character(x)) {
    return("string")
  }
  if (is.logical(x)) {
    return("boolean")
  }
  if (is.integer(x)) {
    return("integer")
  }
  if (is.numeric(x)) {
    return("number")
  }
  envelope_error(sprintf("cannot infer a column type for class %s", class(x)[1]))
}

encode_cell <- function(v, type) {
  if (length(v) != 1 || is.na(v)) {
    return(NULL)
  }
  switch(type,
    string = {
      if (!is.character(v) && !is.factor(v)) envelope_error("string column needs character values")
      as.character(v)
    },
    boolean = {
      if (!is.logical(v)) envelope_error("boolean column needs logical values")
      v
    },
    integer = {
      if (!is.numeric(v) || is.logical(v)) envelope_error("integer column needs numeric values")
      if (v != trunc(v)) envelope_error("integer column needs whole numbers")
      if (abs(v) > safe_int) envelope_error("integer beyond 2^53-1: declare the column integer64")
      as.numeric(v)
    },
    integer64 = {
      s <- if (is.character(v)) v else if (inherits(v, "integer64")) as.character(v) else sprintf("%.0f", v)
      if (!grepl(int64_re, s)) envelope_error("integer64 cells are decimal strings")
      s
    },
    number = {
      if (!is.numeric(v) || is.logical(v)) envelope_error("number column needs numeric values")
      if (!is.finite(v)) {
        return(NULL)
      }
      as.numeric(v)
    },
    date = {
      if (!inherits(v, "Date")) envelope_error("date column needs Date values")
      format_date(v)
    },
    datetime = {
      if (!inherits(v, "POSIXt")) envelope_error("datetime column needs POSIXct values")
      format_datetime(v)
    },
    envelope_error(sprintf("unknown column type '%s'", type))
  )
}

#' Build a fusion table from a data frame
#'
#' Column types are inferred from the R classes (`character`/factor to `string`, `integer` to
#' `integer`, `double` to `number`, `logical` to `boolean`, `Date` to `date`, `POSIXct` to
#' `datetime`, `bit64::integer64` to `integer64`) unless overridden with `types`. `NA`, `NaN`
#' and infinities encode as `null`.
#'
#' @param df A data frame (or tibble).
#' @param types Optional named character vector of column types, e.g. `c(n = "integer")` for a
#'   double column that holds whole numbers.
#' @return An object of class `fusion_table`, accepted as a handler return value or a body.
#' @export
fusion_table <- function(df, types = NULL) {
  if (!is.data.frame(df)) envelope_error("fusion_table() needs a data frame")
  names_ <- names(df)
  if (any(!nzchar(names_)) || anyDuplicated(names_)) envelope_error("column names must be unique and non-empty")
  col_types <- vapply(names_, function(n) {
    t <- if (!is.null(types) && n %in% names(types)) types[[n]] else infer_column_type(df[[n]])
    if (!t %in% column_types) envelope_error(sprintf("unknown column type '%s'", t))
    t
  }, character(1))
  columns <- lapply(seq_along(names_), function(j) list(name = names_[j], type = col_types[[j]]))
  n <- nrow(df)
  rows <- if (n == 0) {
    list()
  } else {
    lapply(seq_len(n), function(i) {
      lapply(seq_along(names_), function(j) {
        v <- df[[j]][i]
        if (is.factor(v)) v <- as.character(v)
        encode_cell(v, col_types[[j]])
      })
    })
  }
  structure(list(json = list(`__table__` = 1, columns = columns, rows = rows), columns = names_, types = col_types),
    class = "fusion_table"
  )
}

#' @export
print.fusion_table <- function(x, ...) {
  cat(sprintf(
    "<fusion_table %d x %d: %s>\n", length(x$json$rows), length(x$columns),
    paste(sprintf("%s:%s", x$columns, x$types), collapse = ", ")
  ))
  invisible(x)
}

# ---- table: JSON -> R -----------------------------------------------------------------------------

is_table_body <- function(body) is_json_object(body) && "__table__" %in% names(body)

decode_cell <- function(v, type, name) {
  if (is.null(v)) {
    return(NA)
  }
  where <- sprintf("column '%s'", name)
  switch(type,
    string = {
      if (!is.character(v) || length(v) != 1) envelope_error(paste(where, "expected a string"))
      v
    },
    boolean = {
      if (!is.logical(v) || length(v) != 1 || is.na(v)) envelope_error(paste(where, "expected a boolean"))
      v
    },
    integer = {
      if (!is.numeric(v) || is.logical(v) || length(v) != 1) envelope_error(paste(where, "expected an integer"))
      if (v != trunc(v)) envelope_error(paste(where, "expected a whole number"))
      if (abs(v) > safe_int) envelope_error(paste(where, "integer beyond 2^53-1 must be integer64"))
      as.numeric(v)
    },
    integer64 = {
      if (!is.character(v) || !grepl(int64_re, v)) envelope_error(paste(where, "integer64 cells are decimal strings"))
      v
    },
    number = {
      if (!is.numeric(v) || is.logical(v) || length(v) != 1) envelope_error(paste(where, "expected a number"))
      as.numeric(v)
    },
    date = {
      if (!is.character(v)) envelope_error(paste(where, "expected a date string"))
      parse_date(v)
    },
    datetime = {
      if (!is.character(v)) envelope_error(paste(where, "expected a datetime string"))
      parse_datetime(v)
    },
    envelope_error(sprintf("unknown column type '%s'", type))
  )
}

empty_column <- function(type) {
  switch(type,
    string = character(0),
    integer64 = character(0),
    integer = integer(0),
    number = numeric(0),
    boolean = logical(0),
    date = as.Date(character(0)),
    datetime = as.POSIXct(character(0), tz = "UTC")
  )
}

assemble_column <- function(cells, type) {
  if (length(cells) == 0) {
    return(empty_column(type))
  }
  switch(type,
    string = ,
    integer64 = vapply(cells, function(c) if (is.na(c[1])) NA_character_ else c, character(1)),
    boolean = vapply(cells, function(c) if (is.na(c[1])) NA else c, logical(1)),
    number = vapply(cells, function(c) if (is.na(c[1])) NA_real_ else c, numeric(1)),
    integer = {
      x <- vapply(cells, function(c) if (is.na(c[1])) NA_real_ else c, numeric(1))
      if (all(is.na(x) | abs(x) <= .Machine$integer.max)) as.integer(x) else x
    },
    date = as.Date(vapply(cells, function(c) if (is.na(c[1])) NA_real_ else as.numeric(c), numeric(1)), origin = "1970-01-01"),
    datetime = as.POSIXct(vapply(cells, function(c) if (is.na(c[1])) NA_real_ else as.numeric(c), numeric(1)),
      origin = "1970-01-01", tz = "UTC"
    )
  )
}

table_from_json <- function(obj) {
  if (!is_table_body(obj) || !identical(as.numeric(obj$`__table__`), 1) || is.logical(obj$`__table__`)) envelope_error("not a fusion table")
  if (!setequal(names(obj), c("__table__", "columns", "rows"))) envelope_error("table must have exactly __table__, columns, rows")
  cols <- obj$columns
  rows <- obj$rows
  if (!is_json_array(cols) || !is_json_array(rows)) envelope_error("columns and rows must be arrays")
  spec <- lapply(cols, function(c) {
    if (!is_json_object(c) || !setequal(names(c), c("name", "type")) || !is.character(c$name) || !nzchar(c$name)) {
      envelope_error("column must be {name, type} with a non-empty name")
    }
    if (!is.character(c$type) || !c$type %in% column_types) envelope_error(sprintf("unknown column type '%s'", as.character(c$type)))
    c
  })
  names_ <- vapply(spec, function(c) c$name, character(1))
  types <- vapply(spec, function(c) c$type, character(1))
  if (anyDuplicated(names_)) envelope_error("duplicate column names")
  ncol <- length(spec)
  decoded <- lapply(seq_along(rows), function(i) {
    r <- rows[[i]]
    if (!is_json_array(r) || length(r) != ncol) envelope_error(sprintf("row %d must be an array of %d cells", i - 1, ncol))
    lapply(seq_len(ncol), function(j) decode_cell(r[[j]], types[j], names_[j]))
  })
  columns <- lapply(seq_len(ncol), function(j) assemble_column(lapply(decoded, function(r) r[[j]]), types[j]))
  names(columns) <- names_
  df <- if (ncol == 0) {
    data.frame(matrix(nrow = length(rows), ncol = 0))
  } else {
    as.data.frame(columns, stringsAsFactors = FALSE, optional = TRUE, check.names = FALSE)
  }
  names(df) <- names_
  attr(df, "fusion_types") <- stats::setNames(types, names_)
  df
}

#' Decode a fusion table into a data frame
#'
#' @param x A `fusion_response` (from [fusion_request()]) whose body is a fusion table, or the
#'   decoded table body itself.
#' @return A data frame with typed columns: `string` to character, `integer` to integer (double
#'   when a value exceeds the 32-bit range), `integer64` to character, `number` to double,
#'   `boolean` to logical, `date` to `Date`, `datetime` to UTC `POSIXct`. `null` becomes `NA`.
#'   The column types travel as `attr(, "fusion_types")`.
#' @export
fusion_as_data_frame <- function(x) {
  body <- if (inherits(x, "fusion_response")) x$body else x
  if (!is_table_body(body)) stop("response body is not a fusion table; use $body")
  table_from_json(body)
}

# ---- envelopes -------------------------------------------------------------------------------

encode_query <- function(operation, params) {
  if (!is.character(operation) || length(operation) != 1 || !grepl(operation_re, operation) || nchar(operation) > 128) {
    envelope_error("operation must match ^[A-Za-z_][A-Za-z0-9_.-]*$ and be at most 128 characters")
  }
  if (!is.list(params)) envelope_error("params must be a list")
  if (length(params) > 0 && (is.null(names(params)) || any(!nzchar(names(params))))) envelope_error("params must be a named list")
  if (length(params) == 0) params <- empty_object()
  list(v = envelope_version, kind = "query", operation = operation, params = params, encoding = "json")
}

encode_response_ok <- function(body) {
  list(v = envelope_version, kind = "response", status = "ok", body = encode_body(body), encoding = "json")
}

encode_response_error <- function(code, message, detail = NULL) {
  if (!code %in% remote_codes) envelope_error(sprintf("unknown remote error code '%s'", code))
  err <- list(code = code, message = substr(as.character(message), 1, 65536))
  if (!is.null(detail) && length(detail) > 0) err$detail <- detail
  list(v = envelope_version, kind = "response", status = "error", error = err, encoding = "json")
}

# A fusion_table or data frame becomes table JSON; anything else must be JSON-able (checked at encode time).
encode_body <- function(body) {
  if (inherits(body, "fusion_table")) {
    return(body$json)
  }
  if (is.data.frame(body)) {
    return(fusion_table(body)$json)
  }
  if (is.null(body)) {
    return(NULL)
  }
  to_json_value(body)
}

# Decode a received envelope (a fromJSON(simplifyVector = FALSE) structure).
decode_envelope <- function(obj) {
  if (!is_json_object(obj)) envelope_error("envelope must be an object")
  # jsonlite keeps repeated member names as separate entries and `$` returns the first, so a handler that
  # walks the whole list could see a member the guards never checked. Python's parser cannot carry duplicates.
  if (anyDuplicated(names(obj))) envelope_error("envelope repeats a member name")
  v <- obj$v
  if (is.null(v) || is.logical(v) || !is.numeric(v) || v != envelope_version) envelope_error("unsupported envelope version")
  if (!is.null(obj$encoding) && !identical(obj$encoding, "json")) envelope_error("unsupported encoding")
  kind <- obj$kind
  if (identical(kind, "query")) {
    if (length(setdiff(names(obj), c("v", "kind", "operation", "params", "encoding"))) > 0 || !all(c("operation", "params") %in% names(obj))) {
      envelope_error("query must have exactly v, kind, operation, params[, encoding]")
    }
    op <- obj$operation
    if (!is.character(op) || length(op) != 1 || !grepl(operation_re, op) || nchar(op) > 128) envelope_error("invalid operation name")
    if (!is_json_object(obj$params)) envelope_error("params must be an object")
    if (anyDuplicated(names(obj$params))) envelope_error("params repeats a member name")
    return(list(kind = "query", operation = op, params = obj$params))
  }
  if (identical(kind, "response")) {
    status <- obj$status
    if (identical(status, "ok")) {
      if (length(setdiff(names(obj), c("v", "kind", "status", "body", "encoding"))) > 0 || !"body" %in% names(obj)) {
        envelope_error("ok response must have exactly v, kind, status, body[, encoding]")
      }
      if (is_table_body(obj$body)) table_from_json(obj$body)
      return(list(kind = "response", status = "ok", body = obj$body, error = NULL))
    }
    if (identical(status, "error")) {
      if (length(setdiff(names(obj), c("v", "kind", "status", "error", "encoding"))) > 0 || !"error" %in% names(obj)) {
        envelope_error("error response must have exactly v, kind, status, error[, encoding]")
      }
      err <- obj$error
      if (!is_json_object(err) || length(setdiff(names(err), c("code", "message", "detail"))) > 0 || !all(c("code", "message") %in% names(err))) {
        envelope_error("error must be {code, message[, detail]}")
      }
      if (!is.character(err$code) || !err$code %in% remote_codes) envelope_error("unknown remote error code")
      if (!is.character(err$message)) envelope_error("error.message must be a string")
      detail <- if (is.null(err$detail)) empty_object() else err$detail
      if (!is_json_object(detail)) envelope_error("error.detail must be an object")
      return(list(
        kind = "response", status = "error", body = NULL,
        error = list(code = err$code, message = err$message, detail = detail)
      ))
    }
    envelope_error("response status must be ok or error")
  }
  envelope_error("kind must be query or response")
}
