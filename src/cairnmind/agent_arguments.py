"""Why a tool call's arguments do not fit the tool's schema, told to the model (#240).

The model gets the argument's path, the schema keyword it broke and a fixed hint, so that it can correct the
call instead of guessing. The value it sent is never echoed: it may be long, or hold what the model read.
Names in the path are the model's own argument names, escaped and cut.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .terminal_safe import safe_text

_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
_RANGE = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern",
          "format", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties", "multipleOf"}
# Built-in tools whose expected_sha256 is the hash read_file returned. A third-party tool's field of the same
# name means whatever that tool says, so it gets no advice about these tools.
HASH_TOOLS = {"write_file", "edit_file", "delete_file"}
HASH_HINT = ("expected_sha256 is null to create a new file with write_file, or else the sha256 that read_file "
             "returned for the current version of the file.")


def _name(value) -> str:
    return safe_text(value, limit=64)


def _hint(error) -> str:
    keyword = error.validator
    if keyword == "type":
        expected = error.validator_value if isinstance(error.validator_value, list) else [error.validator_value]
        named = [value for value in expected if isinstance(value, str) and value in _TYPES]
        return "has the wrong type" + (f"; the tool takes {' or '.join(named)}" if named else "")
    if keyword in {"enum", "const"}:
        return "is not one of the values the tool takes"
    if keyword in _RANGE:
        return f"breaks the tool's {keyword} rule (its length, range or form)"
    if keyword in {"anyOf", "oneOf"}:
        return "fits none of the forms the tool takes"
    return "does not fit the tool's schema"


def argument_problem(schema, arguments, tool: str | None = None) -> dict | None:
    """{argument, keyword, hint} for the most relevant error, or None when the arguments fit. `tool` names a
    built-in tool; only HASH_TOOLS get HASH_HINT, never a third-party tool."""
    error = best_match(Draft202012Validator(schema).iter_errors(arguments))
    if error is None:
        return None
    path = [_name(part) for part in list(error.absolute_path)[:8]]
    keyword = _name(error.validator)
    instance = error.instance if isinstance(error.instance, dict) else {}
    if keyword == "required" and isinstance(error.validator_value, list):
        missing = [name for name in error.validator_value if isinstance(name, str) and name not in instance]
        if missing:
            path.append(_name(missing[0]))
        hint = "is required and missing"
    elif keyword == "additionalProperties":
        known = (error.schema.get("properties") or {}) if isinstance(error.schema, dict) else {}
        extra = sorted(_name(name) for name in instance if name not in known)[:4]
        hint = "has arguments the tool does not take" + (f": {', '.join(extra)}" if extra else "")
    else:
        hint = _hint(error)
    argument = "/".join(path)
    if argument:
        hint = f"{argument} {hint}."
    elif keyword == "additionalProperties":
        hint = f"The call {hint}."
    else:
        hint = "The call does not fit the tool's schema."
    if tool in HASH_TOOLS and path[:1] == ["expected_sha256"]:
        hint += " " + HASH_HINT
    return {"argument": argument, "keyword": keyword, "hint": hint}
