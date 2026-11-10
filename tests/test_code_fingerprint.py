"""The code fingerprint got cheaper without reading less (issue #24).

Any edit must still change the fingerprint and invalidate an ACK, including an edit that
leaves every size and time of the file as it was. That is why file content is always read.
"""
from __future__ import annotations

import builtins
import io
import mmap
import os
from pathlib import Path
import subprocess
import time

import pytest

from cairnmind import verification
from cairnmind.memory_paths import is_reserved_memory
from cairnmind.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.verification import capture_code_state


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def workspace(tmp_path):
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


def test_every_capture_reads_every_file_twice(workspace, monkeypatch):
    store, project, root = workspace
    capture_code_state(store, project["id"])
    time.sleep(2.2)  # files this old would be skipped by any cache keyed by size and times
    opened = opened_under(root, monkeypatch)
    state = capture_code_state(store, project["id"])
    assert state["status"] == "fresh"
    assert sorted(opened) == ["app.py", "app.py", "pkg/util.py", "pkg/util.py", "untracked.txt", "untracked.txt"]


def test_an_edit_through_a_shared_mapping_changes_the_fingerprint(workspace):
    store, project, root = workspace
    target = root / "app.py"
    with target.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as mapping:
        mapping[0:1] = b"W"  # the first store faults and moves the file times
        time.sleep(2.2)
        before = capture_code_state(store, project["id"])
        identity = [getattr(target.stat(), name) for name in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino")]
        mapping[1:2] = b"X"  # a later store to the same dirty page moves none of them
        after_identity = [getattr(target.stat(), name) for name in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino")]
        after = capture_code_state(store, project["id"])
    assert before["status"] == after["status"] == "fresh"
    assert after["fingerprint"] != before["fingerprint"]
    if after_identity != identity:
        pytest.skip("this filesystem updated the file times for the second store; the edit was still detected")


def test_every_kind_of_edit_changes_the_fingerprint(workspace):
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


def test_the_fingerprint_is_the_one_computed_by_resolving_every_file(workspace, monkeypatch):
    store, project, root = workspace
    (root / "inner").mkdir()
    (root / "inner" / "deep.py").write_text("DEEP = 1\n")
    (root / "link.py").symlink_to(root / "app.py")
    current = capture_code_state(store, project["id"])
    # The per-directory check is an optimisation of the per-file check it replaced.
    monkeypatch.setattr(verification, "_inside", lambda root: None)
    assert capture_code_state(store, project["id"]) == current
    assert current["status"] == "fresh"


def test_a_directory_that_leads_outside_the_worktree_is_reported_on_every_capture(workspace, tmp_path):
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


def test_directories_are_resolved_once_per_manifest_not_once_per_file(workspace, monkeypatch):
    store, project, root = workspace
    for index in range(40):
        (root / "pkg" / f"module_{index}.py").write_text(f"VALUE = {index}\n")
    resolved = []
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: resolved.append(str(self)) or original(self, *a, **k))
    assert capture_code_state(store, project["id"])["status"] == "fresh"
    below = [path for path in resolved if path.startswith(str(root) + os.sep) and path.endswith(".py")]
    assert below == []  # no file is resolved; only the root and its directories are
    assert len([path for path in resolved if path == str(root / "pkg")]) == 2  # once for each of the two manifests


def test_editing_a_file_invalidates_an_acknowledged_packet(workspace):
    store, project, root = workspace
    manager = SessionManager(store, store.settings)
    ready = manager.prepare(project["id"], "codex", timeout=0)
    packet = ready["packet"]
    assert ready["status"] == "ready", ready["unknowns"]
    manager.ack(ready["session_id"], packet["id"], packet["hash"], {"plan": None, "task": None})
    manager.require_ack(ready["session_id"])
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


@pytest.mark.parametrize("path", [".MEMORY/devbrain", ".Memory/DevBrain/x.json", "a/../.MEMORY/devbrain", ".memory/DEVBRAIN"])
def test_the_fast_path_leaves_case_to_the_platform_s_path_comparison(path):
    # What the check answered before the shortcut existed: true on Windows, false on Linux.
    normalised = Path(os.path.normpath(path))
    reference = not normalised.is_absolute() and normalised.is_relative_to(Path(".memory/devbrain"))
    assert is_reserved_memory(path) is reference
