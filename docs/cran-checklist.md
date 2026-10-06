# CRAN submission checklist (later milestone)

- [ ] Package name decided (A2) and free on CRAN (`safeinsights.fusion` was free on 2026-09-18; decided 2026-10-06).
- [ ] `R CMD check --as-cran` clean on R-oldrel, R-release, R-devel (Linux, macOS, Windows via R-hub).
- [ ] Tests that need `python3` or the repository (`FUSION_SDK_ROOT`) skip cleanly when absent;
      they already do (`skip_without_fake()`), so CRAN runs the unit and simulator tests only.
- [ ] `Description:` reviewed; `URL`/`BugReports` public.
- [ ] No writes outside `tempdir()`; no network calls in examples or tests (the fake pair is loopback
      and skipped on CRAN).
- [ ] `cran-comments.md` filled in; `NEWS.md` mirrors `CHANGELOG.md`.
- [ ] License `AGPL (>= 3)` with no `LICENSE` file in the tarball (standard abbreviation).
