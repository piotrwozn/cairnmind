"""End-to-end ArcadeDB persistence tests for `cairnmind pulse`."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from conftest import time_budget
from cairnmind import cli
from cairnmind.scip.ingest import index_project
from cairnmind.store import Store
from cairnmind.memory.verification import run_check

runner = CliRunner()
_LATENCY_SCALE = float(os.environ.get("CAIRNMIND_LATENCY_SCALE") or "1")


def _init_git_repo(root: Path) -> None:
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


def test_pulse_arcade_end_to_end_read_only_and_sub_500ms(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _init_git_repo(root)
    (root / ".cairnmind").mkdir()
    (root / ".cairnmind" / "checks.toml").write_text(
        '[checks.unit]\nargv = ["python3", "-c", "import sys; sys.exit(0)"]\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", ".cairnmind/checks.toml"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "add checks"], check=True, capture_output=True)

    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    index_project(store, project["id"])
    src = store.snapshot(project["id"], "manual://task", "Task spec", "manual")
    task = store.remember(
        project["id"],
        {
            "kind": "task",
            "content": "Keep verifier green",
            "status": "proposed",
            "metadata": {"state": "in_progress", "requires_test": True, "required_profiles": ["unit"]},
        },
        [src["id"]],
    )
    run = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert run["passed"] is True

    before_files = sorted(str(p.relative_to(store.data_dir)) for p in store.data_dir.rglob("*") if p.is_file())
    # Ensure pulse never instantiates full Store or starts service/engine.
    monkeypatch.setattr(Store, "__init__", lambda *a, **k: pytest.fail("pulse must stay strictly read-only"))
    monkeypatch.setattr(cli, "ensure_service", lambda *a, **k: pytest.fail("pulse must not start service"))

    res1 = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "pulse", "demo", "--json"])
    assert res1.exit_code == 0, res1.output
    data1 = json.loads(res1.stdout)
    assert data1["status"] == "healthy"
    assert data1["would_block_done"] is False
    assert data1["latency_ms"] < int(time_budget(0.5) * 1000 * _LATENCY_SCALE)
    assert data1["backend"] == "arcadedb"

    # Edit tracked file -> pulse catches drift + stale test!
    (root / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
    res2 = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "pulse", "demo", "--json", "--strict"])
    assert res2.exit_code == 1, res2.output
    data2 = json.loads(res2.stdout)
    assert data2["status"] == "drift"
    assert "app.py" in data2["drift_files"]
    assert task["id"] in data2["stale_test_tasks"]
    assert data2["would_block_done"] is True
    assert any("cairnmind check run" in a for a in data2["recommended_actions"])

    # Verify read-only: no lock or temporary files were created in data_dir.
    after_files = sorted(str(p.relative_to(store.data_dir)) for p in store.data_dir.rglob("*") if p.is_file())
    assert after_files == before_files
