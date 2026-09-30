"""Test doubles for the *other* side of a round: a scripted source RC speaking raw HTTP."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

JSON = Any


class RawTunnel:
    """Bare urllib client for tests that need to speak to the fake without the SDK."""

    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint, self.token = endpoint, token

    def call(self, method: str, path: str, body: JSON = None, timeout: float = 10) -> tuple[int, JSON]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.endpoint + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)


def ok_table(rows: list[list[Any]], cols: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    cols = cols or [("grade", "string"), ("n", "integer")]
    return {
        "v": 1,
        "kind": "response",
        "status": "ok",
        "body": {"__table__": 1, "columns": [{"name": n, "type": t} for n, t in cols], "rows": rows},
        "encoding": "json",
    }


def error_envelope(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if detail:
        err["detail"] = detail
    return {"v": 1, "kind": "response", "status": "error", "error": err, "encoding": "json"}


def echo_handler(query: JSON) -> JSON:
    """Default scripted handler: a one-row table naming the operation and the param count."""
    return ok_table([[query["operation"], len(query["params"])]], [("operation", "string"), ("n_params", "integer")])


class ScriptedSource:
    """A source research container without the SDK: long-poll, run `handler`, respond.

    `handler(query_envelope) -> response_envelope`. Records every delivered correlationId and stops
    on a terminal body (its code is kept in `terminal_code`).
    """

    def __init__(
        self, endpoint: str, token: str, handler: Callable[[JSON], JSON] = echo_handler, *, hold_s: float = 2.0
    ) -> None:
        self.http = RawTunnel(endpoint, token)
        self.handler = handler
        self.hold_s = hold_s
        self.delivered: list[str] = []
        self.calls = 0
        self.terminal_code: str | None = None
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="scripted-source")

    def start(self) -> ScriptedSource:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def __enter__(self) -> ScriptedSource:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                status, body = self.http.call("GET", "/v1/messages/next", timeout=self.hold_s + 10)
            except Exception as exc:
                self.errors.append(type(exc).__name__)
                self._stop.wait(0.05)
                continue
            if status == 204:
                continue
            if status == 503:
                self._stop.wait(0.05)
                continue
            if status != 200:
                self.errors.append(f"next:{status}")
                self._stop.wait(0.05)
                continue
            if body.get("terminal"):
                self.terminal_code = body["code"]
                return
            cid = body["correlationId"]
            self.delivered.append(cid)
            self.calls += 1
            reply = self.handler(body["payload"])
            for _attempt in range(20):  # a real source retries BACKPRESSURE; so does this double
                status, out = self.http.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": reply})
                if status != 429:
                    break
                self._stop.wait(0.05)
            if status == 200 and isinstance(out, dict) and out.get("terminal"):
                self.terminal_code = out["code"]
                return
            if status != 202:
                self.errors.append(f"respond:{status}")
