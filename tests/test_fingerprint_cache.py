"""The code fingerprint reads a file again only when it may have changed (issue #24).

The guarantee must stay what it was: any edit changes the fingerprint and invalidates an ACK.
"""
from __future__ import annotations

import builtins
import io
import os
from pathlib import Path
import subprocess

import pytest

from devbrain import verification
from devbrain.memory_paths import is_reserved_memory
from devbrain.sessions import SessionManager
from devbrain.store import Store
from devbrain.verification import capture_code_state


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "app.py").write_text("VALUE = 1\n")
    (root / "pkg" / "util.py").write_text("LIMIT = 3\n")
    (root / "untracked.txt").write_text("not committed\n")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    git(root, "add", "app.py", "pkg")
    git(root, "commit", "-m", "initial")
    store = Store(tmp_path / "memory")
    project = store.add_project("fingerprint", root)
    verification._HASHES.clear()
    # The files were written a moment ago; the racy-clean rule has its own test below.
    monkeypatch.setattr(verification, "RACY_WINDOW_NS", -10**12)
    return store, project, root


def opened_under(root, monkeypatch):
    """Record every file opened below the root, through any of the open primitives."""
    opened, original = [], io.open

    def guarded(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and Path(file).resolve().is_relative_to(root):
            opened.append(Path(file).relative_to(root).as_posix())
        return original(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", guarded)
    monkeypatch.setattr(builtins, "open", guarded)
    return opened


def test_an_unchanged_worktree_is_fingerprinted_without_opening_a_file(workspace, monkeypatch):
    store, project, root = workspace
    opened = opened_under(root, monkeypatch)
    first = capture_code_state(store, project["id"])
    assert first["status"] == "fresh" and sorted(set(opened)) == ["app.py", "pkg/util.py", "untracked.txt"]
    del opened[:]
    second = capture_code_state(store, project["id"])
    assert opened == [] and second["fingerprint"] == first["fingerprint"]
    verification._HASHES.clear()
    assert capture_code_state(store, project["id"])["fingerprint"] == first["fingerprint"]  # cached or read: the same


def test_every_kind_of_edit_changes_the_fingerprint(workspace, monkeypatch):
    store, project, root = workspace
    seen = {capture_code_state(store, project["id"])["fingerprint"]}

    def changed():
        state = capture_code_state(store, project["id"])
        assert state["status"] == "fresh" and state["fingerprint"] not in seen
        seen.add(state["fingerprint"])

    target = root / "pkg" / "util.py"
    before = target.stat()
    target.write_text("LIMIT = 4\n")  # the same size
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))  # and the old modification time
    assert (target.stat().st_size, target.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    changed()
    replacement = root / "pkg" / "util.tmp"
    replacement.write_text("LIMIT = 5\n")
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.rename(target)  # another inode, the same size and modification time
    changed()
    target.chmod(0o755)
    changed()
    (root / "untracked.txt").write_text("still not committed\n")
    changed()
    (root / "new.py").write_text("NEW = 1\n")
    changed()
    (root / "new.py").unlink()
    (root / "app.py").write_text("VALUE = 2\n")
    changed()
    git(root, "commit", "-qam", "edit")
    changed()


def test_a_file_touched_just_before_it_was_read_is_read_again(workspace, monkeypatch):
    store, project, root = workspace
    monkeypatch.setattr(verification, "RACY_WINDOW_NS", 2_000_000_000)
    for name in ("app.py", "pkg/util.py", "untracked.txt"):
        os.utime(root / name)  # touched now, well inside the window
    opened = opened_under(root, monkeypatch)
    first = capture_code_state(store, project["id"])
    # On a filesystem with coarse timestamps a second write inside the same tick would look
    # identical, so nothing written within the window is remembered.
    assert verification._HASHES == {}
    del opened[:]
    assert capture_code_state(store, project["id"])["fingerprint"] == first["fingerprint"]
    assert sorted(set(opened)) == ["app.py", "pkg/util.py", "untracked.txt"]
    monkeypatch.setattr(verification.time, "time_ns", lambda: 10**19)  # long after every file time
    capture_code_state(store, project["id"])
    assert len(verification._HASHES) == 3
    del opened[:]
    capture_code_state(store, project["id"])
    assert opened == []


def test_a_file_that_changes_while_it_is_read_is_not_remembered(workspace, monkeypatch):
    store, project, root = workspace
    target = root / "app.py"
    capture_code_state(store, project["id"])
    assert str(target) in verification._HASHES
    target.write_text("VALUE = 20\n")
    original = Path.open

    def racing_open(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        if path == target:
            with original(target, "a") as writer:
                writer.write("# changed during the read\n")
        return stream

    monkeypatch.setattr(Path, "open", racing_open)
    state = capture_code_state(store, project["id"])
    assert state["status"] == "unknown" and any("app.py" in text for text in state["unknowns"])
    assert str(target) not in verification._HASHES


def test_a_directory_that_leads_outside_the_worktree_is_still_reported_on_every_capture(workspace, tmp_path):
    store, project, root = workspace
    assert capture_code_state(store, project["id"])["status"] == "fresh"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "util.py").write_text("LIMIT = 3\n")
    kept = root / "pkg-kept"
    (root / "pkg").rename(kept)
    (root / "pkg").symlink_to(outside, target_is_directory=True)
    state = capture_code_state(store, project["id"])
    # Whether a directory is inside is decided again for each manifest, never remembered.
    assert state["status"] == "unknown" and "external input: pkg/util.py" in state["unknowns"]
    (root / "pkg").unlink()
    kept.rename(root / "pkg")
    assert capture_code_state(store, project["id"])["status"] == "fresh"


def test_the_cache_is_bounded(workspace, monkeypatch):
    store, project, root = workspace
    monkeypatch.setattr(verification, "_HASH_LIMIT", 2)
    first = capture_code_state(store, project["id"])
    assert len(verification._HASHES) <= 2
    assert capture_code_state(store, project["id"])["fingerprint"] == first["fingerprint"]


def test_editing_a_remembered_file_still_invalidates_an_acknowledged_packet(workspace):
    store, project, root = workspace
    manager = SessionManager(store, store.settings)
    ready = manager.prepare(project["id"], "codex", timeout=0)
    packet = ready["packet"]
    assert ready["status"] == "ready", ready["unknowns"]
    manager.ack(ready["session_id"], packet["id"], packet["hash"], {"plan": None, "task": None})
    manager.require_ack(ready["session_id"])
    assert str(root / "app.py") in verification._HASHES
    target = root / "app.py"
    before = target.stat()
    target.write_text("VALUE = 9\n")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(ready["session_id"])


@pytest.mark.parametrize("path, expected", [
    (".memory/devbrain", True), (".memory/devbrain/manifest.json", True), ("a/../.memory/devbrain/x", True),
    ("./.memory/devbrain/objects/a.json", True), (".memory/notes.md", False), (".memory/devbrain-other/x", False),
    ("src/.memory/devbrain/x", False), ("src/app.py", False), ("memory/devbrain/x", False), ("/abs/.memory/devbrain", False),
])
def test_the_fast_path_of_the_reserved_memory_check_changes_no_answer(path, expected):
    assert is_reserved_memory(path) is expected
    assert is_reserved_memory(Path(path)) is expected
