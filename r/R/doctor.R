#' Check the environment contract and each tunnel's `/v1/info`
#'
#' Content-free: prints labels, hosts, leg ids, peer slugs, roles, states and API versions; never a
#' token. Used by the Docker smoke and by researchers debugging a container.
#'
#' @param env Environment variables; defaults to [Sys.getenv()].
#' @param wait If `TRUE`, wait for `CHANNEL_UP` up to `FUSION_READY_TIMEOUT_S`.
#' @param json If `TRUE`, print a JSON report instead of text.
#' @return `TRUE` when every tunnel is reachable, has the expected role and a compatible API
#'   (and is `CHANNEL_UP` when `wait = TRUE`); `FALSE` otherwise. Invisible.
#' @export
fusion_doctor <- function(env = Sys.getenv(), wait = FALSE, json = FALSE) {
  env <- as.list(env)
  s <- fusion_settings(env)
  fusion_log_configure(s$log_level)
  report <- list(sdk = as.character(utils::packageVersion("safeinsights.fusion")), apiMajor = sdk_api_major, ok = TRUE, tunnels = list())
  cfg <- tryCatch(list(role = read_role(env), tunnels = read_tunnels(env)), fusion_error = function(e) e)
  if (inherits(cfg, "error")) {
    report$ok <- FALSE
    report$error <- conditionMessage(cfg)
    doctor_emit(report, json)
    return(invisible(FALSE))
  }
  report$role <- cfg$role
  deadline <- now_s() + s$ready_timeout_s
  for (t in cfg$tunnels) {
    host <- sub("^http://", "", t$endpoint)
    row <- list(label = t$label, endpoint = host, ok = FALSE)
    transport <- tunnel_transport(t$endpoint, t$token, t$label, s$http_timeout_s)
    info <- tryCatch(if (wait) wait_ready(transport, deadline, s, t$label) else transport$info(),
      fusion_retryable = function(e) structure(list(code = e$code), class = "unreachable"),
      fusion_error = function(e) e
    )
    if (inherits(info, "unreachable")) {
      row$error <- sprintf("unreachable (%s)", info$code)
    } else if (inherits(info, "error")) {
      row$error <- class(info)[1]
    } else {
      row$legId <- info$leg_id
      row$peerOrgSlug <- info$peer_org_slug
      row$role <- info$role
      row$state <- info$state
      row$apiVersion <- info$api_version
      row$roleMatches <- identical(info$role, cfg$role)
      row$apiCompatible <- identical(api_major(info$api_version), sdk_api_major)
      row$ok <- row$roleMatches && row$apiCompatible && (info$state == ready_state || !wait)
      fusion_log("doctor.info", "INFO", peer = t$label, state = info$state, apiVersion = info$api_version)
    }
    report$ok <- report$ok && isTRUE(row$ok)
    report$tunnels[[length(report$tunnels) + 1]] <- row
  }
  doctor_emit(report, json)
  invisible(isTRUE(report$ok))
}

doctor_emit <- function(report, json) {
  if (json) {
    cat(jsonlite::toJSON(report, auto_unbox = TRUE, pretty = TRUE, null = "null"), "\n")
    return(invisible(NULL))
  }
  cat(sprintf("safeinsights.fusion %s (local API major %d)\n", report$sdk, report$apiMajor))
  if (!is.null(report$error)) cat("config error:", report$error, "\n") else cat("role:", report$role, "\n")
  for (row in report$tunnels) {
    status <- if (isTRUE(row$ok)) "ok" else "FAIL"
    detail <- if (!is.null(row$error)) {
      row$error
    } else {
      sprintf("leg=%s peer=%s role=%s state=%s api=%s", row$legId, row$peerOrgSlug, row$role, row$state, row$apiVersion)
    }
    cat(sprintf("  [%s] %s @ %s: %s\n", status, row$label, row$endpoint, detail))
  }
  cat("result:", if (isTRUE(report$ok)) "ok" else "problems found", "\n")
  invisible(NULL)
}
