"""fake_tunnel_pair — two Fusion Tunnel App local APIs joined by an in-memory relay.

Stdlib only. Implements spec/local-api.md (local API 2.0) with the fault injection described in
spec/scenarios/README.md, so both SDKs can be proven without a real tunnel. This is a test
double, not a product: no crypto, no networking beyond the two loopback listeners per leg.
"""

from .scenarios import CAP_NAMES, Faults, LegSpec, Scenario, load_scenario
from .server import FakeTunnelPair, LegEndpoints, TunnelEndpoint

__all__ = [
    "CAP_NAMES",
    "FakeTunnelPair",
    "Faults",
    "LegEndpoints",
    "LegSpec",
    "Scenario",
    "TunnelEndpoint",
    "load_scenario",
]
