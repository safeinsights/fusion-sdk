"""`python -m safeinsights_fusion doctor`."""

from __future__ import annotations

import io
import json

import pytest

from safeinsights_fusion import Settings
from safeinsights_fusion.__main__ import doctor, main

from .conftest import FakeFactory


def test_doctor_reports_each_tunnel_without_secrets(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("hub-two-legs")
    env = pair.env("destination")
    out = io.StringIO()
    assert doctor(env, out=out) == 0
    text = out.getvalue()
    assert "leg=leg-a" in text and "leg=leg-b" in text and "result: ok" in text
    for token in json.loads(env["FUSION_TUNNEL_TOKENS"]).values():
        assert token not in text


def test_doctor_json_and_failures(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("happy")
    env = pair.env("destination")
    env["FUSION_TUNNEL_TOKEN"] = "wrong"
    out = io.StringIO()
    assert doctor(env, as_json=True, out=out) == 1
    report = json.loads(out.getvalue())
    assert report["ok"] is False and report["tunnels"][0]["error"] == "ConfigError"

    env = pair.env("source")  # a source pointed at the destination tunnel: role mismatch
    env["FUSION_TUNNEL_ENDPOINT"] = pair.endpoints[0].destination.endpoint
    env["FUSION_TUNNEL_TOKEN"] = pair.endpoints[0].destination.token
    out = io.StringIO()
    assert doctor(env, as_json=True, out=out) == 1
    assert json.loads(out.getvalue())["tunnels"][0]["roleMatches"] is False

    out = io.StringIO()
    assert doctor({}, out=out) == 1 and "config error" in out.getvalue()


def test_doctor_wait_and_unreachable(fake: FakeFactory, settings: Settings) -> None:
    pair = fake("slow-ready", ready_delay_ms=300)
    env = pair.env("destination")
    env["FUSION_READY_TIMEOUT_S"] = "10"
    env["FUSION_READY_POLL_S"] = "0.1"
    out = io.StringIO()
    assert doctor(env, wait=True, out=out) == 0 and "state=CHANNEL_UP" in out.getvalue()
    env["FUSION_TUNNEL_ENDPOINT"] = "http://127.0.0.1:9"
    env["FUSION_HTTP_TIMEOUT_S"] = "1"
    out = io.StringIO()
    assert doctor(env, out=out) == 1 and "unreachable" in out.getvalue()


def test_main_parses() -> None:
    with pytest.raises(SystemExit):
        main([])
