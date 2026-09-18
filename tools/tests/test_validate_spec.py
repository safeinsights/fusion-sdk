"""Tests for tools/validate_spec.py: the validator itself and the committed spec."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import validate_spec as vs  # noqa: E402


@pytest.fixture(scope="module")
def envelope() -> vs.Validator:
    return vs.Validator(json.loads((ROOT / "spec" / "envelope.schema.json").read_text()))


def test_committed_spec_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert vs.main(["validate_spec", str(ROOT)]) == 0
    out = capsys.readouterr().out
    assert "0 problem(s)" in out


def test_type_integer_accepts_whole_float() -> None:
    v = vs.Validator({"type": "integer"})
    assert v.validate(2.0) == []
    assert v.validate(2.5)
    assert v.validate(True)


def test_json_equal_distinguishes_bool_from_int() -> None:
    assert vs._json_equal(1, 1.0)
    assert not vs._json_equal(True, 1)
    assert vs._json_equal({"a": [1, {"b": None}]}, {"a": [1.0, {"b": None}]})
    assert not vs._json_equal({"a": 1}, {"a": 1, "b": 2})


def test_one_of_requires_exactly_one_branch() -> None:
    v = vs.Validator({"oneOf": [{"type": "number"}, {"type": "integer"}]})
    assert v.validate(1.5) == []
    assert v.validate(1)  # matches both branches


def test_additional_properties_false() -> None:
    v = vs.Validator({"type": "object", "properties": {"a": {}}, "additionalProperties": False})
    assert v.validate({"a": 1}) == []
    assert v.validate({"a": 1, "b": 2}) == ["$: additional property 'b'"]


def test_ref_and_pattern(envelope: vs.Validator) -> None:
    assert envelope.validate("counts_by_group", {"$ref": "#/$defs/operationName"}) == []
    assert envelope.validate("9x", {"$ref": "#/$defs/operationName"})


def test_unsupported_keyword_is_loud() -> None:
    with pytest.raises(vs.SchemaError):
        vs.Validator({"uniqueItems": True}).validate([1])


def test_fixture_expectations_are_independent(envelope: vs.Validator, tmp_path: Path) -> None:
    """A fixture whose schemaValid flag contradicts the schema is reported."""
    spec = tmp_path / "spec"
    (spec / "fixtures").mkdir(parents=True)
    (spec / "scenarios").mkdir()
    for name in ("envelope.schema.json", "errors.json"):
        (spec / name).write_text((ROOT / "spec" / name).read_text())
    (spec / "scenarios" / "scenario.schema.json").write_text(
        (ROOT / "spec" / "scenarios" / "scenario.schema.json").read_text()
    )
    (spec / "scenarios" / "s.json").write_text(json.dumps({"id": "s", "legs": [{"legId": "l", "peerOrgSlug": "p"}]}))
    bad = {
        "id": "bad",
        "description": "",
        "kind": "envelope",
        "input": {"v": 2},
        "valid": False,
        "schemaValid": True,
        "error": "DECODE",
    }
    (spec / "fixtures" / "bad.json").write_text(json.dumps(bad))
    assert vs.main(["validate_spec", str(tmp_path)]) == 1


def test_every_fixture_has_unique_id() -> None:
    ids = [json.loads(p.read_text())["id"] for p in (ROOT / "spec" / "fixtures").glob("*.json")]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 40
