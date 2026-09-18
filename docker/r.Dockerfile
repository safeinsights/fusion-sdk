# Dependency-footprint tripwire (ADR 0003): the package must install on a stock
# r-base image with only curl and jsonlite (from Debian's r-cran-* binaries).
FROM r-base:latest
RUN apt-get update \
 && apt-get install -y --no-install-recommends r-cran-curl r-cran-jsonlite ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /sdk
COPY r/ /sdk/r/
RUN R CMD INSTALL --no-docs --no-multiarch /sdk/r \
 && Rscript -e 'library(sifusion); cat("sifusion", as.character(packageVersion("sifusion")), R.version.string, "\n")'
COPY docker/smoke-r.sh /sdk/smoke.sh
ENTRYPOINT ["/bin/sh", "/sdk/smoke.sh"]
