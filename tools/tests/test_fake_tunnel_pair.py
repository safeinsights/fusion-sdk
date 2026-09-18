"""Tests for tools/fake_tunnel_pair: the double must implement spec/local-api.md exactly."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from fake_tunnel_pair import FakeTunnelPair, LegEndpoints, load_scenario  # noqa: E402
from fake_tunnel_pair.server import canonical_bytes  # noqa: E402

QUERY = {"v": 1, "kind": "query", "operation": "count", "params": {"ids": ["a", "b"]}, "encoding": "json"}
REPLY = {"v": 1, "kind": "response", "status": "ok", "body": 2, "encoding": "json"}


class Http:
    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint
        self.token = token

    def call(
        self, method: str, path: str, body: Any = None, token: str | None = None, timeout: float = 10
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.endpoint + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token if token is None else token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)


def scenario(name: str, **overrides: Any) -> Any:
    sc = load_scenario(name)
    return replace(sc, **overrides) if overrides else sc


@pytest.fixture
def happy() -> Iterator[tuple[FakeTunnelPair, Http, Http]]:
    with FakeTunnelPair(scenario("happy", longpoll_ms=300)) as pair:
        leg: LegEndpoints = pair.endpoints[0]
        yield pair, Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)


def one_round(dst: Http, src: Http, query: Any = QUERY, reply: Any = REPLY) -> tuple[str, Any]:
    status, body = dst.call("POST", "/v1/request", {"payload": query})
    assert status == 202, body
    cid = body["correlationId"]
    status, msg = src.call("GET", "/v1/messages/next")
    assert status == 200 and msg["correlationId"] == cid
    assert src.call("POST", f"/v1/messages/{msg['messageId']}/ack")[0] == 204
    status, posted = src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": reply})
    assert status == 202, posted
    status, resp = dst.call("GET", f"/v1/responses/{cid}")
    assert status == 200 and resp["correlationId"] == cid
    assert dst.call("POST", f"/v1/messages/{resp['messageId']}/ack")[0] == 204
    return cid, resp


def test_info_and_auth(happy: tuple[FakeTunnelPair, Http, Http]) -> None:
    _, dst, src = happy
    status, info = dst.call("GET", "/v1/info")
    assert status == 200
    assert info["role"] == "destination" and info["state"] == "CHANNEL_UP" and info["apiVersion"] == "1.0.0"
    assert info["peerOrgSlug"] == "dp-a" and info["legId"] == "leg-a"
    assert "guards" not in info
    assert src.call("GET", "/v1/info")[1]["role"] == "source"
    assert dst.call("GET", "/v1/info", token="wrong")[0] == 401
    assert dst.call("GET", "/v1/info", token=src.token)[0] == 401  # tokens are per tunnel


def test_direction_matrix(happy: tuple[FakeTunnelPair, Http, Http]) -> None:
    _, dst, src = happy
    assert src.call("POST", "/v1/request", {"payload": QUERY})[0] == 403
    assert src.call("GET", "/v1/responses/x")[0] == 403
    assert src.call("POST", "/v1/complete", {})[0] == 403
    assert dst.call("GET", "/v1/messages/next")[0] == 403
    assert dst.call("POST", "/v1/messages", {"inReplyTo": "x", "payload": REPLY})[0] == 403


def test_happy_round_and_budget(happy: tuple[FakeTunnelPair, Http, Http]) -> None:
    pair, dst, src = happy
    _, resp = one_round(dst, src)
    assert resp["payload"] == REPLY
    assert resp["budget"]["roundsUsed"] == 1 and resp["budget"]["roundsMax"] is None
    assert resp["budget"]["responseBytesUsed"] == canonical_bytes(REPLY)
    assert resp["budget"]["queryBytesUsed"] == canonical_bytes(QUERY)
    assert resp["receivedAt"].endswith("Z")
    snap = pair.leg("leg-a").snapshot()
    assert snap["inFlight"] is None and snap["rounds"][0]["responseAcked"] is True


def test_single_in_flight_409_and_empty_hold_204(happy: tuple[FakeTunnelPair, Http, Http]) -> None:
    _, dst, _ = happy
    cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
    assert dst.call("POST", "/v1/request", {"payload": QUERY})[0] == 409
    # Same correlationId re-issue is idempotent (T1).
    assert dst.call("POST", "/v1/request", {"payload": QUERY, "correlationId": cid}) == (202, {"correlationId": cid})
    t0 = time.monotonic()
    assert dst.call("GET", f"/v1/responses/{cid}")[0] == 204
    assert 0.2 <= time.monotonic() - t0 < 5
    assert dst.call("GET", "/v1/responses/unknown")[0] == 404
    assert dst.call("POST", "/v1/complete", {})[0] == 409


def test_not_ready_then_up() -> None:
    with FakeTunnelPair(scenario("slow-ready", longpoll_ms=200, ready_delay_ms=600)) as pair:
        leg = pair.endpoints[0]
        dst = Http(leg.destination.endpoint, leg.destination.token)
        status, body = dst.call("POST", "/v1/request", {"payload": QUERY})
        assert status == 503
        assert body["code"] == "NOT_READY"
        assert dst.call("GET", "/v1/info")[1]["state"] == "RELAY_ATTACHED"
        time.sleep(0.7)
        assert dst.call("GET", "/v1/info")[1]["state"] == "CHANNEL_UP"
        assert dst.call("POST", "/v1/request", {"payload": QUERY})[0] == 202


def test_redelivery_of_unacked_query_and_dead_letter() -> None:
    # Hold (100 ms) is shorter than redelivery (200 ms) so an immediate re-poll is an empty hold.
    with FakeTunnelPair(scenario("dead-letter", longpoll_ms=100)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        m1 = src.call("GET", "/v1/messages/next")[1]
        assert src.call("GET", "/v1/messages/next")[0] == 204  # not yet due for redelivery
        time.sleep(0.15)
        m2 = src.call("GET", "/v1/messages/next")[1]
        assert m2["messageId"] == m1["messageId"] and m2["correlationId"] == cid
        time.sleep(0.25)
        status, body = src.call("GET", "/v1/messages/next")
        assert status == 200 and body == {
            "terminal": True,
            "code": "SESSION_ERRORED",
            "message": body["message"],
            "detail": body["detail"],
        }
        assert dst.call("GET", f"/v1/responses/{cid}")[1]["code"] == "SESSION_ERRORED"
        assert dst.call("GET", "/v1/info")[1]["state"] == "ERRORED"


def test_timeout_reissue_scenario() -> None:
    with FakeTunnelPair(scenario("timeout-reissue", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        one_round(dst, src)
        # Round 2: the source answers but the response is "lost".
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        msg = src.call("GET", "/v1/messages/next")[1]
        src.call("POST", f"/v1/messages/{msg['messageId']}/ack")
        assert src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": REPLY})[0] == 202
        assert dst.call("GET", f"/v1/responses/{cid}")[0] == 204
        # Re-issue with the same id: the source tunnel replays; the RC is not asked again.
        assert dst.call("POST", "/v1/request", {"payload": QUERY, "correlationId": cid})[0] == 202
        status, resp = dst.call("GET", f"/v1/responses/{cid}")
        assert status == 200 and resp["payload"] == REPLY
        assert src.call("GET", "/v1/messages/next")[0] == 204
        assert pair.leg("leg-a").snapshot()["rounds"][1]["queryDeliveries"] == 1


def test_restart_404_scenario() -> None:
    with FakeTunnelPair(scenario("restart-404", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        one_round(dst, src)
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        status, body = dst.call("GET", f"/v1/responses/{cid}")
        assert status == 404 and body["code"] == "UNKNOWN_CORRELATION"
        # Forgotten: the tunnel no longer counts it in flight, so a re-issue with the same id is accepted.
        assert dst.call("POST", "/v1/request", {"payload": QUERY, "correlationId": cid})[0] == 202
        msg = src.call("GET", "/v1/messages/next")[1]
        assert msg["correlationId"] == cid
        src.call("POST", f"/v1/messages/{msg['messageId']}/ack")
        src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": REPLY})
        assert dst.call("GET", f"/v1/responses/{cid}")[1]["payload"] == REPLY


def test_redeliver_after_ack_scenario() -> None:
    with FakeTunnelPair(scenario("redeliver-after-ack", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        one_round(dst, src)
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        m1 = src.call("GET", "/v1/messages/next")[1]
        src.call("POST", f"/v1/messages/{m1['messageId']}/ack")
        m2 = src.call("GET", "/v1/messages/next")[1]
        assert m2["messageId"] == m1["messageId"] and m2["correlationId"] == cid
        assert src.call("GET", "/v1/messages/next")[0] == 204
        _, p1 = src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": REPLY})
        _, p2 = src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": REPLY})
        assert p1["messageId"] == p2["messageId"]  # idempotent second response
        assert pair.leg("leg-a").snapshot()["budget"]["roundsUsed"] == 2


def test_backpressure_and_transient_faults() -> None:
    with FakeTunnelPair(scenario("backpressure", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        status, body = dst.call("POST", "/v1/request", {"payload": QUERY})
        assert status == 429 and body["retryable"] is True
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        msg = src.call("GET", "/v1/messages/next")[1]
        src.call("POST", f"/v1/messages/{msg['messageId']}/ack")
        assert src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": REPLY})[0] == 429
        assert src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": REPLY})[0] == 202
    with FakeTunnelPair(scenario("transient-errors", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst = Http(leg.destination.endpoint, leg.destination.token)
        src = Http(leg.source.endpoint, leg.source.token)
        one_round(dst, src)  # submit call 1
        assert dst.call("POST", "/v1/request", {"payload": QUERY})[0] == 503  # call 2: injected NOT_READY
        assert dst.call("POST", "/v1/request", {"payload": QUERY})[0] == 500  # call 3: injected 500
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]  # call 4
        assert dst.call("POST", "/v1/request", {"payload": QUERY, "correlationId": cid})[0] == 202


def test_limit_exceeded_rounds_and_bytes() -> None:
    with FakeTunnelPair(scenario("limit-exceeded-rounds", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        assert src.call("GET", "/v1/info")[1]["caps"]["maxRounds"] == 2
        one_round(dst, src)
        _, resp = one_round(dst, src)
        assert resp["budget"] == {**resp["budget"], "roundsUsed": 2, "roundsMax": 2}
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        status, body = dst.call("GET", f"/v1/responses/{cid}")
        assert (
            status == 200
            and body["terminal"] is True
            and body["code"] == "LIMIT_EXCEEDED"
            and body["detail"]["cap"] == "maxRounds"
        )
        assert src.call("GET", "/v1/messages/next")[1]["code"] == "LIMIT_EXCEEDED"
        assert dst.call("POST", "/v1/request", {"payload": QUERY})[1]["code"] == "LIMIT_EXCEEDED"
    with FakeTunnelPair(scenario("limit-exceeded-bytes", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        msg = src.call("GET", "/v1/messages/next")[1]
        src.call("POST", f"/v1/messages/{msg['messageId']}/ack")
        big = {**REPLY, "body": "x" * 200}
        status, body = src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": big})
        assert (
            status == 200 and body["code"] == "LIMIT_EXCEEDED" and body["detail"]["cap"] == "maxResponseBytesPerRound"
        )
        assert dst.call("GET", f"/v1/responses/{cid}")[1]["code"] == "LIMIT_EXCEEDED"
        assert pair.leg("leg-a").snapshot()["budget"]["responseBytesUsed"] == 0  # nothing was sent


def test_session_errored_after_round() -> None:
    with FakeTunnelPair(scenario("session-errored", longpoll_ms=200)) as pair:
        leg = pair.endpoints[0]
        dst, src = Http(leg.destination.endpoint, leg.destination.token), Http(leg.source.endpoint, leg.source.token)
        one_round(dst, src)
        assert dst.call("POST", "/v1/request", {"payload": QUERY})[1]["code"] == "SESSION_ERRORED"
        assert src.call("GET", "/v1/messages/next")[1]["code"] == "SESSION_ERRORED"


def test_complete_is_study_complete_for_the_source(happy: tuple[FakeTunnelPair, Http, Http]) -> None:
    _, dst, src = happy
    one_round(dst, src)
    assert dst.call("POST", "/v1/complete", {}) == (202, {})
    assert dst.call("POST", "/v1/complete", {}) == (202, {})
    status, body = src.call("GET", "/v1/messages/next")
    assert status == 200 and body["terminal"] is True and body["code"] == "STUDY_COMPLETE"
    assert dst.call("GET", "/v1/info")[1]["state"] == "CLOSED"
    assert dst.call("POST", "/v1/request", {"payload": QUERY})[1]["code"] == "STUDY_COMPLETE"


def test_source_validation_and_ack_404(happy: tuple[FakeTunnelPair, Http, Http]) -> None:
    _, dst, src = happy
    assert src.call("POST", "/v1/messages", {"inReplyTo": "nope", "payload": REPLY})[0] == 400
    assert src.call("POST", "/v1/messages", {"payload": REPLY})[0] == 422
    assert dst.call("POST", "/v1/request", {"nope": 1})[0] == 422
    assert src.call("POST", "/v1/messages/unknown/ack")[0] == 404
    assert dst.call("POST", "/v1/messages/unknown/ack")[0] == 404
    # A query delivered but not yet ACKed can still be answered.
    cid = dst.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
    src.call("GET", "/v1/messages/next")
    assert src.call("POST", "/v1/messages", {"inReplyTo": cid, "payload": REPLY})[0] == 202


def test_hub_two_legs_isolated_and_env() -> None:
    with FakeTunnelPair(scenario("hub-two-legs", longpoll_ms=200)) as pair:
        a, b = pair.endpoints
        assert {a.peer_org_slug, b.peer_org_slug} == {"dp-a", "dp-b"}
        dst_a, src_a = Http(a.destination.endpoint, a.destination.token), Http(a.source.endpoint, a.source.token)
        dst_b, src_b = Http(b.destination.endpoint, b.destination.token), Http(b.source.endpoint, b.source.token)
        # A's token does not open B's tunnel.
        assert dst_b.call("GET", "/v1/info", token=a.destination.token)[0] == 401
        cid_a = dst_a.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        cid_b = dst_b.call("POST", "/v1/request", {"payload": QUERY})[1]["correlationId"]
        # Concurrent in-flight rounds on different legs are fine; B's source only sees B's query.
        assert src_b.call("GET", "/v1/messages/next")[1]["correlationId"] == cid_b
        assert src_a.call("GET", "/v1/messages/next")[1]["correlationId"] == cid_a
        env = pair.env("destination")
        assert env["FUSION_ROLE"] == "destination"
        assert set(json.loads(env["FUSION_TUNNEL_ENDPOINTS"])) == {"leg-a", "leg-b"}
        assert json.loads(env["FUSION_TUNNEL_TOKENS"])["leg-b"] == b.destination.token
        with pytest.raises(ValueError, match="single"):
            pair.env("source", "single")
        ann = pair.announce()
        assert ann["env"]["source"]["single"] is None and len(ann["legs"]) == 2


def test_single_leg_env_styles(happy: tuple[FakeTunnelPair, Http, Http]) -> None:
    pair, dst, _ = happy
    env = pair.env("destination")
    assert env["FUSION_TUNNEL_ENDPOINT"] == dst.endpoint and env["FUSION_TUNNEL_TOKEN"] == dst.token
    assert "FUSION_TUNNEL_ENDPOINTS" in pair.env("destination", "map")


def test_cli_announces_and_exits_on_stdin_eof(tmp_path: Path) -> None:
    announce = tmp_path / "announce.json"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "fake_tunnel_pair",
            "--scenario",
            "happy",
            "--longpoll-ms",
            "100",
            "--announce",
            str(announce),
        ],
        cwd=str(TOOLS),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        data = json.loads(line)
        assert data["legs"][0]["legId"] == "leg-a" and data["pid"] == proc.pid
        assert json.loads(announce.read_text())["pid"] == proc.pid
        dst = Http(data["legs"][0]["destination"]["endpoint"], data["legs"][0]["destination"]["token"])
        assert dst.call("GET", "/v1/info")[0] == 200
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
