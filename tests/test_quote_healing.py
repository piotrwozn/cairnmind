"""Fast unit tests for self-healing quote detection, AST/context mapping, SHA-256 validation, and package CAS."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from cairnmind.memory import memory as portable
from cairnmind.memory.quote_healing import (
    DiffHunk,
    QuoteCASConflict,
    ast_enclosing_scope,
    git_diff_hunks,
    heal_snapshot_package,
    locate_and_validate_quote,
    parse_unified_diff_hunks,
    scan_git_changes,
    sha256_quote,
    tokenize_code,
    validate_quote_checksum,
)
from cairnmind.storage.sql import utc_now
from cairnmind.terminal.terminal_detail import QUOTE_HEALING_LABELS, evidence_parts, joined
from cairnmind.terminal.terminal_i18n import tr
from cairnmind.terminal.terminal_theme import TOKENS, evidence_style


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Quote Fixture")
    _git(root, "config", "user.email", "quote@example.invalid")
    return root


def test_parse_unified_diff_hunks_handles_single_and_multiline_ranges():
    diff = (
        "diff --git a/auth.py b/auth.py\n"
        "--- a/auth.py\n"
        "+++ b/auth.py\n"
        "@@ -2,0 +3,4 @@\n"
        "+# added header\n"
        "@@ -10 +14,2 @@\n"
        "-old\n"
        "+new1\n"
        "+new2\n"
    )
    hunks = parse_unified_diff_hunks(diff)
    assert hunks == [
        DiffHunk(old_start=2, old_count=0, new_start=3, new_count=4),
        DiffHunk(old_start=10, old_count=1, new_start=14, new_count=2),
    ]


def test_relocates_moved_block_in_refactored_python_file_with_real_git_diff(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    old_code = (
        "import hashlib\n"
        "\n"
        "def verify_token(token: str) -> bool:\n"
        "    if not token:\n"
        "        return False\n"
        "    return len(token) == 64\n"
    )
    (repo / "auth.py").write_text(old_code)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial auth")
    c1 = _git(repo, "rev-parse", "HEAD")

    new_code = (
        '"""Authentication helpers."""\n'
        "import hashlib\n"
        "import hmac\n"
        "\n"
        "\n"
        "def helper_one() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def helper_two() -> int:\n"
        "    return 2\n"
        "\n"
        "\n"
        "def verify_token(token: str) -> bool:\n"
        "    if not token:\n"
        "        return False\n"
        "    return len(token) == 64\n"
    )
    (repo / "auth.py").write_text(new_code)
    _git(repo, "commit", "-qam", "refactor auth with helpers above")
    c2 = _git(repo, "rev-parse", "HEAD")

    quote = "def verify_token(token: str) -> bool:\n    if not token:\n        return False\n    return len(token) == 64"
    changes = scan_git_changes(repo, c1, c2)
    assert changes["auth.py"] == {"status": "M", "new_path": "auth.py"}
    hunks = git_diff_hunks(repo, c1, c2, "auth.py")

    outcome = locate_and_validate_quote(
        old_content=old_code,
        new_content=new_code,
        quote_text=quote,
        old_line_start=3,
        old_line_end=6,
        old_path="auth.py",
        new_path="auth.py",
        old_commit_sha=c1,
        new_commit_sha=c2,
        diff_hunks=hunks,
    )
    assert outcome.status == "healed"
    assert outcome.ref_status == "valid"
    assert outcome.stale_ref is False
    assert outcome.requires_intervention is False
    assert (outcome.old_line_start, outcome.old_line_end) == (3, 6)
    assert (outcome.new_line_start, outcome.new_line_end) == (14, 17)
    assert outcome.quote_sha256 == sha256_quote(quote)
    assert outcome.candidate_sha256 == outcome.quote_sha256
    assert outcome.new_commit_sha == c2


def test_relocates_quote_in_renamed_file_via_git_diff(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    old_code = "LOCKOUT_THRESHOLD = 5\nWINDOW_SECONDS = 300\n"
    (repo / "limits.py").write_text(old_code)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    c1 = _git(repo, "rev-parse", "HEAD")

    (repo / "security").mkdir()
    _git(repo, "mv", "limits.py", "security/limits.py")
    new_code = "# Security policy constants\n\nLOCKOUT_THRESHOLD = 5\nWINDOW_SECONDS = 300\n"
    (repo / "security" / "limits.py").write_text(new_code)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "move limits into security package")
    c2 = _git(repo, "rev-parse", "HEAD")

    changes = scan_git_changes(repo, c1, c2)
    assert changes["limits.py"] == {"status": "R", "new_path": "security/limits.py"}
    hunks = git_diff_hunks(repo, c1, c2, "limits.py", "security/limits.py")

    outcome = locate_and_validate_quote(
        old_content=old_code,
        new_content=new_code,
        quote_text="LOCKOUT_THRESHOLD = 5",
        old_line_start=1,
        old_line_end=1,
        old_path="limits.py",
        new_path="security/limits.py",
        old_commit_sha=c1,
        new_commit_sha=c2,
        diff_hunks=hunks,
    )
    assert outcome.status == "healed"
    assert outcome.new_path == "security/limits.py"
    assert (outcome.new_line_start, outcome.new_line_end) == (3, 3)


def test_relocates_moved_block_in_typescript_and_markdown_files():
    old_ts = (
        "export class AuthGuard {\n"
        "  validate(token: string): boolean {\n"
        "    return token.length === 64;\n"
        "  }\n"
        "}\n"
    )
    new_ts = (
        "export const VERSION = 2;\n"
        "export function noop(): void {}\n"
        "\n"
        "export class AuthGuard {\n"
        "  validate(token: string): boolean {\n"
        "    return token.length === 64;\n"
        "  }\n"
        "}\n"
    )
    assert ast_enclosing_scope("guard.ts", old_ts, 3, 3) == (("class", "AuthGuard"), ("method", "validate"))
    ts_outcome = locate_and_validate_quote(
        old_content=old_ts, new_content=new_ts,
        quote_text="    return token.length === 64;",
        old_line_start=3, old_line_end=3, old_path="guard.ts",
        old_commit_sha="a" * 40, new_commit_sha="b" * 40,
    )
    assert ts_outcome.status == "healed"
    assert (ts_outcome.new_line_start, ts_outcome.new_line_end) == (6, 6)

    old_md = "# Spec\n\nAccounts lock after five failed logins.\n"
    new_md = "# Spec\n\n## Overview\n\nIntro paragraph.\n\nAccounts lock after five failed logins.\n"
    md_outcome = locate_and_validate_quote(
        old_content=old_md, new_content=new_md,
        quote_text="Accounts lock after five failed logins.",
        old_line_start=3, old_line_end=3, old_path="docs/spec.md",
        old_commit_sha="a" * 40, new_commit_sha="b" * 40,
    )
    assert md_outcome.status == "healed"
    assert (md_outcome.new_line_start, md_outcome.new_line_end) == (7, 7)


@pytest.mark.parametrize("mutated", [
    "TIMEOUT  = 30",      # 1 extra space (same tokens!)
    " TIMEOUT = 30",      # leading space
    "TIMEOUT = 30 ",      # trailing space
    "TIMEOUT = 31",       # 1 digit changed
    "TIMEOut = 30",       # 1 letter case changed
])
def test_validate_quote_checksum_rejects_even_one_character_change(mutated):
    original = "TIMEOUT = 30"
    ok, cand_sha = validate_quote_checksum(sha256_quote(original), mutated)
    assert ok is False
    assert cand_sha != sha256_quote(original)


def test_single_character_whitespace_or_formatting_diff_immediately_marks_stale_ref():
    old_code = "def cfg():\n    TIMEOUT = 30\n    return TIMEOUT\n"
    # Moved down AND 1 extra space added around '=' (tokens identical, SHA-256 differs by 1 char!)
    new_code = "# header\n\ndef cfg():\n    TIMEOUT  = 30\n    return TIMEOUT\n"
    outcome = locate_and_validate_quote(
        old_content=old_code,
        new_content=new_code,
        quote_text="    TIMEOUT = 30",
        old_line_start=2,
        old_line_end=2,
        old_path="cfg.py",
        old_commit_sha="1" * 40,
        new_commit_sha="2" * 40,
    )
    assert outcome.status == "stale_ref"
    assert outcome.ref_status == "stale_ref"
    assert outcome.stale_ref is True
    assert outcome.requires_intervention is True
    assert outcome.strategy == "token_match+sha256_mismatch"
    assert outcome.candidate_sha256 != outcome.quote_sha256


def test_deleted_file_and_completely_replaced_block_mark_stale_ref():
    old_code = "def auth():\n    return check_signature()\n"
    deleted = locate_and_validate_quote(
        old_content=old_code,
        new_content=None,
        quote_text="    return check_signature()",
        old_line_start=2,
        old_line_end=2,
        old_path="auth.py",
        new_path=None,
        old_commit_sha="1" * 40,
        new_commit_sha="2" * 40,
    )
    assert deleted.status == "stale_ref" and deleted.stale_ref is True and deleted.strategy == "file_deleted"

    replaced = locate_and_validate_quote(
        old_content=old_code,
        new_content="def totally_different():\n    pass\n",
        quote_text="    return check_signature()",
        old_line_start=2,
        old_line_end=2,
        old_path="auth.py",
        old_commit_sha="1" * 40,
        new_commit_sha="2" * 40,
    )
    assert replaced.status == "stale_ref" and replaced.stale_ref is True and replaced.requires_intervention is True


def test_edge_case_partial_line_modification_rejects_repair_as_validation_failed():
    old_code = (
        "def connect(host: str, port: int = 5432, timeout: int = 10):\n"
        "    return open_socket(host, port, timeout)\n"
    )
    # Only a fragment of the line changed (5432 -> 5433) AND the function shifted down.
    new_code = (
        "# refactored module\n"
        "\n"
        "def connect(host: str, port: int = 5433, timeout: int = 10):\n"
        "    return open_socket(host, port, timeout)\n"
    )
    outcome = locate_and_validate_quote(
        old_content=old_code,
        new_content=new_code,
        quote_text="def connect(host: str, port: int = 5432, timeout: int = 10):",
        old_line_start=1,
        old_line_end=1,
        old_path="db.py",
        old_commit_sha="1" * 40,
        new_commit_sha="2" * 40,
    )
    assert outcome.status == "validation_failed"
    assert outcome.ref_status == "stale_ref"
    assert outcome.stale_ref is True
    assert outcome.validation_status == "validation_failed"
    assert outcome.requires_intervention is True
    assert outcome.strategy == "partial_modification_rejected"
    assert outcome.candidate_sha256 != outcome.quote_sha256


def test_edge_case_multiple_identical_boilerplate_blocks_without_unique_context_raises_ambiguity():
    boilerplate = "if not payload:\n    return None"
    old_code = (
        "# header\n"
        "\n"
        "if not payload:\n"
        "    return None\n"
        "\n"
        "if not payload:\n"
        "    return None\n"
    )
    # Inserted lines at top; both boilerplate blocks share identical surrounding blank/module context.
    new_code = (
        "# header\n"
        "# extra comment\n"
        "\n"
        "if not payload:\n"
        "    return None\n"
        "\n"
        "if not payload:\n"
        "    return None\n"
    )
    outcome = locate_and_validate_quote(
        old_content=old_code,
        new_content=new_code,
        quote_text=boilerplate,
        old_line_start=3,
        old_line_end=4,
        old_path="handlers.py",
        old_commit_sha="1" * 40,
        new_commit_sha="2" * 40,
    )
    assert outcome.status == "ambiguous_context"
    assert outcome.ref_status == "ambiguous_context"
    assert outcome.requires_intervention is True
    assert outcome.new_line_start is None
    assert outcome.candidate_lines == ((4, 5), (7, 8))
    assert outcome.strategy == "ambiguity_conflict"


def test_multiple_identical_blocks_disambiguated_when_ast_scope_and_context_are_unique():
    boilerplate = "    if not token:\n        raise ValueError('unauthorized')"
    old_code = (
        "def login(token):\n"
        "    if not token:\n"
        "        raise ValueError('unauthorized')\n"
        "    return start_session(token)\n"
        "\n"
        "def logout(token):\n"
        "    if not token:\n"
        "        raise ValueError('unauthorized')\n"
        "    return end_session(token)\n"
    )
    # Reordered: logout moved above login, plus helper inserted.
    new_code = (
        "def audit(event):\n"
        "    return event\n"
        "\n"
        "def logout(token):\n"
        "    if not token:\n"
        "        raise ValueError('unauthorized')\n"
        "    return end_session(token)\n"
        "\n"
        "def login(token):\n"
        "    if not token:\n"
        "        raise ValueError('unauthorized')\n"
        "    return start_session(token)\n"
    )
    outcome = locate_and_validate_quote(
        old_content=old_code,
        new_content=new_code,
        quote_text=boilerplate,
        old_line_start=2,
        old_line_end=3,
        old_path="auth.py",
        old_commit_sha="1" * 40,
        new_commit_sha="2" * 40,
    )
    assert outcome.status == "healed"
    assert (outcome.new_line_start, outcome.new_line_end) == (10, 11)
    assert outcome.strategy == "git_diff+ast_context_disambiguated"


def test_tokenize_code_handles_unicode_multibyte_characters():
    src = '# Zażółć gęślą jaźń\ndef sprawdź():\n    return "ąęółżźćń"\n'
    tokens = tokenize_code("pl.py", src)
    assert any(tok.text == "sprawdź" and tok.line_start == 2 for tok in tokens)
    assert any("ąęółżźćń" in tok.text and tok.line_start == 3 for tok in tokens)


def _write_synthetic_snapshot(root: Path, project_id: str, rel_path: str, content: str, commit_sha: str, quote: str) -> Path:
    now = utc_now()
    abs_path = str((root / rel_path).resolve())
    src_id = str(uuid4())
    rec_id = str(uuid4())
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source = {
        "id": src_id, "project_id": project_id, "uri": abs_path, "kind": "code",
        "content": content, "sha256": digest,
        "metadata": {"path": abs_path, "root": str(root.resolve()), "relative_path": rel_path,
                     "git_commit": commit_sha, "content_hash": digest, "snapshot": "working_tree"},
        "is_current": True, "created_at": now,
    }
    record = {
        "id": rec_id, "project_id": project_id, "kind": "decision", "key": "auth-check",
        "content": "Require 64-char tokens.", "status": "proposed",
        "source_ids": [src_id], "version": 1, "supersedes": None,
        "metadata": {"evidence": quote}, "created_at": now, "updated_at": now,
    }
    rec_node = {"id": f"record:{rec_id}", "project_id": project_id, "kind": "decision",
                "label": record["content"], "source_id": src_id,
                "metadata": {**record["metadata"], "record_id": rec_id, "status": "proposed"}}
    src_node = {"id": f"source:{src_id}", "project_id": project_id, "kind": "source",
                "label": abs_path, "source_id": src_id,
                "metadata": {"source_id": src_id, "source_kind": "code"}}
    edge = {"id": hashlib.sha256(f"record:{rec_id}\0supported_by\0source:{src_id}".encode()).hexdigest(),
            "project_id": project_id, "from_id": f"record:{rec_id}", "to_id": f"source:{src_id}",
            "kind": "supported_by", "source_id": src_id, "status": "observed"}
    payload = {
        "sources": [source],
        "records": [record],
        "record_revisions": [{"id": f"{rec_id}:1", "project_id": project_id, "record_id": rec_id,
                              "version": 1, "snapshot": record, "recorded_at": now}],
        "plans": [], "plan_revisions": [],
        "nodes": [rec_node, src_node], "edges": [edge], "check_runs": [],
    }
    for table in portable.TABLES:
        payload[table].sort(key=lambda r: r["id"])
    objects = portable._descriptors(payload)
    folder = root / ".memory" / "cairnmind"
    for table, rows in payload.items():
        for desc, row in zip(objects[table], rows, strict=True):
            portable._write_whole(folder / desc["path"], portable._bytes(row))
    manifest = {
        "format": portable.FORMAT, "format_version": portable.VERSION,
        "project": {"id": project_id, "name": "demo"},
        "origin_root": str(root.resolve()), "root_aliases": [str(root.resolve())],
        "active_plan_id": None,
        "counts": {table: len(payload[table]) for table in portable.TABLES},
        "state_hash": portable._state_hash(payload),
        "processing": {}, "completeness": {"meaning": "committed_rows_only", "models_required": False},
        "objects": objects, "created_at": now, "parent_id": None, "ancestors": [],
        "content_hash": "0" * 64,
    }
    manifest["snapshot_id"] = portable._manifest_id(manifest)
    raw = portable._bytes(manifest)
    portable._atomic_write(folder / "manifests" / f"{manifest['snapshot_id']}.json", raw)
    manifest_path = folder / "manifest.json"
    portable._atomic_write(manifest_path, raw)
    return manifest_path


def test_edge_case_snapshot_package_healing_enforces_manifest_cas(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    old_code = "def check(token):\n    return len(token) == 64\n"
    (repo / "auth.py").write_text(old_code)
    _git(repo, "add", "auth.py")
    _git(repo, "commit", "-qm", "v1")
    c1 = _git(repo, "rev-parse", "HEAD")

    project_id = str(uuid4())
    quote = "    return len(token) == 64"
    manifest_path = _write_synthetic_snapshot(repo, project_id, "auth.py", old_code, c1, quote)
    initial_id = json.loads(manifest_path.read_text())["snapshot_id"]

    new_code = "# comment 1\n# comment 2\n# comment 3\n\ndef check(token):\n    return len(token) == 64\n"
    (repo / "auth.py").write_text(new_code)
    _git(repo, "commit", "-qam", "v2 shifted")
    c2 = _git(repo, "rev-parse", "HEAD")

    # Agent A heals with expected_snapshot_id = initial_id -> succeeds and bumps manifest & record version.
    res_a = heal_snapshot_package(manifest_path, repo, expected_snapshot_id=initial_id, actor="agent-a")
    assert res_a["status"] == "completed"
    assert res_a["snapshot_id"] != initial_id
    assert len(res_a["healed"]) == 1
    assert res_a["healed"][0]["new_lines"] == [6, 6]
    assert res_a["healed"][0]["new_commit_sha"] == c2
    assert res_a["healed"][0]["previous_version"] == 1 and res_a["healed"][0]["version"] == 2
    assert len(res_a["audit_entries"]) == 1
    # Rewritten snapshot remains a 100% valid portable package.
    inspected = portable.inspect_snapshot(manifest_path)
    assert inspected["status"] == "valid" and inspected["snapshot_id"] == res_a["snapshot_id"]

    # Agent B trying to heal the same stale snapshot ID is blocked by optimistic concurrency CAS.
    with pytest.raises(QuoteCASConflict, match="CAS conflict"):
        heal_snapshot_package(manifest_path, repo, expected_snapshot_id=initial_id, actor="agent-b")


def test_quote_healing_terminal_labels_have_polish_translations_and_non_green_styles():
    for (state, survives), text in QUOTE_HEALING_LABELS.items():
        assert tr(text, "en") == text and tr(text, "pl") != text
        parts = evidence_parts({"evidence_state": state, "quote_survives": survives}, tr)
        assert parts and text in joined(parts[0])
        style = evidence_style(state)
        assert style.color is None or style.color.name != TOKENS["ok"]
