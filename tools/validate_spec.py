#!/usr/bin/env python3
"""Validate spec/ with the standard library only.

Checks, in order:
  1. spec/envelope.schema.json and spec/scenarios/scenario.schema.json parse.
  2. Every spec/fixtures/*.json has the fixture shape and its `schemaValid` expectation holds
     against the envelope schema (tables validate against #/$defs/table).
  3. Every spec/scenarios/*.json validates against the scenario schema and its id matches its file name.
  4. spec/errors.json is well formed and its remote codes equal the schema's enum.

Exit status 1 on any failure; prints one line per problem. Run from the repo root:

    python3 tools/validate_spec.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

JSON = Any


class SchemaError(Exception):
    """Raised for a schema that this validator does not support."""


class Validator:
    """A JSON Schema 2020-12 subset: the keywords spec/ uses, nothing more.

    Supported: type (incl. list), const, enum, properties, required, additionalProperties,
    items, prefixItems, minItems, maxItems, minLength, maxLength, pattern, minimum, maximum,
    oneOf, anyOf, allOf, not, $ref (local, '#/…' pointers), $defs.
    """

    def __init__(self, schema: JSON) -> None:
        self.root = schema

    def validate(self, instance: JSON, schema: JSON | None = None, path: str = "$") -> list[str]:
        if schema is None:
            schema = self.root
        if schema is True:
            return []
        if schema is False:
            return [f"{path}: schema false"]
        if not isinstance(schema, dict):
            raise SchemaError(f"unsupported schema node at {path}: {schema!r}")
        errors: list[str] = []
        for key, value in schema.items():
            handler = getattr(self, f"_kw_{key.lstrip('$')}", None)
            if handler is None:
                if key in ("$schema", "$id", "title", "description", "$comment", "$defs", "definitions"):
                    continue
                raise SchemaError(f"unsupported keyword {key!r} at {path}")
            errors.extend(handler(instance, value, schema, path))
        return errors

    # -- keyword handlers -------------------------------------------------

    def _kw_ref(self, inst: JSON, ref: str, schema: JSON, path: str) -> list[str]:
        if not ref.startswith("#/"):
            raise SchemaError(f"only local refs are supported: {ref}")
        node: JSON = self.root
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            node = node[part]
        return self.validate(inst, node, path)

    def _kw_type(self, inst: JSON, typ: str | list[str], schema: JSON, path: str) -> list[str]:
        types = [typ] if isinstance(typ, str) else typ
        if any(self._is_type(inst, t) for t in types):
            return []
        return [f"{path}: expected type {typ}, got {type(inst).__name__}"]

    @staticmethod
    def _is_type(inst: JSON, t: str) -> bool:
        if t == "object":
            return isinstance(inst, dict)
        if t == "array":
            return isinstance(inst, list)
        if t == "string":
            return isinstance(inst, str)
        if t == "boolean":
            return isinstance(inst, bool)
        if t == "null":
            return inst is None
        if t == "number":
            return isinstance(inst, (int, float)) and not isinstance(inst, bool)
        if t == "integer":
            if isinstance(inst, bool):
                return False
            if isinstance(inst, int):
                return True
            return isinstance(inst, float) and math.isfinite(inst) and inst == math.floor(inst)
        raise SchemaError(f"unknown type {t}")

    def _kw_const(self, inst: JSON, const: JSON, schema: JSON, path: str) -> list[str]:
        return [] if _json_equal(inst, const) else [f"{path}: expected const {const!r}"]

    def _kw_enum(self, inst: JSON, enum: list[JSON], schema: JSON, path: str) -> list[str]:
        return [] if any(_json_equal(inst, e) for e in enum) else [f"{path}: {inst!r} not in enum"]

    def _kw_properties(self, inst: JSON, props: dict[str, JSON], schema: JSON, path: str) -> list[str]:
        if not isinstance(inst, dict):
            return []
        errors: list[str] = []
        for name, sub in props.items():
            if name in inst:
                errors.extend(self.validate(inst[name], sub, f"{path}.{name}"))
        return errors

    def _kw_required(self, inst: JSON, req: list[str], schema: JSON, path: str) -> list[str]:
        if not isinstance(inst, dict):
            return []
        return [f"{path}: missing required {name!r}" for name in req if name not in inst]

    def _kw_additionalProperties(self, inst: JSON, ap: JSON, schema: JSON, path: str) -> list[str]:
        if not isinstance(inst, dict):
            return []
        declared = set(schema.get("properties", {}))
        errors: list[str] = []
        for name in inst:
            if name in declared:
                continue
            if ap is False:
                errors.append(f"{path}: additional property {name!r}")
            elif ap is not True:
                errors.extend(self.validate(inst[name], ap, f"{path}.{name}"))
        return errors

    def _kw_items(self, inst: JSON, items: JSON, schema: JSON, path: str) -> list[str]:
        if not isinstance(inst, list):
            return []
        start = len(schema.get("prefixItems", []))
        errors: list[str] = []
        for i in range(start, len(inst)):
            errors.extend(self.validate(inst[i], items, f"{path}[{i}]"))
        return errors

    def _kw_prefixItems(self, inst: JSON, prefix: list[JSON], schema: JSON, path: str) -> list[str]:
        if not isinstance(inst, list):
            return []
        errors: list[str] = []
        for i, sub in enumerate(prefix[: len(inst)]):
            errors.extend(self.validate(inst[i], sub, f"{path}[{i}]"))
        return errors

    def _kw_minItems(self, inst: JSON, n: int, schema: JSON, path: str) -> list[str]:
        return [f"{path}: fewer than {n} items"] if isinstance(inst, list) and len(inst) < n else []

    def _kw_maxItems(self, inst: JSON, n: int, schema: JSON, path: str) -> list[str]:
        return [f"{path}: more than {n} items"] if isinstance(inst, list) and len(inst) > n else []

    def _kw_minLength(self, inst: JSON, n: int, schema: JSON, path: str) -> list[str]:
        return [f"{path}: shorter than {n}"] if isinstance(inst, str) and len(inst) < n else []

    def _kw_maxLength(self, inst: JSON, n: int, schema: JSON, path: str) -> list[str]:
        return [f"{path}: longer than {n}"] if isinstance(inst, str) and len(inst) > n else []

    def _kw_pattern(self, inst: JSON, pat: str, schema: JSON, path: str) -> list[str]:
        if not isinstance(inst, str):
            return []
        return [] if re.search(pat, inst) else [f"{path}: {inst!r} does not match {pat}"]

    def _kw_minimum(self, inst: JSON, n: float, schema: JSON, path: str) -> list[str]:
        if isinstance(inst, bool) or not isinstance(inst, (int, float)):
            return []
        return [f"{path}: {inst} < {n}"] if inst < n else []

    def _kw_maximum(self, inst: JSON, n: float, schema: JSON, path: str) -> list[str]:
        if isinstance(inst, bool) or not isinstance(inst, (int, float)):
            return []
        return [f"{path}: {inst} > {n}"] if inst > n else []

    def _kw_oneOf(self, inst: JSON, subs: list[JSON], schema: JSON, path: str) -> list[str]:
        matches = [i for i, sub in enumerate(subs) if not self.validate(inst, sub, path)]
        if len(matches) == 1:
            return []
        return [f"{path}: oneOf matched {len(matches)} branches"]

    def _kw_anyOf(self, inst: JSON, subs: list[JSON], schema: JSON, path: str) -> list[str]:
        if any(not self.validate(inst, sub, path) for sub in subs):
            return []
        return [f"{path}: anyOf matched no branch"]

    def _kw_allOf(self, inst: JSON, subs: list[JSON], schema: JSON, path: str) -> list[str]:
        errors: list[str] = []
        for sub in subs:
            errors.extend(self.validate(inst, sub, path))
        return errors

    def _kw_not(self, inst: JSON, sub: JSON, schema: JSON, path: str) -> list[str]:
        return [f"{path}: matched a forbidden schema"] if not self.validate(inst, sub, path) else []


def _json_equal(a: JSON, b: JSON) -> bool:
    """Equality with JSON semantics: 1 == 1.0, but True != 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


