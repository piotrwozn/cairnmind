"""A new native session sees imported worktree-scoped state and still needs ACK."""
from __future__ import annotations

import subprocess

import pytest

from cairnmind.memory import MemoryManager
from cairnmind.sessions import SessionManager
from cairnmind.store import Store


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_imported_scoped_decision_and_checkpoint_are_in_fresh_native_resume(tmp_path):
    source_root, destination_root = tmp_path / "computer-a", tmp_path / "computer-b"
    source_root.mkdir()
    git(source_root, "init", "--quiet")
    (source_root / "app.py").write_text("print('portable')\n")
    git(source_root, "add", "app.py")
    git(source_root, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
        "commit", "--quiet", "-m", "Fixture")
    subprocess.run(["git", "clone", "--quiet", str(source_root), str(destination_root)], check=True)
    source, destination = Store(tmp_path / "source-data"), Store(tmp_path / "destination-data")
    project = source.add_project("Scoped memory", source_root)
    evidence = source.snapshot(project["id"], "manual://scope", "Keep local context across computers", "manual")
    decision = source.remember(project["id"], {
        "kind": "decision", "content": "Use the saved portability decision", "status": "confirmed",
        "metadata": {"worktree": str(source_root)},
    }, [evidence["id"]])
    checkpoint = source.checkpoint(project["id"], {
        "goal": "Continue portable work", "next_step": "Review saved decision", "worktree": str(source_root),
    })
    snapshot = MemoryManager(source).snapshot(project["id"])
    MemoryManager(destination).import_snapshot(snapshot["path"], destination_root)
    manager = SessionManager(destination, destination.settings)
    prepared = manager.prepare(project["id"], "codex", destination_root, timeout=0)
    assert prepared["status"] == "ready", prepared["unknowns"]
    packet = prepared["packet"]
    assert {decision["id"], checkpoint["id"]} <= {row["id"] for row in packet["records"]}
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(prepared["session_id"])
    ack = manager.ack(prepared["session_id"], packet["id"], packet["hash"], {"plan": None, "task": None})
    assert ack["packet_id"] == packet["id"]
    manager.require_ack(prepared["session_id"])
    destination.remember(project["id"], {
        "id": decision["id"], "kind": "decision", "content": "The imported decision has changed",
        "status": "confirmed", "expected_version": decision["version"],
        "metadata": {"worktree": str(source_root)},
    }, [evidence["id"]])
    with pytest.raises(ValueError, match="resume_required"):
        manager.require_ack(prepared["session_id"])
    # Scope views must not rewrite archived provenance.
    original = next(row for row in destination.list_records(project["id"])
                    if row["id"] == checkpoint["id"])
    assert original["metadata"]["worktree"] == str(source_root)
