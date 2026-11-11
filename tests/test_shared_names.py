"""The names a project shares with other versions and machines, after the rename (issue #40).

Snapshots are written to .memory/cairnmind and check profiles are declared in
.cairnmind/checks.toml. What versions up to 0.9.0 left under the devbrain names is still
read, never rewritten and never deleted.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cairnmind import memory as portable
from cairnmind.agent_tools import ToolError, _parts
from cairnmind.ingest import index_project
from cairnmind.memory import MemoryManager, inspect_snapshot
from cairnmind.memory_paths import is_reserved_memory, snapshot_directory
from cairnmind.store import Store
from cairnmind.verification import capture_code_state, load_profiles


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    store = Store(tmp_path / "memory")
    project = store.add_project("shared", root)
    note = store.snapshot(project["id"], "note://decision", "Use cookies.", "note")
    store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "Use cookies."}, [note["id"]])
    return store, project["id"], root, MemoryManager(store)


def as_written_by_an_earlier_version(root, store):
    """Turn the package just written into what 0.9.0 left: the earlier directory, format name and receipt."""
    current, earlier = root / ".memory/cairnmind", root / ".memory/devbrain"
    shutil.move(current, earlier)
    for path in [earlier / "manifest.json", *sorted((earlier / "manifests").glob("*.json"))]:
        manifest = json.loads(path.read_text())
        published = manifest["snapshot_id"]
        manifest["format"] = "devbrain-portable-memory"
        manifest["snapshot_id"] = portable._manifest_id(manifest)
        raw = portable._bytes(manifest)
        path.write_bytes(raw)
        if path.parent.name == "manifests":
            path.rename(path.with_name(manifest["snapshot_id"] + ".json"))
    # The earlier version recorded its publication under the path and the identifier it wrote.
    store.command("UPDATE memory_snapshots SET path=:path, snapshot_id=:id, manifest_sha=:sha WHERE snapshot_id=:published",
                  {"path": str(earlier / "manifest.json"), "id": manifest["snapshot_id"], "published": published,
                   "sha": __import__("hashlib").sha256((earlier / "manifest.json").read_bytes()).hexdigest()})
    return earlier


def test_a_snapshot_is_written_to_the_current_directory_with_the_current_format_name(workspace):
    store, pid, root, manager = workspace
    result = manager.snapshot(pid)
    assert result["status"] == "created"
    manifest = json.loads((root / ".memory/cairnmind/manifest.json").read_text())
    assert manifest["format"] == "cairnmind-portable-memory"
    assert not (root / ".memory/devbrain").exists()
    assert snapshot_directory(root) == root / ".memory/cairnmind"
    status = manager.status(pid)
    assert status["path"] == status["write_path"] == str(root / ".memory/cairnmind/manifest.json")
    assert status["earlier_package"] is None and status["latest"]["snapshot_id"] == manifest["snapshot_id"]


def test_a_package_of_an_earlier_version_is_read_where_it_is_and_never_rewritten(workspace, tmp_path):
    store, pid, root, manager = workspace
    manager.snapshot(pid)
    earlier = as_written_by_an_earlier_version(root, store)
    before = {path.relative_to(earlier).as_posix(): path.read_bytes() for path in earlier.rglob("*") if path.is_file()}
    inspected = inspect_snapshot(earlier)
    assert inspected["project"]["id"] == pid  # the earlier format name is still a supported format
    assert snapshot_directory(root) == earlier
    status = manager.status(pid)
    assert status["path"] == str(earlier / "manifest.json") and status["latest"]["snapshot_id"] == inspected["snapshot_id"]
    assert status["write_path"] == str(root / ".memory/cairnmind/manifest.json")
    assert status["earlier_package"] == {"path": str(earlier), "state": "only_copy"}
    # Another machine imports it from the earlier directory.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True, capture_output=True)
    shutil.copytree(earlier, clone / ".memory/devbrain")
    other = Store(tmp_path / "other-memory")
    imported = MemoryManager(other).import_snapshot(clone / ".memory/devbrain", clone)
    assert imported["snapshot_id"] == inspected["snapshot_id"]
    assert [record["content"] for record in other.list_records(pid)] == ["Use cookies."]
    after = {path.relative_to(earlier).as_posix(): path.read_bytes() for path in earlier.rglob("*") if path.is_file()}
    assert after == before


def test_the_next_snapshot_goes_to_the_current_directory_and_continues_the_history(workspace):
    store, pid, root, manager = workspace
    manager.snapshot(pid)
    earlier = as_written_by_an_earlier_version(root, store)
    first = json.loads((earlier / "manifest.json").read_text())
    note = store.snapshot(pid, "note://second", "Sessions last 30 minutes.", "note")
    store.remember(pid, {"kind": "fact", "key": "sessions", "content": "Sessions last 30 minutes."}, [note["id"]])
    second = manager.snapshot(pid)
    assert second["status"] == "created"
    manifest = json.loads((root / ".memory/cairnmind/manifest.json").read_text())
    assert manifest["parent_id"] == first["snapshot_id"]  # the receipt of the earlier publication is the parent
    assert (earlier / "manifest.json").is_file()  # left where it was
    assert snapshot_directory(root) == root / ".memory/cairnmind"
    status = manager.status(pid)
    assert status["latest"]["snapshot_id"] == manifest["snapshot_id"]
    assert status["earlier_package"] == {"path": str(earlier), "state": "superseded"}


def test_a_newer_snapshot_published_into_the_earlier_directory_is_reported_not_ignored_silently(workspace):
    store, pid, root, manager = workspace
    manager.snapshot(pid)
    shutil.copytree(root / ".memory/cairnmind", root / ".memory/devbrain")
    assert manager.status(pid)["earlier_package"] == {"path": str(root / ".memory/devbrain"), "state": "superseded"}
    # A collaborator on an earlier version publishes a snapshot that is not part of this history.
    foreign = json.loads((root / ".memory/devbrain/manifest.json").read_text())
    foreign["snapshot_id"] = "f" * 64
    (root / ".memory/devbrain/manifest.json").write_text(json.dumps(foreign))
    assert manager.status(pid)["earlier_package"]["state"] == "diverged"
    (root / ".memory/devbrain/manifest.json").write_text("not json")
    assert manager.status(pid)["earlier_package"]["state"] == "unreadable"


def test_both_directories_are_reserved_for_indexing_the_fingerprint_and_agent_tools(workspace):
    store, pid, root, manager = workspace
    before = capture_code_state(store, pid)["fingerprint"]
    manager.snapshot(pid)
    shutil.copytree(root / ".memory/cairnmind", root / ".memory/devbrain")
    assert capture_code_state(store, pid)["fingerprint"] == before
    indexed = index_project(store, pid)
    assert indexed["files"] == 1 and not indexed["errors"]  # app.py only: neither package is project content
    for name in (".memory/cairnmind/manifest.json", ".memory/devbrain/objects/a.json"):
        assert is_reserved_memory(name)
        with pytest.raises(ToolError, match="managed memory"):
            _parts(name)
    assert _parts(".memory/notes.md") == (".memory", "notes.md")


def checks(root, directory, command="true"):
    path = root / directory / "checks.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f'[checks.unit]\nargv = ["{command}"]\n')
    return path


def test_check_profiles_are_read_from_the_current_file_or_the_earlier_one(workspace):
    store, pid, root, manager = workspace
    assert load_profiles(store, pid) == {}
    checks(root, ".devbrain", "earlier")
    assert load_profiles(store, pid)["unit"]["argv"] == ["earlier"]
    (root / ".devbrain/checks.toml").unlink()
    checks(root, ".cairnmind", "current")
    assert load_profiles(store, pid)["unit"]["argv"] == ["current"]


def test_two_declarations_of_check_profiles_are_refused(workspace):
    store, pid, root, manager = workspace
    checks(root, ".devbrain", "earlier")
    checks(root, ".cairnmind", "current")
    with pytest.raises(ValueError, match="declared in both .cairnmind/checks.toml and .devbrain/checks.toml"):
        load_profiles(store, pid)


def test_the_directory_of_check_profiles_is_not_indexed_as_project_content(workspace):
    store, pid, root, manager = workspace
    checks(root, ".cairnmind")
    (root / ".cairnmind/notes.md").write_text("not project documentation\n")
    assert index_project(store, pid)["files"] == 1


def test_an_unchanged_project_gets_no_second_copy_in_the_current_directory(workspace):
    store, pid, root, manager = workspace
    manager.snapshot(pid)
    earlier = as_written_by_an_earlier_version(root, store)
    again = manager.snapshot(pid)  # what the auto-save does on its first tick after the upgrade
    assert again["status"] == "unchanged" and again["manifest_path"] == str(earlier / "manifest.json")
    assert not (root / ".memory/cairnmind/manifest.json").exists()


def test_a_package_in_the_earlier_directory_that_this_store_never_saw_blocks_publication(workspace):
    store, pid, root, manager = workspace
    manager.snapshot(pid)
    earlier = as_written_by_an_earlier_version(root, store)
    foreign = json.loads((earlier / "manifest.json").read_text())
    foreign["created_at"] = "2026-01-01T00:00:00+00:00"
    foreign["snapshot_id"] = portable._manifest_id(foreign)
    (earlier / "manifest.json").write_bytes(portable._bytes(foreign))
    with pytest.raises(portable.MemoryConflict, match="was not published or imported by this store"):
        manager.snapshot(pid)
    assert not (root / ".memory/cairnmind/manifest.json").exists()


def test_a_broken_current_manifest_does_not_break_status(workspace):
    store, pid, root, manager = workspace
    manager.snapshot(pid)
    shutil.copytree(root / ".memory/cairnmind", root / ".memory/devbrain")
    broken = json.loads((root / ".memory/cairnmind/manifest.json").read_text())
    broken["ancestors"] = None
    (root / ".memory/cairnmind/manifest.json").write_text(json.dumps(broken))
    status = manager.status(pid)
    assert status["earlier_package"]["state"] == "unreadable" and status["error"] == "invalid_snapshot"


def test_two_profile_files_are_named_as_the_reason_a_task_cannot_be_verified(workspace):
    from cairnmind.verification import verification_status

    store, pid, root, manager = workspace
    checks(root, ".devbrain")
    checks(root, ".cairnmind")
    note = store.snapshot(pid, "note://task", "Do it.", "note")
    task = store.remember(pid, {"kind": "task", "key": "t", "content": "Do it.",
                                "metadata": {"required_profiles": ["unit"]}}, [note["id"]])
    status = verification_status(store, task)
    assert status["status"] == "unknown" and "declared in both" in status["reasons"][0]
