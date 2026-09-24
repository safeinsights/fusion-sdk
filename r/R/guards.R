# Source-side guards (ADR 0002): maxDistinctPersonIds before the handler, minGroupSize after it.
# A breach refuses the whole round with GUARD_REFUSED; there is no partial result.

cardinalities <- c("aggregate", "per-group", "per-record")

guards_from_info <- function(guards) {
  if (is.null(guards)) {
    return(NULL)
  }
  g <- function(key) {
    v <- guards[[key]]
    if (is.numeric(v) && !is.logical(v) && length(v) == 1 && v > 0) as.integer(v) else NULL
  }
  structure(list(max_distinct_person_ids = g("maxDistinctPersonIds"), min_group_size = g("minGroupSize")), class = "fusion_guards")
}

guards_enabled <- function(guards) {
  !is.null(guards) && (!is.null(guards$max_distinct_person_ids) || !is.null(guards$min_group_size))
}

guard_refused <- function(guard, limit, message, observed = NULL) {
  stop(structure(list(message = message, call = NULL, guard = guard, limit = limit, observed = observed),
    class = c("fusion_guard_refused", "error", "condition")
  ))
}

guard_detail <- function(e) {
  d <- list(guard = e$guard, limit = e$limit)
  if (!is.null(e$observed)) d$observed <- e$observed
  d
}

# Stable key for a Person-ID value; 1 and 1.0 are the same id.
canonical_scalar <- function(v) {
  if (is.null(v)) {
    return("null")
  }
  if (is.character(v) && length(v) == 1) {
    return(paste0("s:", v))
  }
  if (is.logical(v) && length(v) == 1) {
    return(paste0("b:", v))
  }
  if (is.numeric(v) && length(v) == 1) {
    if (!is.na(v) && v == trunc(v)) {
      return(paste0("n:", sprintf("%.0f", v)))
    }
    return(paste0("n:", shortest_number(v)))
  }
  paste0("j:", encode_json(v))
}

# Distinct values of a param: a list counts its unique members, a scalar counts one, missing counts zero.
distinct_count <- function(values) {
  if (is.null(values)) {
    return(0L)
  }
  if (is.list(values) && is.null(names(values))) {
    return(length(unique(vapply(values, canonical_scalar, character(1)))))
  }
  if (is.atomic(values) && length(values) > 1) {
    return(length(unique(vapply(as.list(values), canonical_scalar, character(1)))))
  }
  1L
}

# The Person-ID parameter must be absent, one scalar, or a flat array of scalars. Objects and nested
# arrays count as one value in distinct_count() but unlist() flattens them, which would let a
# destination bypass maxDistinctPersonIds.
person_id_shape_ok <- function(values) {
  if (is.null(values)) {
    return(TRUE)
  }
  if (is.atomic(values)) {
    return(length(values) >= 1 && !anyNA(values))
  }
  if (is_json_array(values)) {
    return(all(vapply(values, is_scalar, logical(1))))
  }
  FALSE
}

check_query <- function(params, spec, guards) {
  if (is.null(guards) || is.null(guards$max_distinct_person_ids) || is.null(spec$person_id_param)) {
    return(invisible(NULL))
  }
  values <- params[[spec$person_id_param]]
  if (!person_id_shape_ok(values)) {
    guard_refused("maxDistinctPersonIds", guards$max_distinct_person_ids,
      sprintf("'%s' must be a flat array of scalar Person-ID values so they can be counted", spec$person_id_param)
    )
  }
  n <- distinct_count(values)
  if (n > guards$max_distinct_person_ids) {
    guard_refused("maxDistinctPersonIds", guards$max_distinct_person_ids,
      sprintf("query names %d distinct values of '%s'; the limit is %d", n, spec$person_id_param, guards$max_distinct_person_ids),
      observed = n
    )
  }
  invisible(NULL)
}

find_count_column <- function(df, types, declared) {
  if (!is.null(declared)) {
    if (!declared %in% names(df)) guard_refused("minGroupSize", 0L, sprintf("declared count column '%s' is not in the result", declared))
    return(declared)
  }
  candidates <- names(df)[types %in% c("integer", "integer64")]
  if (length(candidates) != 1) {
    guard_refused("minGroupSize", 0L, sprintf(
      "cannot identify the group-count column (%d integer columns); register the operation with count_column",
      length(candidates)
    ))
  }
  candidates
}

check_result <- function(body_json, spec, guards) {
  if (is.null(guards) || is.null(guards$min_group_size) || !identical(spec$cardinality, "per-group")) {
    return(invisible(NULL))
  }
  limit <- guards$min_group_size
  if (!is_table_body(body_json)) guard_refused("minGroupSize", limit, "per-group results must be a fusion table so group sizes can be checked")
  df <- table_from_json(body_json)
  types <- attr(df, "fusion_types")
  col <- tryCatch(find_count_column(df, types, spec$count_column),
    fusion_guard_refused = function(e) guard_refused("minGroupSize", limit, conditionMessage(e))
  )
  values <- df[[col]]
  if (is.character(values)) values <- suppressWarnings(as.numeric(values))
  if (any(is.na(values)) || any(values < limit)) {
    guard_refused("minGroupSize", limit, sprintf("a group in '%s' is smaller than the minimum group size %d", col, limit))
  }
  invisible(NULL)
}

# Compare the registry with the manifest's approved operation list (ask T3). Returns problems.
preflight <- function(operations, approved) {
  if (is.null(approved)) {
    return(character(0))
  }
  problems <- character(0)
  by_name <- list()
  for (op in approved) {
    if (is.character(op$name)) by_name[[op$name]] <- if (is.character(op$cardinality)) op$cardinality else ""
  }
  for (name in names(operations)) {
    spec <- operations[[name]]
    if (is.null(by_name[[name]])) {
      problems <- c(problems, sprintf("operation '%s' is registered but not in the approved list", name))
    } else if (nzchar(by_name[[name]]) && by_name[[name]] != spec$cardinality) {
      problems <- c(problems, sprintf("operation '%s' is registered as %s but approved as %s", name, spec$cardinality, by_name[[name]]))
    }
  }
  for (name in names(by_name)) {
    if (is.null(operations[[name]])) problems <- c(problems, sprintf("operation '%s' is approved but no handler is registered", name))
  }
  problems
}
