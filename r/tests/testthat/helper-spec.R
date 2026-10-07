# Locate the repository root (for spec/ fixtures and tools/fake_tunnel_pair). Under R CMD check the
# tests run from a copy, so CI exports FUSION_SDK_ROOT; locally the source tree is found by walking up.
fusion_sdk_root <- function() {
  root <- Sys.getenv("FUSION_SDK_ROOT", "")
  if (nzchar(root) && file.exists(file.path(root, "spec", "envelope.schema.json"))) {
    return(normalizePath(root))
  }
  d <- normalizePath(testthat::test_path("."), mustWork = FALSE)
  for (i in 1:6) {
    if (file.exists(file.path(d, "spec", "envelope.schema.json"))) {
      return(d)
    }
    d <- dirname(d)
  }
  NULL
}

skip_without_root <- function() {
  if (is.null(fusion_sdk_root())) testthat::skip("FUSION_SDK_ROOT not set and the repository root was not found")
}

python_bin <- function() {
  p <- Sys.getenv("FUSION_PYTHON", "")
  if (nzchar(p)) {
    return(p)
  }
  unname(Sys.which("python3"))
}

skip_without_fake <- function() {
  skip_without_root()
  if (!nzchar(python_bin())) testthat::skip("python3 not found; the fake tunnel pair needs it")
}

# Structural JSON equality with number semantics (2 == 2.0), logicals distinct from numbers.
json_equal <- function(a, b) {
  if (is.null(a) || is.null(b)) {
    return(is.null(a) && is.null(b))
  }
  if (is.list(a) || is.list(b)) {
    if (!is.list(a) || !is.list(b) || length(a) != length(b)) {
      return(FALSE)
    }
    na <- names(a)
    nb <- names(b)
    if (is.null(na) != is.null(nb)) {
      return(FALSE)
    }
    if (!is.null(na)) {
      if (!setequal(na, nb) || anyDuplicated(na)) {
        return(FALSE)
      }
      return(all(vapply(na, function(k) json_equal(a[[k]], b[[k]]), logical(1))))
    }
    return(all(vapply(seq_along(a), function(i) json_equal(a[[i]], b[[i]]), logical(1))))
  }
  if (is.logical(a) || is.logical(b)) {
    return(is.logical(a) && is.logical(b) && identical(a, b))
  }
  if (is.numeric(a) && is.numeric(b)) {
    return(length(a) == length(b) && all(a == b))
  }
  identical(a, b)
}

read_json_file <- function(path) jsonlite::fromJSON(path, simplifyVector = FALSE)

# Reparse an R structure the way the wire would deliver it.
wire_roundtrip <- function(x) safeinsights.fusion:::decode_json(safeinsights.fusion:::encode_json(x))

fast_settings <- function(...) {
  safeinsights.fusion:::fusion_settings_with(safeinsights.fusion::fusion_settings(character(0)),
    ready_timeout_s = 15, ready_poll_s = 0.1, ready_poll_max_s = 0.5, poll_http_timeout_s = 5, http_timeout_s = 5,
    round_timeout_s = 5, round_max_reissues = 2L, retry_base_ms = 20, retry_max_ms = 200, memo_max_entries = 8L,
    warn_bytes = 2^20, log_level = "DEBUG", ...
  )
}

# Capture SDK log lines for the duration of `expr`.
capture_fusion_log <- function(expr) {
  lines <- character(0)
  old <- safeinsights.fusion::fusion_log_sink(function(line) lines <<- c(lines, line))
  on.exit(safeinsights.fusion::fusion_log_sink(old), add = TRUE)
  force(expr)
  lines
}
