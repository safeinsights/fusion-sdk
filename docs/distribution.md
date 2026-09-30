# Distribution and release

Decision A11 / ADR 0003: Data Partners install the SDK in their base images and rebuild on their own
schedule; vendoring is the documented fallback. Package names are working names until A2 closes.

## Release procedure

1. Bump the shared version in `python/src/safeinsights_fusion/__init__.py`, `python/pyproject.toml`,
   `r/DESCRIPTION` (`Version:`) and move the `Unreleased` section of `CHANGELOG.md` under the new
   version with the date. Update `docs/compatibility.md` with the tunnel `apiVersion` range tested.
2. Open a PR; `checks.yml` and `matrix.yml` must be green.
3. Tag `vX.Y.Z` on `main`. `release.yml` then:
   - builds the Python sdist and wheel (`uv build --project python`) and publishes them to PyPI with
     **trusted publishing** (no long-lived token; the PyPI project must have the GitHub environment
     `pypi` registered as a trusted publisher),
   - builds the R source tarball (`R CMD build r`),
   - generates a CycloneDX **SBOM** for the repository with Trivy and fails on HIGH/CRITICAL license findings,
   - creates the GitHub release with the wheel, sdist, R tarball and SBOM attached.
4. R users install from **r-universe** (`install.packages("sifusion", repos = "https://safeinsights.r-universe.dev")`)
   once the `safeinsights` universe lists this repository (a `packages.json` entry pointing at the
   `r/` subdir), or with `remotes::install_github("safeinsights/fusion-sdk", subdir = "r")`.
   A CRAN submission is a later milestone: `docs/cran-checklist.md`.

## Base images (ask S2)

- Python: `pip install safeinsights-fusion` (optionally `[pandas]`) in the image at `org_code_env.url`.
- R: `install.packages("sifusion", repos = c("https://safeinsights.r-universe.dev", getOption("repos")))`.
- The Docker smoke (`docker/compose.smoke.yml`) proves both installs on stock `python:3.12-slim` and
  `r-base`; the `r-base-ecr` service does the same against `public.ecr.aws/docker/library/r-base` for
  images that pull from ECR Public rather than Docker Hub.

## Vendoring (fallback)

```sh
sh python/vendor.sh  ./drop      # -> ./drop/safeinsights_fusion/  (import safeinsights_fusion)
Rscript r/vendor.R   ./drop      # -> ./drop/sifusion.R + ./drop/sifusion/*.R  (source("sifusion.R"))
```

Copy the drop into the research container's `WORKDIR` alongside the researcher's files; nothing
else is installed. `python3 tools/test_vendored.py` proves both drops against the fake pair exactly
this way. Vendored R code exposes the functions unprefixed (`fusion_connect()` instead of
`sifusion::fusion_connect()`); the examples use the unprefixed form so they work either way.

## Supply chain

The SDK sits inside the supply-chain boundary of every Data Partner image (threat model SC-02):
no runtime dependencies in Python, only `curl` and `jsonlite` in R, no compiled code, no network
calls other than the tunnel. Trivy runs on every push (`checks.yml`) and the release attaches an SBOM.
