"""`python -m safeinsights_fusion doctor [--wait] [--json]`: env + GET /v1/info check. Content-free."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from . import __version__, _log
from ._config import Settings, read_role, read_tunnels
from ._transport import READY_STATE, Retryable
from ._tunnel import TunnelClient
from .destination import API_MAJOR, wait_ready
from .errors import FusionError


def _host(endpoint: str) -> str:
    p = urlsplit(endpoint)
    return f"{p.hostname}:{p.port}" if p.port else str(p.hostname)


def doctor(env: Mapping[str, str] | None = None, *, wait: bool = False, as_json: bool = False, out: Any = None) -> int:
    out = out or sys.stdout
    settings = Settings.from_env(env)
    _log.configure(settings.log_level)
    report: dict[str, Any] = {"sdk": __version__, "apiMajor": API_MAJOR, "ok": True, "tunnels": []}
    try:
        role = read_role(env)
        tunnels = read_tunnels(env)
    except FusionError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        _emit(report, as_json, out)
        return 1
    report["role"] = role
    deadline = time.monotonic() + settings.ready_timeout_s
    for t in tunnels:
        row: dict[str, Any] = {"label": t.label, "endpoint": _host(t.endpoint)}
        client = TunnelClient(t.endpoint, t.token, peer=t.label, http_timeout_s=settings.http_timeout_s)
        try:
            info = wait_ready(client, deadline=deadline, settings=settings, label=t.label) if wait else client.info()
            row.update(
                legId=info.leg_id,
                peerOrgSlug=info.peer_org_slug,
                role=info.role,
                state=info.state,
                apiVersion=info.api_version,
            )
            row["roleMatches"] = info.role == role
            row["apiCompatible"] = info.api_major == API_MAJOR
            row["ok"] = row["roleMatches"] and row["apiCompatible"] and (info.state == READY_STATE or not wait)
            _log.event("doctor.info", peer=t.label, state=info.state, apiVersion=info.api_version)
        except Retryable as exc:
            row.update(ok=False, error=f"unreachable ({exc.code})")
        except FusionError as exc:
            row.update(ok=False, error=type(exc).__name__)
        report["ok"] = report["ok"] and bool(row["ok"])
        report["tunnels"].append(row)
    _emit(report, as_json, out)
    return 0 if report["ok"] else 1


def _emit(report: dict[str, Any], as_json: bool, out: Any) -> None:
    if as_json:
        print(json.dumps(report, indent=2), file=out)
        return
    print(f"safeinsights_fusion {report['sdk']} (local API major {report['apiMajor']})", file=out)
    if "error" in report:
        print(f"config error: {report['error']}", file=out)
    else:
        print(f"role: {report['role']}", file=out)
    for row in report["tunnels"]:
        status = "ok" if row.get("ok") else "FAIL"
        detail = (
            row.get("error")
            or f"leg={row.get('legId')} peer={row.get('peerOrgSlug')} role={row.get('role')} state={row.get('state')} api={row.get('apiVersion')}"
        )
        print(f"  [{status}] {row['label']} @ {row['endpoint']}: {detail}", file=out)
    print("result: " + ("ok" if report["ok"] else "problems found"), file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m safeinsights_fusion", description="SafeInsights Fusion SDK utilities"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check the env contract and each tunnel's /v1/info (content-free)")
    d.add_argument("--wait", action="store_true", help="wait for CHANNEL_UP up to FUSION_READY_TIMEOUT_S")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor(wait=args.wait, as_json=args.json)
    return 2


if __name__ == "__main__":
    sys.exit(main())
