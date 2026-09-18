#!/bin/sh
# Smoke: the package installed from source imports, and the fake tunnel's /v1/info is reachable.
# Phase 2/4 add the SDK's own `doctor` here.
set -eu
Rscript - <<'R'
url <- paste0(Sys.getenv("FUSION_TUNNEL_ENDPOINT"), "/v1/info")
h <- curl::new_handle()
curl::handle_setheaders(h, Authorization = paste("Bearer", Sys.getenv("FUSION_TUNNEL_TOKEN")))
ok <- FALSE
for (i in 1:30) {
  res <- tryCatch(curl::curl_fetch_memory(url, handle = h), error = function(e) NULL)
  if (!is.null(res) && res$status_code == 200) { ok <- TRUE; break }
  Sys.sleep(1)
}
if (!ok) stop("fake tunnel never became reachable")
cat("fake tunnel reachable: 200\n")
R
