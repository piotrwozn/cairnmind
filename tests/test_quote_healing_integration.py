"""End-to-end ArcadeDB + Git + HTTP service + CLI tests for self-healing quotes."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.confirmation import confirmed_by
from cairnmind.evidence import evidence_states, orphans_report
from cairnmind.ingest import index_project
from cairnmind.inspector import Inspector
from cairnmind.memory import MemoryManager
from cairnmind.quote_healing import (
    QuoteCASConflict,
    heal_project_quotes,
    healing_audit_log,
    is_healing_audit_source,
    sha256_quote,
)
from cairnmind.service import create_app
from cairnmind.store import Store

from conftest import person_proven


class NoInference:
    async def models(self):
        pytest.fail("Self-healing quotes must not call any LLM model")

    async def chat(self, *args, **kwargs):
        pytest.fail("Self-healing quotes must not call any LLM model")

    async def embed(self, *args, **kwargs):
        pytest.fail("Self-healing quotes must not call any LLM model")


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _create_repo(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Cairnmind Dev")
    _git(root, "config", "user.email", "dev@example.invalid")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial commit")
    return root, _git(root, "rev-parse", "HEAD")


def _commit_files(root: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


AUTH_V1 = (
    "import hashlib\n"
    "\n"
    "MAX_ATTEMPTS = 5\n"
    "\n"
    "def verify_session(token: str) -> bool:\n"
    "    if not token:\n"
    "        return False\n"
    "    return len(token) == 64\n"
)

QUOTE_FUNC = (
    "def verify_session(token: str) -> bool:\n"
    "    if not token:\n"
    "        return False\n"
    "    return len(token) == 64"
)

AUTH_V2_REFACTORED = (
    '"""Session verification module."""\n'
    "import hashlib\n"
    "import hmac\n"
    "\n"
    "MAX_ATTEMPTS = 5\n"
    "\n"
    "\n"
    "def normalize(raw: str) -> str:\n"
    "    return raw.strip()\n"
    "\n"
    "\n"
    "def audit_login(user: str) -> None:\n"
    "    pass\n"
    "\n"
    "\n"
    "def verify_session(token: str) -> bool:\n"
    "    if not token:\n"
    "        return False\n"
    "    return len(token) == 64\n"
)


def _setup_origin_snapshot(tmp_path: Path, files: dict[str, str], quotes: list[tuple[str, str, str, str]]):
    """Create origin repo + store + records quoting files + portable snapshot."""
    origin_root, c1 = _create_repo(tmp_path / "origin-repo", files)
    origin_store = Store(tmp_path / "origin-db")
    project = origin_store.add_project("cairnmind-demo", origin_root)
    index_project(origin_store, project["id"])
    records = {}
    for key, rel_path, content_text, quote in quotes:
        src = origin_store.query(
            "SELECT FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
            {"project": project["id"], "uri": str((origin_root / rel_path).resolve())},
        )[0]
        rec = origin_store.remember(
            project["id"],
            {"kind": "decision", "key": key, "content": content_text, "metadata": {"evidence": quote}},
            [src["id"]],
        )
        records[key] = rec
    snap = MemoryManager(origin_store).snapshot(project["id"])
    return origin_store, origin_root, project, c1, records, snap


def test_acceptance_snapshot_import_relocates_moved_block_bumps_version_and_audits(tmp_path):
    """Acceptance criteria 1 & 3: moved block relocated on import, lines/commit/version updated, audit logged."""
    _, origin_root, project, c1, records, snap = _setup_origin_snapshot(
        tmp_path,
        {"auth.py": AUTH_V1},
        [("verify-fn", "auth.py", "Session tokens must be 64 characters.", QUOTE_FUNC)],
    )
    rec = records["verify-fn"]
    assert rec["version"] == 1

    # Clone to destination and refactor auth.py so verify_session moves from lines 5-8 to lines 16-19.
    dest_root = tmp_path / "dest-repo"
    subprocess.run(["git", "clone", "-q", str(origin_root), str(dest_root)], check=True, capture_output=True)
    _git(dest_root, "config", "user.name", "Dest Dev")
    _git(dest_root, "config", "user.email", "dest@example.invalid")
    c2 = _commit_files(dest_root, {"auth.py": AUTH_V2_REFACTORED}, "refactor auth.py")

    dest_store = Store(tmp_path / "dest-db")
    manager = MemoryManager(dest_store)

    # Dry run previews healing without mutating destination store.
    dry = manager.import_snapshot(snap["manifest_path"], dest_root, dry_run=True)
    assert dry["status"] == "dry_run"
    assert dry["quote_healing"]["status"] == "preview"
    assert len(dry["quote_healing"]["healed"]) == 1
    assert dry["quote_healing"]["healed"][0]["new_lines"] == [16, 19]
    assert dest_store.list_projects() == []

    # Real import executes Phases 1, 2, 3 atomically.
    imported = manager.import_snapshot(snap["manifest_path"], dest_root)
    assert imported["status"] == "imported"
    healing = imported["quote_healing"]
    assert healing["status"] == "completed"
    assert len(healing["healed"]) == 1
    entry = healing["healed"][0]
    assert entry["record_id"] == rec["id"]
    assert entry["old_commit_sha"] == c1 and entry["new_commit_sha"] == c2
    assert entry["old_lines"] == [5, 8] and entry["new_lines"] == [16, 19]
    assert entry["quote_sha256"] == sha256_quote(QUOTE_FUNC)
    assert entry["previous_version"] == 1 and entry["version"] == 2

    # Record metadata updated and version bumped with immutable revision history [1, 2].
    stored = next(r for r in dest_store.list_records(project["id"]) if r["id"] == rec["id"])
    assert stored["version"] == 2
    assert [rev["version"] for rev in dest_store.record_history(rec["id"])] == [1, 2]
    qref = stored["metadata"]["quote_ref"]
    assert qref["status"] == "healed"
    assert qref["ref_status"] == "valid"
    assert qref["commit_sha"] == c2
    assert (qref["line_start"], qref["line_end"]) == (16, 19)
    assert qref["previous_commit_sha"] == c1
    assert qref["previous_lines"] == [5, 8]
    assert qref["quote_sha256"] == sha256_quote(QUOTE_FUNC)

    # Record stays anchored in evidence_states!
    ev = evidence_states(dest_store, project["id"], [stored])[rec["id"]]
    assert ev["evidence_state"] == "anchored"

    # Transparent audit log entry exists and is never chunked into search index.
    audits = healing_audit_log(dest_store, project["id"], record_id=rec["id"])
    assert len(audits) == 1
    assert audits[0]["audit_source_id"] == qref["audit_source_id"]
    assert audits[0]["outcome"] == "healed"
    assert audits[0]["old_lines"] == [5, 8] and audits[0]["new_lines"] == [16, 19]
    assert audits[0]["old_commit_sha"] == c1 and audits[0]["new_commit_sha"] == c2
    audit_source = dest_store.get_source(qref["audit_source_id"])
    assert is_healing_audit_source(audit_source)
    assert dest_store.query("SELECT id FROM chunks WHERE source_id=:sid", {"sid": audit_source["id"]}) == []

    # Re-importing the exact same snapshot is a no-op.
    repeat = manager.import_snapshot(snap["manifest_path"], dest_root)
    assert repeat["status"] == "unchanged"


def test_acceptance_single_char_change_marks_stale_ref_and_partial_line_edit_marks_validation_failed(tmp_path):
    """Acceptance criterion 2 & Edge Case 1: 1-char diff -> stale_ref; partial line edit -> validation_failed."""
    files = {
        "formatting.py": "def limits():\n    TIMEOUT = 30\n    return TIMEOUT\n",
        "partial.py": "def connect(host: str, port: int = 5432):\n    return host, port\n",
    }
    _, origin_root, project, _, records, snap = _setup_origin_snapshot(
        tmp_path,
        files,
        [
            ("fmt-quote", "formatting.py", "Default timeout is 30s.", "    TIMEOUT = 30"),
            ("partial-quote", "partial.py", "Default DB port is 5432.", "def connect(host: str, port: int = 5432):"),
        ],
    )

    dest_root = tmp_path / "dest-repo"
    subprocess.run(["git", "clone", "-q", str(origin_root), str(dest_root)], check=True, capture_output=True)
    _git(dest_root, "config", "user.name", "Dest Dev")
    _git(dest_root, "config", "user.email", "dest@example.invalid")
    _commit_files(
        dest_root,
        {
            # 1 extra space inside the quoted line (1 character difference!) -> stale_ref
            "formatting.py": "# moved\n\ndef limits():\n    TIMEOUT  = 30\n    return TIMEOUT\n",
            # Partial modification of line fragment (5432 -> 5433) -> validation_failed + stale_ref
            "partial.py": "# moved\n\ndef connect(host: str, port: int = 5433):\n    return host, port\n",
        },
        "modify both files",
    )

    dest_store = Store(tmp_path / "dest-db")
    imported = MemoryManager(dest_store).import_snapshot(snap["manifest_path"], dest_root)
    healing = imported["quote_healing"]
    assert len(healing["stale_ref"]) == 1
    assert len(healing["validation_failed"]) == 1
    assert healing["healed"] == []

    all_records = {r["id"]: r for r in dest_store.list_records(project["id"])}
    fmt_rec = all_records[records["fmt-quote"]["id"]]
    partial_rec = all_records[records["partial-quote"]["id"]]

    # Original quote text is NEVER overwritten or guessed!
    assert fmt_rec["metadata"]["evidence"] == "    TIMEOUT = 30"
    assert partial_rec["metadata"]["evidence"] == "def connect(host: str, port: int = 5432):"

    assert fmt_rec["metadata"]["quote_ref"]["status"] == "stale_ref"
    assert fmt_rec["metadata"]["quote_ref"]["stale_ref"] is True
    assert partial_rec["metadata"]["quote_ref"]["status"] == "validation_failed"
    assert partial_rec["metadata"]["quote_ref"]["ref_status"] == "stale_ref"
    assert partial_rec["metadata"]["quote_ref"]["stale_ref"] is True
    assert partial_rec["metadata"]["quote_ref"]["requires_intervention"] is True

    # Derived evidence states reflect stale_ref & validation_failed clearly.
    states = evidence_states(dest_store, project["id"], [fmt_rec, partial_rec])
    assert states[fmt_rec["id"]]["evidence_state"] == "stale_ref"
    assert states[fmt_rec["id"]]["quote_survives"] is False
    assert states[partial_rec["id"]]["evidence_state"] == "validation_failed"
    assert states[partial_rec["id"]]["ref_status"] == "stale_ref"
    assert states[partial_rec["id"]]["requires_intervention"] is True

    # Orphans report surfaces both clearly.
    report = orphans_report(dest_store, project, dest_root, [fmt_rec, partial_rec])
    assert "Stale quote references" in report["text"]
    assert "Quote validation failed" in report["text"]

    # Both failures are transparently recorded in the audit log.
    audits = healing_audit_log(dest_store, project["id"])
    assert {a["outcome"] for a in audits} == {"stale_ref", "validation_failed"}


def test_edge_case_duplicate_boilerplate_reports_ambiguity_conflict_on_import(tmp_path):
    """Edge Case 2: duplicate identical blocks without unique context report ambiguous_context."""
    boiler_v1 = (
        "# handlers\n"
        "\n"
        "if not req:\n"
        "    return None\n"
    )
    boiler_v2 = (
        "# handlers\n"
        "# added comment\n"
        "\n"
        "if not req:\n"
        "    return None\n"
        "\n"
        "if not req:\n"
        "    return None\n"
    )
    _, origin_root, project, _, records, snap = _setup_origin_snapshot(
        tmp_path,
        {"handlers.py": boiler_v1},
        [("boiler", "handlers.py", "Empty requests return None.", "if not req:\n    return None")],
    )
    dest_root = tmp_path / "dest-repo"
    subprocess.run(["git", "clone", "-q", str(origin_root), str(dest_root)], check=True, capture_output=True)
    _git(dest_root, "config", "user.name", "Dest Dev")
    _git(dest_root, "config", "user.email", "dest@example.invalid")
    _commit_files(dest_root, {"handlers.py": boiler_v2}, "duplicate boilerplate")

    dest_store = Store(tmp_path / "dest-db")
    imported = MemoryManager(dest_store).import_snapshot(snap["manifest_path"], dest_root)
    healing = imported["quote_healing"]
    assert len(healing["ambiguous_context"]) == 1
    assert healing["healed"] == []
    amb = healing["ambiguous_context"][0]
    assert amb["candidate_lines"] == [[4, 5], [7, 8]]

    rec_id = records["boiler"]["id"]
    stored = next(r for r in dest_store.list_records(project["id"]) if r["id"] == rec_id)
    assert stored["metadata"]["quote_ref"]["status"] == "ambiguous_context"
    assert stored["metadata"]["quote_ref"]["requires_intervention"] is True
    assert evidence_states(dest_store, project["id"], [stored])[rec_id]["evidence_state"] == "ambiguous_context"

    audits = healing_audit_log(dest_store, project["id"], record_id=rec_id)
    assert len(audits) == 1 and audits[0]["outcome"] == "ambiguous_context"


def test_edge_case_concurrent_agents_blocked_by_optimistic_concurrency_cas(tmp_path, monkeypatch):
    """Edge Case 3: parallel modification of the same record/snapshot is blocked by CAS."""
    repo, _ = _create_repo(tmp_path / "repo", {"auth.py": AUTH_V1})
    store = Store(tmp_path / "db")
    project = store.add_project("team-cas", repo)
    index_project(store, project["id"])
    src = store.query("SELECT FROM sources WHERE project_id=:p AND is_current=true", {"p": project["id"]})[0]
    rec = store.remember(
        project["id"],
        {"kind": "decision", "key": "verify-fn", "content": "Tokens are 64 chars.", "metadata": {"evidence": QUOTE_FUNC}},
        [src["id"]],
    )
    snap = MemoryManager(store).snapshot(project["id"])

    # Refactor code in a new commit.
    _commit_files(repo, {"auth.py": AUTH_V2_REFACTORED}, "refactor auth")

    # 1. Explicit expected_versions CAS: Agent A and Agent B both observed version 1.
    res_a = heal_project_quotes(
        store, project["id"],
        expected_versions={rec["id"]: 1},
        expected_snapshot_id=snap["snapshot_id"],
        actor="agent-a",
    )
    assert res_a["status"] == "completed" and len(res_a["healed"]) == 1
    assert res_a["healed"][0]["version"] == 2

    # Agent B arrives with stale expected_version=1 -> blocked by QuoteCASConflict!
    with pytest.raises(QuoteCASConflict, match="CAS conflict"):
        heal_project_quotes(store, project["id"], expected_versions={rec["id"]: 1}, actor="agent-b")

    # 2. Mid-flight race simulation: another agent modifies the record AFTER Phase 1/2 read
    #    and BEFORE Phase 3 commits -> Phase 3 CAS aborts atomically with zero extra audit entries!
    _commit_files(repo, {"auth.py": "# one more line\n" + AUTH_V2_REFACTORED}, "shift again")
    before_audits = len(healing_audit_log(store, project["id"], record_id=rec["id"]))

    import cairnmind.quote_healing as qh
    real_plan = qh._plan_records_healing

    def plan_with_concurrent_writer(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        # Concurrent Agent B edits the record right between Phase 1/2 and Phase 3 commit!
        current = next(r for r in store.list_records(project["id"]) if r["id"] == rec["id"])
        store.remember(
            project["id"],
            {"id": rec["id"], "expected_version": current["version"], "content": "Tokens are 64 chars (updated by Agent B)."},
            current["source_ids"],
        )
        return plan

    monkeypatch.setattr(qh, "_plan_records_healing", plan_with_concurrent_writer)
    with pytest.raises(QuoteCASConflict, match="CAS conflict"):
        heal_project_quotes(store, project["id"], actor="agent-a-racing")

    # Agent B's edit survived intact, and Agent A's failed transaction rolled back its audit entry!
    stored = next(r for r in store.list_records(project["id"]) if r["id"] == rec["id"])
    assert stored["version"] == 3
    assert "Agent B" in stored["content"]
    assert len(healing_audit_log(store, project["id"], record_id=rec["id"])) == before_audits


def test_healing_preserves_local_person_confirmation(tmp_path):
    """Updating line numbers/commit of a SHA-256-verified quote must not lapse a person's confirmation."""
    repo, _ = _create_repo(tmp_path / "repo", {"auth.py": AUTH_V1})
    store = Store(tmp_path / "db")
    project = store.add_project("confirm-keep", repo)
    index_project(store, project["id"])
    src = store.query("SELECT FROM sources WHERE project_id=:p AND is_current=true", {"p": project["id"]})[0]
    rec = store.remember(
        project["id"],
        {"kind": "decision", "key": "verify-fn", "content": "Tokens are 64 chars.", "metadata": {"evidence": QUOTE_FUNC}},
        [src["id"]],
    )
    with person_proven():
        reviewed = Inspector(store).review(project["id"], rec["id"], "confirm", rec["version"])["record"]
    assert reviewed["status"] == "confirmed" and confirmed_by(reviewed) == "person"

    c2 = _commit_files(repo, {"auth.py": AUTH_V2_REFACTORED}, "shift verified block")
    res = heal_project_quotes(store, project["id"])
    assert res["status"] == "completed" and len(res["healed"]) == 1

    healed_rec = next(r for r in store.list_records(project["id"]) if r["id"] == rec["id"])
    assert healed_rec["status"] == "confirmed"
    assert confirmed_by(healed_rec) == "person"
    assert healed_rec["version"] == reviewed["version"] + 1
    assert healed_rec["metadata"]["quote_ref"]["commit_sha"] == c2
    assert (healed_rec["metadata"]["quote_ref"]["line_start"], healed_rec["metadata"]["quote_ref"]["line_end"]) == (16, 19)


