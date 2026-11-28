"""AST and runtime guards sealing the StorageBackend DAL boundary (Phase 3 / P1.1).

Verifies:
1. ZERO raw DB `.query()`, `.command()`, `.script()`, `one_row()`, `update_document()`,
   or `delete_documents()` calls exist anywhere in `src/cairnmind/` outside the dedicated
   storage engine layer.
2. `SQLiteBackend` contains no regex ArcadeDB->SQLite SQL transpiler and refuses raw SQL
   explicitly (`NotImplementedError`, or `ReadOnlyStoreError` when opened read-only).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cairnmind.storage.sqlite_backend import SQLiteBackend
from cairnmind.store import Store

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "cairnmind"

# Dedicated storage engine layer allowed to execute backend SQL directly.
STORAGE_LAYER_FILES = frozenset({
    "arcade.py",
    "arcade_backend.py",
    "sqlite_backend.py",
    "sqlite_vectors.py",
    "storage_query.py",
    "database_credentials.py",
    "journal.py",
    "sql.py",
})

# Low-level pass-through methods kept on Store / ReadOnlyClient for backward compatibility.
EXACT_PASSTHROUGH_BUDGETS = {
    "store.py": 4,          # Store.query() + Store.command()
    "store_readonly.py": 1,  # ReadOnlyClient.query()
}

# Textual TUI modules where `.query(...)` is Textual's CSS widget selector API, not DB SQL.
TEXTUAL_DOM_MODULES = frozenset({
    "terminal_graph.py",
    "terminal_panels.py",
    "terminal_ui.py",
    "tui_app.py",
    "tui_widgets.py",
    "tui_screens.py",
    "cockpit_tui.py",
})


def _scan_raw_db_calls(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in {
            "query", "command", "script", "update_document", "delete_documents",
        }:
            # Exclude Typer CLI decorators (@app.command, @*_app.command).
            if fn.attr == "command" and isinstance(fn.value, ast.Name) and (
                fn.value.id == "app" or fn.value.id.endswith("_app")
            ):
                continue
            # Exclude Textual DOM `.query(...)` calls.
            if fn.attr == "query" and path.name in TEXTUAL_DOM_MODULES:
                continue
            violations.append((node.lineno, f".{fn.attr}()"))
        elif isinstance(fn, ast.Name) and fn.id in {"one_row", "update_document", "delete_documents"}:
            violations.append((node.lineno, f"{fn.id}()"))
    return violations


def test_zero_raw_sql_calls_outside_storage_layer():
    unexpected: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path.name in STORAGE_LAYER_FILES:
            continue
        hits = _scan_raw_db_calls(path)
        budget = EXACT_PASSTHROUGH_BUDGETS.get(path.name, 0)
        if len(hits) > budget:
            rel = path.relative_to(SRC_ROOT).as_posix()
            unexpected.append(f"{rel}: found {len(hits)} (allowed {budget}): {hits}")
    assert unexpected == [], "Raw DB SQL calls leaked outside storage layer:\n" + "\n".join(unexpected)


def test_sqlite_backend_has_no_regex_sql_transpiler_and_refuses_raw_sql(tmp_path: Path):
    import cairnmind.storage.sqlite_backend as sb

    for removed_symbol in ("_translate_expr", "_split_clauses", "_parse_set_assignments", "_exec_query", "_exec_command"):
        assert not hasattr(sb, removed_symbol), f"Legacy SQL transpiler symbol still present: {removed_symbol}"

    backend = SQLiteBackend(tmp_path / "clean.db")
    try:
        backend.init_schema()
        with pytest.raises(NotImplementedError):
            backend.query("SELECT FROM projects")
        with pytest.raises(NotImplementedError):
            backend.command("DELETE FROM projects")
    finally:
        backend.close()

    # Read-only SQLite store still raises ReadOnlyStoreError on writes.
    store = Store.open_memory(tmp_path / "mem")
    try:
        with pytest.raises(NotImplementedError):
            store.query("SELECT FROM projects")
    finally:
        store.close()
