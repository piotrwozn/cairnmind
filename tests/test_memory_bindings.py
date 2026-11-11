"""Portable scopes and reserved artifacts, using actual Git and ArcadeDB."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from cairnmind.ingest import current_source, index_project
from cairnmind.memory_paths import (MEMORY_RELATIVE, bound_metadata, is_memory_path,
                                  is_reserved_memory, memory_bindings)
from cairnmind.retrieval import Retriever
from cairnmind.store import Store
from cairnmind.verification import capture_code_state, run_check, verification_status


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Memory fixture")
    git(root, "config", "user.email", "memory@example.invalid")
    (root / "app.py").write_text("print('local checkout')\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    store = Store(tmp_path / "database")
    project = store.add_project("portable", root)
    return store, project, root


def binding(store, project, origin, local):
    store.command("INSERT INTO memory_bindings CONTENT :row", {"row": {
        "id": str(uuid4()), "project_id": project["id"], "origin_root": str(origin), "local_root": str(local),
    }})


def receipt(store, project, *sources):
    store.command("INSERT INTO memory_imports CONTENT :row", {"row": {
        "id": str(uuid4()), "project_id": project["id"], "source_ids": [source["id"] for source in sources],
    }})


def test_reserved_path_is_narrow_and_lexical(tmp_path):
    assert is_reserved_memory(MEMORY_RELATIVE)
    assert is_reserved_memory(".memory/cairnmind/snapshot.json")
    assert is_reserved_memory(".memory/devbrain/snapshot.json")  # where versions up to 0.9.0 wrote; still reserved
    assert not is_reserved_memory(".memory/cairnmind-other/snapshot.json")
    assert not is_reserved_memory("nested/.memory/cairnmind/snapshot.json")
    assert not is_reserved_memory(".memory/notes.md")
    assert not is_reserved_memory(".memory/devbrain-other/snapshot.json")
    assert not is_reserved_memory("nested/.memory/devbrain/snapshot.json")
    assert is_memory_path(tmp_path / MEMORY_RELATIVE / "latest.json", tmp_path)
    assert is_memory_path(MEMORY_RELATIVE / "latest.json", tmp_path)
    assert not is_memory_path(tmp_path / MEMORY_RELATIVE / "../../app.py", tmp_path)


def test_managed_snapshot_writes_do_not_change_code_inputs(workspace):
    store, project, root = workspace
    initial = capture_code_state(store, project["id"])
    folder = root / MEMORY_RELATIVE
    folder.mkdir(parents=True)
    snapshot = folder / "snapshot.json"
    snapshot.write_text('{"generation":1}')
    assert capture_code_state(store, project["id"])["fingerprint"] == initial["fingerprint"]
    git(root, "add", ".memory/cairnmind")
    snapshot.write_text('{"generation":2}')
    assert capture_code_state(store, project["id"])["fingerprint"] == initial["fingerprint"]
    git(root, "commit", "-qm", "snapshot only")
    # HEAD is deliberately still part of verification identity.
    assert capture_code_state(store, project["id"])["fingerprint"] != initial["fingerprint"]
    baseline = capture_code_state(store, project["id"])
    (root / ".memory" / "notes.txt").write_text("ordinary project input")
    assert capture_code_state(store, project["id"])["fingerprint"] != baseline["fingerprint"]


def test_explicit_reserved_check_inputs_are_rejected_broad_globs_remain_safe(workspace):
    store, project, root = workspace
    folder = root / MEMORY_RELATIVE
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text("one")
    for pattern in (".memory/cairnmind", ".memory/cairnmind/*", ".memory/devbrain", ".memory/devbrain/*",
                    ".memory/devbrain/snapshot.json"):
        with pytest.raises(ValueError, match="reserved"):
            capture_code_state(store, project["id"], extra_inputs=[pattern])
    first = capture_code_state(store, project["id"], extra_inputs=[".memory/**/*"])
    (folder / "snapshot.json").write_text("two")
    second = capture_code_state(store, project["id"], extra_inputs=[".memory/**/*"])
    assert first["status"] == second["status"] == "fresh"
    assert first["fingerprint"] == second["fingerprint"]


def test_reserved_snapshot_is_not_ingested_or_refreshed(workspace):
    store, project, root = workspace
    folder = root / MEMORY_RELATIVE
    folder.mkdir(parents=True)
    (folder / "archive.md").write_text("A snapshot must not create another extraction job")
    visible = root / ".memory" / "notes.txt"
    visible.write_text("Ordinary project notes")
    indexed = index_project(store, project["id"])
    assert indexed["files"] == 2
    paths = {row["uri"] for row in store.query("SELECT uri FROM sources WHERE project_id=:project",
                                                {"project": project["id"]})}
    assert str(visible) in paths
    assert str(folder / "archive.md") not in paths
    source = store.snapshot(project["id"], str(folder / "archive.md"), "saved content", "document",
                            {"root": str(root), "path": str(folder / "archive.md")})
    with pytest.raises(ValueError, match="excluded"):
        current_source(store, source["id"])


def test_bound_knowledge_and_graph_preserve_original_provenance(workspace, tmp_path):
    store, project, root = workspace
    origin = tmp_path / "different-computer" / "repo"
    source = store.snapshot(project["id"], "conversation://portable", "Use amber authentication", "transcript",
                            {"root": str(origin), "worktree": str(origin)})
    record = store.remember(project["id"], {"kind": "decision", "content": "Use amber authentication",
                                           "metadata": {"worktree": str(origin)}}, [source["id"]])
    retriever = Retriever(store, store.settings)
    assert not retriever.records(project["id"])
    binding(store, project, origin, root)
    assert retriever.source_in_scope(source, project["id"])
    assert [item["id"] for item in retriever.records(project["id"])] == [record["id"]]
    assert any(hit.get("record_id") == record["id"] for hit in retriever.search(project["id"], "amber"))
    graph = retriever.graph(project["id"], f"record:{record['id']}")
    assert f"record:{record['id']}" in {node["id"] for node in graph["nodes"]}
    assert store.get_source(source["id"])["metadata"]["root"] == str(origin)
    assert store.record_history(record["id"])[0]["metadata"]["worktree"] == str(origin)
    other = store.add_project("other", tmp_path / "other")
    assert not Retriever(store, store.settings).source_in_scope(source, other["id"])
    assert not memory_bindings(store, other["id"])


def test_bindings_reject_unregistered_targets_and_leave_other_scopes_historical(workspace, tmp_path):
    store, project, root = workspace
    origin = tmp_path / "old-root"
    binding(store, project, origin, tmp_path / "unregistered")
    assert not memory_bindings(store, project["id"])
    binding(store, project, origin, root)
    original = {"root": str(origin), "path": str(origin / "app.py"), "relative_path": "app.py"}
    mapped = bound_metadata(store, project["id"], original)
    assert mapped["root"] == str(root)
    assert mapped["path"] == str(root / "app.py")
    assert original["root"] == str(origin)
    unrelated = {"root": str(tmp_path / "different-worktree")}
    assert bound_metadata(store, project["id"], unrelated) == unrelated


@pytest.mark.parametrize("same_path", [False, True])
def test_imported_file_is_historical_until_local_index(workspace, tmp_path, monkeypatch, same_path):
    store, project, root = workspace
    origin = root if same_path else tmp_path / "foreign-host"
    source_path = origin / "app.py"
    source = store.snapshot(project["id"], str(source_path), "foreign archived code", "code",
                            {"root": str(origin), "path": str(source_path), "relative_path": "app.py"})
    store.command("UPDATE sources SET is_current=false,current_key=null WHERE id=:id", {"id": source["id"]})
    binding(store, project, origin, root)
    receipt(store, project, source)
    original_read = Path.read_bytes

    def forbid_foreign_read(path):
        if path == source_path:
            raise AssertionError("Imported source provenance must not be read as a host file")
        return original_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", forbid_foreign_read)
        historical = current_source(store, source["id"])
    assert historical["content"] == "foreign archived code"
    assert historical["is_current"] is False
    assert "historical" in historical["freshness_warning"]
    index_project(store, project["id"])
    local = current_source(store, source["id"])
    assert local["id"] != source["id"]
    assert local["is_current"]
    assert local["content"] == "print('local checkout')\n"
    assert store.get_source(source["id"])["content"] == "foreign archived code"
    (root / "app.py").write_text("print('changed after local indexing')\n")
    refreshed = current_source(store, source["id"])
    assert refreshed["id"] != local["id"]
    assert refreshed["content"] == "print('changed after local indexing')\n"


@pytest.mark.parametrize("same_path", [False, True])
def test_imported_check_is_historical_even_with_matching_code(workspace, same_path):
    store, project, root = workspace
    folder = root / ".devbrain"
    folder.mkdir()
    (folder / "checks.toml").write_text(f"[checks.unit]\nargv={json.dumps([sys.executable, 'app.py'])}\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "check profile")
    source = store.snapshot(project["id"], "fixture://task", "Check the portable checkout", "manual")
    task = store.remember(project["id"], {"kind": "task", "content": "Check the checkout", "metadata": {
        "requires_test": True, "required_profiles": ["unit"],
    }}, [source["id"]])
    foreign = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert foreign["passed"]
    store.command("UPDATE check_runs SET origin='portable_import',worktree=:root WHERE id=:id", {
        "root": str(root if same_path else root.parent / "other-computer"), "id": foreign["id"],
    })
    historical = verification_status(store, task)
    assert historical["status"] == "reported"
    assert "historical" in historical["reasons"][0]
    assert not historical["run_ids"]
    local = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert local["passed"]
    assert verification_status(store, task) == {"status": "fresh", "reasons": [], "run_ids": [local["id"]]}


def test_return_import_preserves_original_local_file_authority(workspace, tmp_path):
    from cairnmind.memory import MemoryManager

    store, project, root = workspace
    checks = root / ".devbrain" / "checks.toml"
    checks.parent.mkdir()
    checks.write_text(f"[checks.unit]\nargv={json.dumps([sys.executable, 'app.py'])}\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "portable check")
    index_project(store, project["id"])
    original = store.query("SELECT FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                           {"project": project["id"], "uri": str(root / "app.py")})[0]
    evidence = store.snapshot(project["id"], "manual://original-task", "Verify this checkout", "manual")
    task = store.remember(project["id"], {"kind": "task", "content": "Verify this checkout", "metadata": {
        "requires_test": True, "required_profiles": ["unit"],
    }}, [evidence["id"]])
    original_run = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert original_run["passed"]
    first = MemoryManager(store).snapshot(project["id"])
    other_root = tmp_path / "second-checkout"
    subprocess.run(["git", "clone", "-q", str(root), str(other_root)], check=True, capture_output=True)
    other = Store(tmp_path / "second-database")
    MemoryManager(other).import_snapshot(first["path"], other_root)
    imported_task = other.list_records(project["id"])[0]
    assert verification_status(other, imported_task)["status"] == "reported"
    note = other.snapshot(project["id"], "manual://second-machine", "Use copper deployment", "manual")
    other.remember(project["id"], {"kind": "decision", "content": "Use copper deployment"}, [note["id"]])
    second = MemoryManager(other).snapshot(project["id"])
    MemoryManager(store).import_snapshot(second["path"], root)
    assert verification_status(store, task) == {"status": "fresh", "reasons": [], "run_ids": [original_run["id"]]}
    current = current_source(store, original["id"])
    assert current["id"] == original["id"]
    assert current["is_current"]
    assert not current.get("freshness_warning")
    (root / "app.py").write_text("print('new local checkout')\n")
    changed = current_source(store, original["id"])
    assert changed["id"] != original["id"]
    assert changed["is_current"]
    assert changed["content"] == "print('new local checkout')\n"
