"""Tests for `cairnmind pulse`: fast read-only memory consistency micro-audit."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from conftest import time_budget
from cairnmind import cli, pulse
from cairnmind.config import Settings

runner = CliRunner()
_LATENCY_SCALE = float(os.environ.get("CAIRNMIND_LATENCY_SCALE") or "1")


def _init_git_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    for args in (
        ("init",),
        ("config", "user.email", "pulse@example.invalid"),
        ("config", "user.name", "Pulse"),
        ("add", "."),
        ("commit", "-qm", "initial"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _FakeStore:
    """Minimal read-only in-memory store stub for fast unit tests without ArcadeDB."""

    def __init__(self, data_dir: Path, project: dict, roots: list[Path], *, records=None, sources=None,
                 plans=None, check_runs=None, packets=None):
        self.data_dir = data_dir
        self.read_only = False
        self.settings = Settings.load(data_dir, create=False)
        self._project = project
        self._roots = [str(r.resolve()) for r in roots]
        self._records = list(records or [])
        self._sources = list(sources or [])
        self._plans = list(plans or [])
        self._check_runs = list(check_runs or [])
        self._packets = list(packets or [])

    def resolve_project(self, ref: str) -> dict:
        if ref in (self._project["id"], self._project["name"], self._project["root"]):
            return self._project
        raise ValueError(f"Unknown project: {ref}")

    def list_records(self, project_id: str) -> list[dict]:
        return [r for r in self._records if r.get("project_id") == project_id]

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        if "FROM project_roots" in sql:
            return [{"project_id": self._project["id"], "path": p} for p in self._roots]
        if "FROM memory_bindings" in sql or "FROM memory_imports" in sql:
            return []
        if "FROM worktree_states" in sql:
            wt = params.get("worktree")
            return [{"active_plan_id": self._plans[0]["id"]}] if self._plans and wt in self._roots else []
        if "FROM plans" in sql:
            return list(self._plans)
        if "FROM check_runs" in sql:
            task_id = params.get("task")
            tasks = params.get("tasks")
            wt = params.get("worktree")
            rows = list(self._check_runs)
            if task_id is not None:
                rows = [r for r in rows if r.get("task_id") == task_id]
            if tasks is not None:
                rows = [r for r in rows if r.get("task_id") in tasks]
            if wt is not None:
                rows = [r for r in rows if r.get("worktree") == wt]
            return rows
        if "FROM resume_packets" in sql:
            wt = params.get("worktree")
            return [p for p in self._packets if wt is None or p.get("worktree") == wt]
        if "FROM sources" in sql:
            ids = params.get("ids")
            if ids is not None:
                return [s for s in self._sources if s.get("id") in ids]
            if "is_current=true" in sql:
                return [s for s in self._sources if s.get("is_current")]
            return list(self._sources)
        return []


def test_pulse_broken_without_config_creates_no_files(tmp_path):
    missing = tmp_path / "no-such-dir" / "memory"
    res = runner.invoke(cli.app, ["--data-dir", str(missing), "pulse", "--json"])
    assert res.exit_code == 2
    data = json.loads(res.stdout)
    assert data["status"] == "broken"
    assert data["would_block_done"] is True
    assert data["integrity_errors"]
    assert not missing.parent.exists()


def test_pulse_broken_on_migration_lock(tmp_path):
    data_dir = tmp_path / "memory"
    data_dir.mkdir()
    lock = data_dir / "migration.lock"
    lock.write_text("locked\n", encoding="utf-8")
    res = runner.invoke(cli.app, ["--data-dir", str(data_dir), "pulse", "--json"])
    assert res.exit_code == 2
    report = json.loads(res.stdout)
    assert report["status"] == "broken"
    assert any("migration.lock" in ref for ref in report["broken_refs"])


def test_pulse_exit_codes_and_rendering_no_color(monkeypatch):
    healthy = {"status": "healthy", "latency_ms": 12, "drift_files": [], "stale_test_tasks": [],
               "degraded_entities": [], "broken_refs": [], "git_available": True}
    drift = {**healthy, "status": "drift", "drift_files": ["src/app.py"],
             "recommended_actions": ["cairnmind index demo"]}
    broken = {**healthy, "status": "broken", "broken_refs": ["snapshot:missing object objects/abc.json"]}

    assert pulse.exit_code_for_pulse(healthy, strict=False) == 0
    assert pulse.exit_code_for_pulse(healthy, strict=True) == 0
    assert pulse.exit_code_for_pulse(drift, strict=False) == 0
    assert pulse.exit_code_for_pulse(drift, strict=True) == 1
    assert pulse.exit_code_for_pulse(broken, strict=False) == 2
    assert pulse.exit_code_for_pulse(broken, strict=True) == 2

    monkeypatch.setenv("NO_COLOR", "1")
    assert pulse._use_color(tty=True) is False
    rendered_plain = pulse.render_pulse(drift, color=False)
    assert "\x1b[" not in rendered_plain
    assert rendered_plain.startswith("PULSE: DRIFT")
    assert "src/app.py" in rendered_plain

    rendered_color = pulse.render_pulse(healthy, color=True)
    assert "\x1b[32mPULSE: HEALTHY\x1b[0m" in rendered_color


def test_pulse_ignores_managed_mcp_config_files_in_drift(tmp_path):
    root = tmp_path / "repo"
    _init_git_repo(root)
    for rel in (
        ".mcp.json",
        ".cursor/mcp.json",
        ".vscode/mcp.json",
        ".roo/mcp.json",
        ".codex/config.toml",
        ".gemini/settings.json",
        ".zed/settings.json",
        ".mcp.json.cairnmind.bak",
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")

    data_dir = tmp_path / "memory"
    project = {"id": "p1", "name": "demo", "root": str(root.resolve())}
    store = _FakeStore(data_dir, project, [root])
    report = pulse.run_pulse(store.settings, "demo", store=store)
    assert report["status"] == "healthy", report
    assert report["drift_files"] == []


def test_pulse_no_git_and_fresh_git_init_fallback(tmp_path):
    # Case 1: plain directory without Git.
    plain = tmp_path / "nogit"
    plain.mkdir()
    app = plain / "main.py"
    app.write_text("x = 1\n", encoding="utf-8")
    old_sha = hashlib.sha256(app.read_bytes()).hexdigest()

    data_dir = tmp_path / "memory"
    project = {"id": "p1", "name": "nogit", "root": str(plain.resolve())}
    src = {
        "id": "src-1",
        "project_id": "p1",
        "uri": str(app.resolve()),
        "kind": "code",
        "sha256": old_sha,
        "is_current": True,
        "metadata": {"path": str(app.resolve()), "root": str(plain.resolve())},
    }
    store = _FakeStore(data_dir, project, [plain], sources=[src])

    healthy = pulse.run_pulse(store.settings, "nogit", store=store)
    assert healthy["git_available"] is False
    assert healthy["status"] == "healthy"
    assert healthy["drift_files"] == []

    # Modify file -> SHA-256 fallback detects drift.
    app.write_text("x = 2\n", encoding="utf-8")
    drifted = pulse.run_pulse(store.settings, "nogit", store=store)
    assert drifted["git_available"] is False
    assert drifted["status"] == "drift"
    assert drifted["drift_files"] == ["main.py"]

    # Case 2: fresh `git init` with no commits yet -> also falls back cleanly.
    subprocess.run(["git", "-C", str(plain), "init"], check=True, capture_output=True)
    fresh_git = pulse.run_pulse(store.settings, "nogit", store=store)
    assert fresh_git["git_available"] is False
    assert fresh_git["drift_files"] == ["main.py"]


def test_pulse_branch_or_tree_diverged(tmp_path):
    root = tmp_path / "repo"
    head1 = _init_git_repo(root)
    subprocess.run(["git", "-C", str(root), "checkout", "-qb", "feature"], check=True, capture_output=True)
    (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "second"], check=True, capture_output=True)
    head2 = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    data_dir = tmp_path / "memory"
    project = {"id": "p1", "name": "demo", "root": str(root.resolve())}
    packet = {
        "created_at": "2026-11-27T09:00:00Z",
        "worktree": str(root.resolve()),
        "payload": {"code": {"head": head1, "branch": "master"}},
    }
    store = _FakeStore(data_dir, project, [root], packets=[packet])
    report = pulse.run_pulse(store.settings, "demo", store=store)
    assert report["status"] == "drift"
    assert report["branch_or_tree_diverged"] is True
    assert report["divergence"]["previous_head"] == head1
    assert report["divergence"]["current_head"] == head2
    assert report["divergence"]["current_branch"] == "feature"


def test_pulse_detects_degraded_entities_and_no_test_profile_warning(tmp_path):
    root = tmp_path / "repo"
    _init_git_repo(root)
    data_dir = tmp_path / "memory"
    project = {"id": "p1", "name": "demo", "root": str(root.resolve())}
    src = {
        "id": "src-ext",
        "project_id": "p1",
        "uri": "external://1",
        "kind": "manual",
        "sha256": "a" * 64,
        "is_current": True,
        "metadata": {"origin_client": "external", "origin_evidence": "degraded"},
    }
    degraded_fact = {
        "id": "fact-09",
        "project_id": "p1",
        "kind": "fact",
        "status": "proposed",
        "version": 1,
        "content": "External fact",
        "source_ids": ["src-ext"],
        "metadata": {"origin_client": "external", "origin_evidence": "degraded"},
    }
    active_task = {
        "id": "task-1",
        "project_id": "p1",
        "kind": "task",
        "status": "proposed",
        "version": 1,
        "content": "Work on feature",
        "source_ids": ["src-ext"],
        "metadata": {"state": "in_progress", "requires_test": False},
    }
    store = _FakeStore(data_dir, project, [root], records=[degraded_fact, active_task], sources=[src])
    report = pulse.run_pulse(store.settings, "demo", store=store)
    assert report["status"] == "drift"
    assert "fact-09" in report["degraded_entities"]
    assert "unverified_no_test_profile" in report["warnings"]
    assert report["would_block_done"] is False


def test_pulse_detects_broken_snapshot_refs_and_missing_db_source(tmp_path):
    root = tmp_path / "repo"
    _init_git_repo(root)
    snap_dir = root / ".memory" / "cairnmind"
    snap_dir.mkdir(parents=True)
    (snap_dir / "manifest.json").write_text(
        json.dumps({
            "format": "cairnmind-portable-memory",
            "version": 1,
            "snapshot_id": "snap-1",
            "git_head": "deadbeef" * 5,
            "objects": {"records": [{"id": "r1", "sha256": "0" * 64, "path": "objects/missing.json", "bytes": 10}]},
        }),
        encoding="utf-8",
    )

    data_dir = tmp_path / "memory"
    project = {"id": "p1", "name": "demo", "root": str(root.resolve())}
    orphan_rec = {
        "id": "rec-broken",
        "project_id": "p1",
        "kind": "fact",
        "status": "proposed",
        "version": 1,
        "content": "Points to deleted source",
        "source_ids": ["src-missing"],
        "metadata": {},
    }
    store = _FakeStore(data_dir, project, [root], records=[orphan_rec], sources=[])
    report = pulse.run_pulse(store.settings, "demo", store=store)
    assert report["status"] == "broken"
    assert pulse.exit_code_for_pulse(report) == 2
    assert any("missing source:src-missing" in ref for ref in report["broken_refs"])
    assert any("objects/missing.json" in ref for ref in report["broken_refs"])
    assert any("not found in Git history" in ref for ref in report["broken_refs"])


def test_pulse_large_repo_fast_path_skips_full_manifest_capture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    head = _init_git_repo(root)
    (root / ".cairnmind").mkdir()
    (root / ".cairnmind" / "checks.toml").write_text('[checks.unit]\nargv = ["true"]\n', encoding="utf-8")

    # Force fast path by lowering threshold below 1 tracked file.
    monkeypatch.setattr(pulse, "_SMALL_REPO_FILE_LIMIT", 0)
    monkeypatch.setattr(
        "cairnmind.verification.capture_code_state",
        lambda *a, **k: pytest.fail("Large-repo fast path must not double-hash entire repo"),
    )
    monkeypatch.setattr(
        "cairnmind.verification._entry",
        lambda *a, **k: pytest.fail("Large-repo fast path must not hash tracked files"),
    )

    data_dir = tmp_path / "memory"
    project = {"id": "p1", "name": "bigmono", "root": str(root.resolve())}
    src = {"id": "s1", "project_id": "p1", "uri": "manual://1", "kind": "manual", "sha256": "b" * 64,
           "is_current": True, "metadata": {}}
    task = {
        "id": "task-14",
        "project_id": "p1",
        "kind": "task",
        "status": "proposed",
        "version": 1,
        "content": "Verify big mono",
        "source_ids": ["s1"],
        "metadata": {"state": "in_progress", "requires_test": True, "required_profiles": ["unit"]},
    }
    from cairnmind.verification import load_profiles, task_definition_hash

    store = _FakeStore(data_dir, project, [root], records=[task], sources=[src])
    prof = load_profiles(store, "p1", root)["unit"]
    passing_run = {
        "id": "run-1",
        "project_id": "p1",
        "task_id": "task-14",
        "worktree": str(root.resolve()),
        "origin": "local_runner",
        "profile_id": "unit",
        "profile_hash": prof["hash"],
        "task_definition_hash": task_definition_hash(task),
        "passed": True,
        "outcome": "completed",
        "returncode": 0,
        "ended_at": "2026-11-27T10:00:00Z",
        "code_before": {"status": "fresh", "head": head, "fingerprint": "fp1"},
        "code_after": {"status": "fresh", "head": head, "fingerprint": "fp1"},
    }
    store._check_runs.append(passing_run)

    healthy = pulse.run_pulse(store.settings, "bigmono", store=store)
    assert healthy["status"] == "healthy"
    assert healthy["stale_test_tasks"] == []
    assert healthy["latency_ms"] < int(time_budget(0.5) * 1000 * _LATENCY_SCALE)

    # Edit file -> fast path catches drift & stale test immediately without scanning disk.
    (root / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
    drifted = pulse.run_pulse(store.settings, "bigmono", store=store)
    assert drifted["status"] == "drift"
    assert drifted["drift_files"] == ["app.py"]
    assert drifted["stale_test_tasks"] == ["task-14"]
    assert drifted["would_block_done"] is True
    assert any("cairnmind check run" in action for action in drifted["recommended_actions"])


def test_pulse_worktree_isolation(tmp_path):
    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    _init_git_repo(wt_a)
    subprocess.run(["git", "-C", str(wt_a), "worktree", "add", "-b", "wt-b", str(wt_b)], check=True, capture_output=True)
    # Dirty file ONLY in worktree B.
    (wt_b / "app.py").write_text("VALUE = 42\n", encoding="utf-8")

    data_dir = tmp_path / "memory"
    project = {"id": "p1", "name": "multi", "root": str(wt_a.resolve())}
    store = _FakeStore(data_dir, project, [wt_a, wt_b])

    rep_a = pulse.run_pulse(store.settings, "multi", worktree=wt_a, store=store)
    rep_b = pulse.run_pulse(store.settings, "multi", worktree=wt_b, store=store)
    assert rep_a["status"] == "healthy" and rep_a["drift_files"] == []
    assert rep_b["status"] == "drift" and rep_b["drift_files"] == ["app.py"]
