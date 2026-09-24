"""The tunnel's local API (spec/local-api.md) over HTTP, implementing `_transport.Transport`.

urllib only: bearer auth, explicit per-call timeouts, JSON both ways, no proxies (the tunnel is a
sidecar), content-free exceptions. Status codes are mapped once, in `_call`, per spec/errors.json.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ._transport import EMPTY, Budget, Delivered, Empty, Info, Retryable, Terminal, UnknownCorrelation
from .errors import ConcurrencyError, ConfigError, FusionError, ProtocolError

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


class TunnelClient:
    def __init__(self, endpoint: str, token: str, *, peer: str, http_timeout_s: float = 10.0) -> None:
        self._http = HttpClient(endpoint, token, default_timeout_s=http_timeout_s)
        self.peer = peer
        self.endpoint = endpoint

    # -- common mapping --

    def _call(self, method: str, path: str, body: JSON = None, *, timeout_s: float | None = None) -> HttpResponse:
        try:
            resp = self._http.request(method, path, body, timeout_s=timeout_s)
        except TransportError as exc:
            raise Retryable("TRANSPORT") from exc
        if resp.body is INVALID_JSON:
            raise ProtocolError(
                f"{method} {path}: tunnel returned a non-JSON body (status {resp.status})", peer=self.peer
            )
        code = resp.body.get("code") if isinstance(resp.body, dict) else None
        if resp.status == 401:
            raise ConfigError("bearer token rejected by the tunnel (401)", peer=self.peer)
        if resp.status == 403:
            raise ConfigError(f"{method} {path} is not allowed for this role (403): check FUSION_ROLE", peer=self.peer)
        if resp.status == 409:
            cid = resp.body.get("correlationId") if isinstance(resp.body, dict) else None
            raise ConcurrencyError(
                f"{method} {path}: 409 CONFLICT (another round is in flight, or the round is unknown)",
                peer=self.peer,
                correlation_id=cid if isinstance(cid, str) else None,
            )
        if resp.status in (400, 413, 422):
            raise ProtocolError(f"{method} {path}: tunnel rejected the request ({resp.status} {code})", peer=self.peer)
        if resp.status == 429:
            raise Retryable("BACKPRESSURE")
        if resp.status == 503:
            raise Retryable("NOT_READY")
        if resp.status >= 500:
            raise Retryable("SERVER_ERROR")
        return resp

    @staticmethod
    def _terminal(resp: HttpResponse) -> Terminal | None:
        if resp.terminal and isinstance(resp.body, dict):
            code = resp.body.get("code")
            if not isinstance(code, str):
                raise ProtocolError("terminal body without a code")
            detail = resp.body.get("detail")
            return Terminal(code, str(resp.body.get("message") or ""), detail if isinstance(detail, dict) else None)
        return None

    def _delivered(self, resp: HttpResponse, path: str) -> Delivered:
        b = resp.body
        if (
            not isinstance(b, dict)
            or not isinstance(b.get("messageId"), str)
            or not isinstance(b.get("correlationId"), str)
            or "payload" not in b
        ):
            raise ProtocolError(
                f"{path}: delivered message is not {{messageId, correlationId, payload}}", peer=self.peer
            )
        received = b.get("receivedAt")
        return Delivered(
            b["messageId"],
            b["correlationId"],
            b["payload"],
            Budget.from_json(b.get("budget")),
            received if isinstance(received, str) else None,
        )

    # -- Transport --

    def info(self) -> Info:
        resp = self._call("GET", "/v1/info")
        b = resp.body
        if resp.status != 200 or not isinstance(b, dict):
            raise ProtocolError(f"GET /v1/info returned status {resp.status}", peer=self.peer)
        required = ("legId", "peerOrgSlug", "role", "state")
        if any(not isinstance(b.get(k), str) for k in required):
            raise ProtocolError("GET /v1/info body is missing legId/peerOrgSlug/role/state", peer=self.peer)
        api_version = b.get("apiVersion")
        return Info(
            api_version=api_version if isinstance(api_version, str) else "0.0.0",
            leg_id=b["legId"],
            peer_org_slug=b["peerOrgSlug"],
            role=b["role"],
            direction=str(b.get("direction") or ""),
            state=b["state"],
            guards=b.get("guards") if isinstance(b.get("guards"), dict) else None,
            caps=b.get("caps") if isinstance(b.get("caps"), dict) else None,
            operations=b.get("operations") if isinstance(b.get("operations"), list) else None,
        )

    def submit(self, payload: JSON, correlation_id: str | None = None) -> str | Terminal:
        body: dict[str, Any] = {"payload": payload}
        if correlation_id is not None:
            body["correlationId"] = correlation_id
        resp = self._call("POST", "/v1/request", body)
        t = self._terminal(resp)
        if t is not None:
            return t
        cid = resp.body.get("correlationId") if isinstance(resp.body, dict) else None
        if resp.status != 202 or not isinstance(cid, str):
            raise ProtocolError(
                f"POST /v1/request returned status {resp.status} without a correlationId", peer=self.peer
            )
        return cid

    def poll_response(self, correlation_id: str, timeout_s: float) -> Delivered | Terminal | Empty:
        path = f"/v1/responses/{correlation_id}"
        resp = self._call("GET", path, timeout_s=timeout_s)
        if resp.status == 204:
            return EMPTY
        if resp.status == 404:
            raise UnknownCorrelation(correlation_id)
        t = self._terminal(resp)
        if t is not None:
            return t
        if resp.status != 200:
            raise ProtocolError(f"GET {path} returned status {resp.status}", peer=self.peer)
        return self._delivered(resp, path)

    def abandon(self, correlation_id: str) -> None:
        """Best effort: one DELETE, any failure ignored (the round is already lost to the caller)."""
        with contextlib.suppress(Retryable, FusionError):
            self._call("DELETE", f"/v1/request/{correlation_id}")

    def next_message(self, timeout_s: float) -> Delivered | Terminal | Empty:
        resp = self._call("GET", "/v1/messages/next", timeout_s=timeout_s)
        if resp.status == 204:
            return EMPTY
        t = self._terminal(resp)
        if t is not None:
            return t
        if resp.status != 200:
            raise ProtocolError(f"GET /v1/messages/next returned status {resp.status}", peer=self.peer)
        return self._delivered(resp, "/v1/messages/next")

    def post_response(self, in_reply_to: str, payload: JSON) -> Budget | Terminal | None:
        resp = self._call("POST", "/v1/messages", {"inReplyTo": in_reply_to, "payload": payload})
        t = self._terminal(resp)
        if t is not None:
            return t
        if resp.status != 202:
            raise ProtocolError(f"POST /v1/messages returned status {resp.status}", peer=self.peer)
        return Budget.from_json(resp.body.get("budget")) if isinstance(resp.body, dict) else None

    def complete(self) -> Terminal | None:
        resp = self._call("POST", "/v1/complete", {})
        t = self._terminal(resp)
        if t is not None:
            return t
        if resp.status != 202:
            raise ProtocolError(f"POST /v1/complete returned status {resp.status}", peer=self.peer)
        return None


__all__ = ["INVALID_JSON", "HttpClient", "HttpResponse", "TransportError", "TunnelClient"]
