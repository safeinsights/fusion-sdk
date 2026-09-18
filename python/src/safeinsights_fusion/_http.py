"""Minimal HTTP client over urllib: bearer auth, explicit per-call timeouts, JSON both ways,
no proxies (the tunnel is a sidecar), and content-free exceptions."""

from __future__ import annotations

import http.client
import json
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

JSON = Any

#: Sentinel body for a response whose bytes were not valid JSON.
INVALID_JSON = object()


class TransportError(Exception):
    """Connection refused/reset, DNS failure, or timeout. Content-free."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: JSON  # parsed JSON, None for an empty body, or INVALID_JSON

    @property
    def terminal(self) -> bool:
        return isinstance(self.body, dict) and self.body.get("terminal") is True


class HttpClient:
    def __init__(self, endpoint: str, token: str, *, default_timeout_s: float = 10.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._token = token
        self.default_timeout_s = default_timeout_s
        # No proxies: the enclave may export HTTP(S)_PROXY for egress, which must never apply to the sidecar.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: JSON = None, *, timeout_s: float | None = None) -> HttpResponse:
        data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # S310: the scheme and host were validated in _config (http://, enclave-local only).
        req = urllib.request.Request(self.endpoint + path, data=data, method=method)  # noqa: S310
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        timeout = self.default_timeout_s if timeout_s is None else timeout_s
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, _parse(resp.read()))
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            return HttpResponse(exc.code, _parse(raw))
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
            raise TransportError(type(exc).__name__) from None


def _parse(raw: bytes) -> JSON:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        return INVALID_JSON


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


@dataclass(frozen=True)
class Backoff:
    """Exponential backoff with full jitter, in seconds."""

    base_ms: int = 250
    max_ms: int = 10_000

    def delay(self, attempt: int) -> float:
        cap = min(self.max_ms, self.base_ms * (2 ** max(0, attempt)))
        return random.uniform(0, cap) / 1000.0  # noqa: S311 - jitter, not security


__all__ = ["INVALID_JSON", "Backoff", "HttpClient", "HttpResponse", "TransportError"]
