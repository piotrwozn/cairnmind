from __future__ import annotations

import time

import pytest

from cairnmind.cockpit_state import CockpitState, ProjectDiscovery, normalize_path
from cairnmind.config import Settings


def test_preferences_are_local_private_and_validate_native_profiles(tmp_path):
    settings = Settings.load(tmp_path / "data")
    state = CockpitState(settings)
    assert len(state.load()["default_slots"]) == 2
    assert not settings.data_dir.exists()
    root = tmp_path / "repos"
    root.mkdir()
    saved = state.update({"scan_roots": [str(root)], "profiles": [
        {"id": "codex", "client": "codex", "label": "Code", "model": "gpt-6.1"},
        {"id": "claude", "client": "claude", "label": "Review"}],
        "project_slots": {"project": [{"id": "first", "profile_id": "codex"},
                                      {"id": "second", "profile_id": "codex"}]}})
    assert saved == state.load()
    assert state.path.stat().st_mode & 0o777 == 0o600
    assert saved["project_slots"]["project"][1]["id"] == "second"
    before = state.path.read_bytes()
    for changes in ({"api_key": "secret"}, {"default_slots": [{"id": "x", "profile_id": []}]},
                    {"profiles": [{"id": "codex", "client": "shell", "label": "Shell"}]},
                    {"profiles": [{"id": "codex", "client": "codex", "label": "Code", "model": "--skip-permissions"}]},
                    {"default_slots": []}):
        with pytest.raises(ValueError):
            state.update(changes)
        assert state.path.read_bytes() == before


def test_selected_roots_only_discovery_ignores_dependency_and_symlink_trees(tmp_path):
    root = tmp_path / "selected"
    root.mkdir()
    repo = root / "actual"
    (repo / ".git").mkdir(parents=True)
    (repo / ".memory/devbrain").mkdir(parents=True)
    (repo / ".memory/devbrain/manifest.json").write_text("{}")
    (root / "node_modules" / "not-project" / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    scanner = ProjectDiscovery()
    scanner.start([str(root)])
    deadline = time.monotonic() + 3
    while scanner.status()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    result = scanner.status()
    assert result["state"] == "completed"
    assert [row["root"] for row in result["projects"]] == [str(repo)]
    assert result["projects"][0]["memory_snapshot"] == str(repo / ".memory/devbrain")
    assert result["projects"] == result["candidates"]
    assert not (tmp_path / "data").exists()


def test_discovery_budget_reports_incomplete_scan(tmp_path):
    for i in range(4):
        (tmp_path / str(i) / ".git").mkdir(parents=True)
    scanner = ProjectDiscovery()
    scanner.start([str(tmp_path)], max_directories=1)
    deadline = time.monotonic() + 3
    while scanner.status()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    assert scanner.status()["truncated"] is True
    assert scanner.status()["state"] == "cancelled"


def test_explicit_windows_path_conversion_uses_no_shell(monkeypatch):
    def missing(*args, **kwargs):
        assert args[0] == ["wslpath", "-u", r"C:\Users\piotr\My Projects"]
        assert "shell" not in kwargs
        raise FileNotFoundError
    monkeypatch.setattr("cairnmind.cockpit_state.subprocess.run", missing)
    assert str(normalize_path(r"C:\Users\piotr\My Projects")) == "/mnt/c/Users/piotr/My Projects"


def test_preferences_refuse_symlink_or_shared_runtime_directory(tmp_path):
    settings = Settings.load(tmp_path / "data")
    settings.data_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    directory = settings.data_dir / "cockpit"
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        CockpitState(settings).update({"last_project": None})
    assert not list(outside.iterdir())
    directory.unlink()
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(ValueError):
        CockpitState(settings).update({"last_project": None})
