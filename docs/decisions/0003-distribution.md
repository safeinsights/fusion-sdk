# ADR 0003 — Distribution: pre-installed in Data Partner base images; vendoring as fallback

**Status:** accepted 2026-09-17 (product owner, plan §11 Q4, Q6) · **Date recorded:** 2026-09-18

## Context

The research-container image is assembled by `crane mutate` (`iac/management-app/codebuild/scripts/containerizer.ts`): the researcher's files are appended at the base image's `WORKDIR`; there is no `pip install` or `install.packages()` at packaging time. Today `osenclave` reaches the container by being installed in the Data Partner's base image (`org_code_env.url`).

## Decision

1. **Primary path:** Data Partners install the SDK in their base images and rebuild on their own schedule. SafeInsights publishes every release to PyPI and to r-universe / GitHub with a changelog and a compatibility table against the tunnel `apiVersion`, and offers `analysis/r/base-container` (plus a Python sibling) as recommended parents.
2. **Fallback:** `python/vendor.sh` and `r/vendor.R` emit a single-directory drop that works when copied next to the researcher's code, because both packages are pure-language with only ubiquitous dependencies (Python: stdlib; R: `curl`, `jsonlite`).
3. **Compatibility is a hard requirement** (plan Risk 8): a study may pair an older SDK on one side with a newer one on the other. Within a major version the envelope stays backward compatible, `connect()`/`serve()` check `apiVersion` major, and the cross-language matrix also runs the previous minor against the current.
4. **Out of scope:** the TOA upload helper. Result release stays with the Data Partner package (`osenclave::toa_results_upload()` today).
5. **License:** AGPL-3.0-or-later, matching setup-app and the Trusted Output App (plan §11 Q9).

## Naming check (2026-09-18, feeds the open A2 decision)

| Registry | Name | Result |
| :-- | :-- | :-- |
| PyPI | `safeinsights-fusion` / `safeinsights_fusion` | free (404) |
| PyPI | `fusion-sdk` | **taken** — "JPMC Fusion Developer Tools" 0.0.4; do not use |
| CRAN | `sifusion` | free (no package page) |
| CRAN | `fusion` | free, but too generic and easy to squat; not recommended |
| r-universe | `safeinsights.r-universe.dev` | not yet created |

Working names (`safeinsights_fusion` / `sifusion`) remain free; A2 stays open for the product owner and is due before Phase 7 publishes anything.

## Consequences

- The Docker smoke on `python:3.12-slim` and `r-base` (Phase 1) is the dependency-footprint tripwire.
- A release that adds a compiled or heavy dependency violates this ADR and needs a new one.
