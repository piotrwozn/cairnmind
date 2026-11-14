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


PYPROJECT = '''# Project metadata.
[project]
name = "sample"
version = "1.4.2"   # bumped by the release script
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",  # [not a table]
    "rich",
]
description = """A
multi-line # not a comment
text"""

[project.scripts]
sample = "sample.cli:main"

[tool.ruff]
line-length = 110
lint.select = ["E", "F"]
"quoted.key" = 'x'
inline = { a = 1, b = [2, 3] }

[[tool.mypy.overrides]]
module = "a"
'''


def test_toml_keys_are_symbols_named_by_their_table():
    result = analyze_code(Path("pyproject.toml"), PYPROJECT)
    assert result["language"] == "toml" and result["errors"] == []
    found = {s["name"]: s for s in result["symbols"]}
    assert set(found) == {
        "project.name", "project.version", "project.requires-python", "project.dependencies",
        "project.description", "project.scripts.sample", "tool.ruff.line-length", "tool.ruff.lint.select",
        "tool.ruff.quoted.key", "tool.ruff.inline", "tool.mypy.overrides.module"}
    python = found["project.requires-python"]
    assert (python["kind"], python["owner"], python["member"]) == ("field", "project", "requires-python")
    assert (python["line_start"], python["line_end"]) == (5, 5)
    # A value over several lines keeps all of them: an array, a multi-line string.
    assert (found["project.dependencies"]["line_start"], found["project.dependencies"]["line_end"]) == (6, 9)
    assert (found["project.description"]["line_start"], found["project.description"]["line_end"]) == (10, 12)
    assert found["tool.ruff.lint.select"]["owner"] == "tool.ruff.lint"
    assert result["imports"] == [] and result["calls"] == []


def test_toml_top_level_keys_and_invalid_files():
    result = analyze_code(Path("settings.toml"), 'title = "x"\n[a]\nb = 1\n')
    assert {s["name"]: s["kind"] for s in result["symbols"]} == {"title": "constant", "a.b": "field"}
    broken = analyze_code(Path("broken.toml"), 'good = 1\nthis is not toml\n[t]\nlater = 2\n')
    # The file is reported as invalid; the keys that can be read are still kept.
    assert broken["errors"]
    assert {s["name"] for s in broken["symbols"]} == {"good", "t.later"}
    # A lock file in TOML syntax is not configuration: it stays text.
    assert analyze_code(Path("uv.lock"), 'version = 1\n')["language"] == "text"
