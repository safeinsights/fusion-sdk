"""Package-level smoke tests."""

from __future__ import annotations

import safeinsights_fusion


def test_version_is_semver() -> None:
    parts = safeinsights_fusion.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_contract_constants() -> None:
    assert safeinsights_fusion.ENVELOPE_VERSION == 1
    assert safeinsights_fusion.API_MAJOR == 1
