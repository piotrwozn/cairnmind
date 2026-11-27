"""Fast-tier unit tests for cross-OS normalised symbol hashing (#411) and decision parsers (#412)."""
from __future__ import annotations

from pathlib import Path

from cairnmind.memory.code_decisions import is_adr_path, parse_adr_document, parse_commit_trailers
from cairnmind.scip.parsing import analyze_code
from cairnmind.memory.symbol_anchor import (
    find_symbol_in_analysis,
    make_symbol_anchor,
    posix_path,
    resolve_symbol_anchor,
    validate_symbol_anchor,
)
from cairnmind.terminal.terminal_detail import joined, record_parts, symbol_parts
from cairnmind.terminal.terminal_i18n import tr


def _sym(path: str, code: str, name: str, kind: str | None = None) -> dict:
    analysis = analyze_code(Path(path), code)
    found = find_symbol_in_analysis(analysis, name, kind)
    assert found is not None, f"symbol {name!r} not found in {analysis['symbols']}"
    return found


def test_python_formatting_comments_and_crlf_produce_identical_body_hash():
    base = (
        "def compute_total(items, tax=0.23):\n"
        "    subtotal = sum(items)\n"
        "    return subtotal * (1 + tax)\n"
    )
    reformatted_crlf = (
        "# top-level comment\r\n"
        "def compute_total(\r\n"
        "    items,\r\n"
        "    tax=0.23,  # trailing comma + inline comment\r\n"
        "):\r\n"
        "        # inner comment explaining formula\r\n"
        "        subtotal = sum(items)\r\n"
        "\r\n"
        "        return subtotal * (1 + tax)\r\n"
    )
    a = _sym("src/billing.py", base, "compute_total", "function")
    b = _sym("src/billing.py", reformatted_crlf, "compute_total", "function")
    assert a["body_hash"] == b["body_hash"]
    assert len(a["body_hash"]) == 64


def test_python_string_quote_style_normalized_while_value_change_alters_hash():
    single = "def greet(name):\n    return 'hello ' + name\n"
    double = 'def greet(name):\n    return "hello " + name\n'
    changed = 'def greet(name):\n    return "hi " + name\n'
    h_single = _sym("src/greet.py", single, "greet")["body_hash"]
    h_double = _sym("src/greet.py", double, "greet")["body_hash"]
    h_changed = _sym("src/greet.py", changed, "greet")["body_hash"]
    assert h_single == h_double
    assert h_single != h_changed


def test_python_indentation_scope_and_decorator_changes_alter_hash():
    inside_if = (
        "def run(flag):\n"
        "    if flag:\n"
        "        step_a()\n"
        "        step_b()\n"
    )
    outside_if = (
        "def run(flag):\n"
        "    if flag:\n"
        "        step_a()\n"
        "    step_b()\n"
    )
    assert _sym("src/run.py", inside_if, "run")["body_hash"] != _sym("src/run.py", outside_if, "run")["body_hash"]

    dec_a = "@cache\ndef fetch():\n    return 1\n"
    dec_b = "@lru_cache\ndef fetch():\n    return 1\n"
    assert _sym("src/run.py", dec_a, "fetch")["body_hash"] != _sym("src/run.py", dec_b, "fetch")["body_hash"]


def test_python_methods_get_qualified_names_and_editing_sibling_does_not_affect_hash():
    v1 = (
        "class Auth:\n"
        "    def verify(self, token):\n"
        "        return bool(token)\n\n"
        "    def revoke(self, token):\n"
        "        return None\n"
    )
    v2 = (
        "class Auth:\n"
        "    def verify(self, token):\n"
        "        # untouched logic\n"
        "        return bool(token)\n\n"
        "    def revoke(self, token):\n"
        "        return token.strip()\n"
    )
    a1 = analyze_code(Path("src/auth.py"), v1)
    a2 = analyze_code(Path("src/auth.py"), v2)
    verify1 = find_symbol_in_analysis(a1, "Auth.verify", "method")
    verify2 = find_symbol_in_analysis(a2, "Auth.verify", "method")
    revoke1 = find_symbol_in_analysis(a1, "Auth.revoke", "method")
    revoke2 = find_symbol_in_analysis(a2, "Auth.revoke", "method")
    assert verify1 is not None and verify2 is not None
    assert verify1["body_hash"] == verify2["body_hash"]
    assert revoke1 is not None and revoke2 is not None
    assert revoke1["body_hash"] != revoke2["body_hash"]