# -- spec checks ------------------------------------------------------------

FIXTURE_KEYS = {"id", "description", "kind", "input", "valid", "schemaValid", "canonical", "decoded", "error"}
FIXTURE_REQUIRED = {"id", "description", "kind", "input", "valid", "schemaValid"}


def load(path: Path) -> JSON:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def check_fixtures(spec: Path, envelope: Validator) -> list[str]:
    problems: list[str] = []
    files = sorted((spec / "fixtures").glob("*.json"))
    if not files:
        return ["no fixtures found"]
    table_schema = {"$ref": "#/$defs/table"}
    for f in files:
        try:
            fx = load(f)
        except json.JSONDecodeError as exc:
            problems.append(f"{f.name}: invalid JSON: {exc}")
            continue
        if not isinstance(fx, dict):
            problems.append(f"{f.name}: fixture is not an object")
            continue
        missing = FIXTURE_REQUIRED - fx.keys()
        extra = fx.keys() - FIXTURE_KEYS
        if missing:
            problems.append(f"{f.name}: missing keys {sorted(missing)}")
        if extra:
            problems.append(f"{f.name}: unknown keys {sorted(extra)}")
        if fx.get("id") != f.stem:
            problems.append(f"{f.name}: id {fx.get('id')!r} != file name")
        if fx.get("kind") not in ("envelope", "table"):
            problems.append(f"{f.name}: kind must be envelope or table")
            continue
        if fx.get("valid") is False and "error" not in fx:
            problems.append(f"{f.name}: invalid fixture needs an error")
        if fx.get("valid") is True and "error" in fx:
            problems.append(f"{f.name}: valid fixture must not carry error")
        if fx.get("valid") is True and fx.get("schemaValid") is False:
            problems.append(f"{f.name}: a decoder-valid fixture cannot be schema-invalid")
        schema = None if fx["kind"] == "envelope" else table_schema
        errors = envelope.validate(fx["input"], schema)
        if bool(errors) == bool(fx.get("schemaValid")):
            verdict = "rejected" if errors else "accepted"
            problems.append(
                f"{f.name}: schema {verdict} input but schemaValid={fx.get('schemaValid')}"
                + (f" ({errors[0]})" if errors else "")
            )
        if "canonical" in fx:
            cerrors = envelope.validate(fx["canonical"], schema)
            if cerrors:
                problems.append(f"{f.name}: canonical form fails the schema: {cerrors[0]}")
    return problems


