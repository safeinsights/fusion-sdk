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

Researcher code never sees a URL, a token, a correlationId or a retransmission. The
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
from .guards import Guards, OperationSpec
from .simulate import SimFaults, Simulator, simulate
from .source import Context, OperationRegistry, operations, serve

__version__ = "0.3.0"

#: Envelope version this SDK speaks (spec/envelope.schema.json).
ENVELOPE_VERSION = 1

#: Local-API major version this SDK requires from ``GET /v1/info.apiVersion``.
API_MAJOR = 2

__all__ = [
    "API_MAJOR",
    "ENVELOPE_VERSION",
    "Budget",
    "Column",
    "ConcurrencyError",
    "ConfigError",
    "Context",
    "EnvelopeError",
    "Fusion",
    "FusionError",
    "Guards",
    "LimitExceededError",
    "NotReadyError",
    "OperationRegistry",
    "OperationSpec",
    "Peer",
    "PeerInfo",
    "ProtocolError",
    "RemoteError",
    "Response",
    "RoundTimeoutError",
    "SessionError",
    "Settings",
    "SimFaults",
    "Simulator",
    "Table",
    "TerminalError",
    "__version__",
    "operations",
    "serve",
    "simulate",
]
