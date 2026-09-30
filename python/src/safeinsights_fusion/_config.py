"""Environment contract (spec/env.md) and tuning defaults (plan §0.3, all PROVISIONAL)."""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from .errors import ConfigError

Role = Literal["source", "destination"]

_LOCAL_HOSTS = {"localhost", "localhost.localdomain", "host.docker.internal"}


@dataclass(frozen=True)
class Settings:
    """Tuning knobs. Defaults are provisional until load-tested with the real tunnel (v2 §15.6)."""

    ready_timeout_s: float = 900.0
    ready_poll_s: float = 2.0
    ready_poll_max_s: float = 15.0
    poll_http_timeout_s: float = 40.0
    http_timeout_s: float = 10.0
    round_timeout_s: float = 600.0
    round_max_reissues: int = 3
    retry_base_ms: int = 250
    retry_max_ms: int = 10_000
    memo_max_entries: int = 256
    warn_bytes: int = 8 * 1024 * 1024
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def num(name: str, default: float) -> float:
            raw = e.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number") from exc
            if value < 0:
                raise ConfigError(f"{name} must be non-negative")
            return value

        return cls(
            ready_timeout_s=num("FUSION_READY_TIMEOUT_S", cls.ready_timeout_s),
            ready_poll_s=num("FUSION_READY_POLL_S", cls.ready_poll_s),
            poll_http_timeout_s=num("FUSION_POLL_HTTP_TIMEOUT_S", cls.poll_http_timeout_s),
            http_timeout_s=num("FUSION_HTTP_TIMEOUT_S", cls.http_timeout_s),
            round_timeout_s=num("FUSION_ROUND_TIMEOUT_S", cls.round_timeout_s),
            round_max_reissues=int(num("FUSION_ROUND_MAX_REISSUES", cls.round_max_reissues)),
            retry_base_ms=int(num("FUSION_RETRY_BASE_MS", cls.retry_base_ms)),
            retry_max_ms=int(num("FUSION_RETRY_MAX_MS", cls.retry_max_ms)),
            memo_max_entries=int(num("FUSION_MEMO_MAX_ENTRIES", cls.memo_max_entries)),
            warn_bytes=int(num("FUSION_WARN_BYTES", cls.warn_bytes)),
            log_level=(e.get("FUSION_LOG_LEVEL") or cls.log_level).upper(),
        )


@dataclass(frozen=True)
class TunnelConfig:
    """One tunnel = one leg: an enclave-local endpoint and its bearer token."""

    label: str
    endpoint: str
    token: str


def read_role(env: Mapping[str, str] | None = None) -> Role:
    e = os.environ if env is None else env
    role = (e.get("FUSION_ROLE") or "").strip().lower()
    if role not in ("source", "destination"):
        raise ConfigError("FUSION_ROLE must be 'source' or 'destination'")
    return "source" if role == "source" else "destination"


def validate_endpoint(url: str, *, label: str = "") -> str:
    """Accept only enclave-local http:// endpoints (spec/env.md)."""
    where = f" ({label})" if label else ""
    parts = urlsplit(url.strip())
    if parts.scheme != "http":
        raise ConfigError(f"tunnel endpoint{where} must use http:// (the tunnel is an enclave-local sidecar)")
    host = parts.hostname
    if not host or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"tunnel endpoint{where} must be http://host[:port] with no path, query or credentials")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        if not (addr.is_loopback or addr.is_private or addr.is_link_local):
            raise ConfigError(f"tunnel endpoint{where} must be a loopback, private or link-local address")
    elif host.lower() not in _LOCAL_HOSTS and "." in host:
        raise ConfigError(f"tunnel endpoint{where} must be a bare service name, localhost, or a private IP")
    return url.strip().rstrip("/")


def read_tunnels(env: Mapping[str, str] | None = None) -> list[TunnelConfig]:
    """Parse FUSION_TUNNEL_ENDPOINT(S)/TOKEN(S) into one TunnelConfig per leg, in env order."""
    e = os.environ if env is None else env
    single_ep, single_tok = e.get("FUSION_TUNNEL_ENDPOINT"), e.get("FUSION_TUNNEL_TOKEN")
    map_ep, map_tok = e.get("FUSION_TUNNEL_ENDPOINTS"), e.get("FUSION_TUNNEL_TOKENS")
    if single_ep and map_ep:
        raise ConfigError("set FUSION_TUNNEL_ENDPOINT or FUSION_TUNNEL_ENDPOINTS, not both")
    if single_ep:
        if not single_tok:
            raise ConfigError("FUSION_TUNNEL_TOKEN is required with FUSION_TUNNEL_ENDPOINT")
        return [TunnelConfig("default", validate_endpoint(single_ep), single_tok)]
    if not map_ep:
        raise ConfigError("FUSION_TUNNEL_ENDPOINT or FUSION_TUNNEL_ENDPOINTS is required")
    if not map_tok:
        raise ConfigError("FUSION_TUNNEL_TOKENS is required with FUSION_TUNNEL_ENDPOINTS")
    try:
        endpoints = json.loads(map_ep)
        tokens = json.loads(map_tok)
    except json.JSONDecodeError as exc:
        raise ConfigError("FUSION_TUNNEL_ENDPOINTS and FUSION_TUNNEL_TOKENS must be JSON objects") from exc
    if not isinstance(endpoints, dict) or not isinstance(tokens, dict) or not endpoints:
        raise ConfigError("FUSION_TUNNEL_ENDPOINTS and FUSION_TUNNEL_TOKENS must be non-empty JSON objects")
    if set(endpoints) != set(tokens):
        raise ConfigError("FUSION_TUNNEL_ENDPOINTS and FUSION_TUNNEL_TOKENS must have the same keys")
    out: list[TunnelConfig] = []
    for label, ep in endpoints.items():
        tok = tokens[label]
        if not isinstance(ep, str) or not isinstance(tok, str) or not tok:
            raise ConfigError(f"FUSION_TUNNEL_ENDPOINTS/TOKENS entry {label!r} must be strings")
        out.append(TunnelConfig(str(label), validate_endpoint(ep, label=str(label)), tok))
    return out


__all__ = ["Role", "Settings", "TunnelConfig", "read_role", "read_tunnels", "validate_endpoint"]
