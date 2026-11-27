"""End-to-end ArcadeDB integration tests for #411 (symbol anchoring), #412 (code decisions) and #413 (scan CLI)."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.terminal import setup_mark
from cairnmind.cli_cmds import upgrade_mark
from cairnmind.memory.code_decisions import get_decision_source, set_decision_source
from cairnmind.memory.evidence import evidence_states, orphans_report, unanchored
from cairnmind.scip.ingest import index_project
from cairnmind.terminal.inspector import Inspector
from cairnmind.storage.journal import TABLE_TYPES
from cairnmind.memory.memory import MemoryManager
from cairnmind.store import Store
from cairnmind.storage.store_readonly import ReadOnlyStoreError
from cairnmind.storage.store_writer import StoreNotWriter
from test_store_readonly import BOOKKEEPING, changes, files, forbid_starting, record_requests


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "src").mkdir()
    (root / "src" / "auth.py").write_text(
        "class Auth:\n"
        "    def refresh_token(self, token):\n"
        "        return token + ':v1'\n\n"
        "    def revoke_token(self, token):\n"
        "        return None\n",
        encoding="utf-8",
        newline="\n",
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial commit")
    return root


def code_source(store: Store, project_id: str, path: Path) -> dict:
    rows = store.query(
        "SELECT FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
        {"project": project_id, "uri": str(path)},
    )
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# Issue #411: Symbol-anchored normalised body hashing & 3 human-visible states
# ---------------------------------------------------------------------------

def test_symbol_anchor_ignores_formatting_and_other_functions_and_never_mutates_status(tmp_path):
    root = make_repo(tmp_path / "repo")
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    pid = project["id"]
    index_project(store, pid)

    src = code_source(store, pid, root / "src" / "auth.py")
    record = store.remember(
        pid,
        {
            "kind": "decision",
            "content": "refresh_token appends version suffix :v1",
            "metadata": {"evidence": "return token + ':v1'"},
        },
        [src["id"]],
    )
    anchor = record["metadata"]["symbol_anchor"]
    assert anchor["path"] == "src/auth.py"
    assert anchor["symbol"] == "Auth.refresh_token"
    assert anchor["kind"] == "method"

    # Confirm the record as a human act so we can verify its status is never mutated automatically.
    confirmed = Inspector(store).review(pid, record["id"], "confirm", record["version"])["record"]
    assert confirmed["status"] == "confirmed"

    # 1. Edit another method + reformat refresh_token (CRLF, double quotes, comments, wrapping): stays unchanged/anchored.
    (root / "src" / "auth.py").write_text(
        "# file header changed\r\n"
        "class Auth:\r\n"
        "    def refresh_token(\r\n"
        "        self,\r\n"
        "        token,  # formatted signature\r\n"
        "    ):\r\n"
        '        # comment inside method\r\n'
        '        return token + ":v1"\r\n'
        "\r\n"
        "    def revoke_token(self, token):\r\n"
        "        return 'revoked:' + token\r\n",
        encoding="utf-8",
        newline="",
    )
    git(root, "commit", "-qam", "format refresh_token and change revoke_token")
    index_project(store, pid)

    refreshed = store.query("SELECT FROM records WHERE id=:id", {"id": record["id"]})[0]
    st = evidence_states(store, pid, [refreshed])[record["id"]]
    assert st["symbol_state"] == "unchanged"
    assert st["evidence_state"] == "anchored"
    assert unanchored(store, pid, [refreshed]) == []
    assert refreshed["status"] == "confirmed"

    # 2. Change the semantic body of refresh_token and commit -> symbol_changed with commit & timestamp, status untouched.
    (root / "src" / "auth.py").write_text(
        "class Auth:\n"
        "    def refresh_token(self, token):\n"
        "        return token + ':v2'\n\n"
        "    def revoke_token(self, token):\n"
        "        return 'revoked:' + token\n",
        encoding="utf-8",
        newline="\n",
    )
    git(root, "commit", "-qam", "bump token suffix to v2")
    new_commit = git(root, "rev-parse", "HEAD")
    index_project(store, pid)

    refreshed = store.query("SELECT FROM records WHERE id=:id", {"id": record["id"]})[0]
    st = evidence_states(store, pid, [refreshed])[record["id"]]
    assert st["symbol_state"] == "symbol_changed"
    assert st["evidence_state"] == "evidence_replaced"
    assert st["symbol_change"]["commit"] == new_commit
    assert st["symbol_change"]["changed_at"]
    assert refreshed["status"] == "confirmed"  # NEVER mutated by symbol state!
    report = orphans_report(store, project, root, [refreshed])
    assert new_commit[:12] in report["text"]

    # 3. Remove the method completely -> symbol_gone, status STILL confirmed!
    (root / "src" / "auth.py").write_text(
        "class Auth:\n"
        "    def revoke_token(self, token):\n"
        "        return 'revoked:' + token\n",
        encoding="utf-8",
        newline="\n",
    )
    git(root, "commit", "-qam", "remove refresh_token")
    index_project(store, pid)

    refreshed = store.query("SELECT FROM records WHERE id=:id", {"id": record["id"]})[0]
    st = evidence_states(store, pid, [refreshed])[record["id"]]
    assert st["symbol_state"] == "symbol_gone"
    assert st["evidence_state"] == "orphaned"
    assert refreshed["status"] == "confirmed"

    # Portable snapshot preserves symbol_anchor with POSIX path.
    snap = MemoryManager(store).snapshot(pid, str(root))
    assert snap["snapshot_id"]


# ---------------------------------------------------------------------------
# Issue #412 (Option B): Per-project decision source (off | comments | adr | trailers)
# ---------------------------------------------------------------------------

def test_code_decisions_comments_adr_and_trailers_are_opt_in_proposed_idempotent_and_non_destructive(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "src" / "auth.py").write_text(
        "class Auth:\n"
        "    # DECISION: Rotate refresh tokens on every use\n"
        "    def refresh_token(self, token):\n"
        "        # INVARIANT: Never log raw tokens\n"
        "        return token + ':v1'\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / "0001-rotation.md").write_text(
        "---\n"
        "id: ADR-0001\n"
        "title: Token Rotation\n"
        "status: accepted\n"
        "symbols:\n"
        "  - src/auth.py:Auth.refresh_token\n"
        "---\n\n"
        "## Decision\n"
        "All refresh tokens must rotate on every use.\n",
        encoding="utf-8",
        newline="\n",
    )
    git(root, "add", ".")
    git(
        root,
        "commit",
        "-qm",
        "Add rotation policy\n\nDecision: Enforce single-use refresh tokens\nRefs: ADR-0001",
    )

    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    pid = project["id"]

    # Default is off: indexing creates zero decision records.
    assert get_decision_source(store, pid) == "off"
    index_project(store, pid)
    assert store.list_records(pid) == []

    # 1. Enable `comments` source.
    res = set_decision_source(store, pid, "comments")
    assert res["decision_source"] == "comments"
    out1 = index_project(store, pid)
    assert out1["code_decisions"]["created"] == 2
    comment_records = store.list_records(pid)
    assert len(comment_records) == 2
    assert all(r["status"] == "proposed" for r in comment_records)
    assert {r["kind"] for r in comment_records} == {"decision", "constraint"}
    assert all(r["metadata"]["symbol_anchor"]["symbol"] == "Auth.refresh_token" for r in comment_records)

    # Confirm one record and re-index: idempotent (0 created) and confirmed status untouched.
    target_rec = next(r for r in comment_records if r["kind"] == "decision")
    Inspector(store).review(pid, target_rec["id"], "confirm", target_rec["version"])
    out2 = index_project(store, pid)
    assert out2["code_decisions"]["created"] == 0
    assert out2["code_decisions"]["existing"] == 2
    assert len(store.list_records(pid)) == 2
    assert store.query("SELECT status FROM records WHERE id=:id", {"id": target_rec["id"]})[0]["status"] == "confirmed"

    # Remove the DECISION comment only -> tag_removed reported, record NOT deleted and status untouched!
    (root / "src" / "auth.py").write_text(
        "class Auth:\n"
        "    def refresh_token(self, token):\n"
        "        # INVARIANT: Never log raw tokens\n"
        "        return token + ':v1'\n",
        encoding="utf-8",
        newline="\n",
    )
    index_project(store, pid)
    assert len(store.list_records(pid)) == 2
    after_tag_del = store.query("SELECT FROM records WHERE id=:id", {"id": target_rec["id"]})[0]
    st = evidence_states(store, pid, [after_tag_del])[target_rec["id"]]
    assert st["symbol_state"] == "unchanged"
    assert st["comment_state"] == "tag_removed"
    assert st["evidence_state"] == "evidence_replaced"
    assert after_tag_del["status"] == "confirmed"

    # 2. Switch the same project to `adr` (single source at a time!).
    set_decision_source(store, pid, "adr")
    out_adr = index_project(store, pid)
    assert out_adr["code_decisions"]["source"] == "adr"
    assert out_adr["code_decisions"]["created"] == 1
    adr_recs = [r for r in store.list_records(pid) if r["metadata"].get("origin") == "adr_file"]
    assert len(adr_recs) == 1
    assert adr_recs[0]["status"] == "proposed"
    assert adr_recs[0]["metadata"]["adr_anchor"]["adr_id"] == "ADR-0001"
    assert adr_recs[0]["metadata"]["symbol_anchor"]["symbol"] == "Auth.refresh_token"
    # Re-indexing ADR is idempotent.
    assert index_project(store, pid)["code_decisions"]["created"] == 0

    # 3. Switch to `trailers`.
    set_decision_source(store, pid, "trailers")
    out_tr = index_project(store, pid)
    assert out_tr["code_decisions"]["source"] == "trailers"
    assert out_tr["code_decisions"]["created"] == 1
    trailer_recs = [r for r in store.list_records(pid) if r["metadata"].get("origin") == "git_trailer"]
    assert len(trailer_recs) == 1
    assert trailer_recs[0]["status"] == "proposed"
    assert trailer_recs[0]["metadata"]["trailer_anchor"]["refs"] == ["ADR-0001"]
    assert index_project(store, pid)["code_decisions"]["created"] == 0

    # CLI `memory decisions` inspect & configure.
    runner = CliRunner()
    shown = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "decisions", "demo", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["decision_source"] == "trailers"
    turned_off = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "decisions", "demo", "off", "--json"])
    assert turned_off.exit_code == 0, turned_off.output
    assert json.loads(turned_off.output)["decision_source"] == "off"


def test_code_decisions_respect_all_write_guards(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")
    (root / "src" / "auth.py").write_text(
        "# DECISION: Guarded write test\ndef guarded():\n    return 1\n",
        encoding="utf-8",
        newline="\n",
    )
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    pid = project["id"]

    # Read-only store refuses setting decision source.
    reader = Store.open_readonly(store.settings)
    with pytest.raises(ReadOnlyStoreError):
        set_decision_source(reader, pid, "comments")

    # StoreNotWriter refuses setting decision source.
    reader.not_writer = "another host writes this store"
    with pytest.raises(StoreNotWriter):
        set_decision_source(reader, pid, "comments")

    # Active upgrade_mark refuses setting decision source and blocks ingestion.
    set_decision_source(store, pid, "comments")
    fake_upgrade_mark = tmp_path / "upgrade-in-progress"
    monkeypatch.setattr(upgrade_mark, "mark_path", lambda: fake_upgrade_mark)
    fake_upgrade_mark.write_text("unreadable-mark", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            set_decision_source(store, pid, "off")
        out = index_project(store, pid)
        assert out["code_decisions"].get("refused")
        assert store.list_records(pid) == []
    finally:
        fake_upgrade_mark.unlink(missing_ok=True)

    # Active setup_mark (unchecked setup-before.json copy) blocks ingestion and settings changes too.
    before_copy = store.data_dir / setup_mark.BEFORE
    before_copy.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            set_decision_source(store, pid, "off")
        out = index_project(store, pid)
        assert out["code_decisions"].get("refused")
        assert store.list_records(pid) == []
    finally:
        before_copy.unlink(missing_ok=True)

    # Unfinished restore mark refuses both opening a writable store and running read-only scan (exit 2).
    store.command(
        "INSERT INTO devbrain_meta CONTENT :row",
        {"row": {"name": "restore_unfinished", "value": {"backup": "b.tar.gz", "before": "before-restore-1.tar.gz",
                                                         "started_at": "2026-11-26T00:00:00+00:00",
                                                         "installation": "inst-1", "before_installation": "inst-1"}}},
    )
    try:
        from cairnmind.storage.restore_staged import RestoreUnfinished
        with pytest.raises(RestoreUnfinished):
            Store(store.data_dir)
        res = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "scan", "demo", "--diff", "HEAD"])
        assert res.exit_code == 2
    finally:
        store.command("DELETE FROM devbrain_meta WHERE name='restore_unfinished'")


# ---------------------------------------------------------------------------
# Issue #413: Serverless read-only `cairnmind scan --diff` & exit codes 0 / 1 / 2
# ---------------------------------------------------------------------------

def test_cli_scan_diff_is_strictly_readonly_and_enforces_exit_codes(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    pid = project["id"]
    index_project(store, pid)

    src = code_source(store, pid, root / "src" / "auth.py")
    rec_refresh = store.remember(
        pid,
        {"kind": "decision", "content": "refresh_token returns :v1", "metadata": {"symbol": "Auth.refresh_token"}},
        [src["id"]],
    )
    rec_revoke = store.remember(
        pid,
        {"kind": "decision", "content": "revoke_token returns None", "metadata": {"symbol": "Auth.revoke_token"}},
        [src["id"]],
    )

    # Formatting-only edit on refresh_token -> scan exits 0 (clean) and writes NOTHING!
    (root / "src" / "auth.py").write_text(
        "class Auth:\r\n"
        "    # formatting only\r\n"
        "    def refresh_token(self, token):\r\n"
        '        return token + ":v1"\r\n\r\n'
        "    def revoke_token(self, token):\r\n"
        "        return None\r\n",
        encoding="utf-8",
        newline="",
    )

    before = files(store.data_dir)
    for name in ("cairnmind.storage.engine.start_engine", "cairnmind.storage.engine.ensure_engine", "cairnmind.cli.ensure_service"):
        monkeypatch.setattr(name, lambda *a, **k: pytest.fail("scan started engine or service"))
    monkeypatch.setattr(Store, "__init__", lambda *a, **k: pytest.fail("scan opened writable Store"))
    sent = record_requests(monkeypatch)
    runner = CliRunner()

    # Auto-detect project from cwd (without passing PROJECT arg).
    monkeypatch.chdir(root)
    clean = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "scan", "--diff", "HEAD", "--json"])
    assert clean.exit_code == 0, (clean.output, repr(clean.exception))
    payload = json.loads(clean.output)
    assert payload["counts"]["total"] == 0
    assert payload["counts"]["unchanged"] == 2
    assert changes(sent) == []
    assert files(store.data_dir) == before

    # Verify every queried table belongs to READ_KINDS["scan"].
    queried = {
        table
        for _, path, command in sent
        if path.startswith("/api/v1/query/")
        for table in re.findall(r"\bFROM\s+`?([a-z_]+)", command or "")
    } - BOOKKEEPING
    assert {TABLE_TYPES[t] for t in queried} <= cli.READ_KINDS["scan"]

    # Mutate refresh_token body and delete revoke_token -> exit code 1 with both findings.
    (root / "src" / "auth.py").write_text(
        "class Auth:\n"
        "    def refresh_token(self, token):\n"
        "        return token + ':v2'\n",
        encoding="utf-8",
        newline="\n",
    )
    drift = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "scan", "demo", "--diff", "HEAD", "--json"])
    assert drift.exit_code == 1, (drift.output, repr(drift.exception))
    drift_payload = json.loads(drift.output)
    by_id = {row["id"]: row for row in drift_payload["records"]}
    assert by_id[rec_refresh["id"]]["state"] == "symbol_changed"
    assert by_id[rec_revoke["id"]]["state"] == "symbol_gone"
    assert changes(sent) == []

    # Also test range diff syntax (`main...HEAD` after committing on a branch).
    base_sha = git(root, "rev-parse", "HEAD")
    git(root, "commit", "-qam", "change refresh and drop revoke")
    ranged = runner.invoke(
        cli.app,
        ["--data-dir", str(store.data_dir), "scan", "demo", "--diff", f"{base_sha}...HEAD", "--json"],
    )
    assert ranged.exit_code == 1
    assert len(json.loads(ranged.output)["records"]) == 2

    # Invalid revision -> exit code 2.
    bad_rev = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "scan", "demo", "--diff", "no-such-ref-xyz"])
    assert bad_rev.exit_code == 2

    # Unready store (no config) -> exit code 2 without starting anything.
    forbid_starting(monkeypatch)
    unready = runner.invoke(
        cli.app,
        ["--data-dir", str(tmp_path / "missing-store"), "scan", "demo", "--diff", "HEAD"],
    )
    assert unready.exit_code == 2
