"""End-to-end `cairnmind add` / `project add` with real Git cloning into a real ArcadeDB Store."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from typer.testing import CliRunner

from cairnmind import cli, ingest, project_clone
from cairnmind.store import Store


def _create_bare_repo(tmp_path: Path, name: str, *, extra_files: dict[str, str] | None = None) -> Path:
    work = tmp_path / f"{name}-work"
    bare = tmp_path / f"{name}.git"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    (work / "service.py").write_text("def verify():\n    return True\n")
    for rel, content in (extra_files or {}).items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "initial commit"], check=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(bare)], check=True)
    return bare


def test_real_store_clones_registers_indexes_and_blocks_conflicts_before_clone(tmp_path, monkeypatch):
    bare = _create_bare_repo(tmp_path, "alpha")
    data = tmp_path / "memory"
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: False)
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    # Never start the service during project add
    monkeypatch.setattr(cli, "ensure_service", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no service")))

    real_parse = project_clone.parse_clone_source
    monkeypatch.setattr(project_clone, "parse_clone_source", lambda spec: (
        project_clone.CloneSource(
            raw=spec, kind="github_shorthand", host="github.com", owner="owner", repo="alpha",
            default_name="alpha", git_url=str(bare), gh_target="owner/alpha")
        if spec == "owner/alpha" else real_parse(spec)))

    runner = CliRunner()
    assert runner.invoke(cli.app, ["--data-dir", str(data), "init"]).exit_code == 0

    added = runner.invoke(cli.app, ["--data-dir", str(data), "add", "--clone", "owner/alpha", "--via", "git"])
    assert added.exit_code == 0, added.output
    payload = json.loads(added.stdout)
    expected_root = (cwd / "alpha").resolve()
    assert payload["project"]["name"] == "alpha"
    assert Path(payload["project"]["root"]) == expected_root
    assert payload["indexed"]["files"] == 1
    assert payload["cloned"] == {"source": "owner/alpha", "url": str(bare), "tool": "git", "destination": str(expected_root)}
    assert (expected_root / "service.py").is_file()

    # Preflight stops before cloning when the project name is already registered in ArcadeDB
    clone_invoked = []
    monkeypatch.setattr(project_clone, "clone_repository", lambda *a, **k: clone_invoked.append(1))
    duplicate = runner.invoke(cli.app, ["--data-dir", str(data), "add", "alpha", str(cwd / "other"),
                                        "--clone", "owner/alpha", "--via", "git"])
    assert duplicate.exit_code == 1 and "already registered" in str(duplicate.exception)
    assert clone_invoked == [] and not (cwd / "other").exists()


def test_real_store_stops_when_cloned_repo_contains_portable_snapshot_until_fresh(tmp_path, monkeypatch):
    bare = _create_bare_repo(tmp_path, "carried", extra_files={".memory/cairnmind/manifest.json": "{}"})
    data = tmp_path / "memory"
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: False)
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})

    real_parse = project_clone.parse_clone_source
    monkeypatch.setattr(project_clone, "parse_clone_source", lambda spec: (
        project_clone.CloneSource(
            raw=spec, kind="github_shorthand", host="github.com", owner="owner", repo="carried",
            default_name="carried", git_url=str(bare), gh_target="owner/carried")
        if spec == "owner/carried" else real_parse(spec)))

    runner = CliRunner()
    assert runner.invoke(cli.app, ["--data-dir", str(data), "init"]).exit_code == 0

    stopped = runner.invoke(cli.app, ["--data-dir", str(data), "project", "add", "--clone", "owner/carried", "--via", "git"])
    assert stopped.exit_code == 1, stopped.output
    body = json.loads(stopped.stdout)
    assert body["status"] == "snapshot_found" and body["registered"] is False
    assert (cwd / "carried" / ".memory" / "cairnmind" / "manifest.json").is_file()
    assert Store(data).list_projects() == []

    # With --fresh into another folder, registration succeeds
    fresh = runner.invoke(cli.app, ["--data-dir", str(data), "add", "carried-fresh", str(cwd / "fresh-copy"),
                                    "--clone", "owner/carried", "--via", "git", "--fresh"])
    assert fresh.exit_code == 0, fresh.output
    assert [row["name"] for row in Store(data).list_projects()] == ["carried-fresh"]
