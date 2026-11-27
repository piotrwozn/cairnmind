"""Small caller-supplied answer contracts, without values, references or coercion.

Schemas are bounded before they reach the local model's JSON grammar builder.
Only ordinary typed objects/arrays/scalars are accepted; length limits are capped
at 65536 characters and array limits at 1024 items.
"""
from __future__ import annotations

from itertools import islice
import json
import math

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


MAX_SCHEMA_BYTES = 8192
MAX_SCHEMA_DEPTH = 6
MAX_SCHEMA_NODES = 64
MAX_PROPERTIES = 32
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_PRIMITIVES = _TYPES - {"object", "array"}
_KEYS = {"type", "properties", "required", "additionalProperties", "items", "description",
         "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"}


def prepare_answer_schema(schema: dict | None) -> dict | None:
    """Validate the restricted JSON Schema subset and return a detached JSON copy.

    No defaults, enum values or const values may supply an answer. Standard
    additionalProperties semantics are retained: callers use explicit false to
    require closed objects. Invalid contracts raise ValueError before inference.
    """
    if schema is None:
        return None
    count = 0

    def visit(node, depth, ancestors):
        nonlocal count
        count += 1
        if depth > MAX_SCHEMA_DEPTH or count > MAX_SCHEMA_NODES:
            raise ValueError("answer_schema exceeds its nesting or node budget")
        if not isinstance(node, dict) or id(node) in ancestors:
            raise ValueError("answer_schema nodes must be non-cyclic objects")
        if any(not isinstance(key, str) for key in node) or set(node) - _KEYS:
            raise ValueError("answer_schema contains unsupported keywords")
        declared = node.get("type")
        if isinstance(declared, str) and declared in _TYPES:
            kinds = {declared}
        elif (isinstance(declared, list) and declared
              and all(isinstance(kind, str) and kind in _PRIMITIVES for kind in declared)
              and len(declared) == len(set(declared))):
            kinds = set(declared)
        else:
            raise ValueError("Every answer_schema node requires a supported type")
        description = node.get("description")
        if description is not None and (not isinstance(description, str) or len(description) > 512):
            raise ValueError("answer_schema descriptions must be strings of at most 512 characters")
        if "description" in node and description is None:
            raise ValueError("answer_schema descriptions must be strings")
        ancestors = ancestors | {id(node)}
        if "object" in kinds:
            properties = node.get("properties", {})
            if (not isinstance(properties, dict) or len(properties) > MAX_PROPERTIES
                    or any(not isinstance(key, str) or len(key) > MAX_SCHEMA_BYTES for key in properties)):
                raise ValueError("answer_schema properties must have at most 32 bounded string names")
            required = node.get("required", [])
            if (not isinstance(required, list) or len(required) > MAX_PROPERTIES
                    or not all(isinstance(key, str) for key in required)
                    or len(required) != len(set(required)) or set(required) - set(properties)):
                raise ValueError("answer_schema required must name distinct declared properties")
            if "additionalProperties" in node and type(node["additionalProperties"]) is not bool:
                raise ValueError("answer_schema additionalProperties must be boolean")
            for child in properties.values():
                visit(child, depth + 1, ancestors)
        elif {"properties", "required", "additionalProperties"}.intersection(node):
            raise ValueError("Object keywords require type object")
        if "array" in kinds:
            if "items" not in node:
                raise ValueError("answer_schema arrays require an items schema")
            visit(node["items"], depth + 1, ancestors)
        elif {"items", "minItems", "maxItems"}.intersection(node):
            raise ValueError("Array keywords require type array")
        if {"minLength", "maxLength"}.intersection(node) and "string" not in kinds:
            raise ValueError("Length keywords require type string")
        if {"minimum", "maximum"}.intersection(node) and not {"integer", "number"}.intersection(kinds):
            raise ValueError("Numeric bounds require a numeric type")
        for minimum, maximum, limit in (("minLength", "maxLength", 65536),
                                        ("minItems", "maxItems", 1024)):
            for key in (minimum, maximum):
                if key in node and (type(node[key]) is not int or not 0 <= node[key] <= limit):
                    raise ValueError("answer_schema length and item bounds exceed their supported limits")
            if minimum in node and maximum in node and node[minimum] > node[maximum]:
                raise ValueError("answer_schema minimum bounds must not exceed maximum bounds")
        for key in ("minimum", "maximum"):
            if key in node:
                value = node[key]
                if type(value) not in {int, float} or (isinstance(value, float) and not math.isfinite(value)):
                    raise ValueError("answer_schema numeric bounds must be finite numbers")
        if "minimum" in node and "maximum" in node and node["minimum"] > node["maximum"]:
            raise ValueError("answer_schema minimum bounds must not exceed maximum bounds")

    visit(schema, 1, set())
    try:
        encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
            raise ValueError("answer_schema exceeds its 8192-byte budget")
        prepared = json.loads(encoded)
        Draft202012Validator.check_schema(prepared)
    except (SchemaError, TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError("answer_schema is not a supported JSON Schema") from exc
    return prepared


def _is_json_value(value) -> bool:
    """Check JSON-native values and cycles without serializing private content."""
    stack, active, complete = [(value, False)], set(), set()
    while stack:
        item, leaving = stack.pop()
        if isinstance(item, (dict, list)):
            identity = id(item)
            if leaving:
                active.remove(identity)
                complete.add(identity)
                continue
            if identity in active:
                return False
            if identity in complete:
                continue
            if isinstance(item, dict) and any(not isinstance(key, str) for key in item):
                return False
            active.add(identity)
            stack.append((item, True))
            stack.extend((child, False) for child in (item.values() if isinstance(item, dict) else item))
        elif type(item) not in {str, int, float, bool, type(None)}:
            return False
        elif isinstance(item, float) and not math.isfinite(item):
            return False
    return True


def _error_path(error, schema) -> str:
    # Only declared schema property names and array offsets are shown. Unknown
    # object keys can themselves contain private values, so they are never echoed.
    path, node = "$", schema
    for part in error.absolute_path:
        if isinstance(part, str) and isinstance(node, dict) and part in node.get("properties", {}):
            path += "/" + part.replace("~", "~0").replace("/", "~1")
            node = node["properties"][part]
        elif type(part) is int and isinstance(node, dict) and "items" in node:
            path += "/" + str(part)
            node = node["items"]
        else:
            path += "/?"
            break
    return path[:200]


def answer_contract_errors(value, schema: dict | None) -> list[str]:
    """Return up to five safe shape errors, without coercing or exposing values.

    Schemas are checked here as well, so direct callers cannot enable external
    reference resolution by bypassing prepare_answer_schema.
    """
    prepared = prepare_answer_schema(schema)
    if prepared is None:
        return []
    if not _is_json_value(value):
        return ["$: answer must be a finite JSON value"]
    result = []
    try:
        for error in islice(Draft202012Validator(prepared).iter_errors(value), 5):
            path = _error_path(error, prepared)
            if error.validator == "type":
                expected = error.validator_value
                name = " or ".join(expected) if isinstance(expected, list) else expected
                reason = f"expected type {name}"
            else:
                reason = {
                    "required": "required object properties are missing",
                    "additionalProperties": "unexpected object properties",
                    "minLength": "string is shorter than the declared minimum",
                    "maxLength": "string exceeds the declared maximum",
                    "minItems": "array has fewer items than required",
                    "maxItems": "array exceeds the declared item limit",
                    "minimum": "number is below the declared minimum",
                    "maximum": "number exceeds the declared maximum",
                }.get(error.validator, "answer violates its declared schema")
            result.append(f"{path}: {reason}")
    except (RecursionError, TypeError, ValueError):
        return ["$: answer nesting or types could not be validated"]
    return result
