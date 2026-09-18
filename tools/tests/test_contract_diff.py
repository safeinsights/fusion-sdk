"""tools/contract_diff.py against a synthetic export that mirrors the fake tunnel pair."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import contract_diff  # noqa: E402

REQ = json.loads((TOOLS.parent / "spec" / "local-api.requirements.json").read_text())


def obj(*names: str, required: tuple[str, ...] | None = None) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {n: {} for n in names},
        "required": list(required if required is not None else names),
    }


def good_export() -> dict[str, object]:
    msg = obj(
        "messageId",
        "correlationId",
        "payload",
        "budget",
        "receivedAt",
        required=("messageId", "correlationId", "payload"),
    )
    return {
        "apiVersion": "1.0.0",
        "routes": {
            "GET /v1/info": {
                "response": obj(
                    "apiVersion",
                    "legId",
                    "peerOrgSlug",
                    "role",
                    "state",
                    "direction",
                    "guards",
                    "caps",
                    "operations",
                    required=("apiVersion", "legId", "peerOrgSlug", "role", "state"),
                )
            },
            "POST /v1/request": {
                "request": obj("payload", "correlationId", required=("payload",)),
                "response": obj("correlationId"),
                "statuses": [202, 403, 409, 422, 429, 503],
            },
            "GET /v1/responses/{correlationId}": {"response": msg, "statuses": [200, 204, 403, 404, 503]},
            "DELETE /v1/request/{correlationId}": {"statuses": [204, 403, 503]},
            "GET /v1/messages/next": {"response": msg, "statuses": [200, 204, 403, 503]},
            "POST /v1/messages": {
                "request": obj("inReplyTo", "payload"),
                "response": obj("messageId", "budget", required=("messageId",)),
                "statuses": [202, 400, 403, 422, 429, 503],
            },
            "POST /v1/messages/{messageId}/ack": {"statuses": [204, 404]},
            "POST /v1/complete": {"statuses": [202, 403, 409, 503]},
        },
        "terminalBody": obj("terminal", "code", "message", "detail", required=("terminal", "code")),
        "budget": obj(
            "roundsUsed",
            "roundsMax",
            "responseBytesUsed",
            "responseBytesMax",
            "queryBytesUsed",
            "queryBytesMax",
            "roundsPerHourUsed",
            "roundsPerHourMax",
        ),
    }


def test_matching_export_is_clean() -> None:
    errors, warnings = contract_diff.check(good_export(), REQ)
    assert errors == [] and warnings == []


def test_missing_required_field_status_and_optional_route() -> None:
    export = good_export()
    routes = export["routes"]
    assert isinstance(routes, dict)
    del routes["DELETE /v1/request/{correlationId}"]  # T7 optional → warning
    routes["POST /v1/request"]["statuses"] = [202, 403]  # 409 etc. missing → error
    routes["GET /v1/responses/{correlationId}"]["response"]["properties"].pop("budget")  # optional → warning
    routes["GET /v1/messages/next"]["response"]["properties"].pop("payload")  # required → error
    export["apiVersion"] = "2.0.0"
    errors, warnings = contract_diff.check(export, REQ)
    assert any("apiVersion major" in e for e in errors)
    assert any("statuses [409, 422, 429, 503]" in e for e in errors)
    assert any("response.payload" in e for e in errors)
    assert any("DELETE /v1/request" in w for w in warnings)
    assert any("response.budget" in w for w in warnings)


def test_cli(tmp_path: Path, capsys: object) -> None:
    p = tmp_path / "export.json"
    p.write_text(json.dumps(good_export()))
    assert contract_diff.main(["contract_diff", str(p)]) == 0
    assert contract_diff.main(["contract_diff"]) == 2
