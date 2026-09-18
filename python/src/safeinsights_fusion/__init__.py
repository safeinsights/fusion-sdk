"""SafeInsights Fusion SDK for Python.

Researcher-facing client for the Enclave Fusion Framework. See the package README and
the repository's ``spec/`` directory for the cross-language contract this implements.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Envelope version this SDK speaks (spec/envelope.schema.json).
ENVELOPE_VERSION = 1

#: Local-API major version this SDK requires from ``GET /v1/info.apiVersion``.
API_MAJOR = 1

__all__ = ["API_MAJOR", "ENVELOPE_VERSION", "__version__"]
