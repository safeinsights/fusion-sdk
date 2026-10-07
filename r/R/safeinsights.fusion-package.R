#' safeinsights.fusion: SafeInsights Fusion SDK
#'
#' Researcher-facing client for the SafeInsights Enclave Fusion Framework.
#' Destination code calls [fusion_connect()] then [fusion_request()] on a peer
#' handle; source code registers operation handlers with [fusion_operations()]
#' and runs [fusion_serve()]. Researcher code never touches the transport.
#'
#' The package mirrors the Python package `safeinsights_fusion` behavior for
#' behavior; both are written from the contract in the repository's `spec/`
#' directory.
#'
#' @importFrom curl new_handle handle_setheaders handle_setopt curl_fetch_memory
#' @importFrom jsonlite fromJSON toJSON
#' @keywords internal
"_PACKAGE"

## usethis namespace: start
## usethis namespace: end
NULL