def test_service_routes_and_cli_heal_and_audit_log_are_transparent_and_model_free(tmp_path, monkeypatch):
    """Service endpoints /memory/heal, /memory/heal-audit and /memory/import enforce CAS (409) without models."""
    repo, _ = _create_repo(tmp_path / "repo", {"auth.py": AUTH_V1})
    store = Store(tmp_path / "db")
    project = store.add_project("api-heal", repo)
    index_project(store, project["id"])
    src = store.query("SELECT FROM sources WHERE project_id=:p AND is_current=true", {"p": project["id"]})[0]
    rec = store.remember(
        project["id"],
        {"kind": "decision", "key": "verify-fn", "content": "Tokens are 64 chars.", "metadata": {"evidence": QUOTE_FUNC}},
        [src["id"]],
    )
    c2 = _commit_files(repo, {"auth.py": AUTH_V2_REFACTORED}, "refactor auth")
    headers = {"Authorization": f"Bearer {store.settings.token}"}

    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        # Dry run previews without mutating.
        dry = client.post(
            "/memory/heal", headers=headers,
            json={"project": "api-heal", "dry_run": True},
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["status"] == "preview"
        assert dry.json()["healed"][0]["new_lines"] == [16, 19]

        # Stale CAS version returns HTTP 409 Conflict!
        conflict = client.post(
            "/memory/heal", headers=headers,
            json={"project": "api-heal", "expected_versions": {rec["id"]: 99}},
        )
        assert conflict.status_code == 409, conflict.text

        # Real heal succeeds with exact CAS version.
        healed = client.post(
            "/memory/heal", headers=headers,
            json={"project": "api-heal", "expected_versions": {rec["id"]: 1}},
        )
        assert healed.status_code == 200, healed.text
        body = healed.json()
        assert body["status"] == "completed"
        assert body["healed"][0]["new_commit_sha"] == c2
        assert body["healed"][0]["new_lines"] == [16, 19]

        # Audit log endpoint returns the recorded entry.
        audit_resp = client.get(
            "/memory/heal-audit", headers=headers,
            params={"project": "api-heal", "record_id": rec["id"]},
        )
        assert audit_resp.status_code == 200, audit_resp.text
        audit_body = audit_resp.json()
        assert audit_body["total"] == 1
        assert audit_body["items"][0]["outcome"] == "healed"
        assert audit_body["items"][0]["new_commit_sha"] == c2

    # CLI help commands work cleanly without initializing anything.
    runner = CliRunner()
    for args in (["memory", "heal", "--help"], ["memory", "heal-audit", "--help"]):
        res = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "unused"), *args])
        assert res.exit_code == 0, res.output
    assert not (tmp_path / "unused").exists()
    # Verify JSON output of dry-run helper works cleanly when routed via CLI.
    monkeypatch.setattr(cli, "request", lambda *a, **k: {"status": "preview", "healed": []})
    cli_res = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "heal", "api-heal", "--dry-run"])
    assert cli_res.exit_code == 0 and json.loads(cli_res.output)["status"] == "preview"
