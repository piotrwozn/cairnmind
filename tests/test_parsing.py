from pathlib import Path

from cairnmind.parsing import analyze_code


def test_python_declarations_imports_and_calls():
    result = analyze_code(Path("auth.py"), """from .tokens import validate as check
import os.path as path, json

class Service:
    def login(self, token):
        return check(token)
""")
    assert result["language"] == "python"
    assert result["errors"] == []
    assert {s["name"] for s in result["symbols"]} == {"Service", "login"}
    tokens = next(x for x in result["imports"] if x["module"] == ".tokens")
    assert tokens["names"] == ["validate"]
    assert tokens["aliases"] == {"validate": "check"}
    assert {x["module"] for x in result["imports"]} == {".tokens", "os.path", "json"}
    assert {c["name"] for c in result["calls"]} == {"check"}


def test_typescript_symbols_alias_import_and_arrow():
    result = analyze_code(Path("auth.ts"), """import defaultClient, {validate as check} from './tokens';
import * as utils from './utils';
interface User { name: string }
export const login = (token: string): boolean => check(token);
export class Auth { valid() { return utils.valid(); } }
""")
    assert result["errors"] == []
    assert {s["name"] for s in result["symbols"]} == {"User", "login", "Auth", "valid"}
    tokens = next(x for x in result["imports"] if x["module"] == "./tokens")
    assert tokens["names"] == ["default", "validate"]
    assert tokens["aliases"] == {"default": "defaultClient", "validate": "check"}
    assert {c["name"] for c in result["calls"]} == {"check", "utils.valid"}


def test_commonjs_and_tsx():
    js = analyze_code(Path("auth.cjs"), "const token = require('./token');\nfunction login() { return token(); }")
    assert js["errors"] == []
    assert js["imports"][0]["module"] == "./token"
    tsx = analyze_code(Path("view.tsx"), "export const View = () => <div>Hello</div>;")
    assert tsx["errors"] == []
    assert tsx["symbols"][0]["name"] == "View"


def test_unsupported_and_invalid_are_explicit():
    result = analyze_code(Path("foo.go"), "package main")
    assert result["language"] == "text"
    assert result["symbols"] == []
    result = analyze_code(Path("broken.py"), "def broken(:\n")
    assert result["errors"]


CONSTANTS_AND_FIELDS = '''"""Module."""
from dataclasses import dataclass

__all__ = ["Settings"]
TIMEOUT = 60
LIMITS: dict = {
    "a": 1,
    "b": 2,
}
TIMEOUT = 90
count, total = 1, 2
obj.attr = 3
table["key"] = 4
TIMEOUT += 1
if True:
    GUARDED = 1
try:
    TRIED = 1
except Exception:
    pass


@dataclass
class Settings:
    data_dir: str
    port: int = 8765
    cockpit_port: int = 8766
    __slots__ = ()

    def url(self):
        local = 5
        self.port = local
        return local


def helper():
    INSIDE = 1
    return INSIDE
'''


def test_python_module_constants_are_symbols_with_whole_statement_lines():
    result = analyze_code(Path("settings.py"), CONSTANTS_AND_FIELDS)
    assert result["errors"] == []
    constants = sorted((s["name"], s["line_start"], s["line_end"]) for s in result["symbols"] if s["kind"] == "constant")
    # Each assignment is kept with its own lines; a multi-line value spans its whole statement.
    assert constants == [("LIMITS", 6, 9), ("TIMEOUT", 5, 5), ("TIMEOUT", 10, 10)]


def test_python_class_fields_are_symbols_named_by_their_class():
    result = analyze_code(Path("settings.py"), CONSTANTS_AND_FIELDS)
    fields = {s["name"]: s for s in result["symbols"] if s["kind"] == "field"}
    assert set(fields) == {"Settings.data_dir", "Settings.port", "Settings.cockpit_port"}
    assert fields["Settings.cockpit_port"]["owner"] == "Settings"
    assert fields["Settings.cockpit_port"]["member"] == "cockpit_port"
    assert (fields["Settings.port"]["line_start"], fields["Settings.port"]["line_end"]) == (26, 26)


def test_python_assignments_that_are_not_definitions_are_skipped():
    names = {s["name"] for s in analyze_code(Path("settings.py"), CONSTANTS_AND_FIELDS)["symbols"]}
    # Dunder names, tuple/attribute/subscript targets, augmented assignments, blocks and functions.
    for skipped in ("__all__", "count", "total", "obj.attr", "GUARDED", "TRIED", "INSIDE", "local",
                    "Settings.__slots__", "Settings.local", "self.port"):
        assert skipped not in names
    assert {"Settings", "url", "helper"} <= names


def test_javascript_top_level_values_stay_out_of_symbols():
    result = analyze_code(Path("limits.js"), "const LIMIT = 5;\nexport const NAME = 'x';\n")
    assert result["symbols"] == []