def check_scenarios(spec: Path, scenario: Validator) -> list[str]:
    problems: list[str] = []
    files = sorted((spec / "scenarios").glob("*.json"))
    files = [f for f in files if f.name != "scenario.schema.json"]
    if not files:
        return ["no scenarios found"]
    for f in files:
        try:
            sc = load(f)
        except json.JSONDecodeError as exc:
            problems.append(f"{f.name}: invalid JSON: {exc}")
            continue
        errors = scenario.validate(sc)
        problems.extend(f"{f.name}: {e}" for e in errors)
        if isinstance(sc, dict) and sc.get("id") != f.stem:
            problems.append(f"{f.name}: id {sc.get('id')!r} != file name")
        if isinstance(sc, dict):
            slugs = [leg.get("peerOrgSlug") for leg in sc.get("legs", [])]
            if len(slugs) != len(set(slugs)):
                problems.append(f"{f.name}: duplicate peerOrgSlug")
    return problems


def check_errors(spec: Path, envelope_schema: JSON) -> list[str]:
    problems: list[str] = []
    errors = load(spec / "errors.json")
    remote = set(errors.get("remoteCodes", {}))
    schema_codes = set(envelope_schema["$defs"]["remoteError"]["properties"]["code"]["enum"])
    if remote != schema_codes:
        problems.append(f"errors.json remoteCodes {sorted(remote)} != schema enum {sorted(schema_codes)}")
    if set(errors.get("terminalCodes", {})) != {"STUDY_COMPLETE", "SESSION_ERRORED", "LIMIT_EXCEEDED"}:
        problems.append("errors.json terminalCodes must be exactly the three terminal codes")
    names = [c.get("python") for c in errors.get("localClasses", [])]
    if len(names) != len(set(names)):
        problems.append("errors.json localClasses has duplicate python names")
    for cls in errors.get("localClasses", []):
        if set(cls) != {"python", "r", "terminal", "when"}:
            problems.append(f"errors.json localClasses entry {cls.get('python')} has wrong keys")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    spec = root / "spec"
    problems: list[str] = []
    try:
        envelope_schema = load(spec / "envelope.schema.json")
        scenario_schema = load(spec / "scenarios" / "scenario.schema.json")
        envelope = Validator(envelope_schema)
        scenario = Validator(scenario_schema)
        problems += check_fixtures(spec, envelope)
        problems += check_scenarios(spec, scenario)
        problems += check_errors(spec, envelope_schema)
    except (OSError, json.JSONDecodeError, SchemaError, KeyError) as exc:
        problems.append(f"fatal: {exc!r}")
    for p in problems:
        print(p)
    n_fx = len(list((spec / "fixtures").glob("*.json")))
    n_sc = len([f for f in (spec / "scenarios").glob("*.json") if f.name != "scenario.schema.json"])
    print(f"validate_spec: {n_fx} fixtures, {n_sc} scenarios, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
