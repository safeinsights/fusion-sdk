"""Typed wrapper of the tunnel's local API (spec/local-api.md) implementing `_transport.Transport`.

Status codes are mapped once, here, per spec/errors.json `httpStatusMapping`.
"""

from __future__ import annotations

import logging
from typing import Any

from . import _log
from ._http import INVALID_JSON, HttpClient, HttpResponse, TransportError
from ._transport import EMPTY, Budget, Delivered, Empty, Info, Retryable, Terminal, UnknownCorrelation
from .errors import ConcurrencyError, ConfigError, ProtocolError

JSON = Any


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
        if resp.status == 401:
            raise ConfigError("bearer token rejected by the tunnel (401)", peer=self.peer)
        if resp.status == 403:
            raise ConfigError(f"{method} {path} is not allowed for this role (403): check FUSION_ROLE", peer=self.peer)
        if resp.status == 409:
            cid = resp.body.get("correlationId") if isinstance(resp.body, dict) else None
            err = ConcurrencyError("another round is in flight at the tunnel (409)", peer=self.peer)
            err.correlation_id = cid  # type: ignore[attr-defined]
            raise err
        if resp.status == 422:
            raise ProtocolError(f"{method} {path}: tunnel rejected the request schema (422)", peer=self.peer)
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
        if resp.status not in (200, 202) or not isinstance(cid, str):
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

    def abandon(self, correlation_id: str) -> bool:
        """Ask T7. Returns False when the tunnel does not implement it."""
        try:
            resp = self._call("DELETE", f"/v1/request/{correlation_id}")
        except Retryable:
            return False
        return resp.status in (200, 202, 204)

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
        if resp.status == 400:
            raise ProtocolError("POST /v1/messages: inReplyTo does not match a delivered query", peer=self.peer)
        if resp.status not in (200, 202):
            raise ProtocolError(f"POST /v1/messages returned status {resp.status}", peer=self.peer)
        return Budget.from_json(resp.body.get("budget")) if isinstance(resp.body, dict) else None

    def ack(self, message_id: str) -> None:
        resp = self._call("POST", f"/v1/messages/{message_id}/ack")
        if resp.status == 404:
            _log.event("ack.unknown", logging.WARNING, peer=self.peer, messageId=message_id)
            return
        if resp.terminal:
            return
        if resp.status not in (200, 202, 204):
            raise ProtocolError(f"ack returned status {resp.status}", peer=self.peer)

    def complete(self) -> Terminal | None:
        resp = self._call("POST", "/v1/complete", {})
        t = self._terminal(resp)
        if t is not None:
            return t
        if resp.status not in (200, 202, 204):
            raise ProtocolError(f"POST /v1/complete returned status {resp.status}", peer=self.peer)
        return None


__all__ = ["TunnelClient"]
