"""fake_tunnel_pair — two Fusion Tunnel App local APIs joined by an in-memory relay.

Stdlib only. Implements spec/local-api.md (including asks T1-T6) with the fault injection
described in spec/scenarios/README.md, so both SDKs can be built and proven before the
real tunnel exists. This is a test double, not a product: no crypto, no networking beyond
the two loopback listeners per leg.
"""

from .scenarios import Caps, Faults, LegSpec, Scenario, load_scenario
from .server import FakeTunnelPair, LegEndpoints, TunnelEndpoint

__all__ = [
    "Caps",
    "FakeTunnelPair",
    "Faults",
    "LegEndpoints",
    "LegSpec",
    "Scenario",
    "TunnelEndpoint",
    "load_scenario",
]
