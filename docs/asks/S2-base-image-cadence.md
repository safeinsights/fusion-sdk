# S2 — Base-image cadence for the fusion SDK

**Repo:** setup-app / Data Partner onboarding docs · **Requested by:** fusion-sdk (ADR 0003)

Decided 2026-09-17: Data Partners rebuild their base images to pick up SDK releases (the `osenclave` model). The containerizer only appends researcher code.

**Ask.** Document for Data Partners: install `safeinsights-fusion` (PyPI) / `sifusion` (r-universe or `remotes::install_github("safeinsights/fusion-sdk", subdir = "r")`) in the base image at `org_code_env.url`; SafeInsights publishes a compatibility table against the tunnel `apiVersion` with every release and offers `analysis/r/base-container` (plus a Python sibling) as recommended parents. The hub's image is SafeInsights-built.