def test_js_ts_tsx_formatting_semicolons_and_comments_produce_identical_hash():
    ts_a = (
        "export function parseToken(raw: string): string {\n"
        "  const trimmed = raw.trim();\n"
        "  return trimmed || 'empty';\n"
        "}\n"
    )
    ts_b = (
        "export function parseToken(\r\n"
        "  raw: string,\r\n"
        "): string {\r\n"
        "  // Prettier removed semicolons and switched quotes\r\n"
        "  const trimmed = raw.trim()\r\n"
        '  return trimmed || "empty"\r\n'
        "}\r\n"
    )
    ts_changed = (
        "export function parseToken(raw: string): string {\n"
        "  const trimmed = raw.trim();\n"
        "  return trimmed || 'missing';\n"
        "}\n"
    )
    assert _sym("web/auth.ts", ts_a, "parseToken")["body_hash"] == _sym("web/auth.ts", ts_b, "parseToken")["body_hash"]
    assert _sym("web/auth.ts", ts_a, "parseToken")["body_hash"] != _sym("web/auth.ts", ts_changed, "parseToken")["body_hash"]

    tsx_a = "export function Badge({ label }: { label: string }) {\n  return <span>{label}</span>;\n}\n"
    tsx_b = "export function Badge({ label }: { label: string }) {\r\n  /* comment */\r\n  return <span>{label}</span>\r\n}\r\n"
    assert _sym("web/Badge.tsx", tsx_a, "Badge")["body_hash"] == _sym("web/Badge.tsx", tsx_b, "Badge")["body_hash"]


def test_posix_path_and_anchor_validation():
    assert posix_path(r"src\cairnmind\sessions.py") == "src/cairnmind/sessions.py"
    assert posix_path("./src/cairnmind/sessions.py") == "src/cairnmind/sessions.py"
    anchor = make_symbol_anchor(r"src\app.py", "App.run", "method", "a" * 64)
    assert anchor["path"] == "src/app.py"
    assert validate_symbol_anchor(anchor) == anchor
    assert validate_symbol_anchor({**anchor, "body_hash": "short"}) is None
    assert validate_symbol_anchor({**anchor, "kind": "constant"}) is None


def test_resolve_symbol_anchor_by_name_and_by_quote():
    code = (
        "def alpha():\n"
        "    return 'one'\n\n"
        "class Box:\n"
        "    def open(self):\n"
        "        return 'opened'\n"
    )
    analysis = analyze_code(Path("src/box.py"), code)
    by_name = resolve_symbol_anchor(r"src\box.py", code, analysis, symbol_name="Box.open")
    assert by_name is not None and by_name["path"] == "src/box.py" and by_name["symbol"] == "Box.open"
    by_quote = resolve_symbol_anchor("src/box.py", code, analysis, quote="return 'opened'")
    assert by_quote == by_name


def test_extract_tagged_comments_python_and_typescript():
    py_code = (
        "class Tokens:\n"
        "    # DECISION: Rotate tokens on every refresh\n"
        "    # and revoke the family on reuse.\n"
        "    def rotate_token(self, tok):\n"
        "        # INVARIANT: Token must never be logged in plain text\n"
        "        return tok\n"
    )
    py_comments = analyze_code(Path("src/auth.py"), py_code)["tagged_comments"]
    assert len(py_comments) == 2
    assert py_comments[0]["tag"] == "DECISION" and py_comments[0]["kind"] == "decision"
    assert py_comments[0]["text"] == "Rotate tokens on every refresh and revoke the family on reuse."
    assert py_comments[0]["symbol"] == "Tokens.rotate_token"
    assert py_comments[1]["tag"] == "INVARIANT" and py_comments[1]["kind"] == "constraint"
    assert py_comments[1]["symbol"] == "Tokens.rotate_token"

    ts_code = (
        "/*\n"
        " * WHY: Keep retry cap at 5 to avoid thundering herd.\n"
        " */\n"
        "export class Retrier {\n"
        "  retry() {\n"
        "    // DECISION: Backoff doubles each attempt\n"
        "    return 5;\n"
        "  }\n"
        "}\n"
    )
    ts_comments = analyze_code(Path("web/retry.ts"), ts_code)["tagged_comments"]
    assert len(ts_comments) == 2
    assert ts_comments[0]["tag"] == "WHY" and ts_comments[0]["symbol"] == "Retrier"
    assert ts_comments[1]["tag"] == "DECISION" and ts_comments[1]["symbol"] == "Retrier.retry"


