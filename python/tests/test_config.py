"""Env contract (spec/env.md) and settings."""

from __future__ import annotations

import json

import pytest

from safeinsights_fusion import ConfigError, Settings
from safeinsights_fusion._config import read_role, read_tunnels, validate_endpoint


def test_single_leg_shorthand() -> None:
    env = {"FUSION_TUNNEL_ENDPOINT": "http://127.0.0.1:8471/", "FUSION_TUNNEL_TOKEN": "t"}
    [t] = read_tunnels(env)
    assert (t.label, t.endpoint, t.token) == ("default", "http://127.0.0.1:8471", "t")


def test_map_form_preserves_order_and_labels() -> None:
    env = {
        "FUSION_TUNNEL_ENDPOINTS": json.dumps({"b": "http://tunnel-b:8471", "a": "http://tunnel-a:8471"}),
        "FUSION_TUNNEL_TOKENS": json.dumps({"a": "ta", "b": "tb"}),
    }
    ts = read_tunnels(env)
    assert [(t.label, t.endpoint, t.token) for t in ts] == [
        ("b", "http://tunnel-b:8471", "tb"),
        ("a", "http://tunnel-a:8471", "ta"),
    ]


@pytest.mark.parametrize(
    "env, match",
    [
        ({}, "required"),
        ({"FUSION_TUNNEL_ENDPOINT": "http://127.0.0.1:1"}, "FUSION_TUNNEL_TOKEN"),
        (
            {
                "FUSION_TUNNEL_ENDPOINT": "http://127.0.0.1:1",
                "FUSION_TUNNEL_TOKEN": "t",
                "FUSION_TUNNEL_ENDPOINTS": "{}",
            },
            "not both",
        ),
        ({"FUSION_TUNNEL_ENDPOINTS": "{}", "FUSION_TUNNEL_TOKENS": "{}"}, "non-empty"),
        ({"FUSION_TUNNEL_ENDPOINTS": "nope", "FUSION_TUNNEL_TOKENS": "{}"}, "JSON"),
        ({"FUSION_TUNNEL_ENDPOINTS": '{"a":"http://x:1"}', "FUSION_TUNNEL_TOKENS": '{"b":"t"}'}, "same keys"),
        ({"FUSION_TUNNEL_ENDPOINTS": '{"a":"http://x:1"}', "FUSION_TUNNEL_TOKENS": '{"a":""}'}, "strings"),
        ({"FUSION_TUNNEL_ENDPOINTS": '{"a":"http://x:1"}'}, "FUSION_TUNNEL_TOKENS"),
    ],
)
def test_malformed_env(env: dict[str, str], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        read_tunnels(env)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8471",
        "http://localhost:8471",
        "http://tunnel-a:8471",
        "http://10.0.1.5:80",
        "http://[::1]:8471",
        "http://169.254.1.1",
    ],
)
def test_local_endpoints_accepted(url: str) -> None:
    assert validate_endpoint(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8471",
        "http://relay.safeinsights.org",
        "http://8.8.8.8",
        "http://127.0.0.1:8471/v1",
        "http://user:pw@127.0.0.1",
        "ftp://x",
        "http://127.0.0.1?x=1",
    ],
)
def test_non_local_endpoints_rejected(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_endpoint(url)


def test_role() -> None:
    assert read_role({"FUSION_ROLE": " Destination "}) == "destination"
    assert read_role({"FUSION_ROLE": "source"}) == "source"
    with pytest.raises(ConfigError):
        read_role({})
    with pytest.raises(ConfigError):
        read_role({"FUSION_ROLE": "hub"})


def test_settings_defaults_and_env() -> None:
    s = Settings.from_env({})
    assert (s.ready_timeout_s, s.round_timeout_s, s.round_max_reissues, s.poll_http_timeout_s) == (900, 600, 3, 40)
    s = Settings.from_env(
        {
            "FUSION_ROUND_TIMEOUT_S": "30",
            "FUSION_ROUND_MAX_REISSUES": "1",
            "FUSION_LOG_LEVEL": "debug",
            "FUSION_READY_POLL_S": "",
        }
    )
    assert (s.round_timeout_s, s.round_max_reissues, s.log_level, s.ready_poll_s) == (30, 1, "DEBUG", 2.0)
    with pytest.raises(ConfigError):
        Settings.from_env({"FUSION_ROUND_TIMEOUT_S": "soon"})
    with pytest.raises(ConfigError):
        Settings.from_env({"FUSION_ROUND_TIMEOUT_S": "-1"})
