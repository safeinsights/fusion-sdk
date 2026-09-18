#!/usr/bin/env python3
"""Diff the SDK's local-API requirements against a JSON-Schema export of the tunnel's zod schemas (Phase 8).

    python3 tools/contract_diff.py path/to/local-api.export.json

Expected export shape (produced in fusion-tunnel-app from `src/schemas/local-api.ts`, e.g. with
zod-to-json-schema), one entry per route:

    {"apiVersion": "1.0.0",
     "routes": {"POST /v1/request": {"request": <json schema>, "response": <json schema>, "statuses": [202, 403, ...]}, ...},
     "terminalBody": <json schema>, "budget": <json schema>}

The check is one-directional: every field and status the SDK relies on (spec/local-api.requirements.json)
must be present in the export; extra fields in the export are fine. Routes marked optional (T7) only warn.
Exit 1 on a hard mismatch. Until the tunnel repo exists this runs only when an export is provided.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def schema_fields(schema: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(all property names, required property names) of an object schema, following simple $ref/allOf."""
    props: set[str] = set()
    required: set[str] = set()
    for sub in schema.get("allOf", []) + schema.get("anyOf", []) + schema.get("oneOf", []) + [schema]:
        props |= set(sub.get("properties", {}))
        required |= set(sub.get("required", []))
    return props, required


def check(export: dict[str, Any], req: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    major = str(export.get("apiVersion", "")).split(".")[0]
    if major != str(req["apiVersionMajor"]):
        errors.append(f"apiVersion major {major!r} != required {req['apiVersionMajor']}")
    routes = export.get("routes", {})
    for route, need in req["routes"].items():
        got = routes.get(route)
        if got is None:
            (warnings if need.get("optional") else errors).append(f"{route}: missing from export")
            continue
        for side in ("request", "response"):
            for field in need.get(side, []):
                name, optional = field.rstrip("?"), field.endswith("?")
                props, required = schema_fields(got.get(side, {}))
                if name not in props:
                    (warnings if optional else errors).append(f"{route}: {side}.{name} not in export schema")
                elif not optional and name not in required:
                    warnings.append(f"{route}: {side}.{name} is optional in the export but the SDK requires it")
        missing = set(need.get("statuses", [])) - set(got.get("statuses", []))
        if missing:
            errors.append(f"{route}: statuses {sorted(missing)} not declared in export")
    for key in ("terminalBody", "budget"):
        props, _ = schema_fields(export.get(key, {}))
        for field in req[key]:
            name, optional = field.rstrip("?"), field.endswith("?")
            if name not in props:
                (warnings if optional else errors).append(f"{key}.{name} not in export schema")
    return errors, warnings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    export = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    req = json.loads((ROOT / "spec" / "local-api.requirements.json").read_text(encoding="utf-8"))
    errors, warnings = check(export, req)
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print(f"contract_diff: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