def test_adr_and_git_trailer_parsers():
    assert is_adr_path("docs/adr/0001-arcadedb.md") is True
    assert is_adr_path("docs/adr/README.md") is False
    assert is_adr_path("docs/guide.md") is False

    adr_text = (
        "---\n"
        "id: ADR-0001\n"
        "title: Single-writer ArcadeDB\n"
        "status: accepted\n"
        "symbols:\n"
        "  - src/store.py:Store.init\n"
        "---\n\n"
        "# ADR-0001: Single-writer ArcadeDB\n\n"
        "## Context\nMultiple processes access memory.\n\n"
        "## Decision\nOnly one installation may write the database at a time.\n"
    )
    parsed = parse_adr_document("docs/adr/0001-arcadedb.md", adr_text)
    assert parsed is not None
    assert parsed["adr_id"] == "ADR-0001"
    assert parsed["kind"] == "decision"
    assert "Only one installation may write the database at a time." in parsed["text"]
    assert parsed["symbol_refs"] == [("src/store.py", "Store.init")]

    trailers = parse_commit_trailers(
        "a" * 40,
        "2026-11-26T12:00:00+00:00",
        "Harden token rotation",
        "Rotate refresh tokens cleanly.\n\nDecision: Revoke entire family on replay\nInvariant: Never store raw tokens\nRefs: ADR-0007\n",
    )
    assert len(trailers) == 2
    assert trailers[0]["tag"] == "DECISION" and trailers[0]["kind"] == "decision" and trailers[0]["refs"] == ["ADR-0007"]
    assert trailers[1]["tag"] == "INVARIANT" and trailers[1]["kind"] == "constraint"

    refs_only = parse_commit_trailers(
        "b" * 40,
        "2026-11-26T12:00:00+00:00",
        "Adopt single-writer lock",
        "Implementation details.\n\nRefs: ADR-0012\n",
    )
    assert len(refs_only) == 1
    assert refs_only[0]["tag"] == "REFS" and "ADR-0012" in refs_only[0]["text"]


def test_terminal_detail_symbol_lines_en_and_pl():
    ev_changed = {
        "evidence_state": "evidence_replaced",
        "quote_survives": False,
        "symbol_state": "symbol_changed",
        "symbol_anchor": make_symbol_anchor("src/auth.py", "rotate_token", "function", "a" * 64),
        "symbol_change": {
            "symbol": "rotate_token",
            "kind": "function",
            "path": "src/auth.py",
            "commit": "1234567890abcdef",
            "changed_at": "2026-11-26T10:00:00+00:00",
        },
        "comment_state": "tag_removed",
        "comment_anchor": {"tag": "DECISION", "path": "src/auth.py", "text_sha256": "b" * 64},
    }
    en_text = joined(record_parts({"record": {"id": "r1", "kind": "decision", "status": "confirmed", "version": 1, "content": "Rule"}, "evidence": ev_changed}, lambda t, **v: tr(t, "en", **v)))
    pl_text = joined(record_parts({"record": {"id": "r1", "kind": "decision", "status": "confirmed", "version": 1, "content": "Rule"}, "evidence": ev_changed}, lambda t, **v: tr(t, "pl", **v)))
    assert "Symbol: needs review: rotate_token changed in commit 1234567890ab (2026-11-26)" in en_text
    assert "Comment tag: the DECISION: comment was removed from src/auth.py" in en_text
    assert "Symbol: wymaga przeglądu: rotate_token zmienił się w commicie 1234567890ab (2026-11-26)" in pl_text
    assert "Tag komentarza: komentarz DECISION: został usunięty z src/auth.py" in pl_text

    # Unanchored evidence returns no extra symbol lines.
    assert symbol_parts({"evidence_state": "anchored", "quote_survives": None}, lambda t, **v: tr(t, "en", **v)) == []
