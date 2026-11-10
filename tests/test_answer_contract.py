from __future__ import annotations

from copy import deepcopy
import math

import pytest

from cairnmind.answer_contract import answer_contract_errors, prepare_answer_schema


def closed(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def test_unknown_answer_must_match_requested_shape_without_rewriting():
    schema = closed({"answer": {"type": "string"}})
    assert answer_contract_errors({"answer": "unknown"}, schema) == []
    malformed = {"answer": {"secret_marker": "unknown"}}
    before = deepcopy(malformed)
    assert answer_contract_errors(malformed, schema) == ["$/answer: expected type string"]
    assert malformed == before


def test_contract_checks_types_closed_fields_and_arrays_without_expected_facts():
    schema = closed({"id": {"type": "string"}, "version": {"type": "integer", "minimum": 1},
                     "approved": {"type": "boolean"},
                     "depends_on": {"type": "array", "items": {"type": "string"}, "maxItems": 2}})
    valid = {"id": "any-id-is-allowed", "version": 12, "approved": False, "depends_on": ["another-id"]}
    assert answer_contract_errors(valid, schema) == []
    assert answer_contract_errors({**valid, "version": True}, schema) == ["$/version: expected type integer"]
    assert answer_contract_errors({**valid, "approved": 1}, schema) == ["$/approved: expected type boolean"]
    assert answer_contract_errors({**valid, "depends_on": [42]}, schema) == ["$/depends_on/0: expected type string"]
    assert answer_contract_errors({**valid, "extra": "PRIVATE"}, schema) == ["$: unexpected object properties"]
    assert answer_contract_errors({key: value for key, value in valid.items() if key != "id"}, schema)


def test_schema_is_detached_and_standard_open_object_semantics_are_preserved():
    schema = {"type": "object", "properties": {"label": {"type": ["string", "null"]}}, "required": ["label"]}
    before = deepcopy(schema)
    prepared = prepare_answer_schema(schema)
    assert prepared == before and prepared is not schema
    prepared["properties"]["label"]["type"].append("integer")
    assert schema == before
    assert answer_contract_errors({"label": None, "extra": "allowed"}, schema) == []
    assert answer_contract_errors({"label": 1}, schema)
    assert prepare_answer_schema(None) is None
    assert answer_contract_errors("unstructured answer", None) == []


@pytest.mark.parametrize("schema", [
    {}, True, [],
    {"type": "object", "$ref": "https://example.invalid/private"},
    {"type": "string", "pattern": ".*"},
    {"type": "string", "enum": ["a supplied answer"]},
    {"type": "string", "const": "a supplied answer"},
    {"type": "string", "default": "a supplied answer"},
    {"type": "string", "oneOf": [{"type": "string"}]},
    {"type": ["object", "null"]},
    {"type": ["string", "string"]},
    {"type": []},
    {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["missing"]},
    {"type": "object", "required": [True]},
    {"type": "object", "additionalProperties": {"type": "string"}},
    {"type": "array"},
    {"type": "array", "items": [{"type": "string"}]},
    {"type": "string", "properties": {}},
    {"type": "string", "minimum": 1},
    {"type": "integer", "minLength": 1},
    {"type": "number", "minimum": math.inf},
    {"type": "number", "maximum": math.nan},
    {"type": "integer", "minimum": True},
    {"type": "number", "minimum": 2, "maximum": 1},
    {"type": "string", "minLength": 2, "maxLength": 1},
    {"type": "string", "minLength": -1},
    {"type": "string", "maxLength": True},
    {"type": "string", "maxLength": 65537},
    {"type": "array", "items": {"type": "string"}, "maxItems": 1025},
    {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 1},
    {"type": "string", "description": "x" * 513},
    {"type": "string", "description": None},
    {"type": "string", "description": "\ud800"},
])
def test_rejects_unsupported_unsafe_or_contradictory_contracts(schema):
    with pytest.raises(ValueError):
        prepare_answer_schema(schema)


def test_array_and_string_grammar_boundaries_are_allowed():
    assert prepare_answer_schema({"type": "string", "maxLength": 65536})
    assert prepare_answer_schema({"type": "array", "items": {"type": "null"}, "maxItems": 1024})


def test_depth_nodes_properties_bytes_and_cycles_are_bounded():
    schema = {"type": "string"}
    for _ in range(5):
        schema = closed({"child": schema})
    assert prepare_answer_schema(schema)
    with pytest.raises(ValueError, match="nesting"):
        prepare_answer_schema(closed({"child": schema}))
    assert prepare_answer_schema(closed({str(index): {"type": "string"} for index in range(32)}))
    with pytest.raises(ValueError, match="properties"):
        prepare_answer_schema(closed({str(index): {"type": "string"} for index in range(33)}))
    # Root + two branch objects + their 31/30 children = exactly 64 nodes.
    schema = closed({"left": closed({str(i): {"type": "string"} for i in range(31)}),
                     "right": closed({str(i): {"type": "string"} for i in range(30)})})
    assert prepare_answer_schema(schema)
    schema["properties"]["right"]["properties"]["extra"] = {"type": "string"}
    with pytest.raises(ValueError, match="node budget"):
        prepare_answer_schema(schema)
    with pytest.raises(ValueError, match="byte budget"):
        prepare_answer_schema(closed({str(i): {"type": "string", "description": "x" * 512} for i in range(16)}))
    cycle = {"type": "array"}
    cycle["items"] = cycle
    with pytest.raises(ValueError, match="cyclic"):
        prepare_answer_schema(cycle)


def test_errors_have_safe_paths_and_never_echo_private_values():
    schema = closed({"declared": {"type": "string"}})
    errors = answer_contract_errors({"declared": {"PRIVATE-VALUE": "OTHER-PRIVATE"},
                                     "PRIVATE-OBJECT-KEY": "PRIVATE-CONTENT"}, schema)
    assert len(errors) == 2
    assert "$/declared: expected type string" in errors
    assert not any("PRIVATE" in error for error in errors)
    many = closed({str(index): {"type": "string"} for index in range(12)})
    assert len(answer_contract_errors({str(index): index for index in range(12)}, many)) == 5
    long = closed({"p" * 1000: {"type": "string"}})
    error = answer_contract_errors({"p" * 1000: False}, long)[0]
    assert len(error.split(":", 1)[0]) == 200


def test_errors_do_not_resolve_external_references_even_if_prepare_was_bypassed(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No external request may occur")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(ValueError, match="unsupported"):
        answer_contract_errors("PRIVATE", {"type": "string", "$ref": "https://example.invalid/private"})


@pytest.mark.parametrize("value", [math.nan, math.inf, {"value": math.nan}, {1: "private"}, ("not a JSON array",)])
def test_non_json_or_non_finite_answers_are_rejected(value):
    assert answer_contract_errors(value, {"type": "object", "additionalProperties": True})


def test_cycle_values_are_rejected_but_shared_acyclic_json_is_allowed():
    cycle = []
    cycle.append(cycle)
    assert answer_contract_errors(cycle, {"type": "array", "items": {"type": "null"}})
    shared = ["same"]
    schema = {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}
    value = [shared, shared]
    assert answer_contract_errors(value, schema) == []
    assert value == [["same"], ["same"]]


def test_length_item_and_numeric_constraints_produce_safe_errors():
    assert answer_contract_errors("", {"type": "string", "minLength": 1})
    assert answer_contract_errors("xx", {"type": "string", "maxLength": 1})
    schema = {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2}
    assert answer_contract_errors([], schema)
    assert answer_contract_errors([1, 2, 3], schema)
    assert answer_contract_errors(0, {"type": "number", "minimum": 1})
    assert answer_contract_errors(2, {"type": "number", "maximum": 1})
