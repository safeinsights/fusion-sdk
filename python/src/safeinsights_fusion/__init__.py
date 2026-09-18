"""SafeInsights Fusion SDK for Python.

Destination::

    from safeinsights_fusion import Fusion

    with Fusion.connect() as fusion:
        r = fusion.peer("dp-a").request("counts_by_group", {"person_ids": ids, "group_by": "grade"})
        df = r.to_pandas()

Source::

    from safeinsights_fusion import operations, serve

    @operations.register("counts_by_group", person_id_param="person_ids", cardinality="per-group")
    def counts_by_group(params, ctx): ...

    serve()

Researcher code never sees a URL, a token, a correlationId, an ACK or a retransmission. The
cross-language contract this package implements lives in the repository's ``spec/`` directory.
"""

from __future__ import annotations

from ._config import Settings
from ._envelope import Column, EnvelopeError, Table
from ._transport import Budget
from .destination import Fusion, Peer, PeerInfo, Response
from .errors import (
    ConcurrencyError,
    ConfigError,
    FusionError,
    LimitExceededError,
    NotReadyError,
    ProtocolError,
    RemoteError,
    RoundTimeoutError,
    SessionError,
    TerminalError,
)

__version__ = "0.1.0"

#: Envelope version this SDK speaks (spec/envelope.schema.json).
ENVELOPE_VERSION = 1

#: Local-API major version this SDK requires from ``GET /v1/info.apiVersion``.
API_MAJOR = 1

__all__ = [
    "API_MAJOR",
    "ENVELOPE_VERSION",
    "Budget",
    "Column",
    "ConcurrencyError",
    "ConfigError",
    "EnvelopeError",
    "Fusion",
    "FusionError",
    "LimitExceededError",
    "NotReadyError",
    "Peer",
    "PeerInfo",
    "ProtocolError",
    "RemoteError",
    "Response",
    "RoundTimeoutError",
    "SessionError",
    "Settings",
    "Table",
    "TerminalError",
    "__version__",
]
