from pathlib import Path

from devbrain.parsing import analyze_code


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
