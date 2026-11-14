"""Portable knowledge snapshots on real ArcadeDB, with isolated Git fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from cairnmind import memory as portable
from cairnmind.memory import MemoryConflict, MemoryManager, inspect_snapshot
from cairnmind.plans import PlanManager
from cairnmind.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.verification import run_check, verification_status


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout.decode().strip()


def repository(root):
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Portable memory fixture")
    git(root, "config", "user.email", "portable@example.invalid")
    (root / "app.py").write_text("VALUE = 1\n")
    (root / ".gitignore").write_text("__pycache__/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture
def origin(tmp_path):
    root = repository(tmp_path / "origin-repository")
    store = Store(tmp_path / "origin-database")
    project = store.add_project("portable-origin", root)
    first = store.snapshot(project["id"], "note://decision", "Use cookies, not browser storage.", "note")
    record = store.remember(project["id"], {"kind": "decision", "key": "authentication",
                                           "content": "Use cookies."}, [first["id"]])
    second = store.snapshot(project["id"], "note://decision", "Use secure HttpOnly cookies.", "note")
    record = store.remember(project["id"], {"id": record["id"], "expected_version": 1,
                                           "content": "Use secure HttpOnly cookies."}, [second["id"]])
    code = store.snapshot(project["id"], str(root / "app.py"), (root / "app.py").read_text(), "code",
                          {"path": str(root / "app.py"), "root": str(root)})
    saved = PlanManager(store).save(project["id"], {"title": "Authentication", "goal": "Implement and verify"},
        [{"key": "implement", "content": "Implement authentication"},
         {"key": "verify", "content": "Verify authentication", "depends_on": ["implement"]}],
        "portable-plan", worktree=str(root), activate=True)
    return {"store": store, "project": project, "root": root, "manager": MemoryManager(store),
            "record": record, "first": first, "second": second, "code": code, "plan": saved["plan"]}


def destination(tmp_path, origin, suffix="destination"):
    root = tmp_path / f"{suffix}-repository"
    subprocess.run(["git", "clone", "-q", str(origin["root"]), str(root)], check=True, capture_output=True)
    store = Store(tmp_path / f"{suffix}-database")
    return store, root, MemoryManager(store)


def core_state(store):
    tables = ("projects", "project_roots", "sources", "records", "record_revisions", "plans", "plan_revisions",
              "worktree_states", "nodes", "edges", "sessions", "resume_packets", "resume_acks", "jobs", "import_cursors")
    return {table: store.query(f"SELECT FROM {table} ORDER BY id") for table in tables}


def head_path(result):
    return Path(result["manifest_path"])


def manifest(path):
    return json.loads(Path(path).read_text())


def objects(path):
    data = manifest(path)
    base = Path(path).parent
    return {table: [json.loads((base / item["path"]).read_text()) for item in entries]
            for table, entries in data["objects"].items()}


def bundle_copy(tmp_path, result, name="tampered"):
    folder = tmp_path / name / ".memory" / "devbrain"
    folder.parent.mkdir(parents=True)
    shutil.copytree(head_path(result).parent, folder)
    return folder / "manifest.json"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def rewrite_consistent_manifest(path, data):
    payload = {table: [json.loads((path.parent / item["path"]).read_text()) for item in entries]
               for table, entries in data["objects"].items()}
    data["state_hash"] = portable._state_hash(payload)
    data["snapshot_id"] = portable._manifest_id(data)
    path.write_bytes(canonical(data))
    history = path.parent / "manifests"
    history.mkdir(exist_ok=True)
    (history / f"{data['snapshot_id']}.json").write_bytes(canonical(data))


def alter_object(path, table, predicate, change):
    data = manifest(path)
    entry = next(item for item in data["objects"][table]
                 if predicate(json.loads((path.parent / item["path"]).read_text())))
    row = json.loads((path.parent / entry["path"]).read_text())
    change(row)
    encoded = canonical(row)
    digest = portable._digest(row)
    relative = f"objects/{digest}.json"
    (path.parent / relative).write_bytes(encoded)
    entry.update(id=row["id"], sha256=digest, path=relative, bytes=len(encoded))
    rewrite_consistent_manifest(path, data)


def advance(origin):
    store, project = origin["store"], origin["project"]
    current = next(row for row in store.list_records(project["id"]) if row["id"] == origin["record"]["id"])
    text = f"Authentication revision {current['version'] + 1}."
    evidence = store.snapshot(project["id"], "note://decision", text, "note")
    origin["record"] = store.remember(project["id"], {"id": current["id"], "expected_version": current["version"],
                                                     "content": text}, [evidence["id"]])
    return origin["manager"].snapshot(project["id"], worktree=str(origin["root"]))


def test_preview_and_import_dry_run_do_not_mutate_project_knowledge(origin, tmp_path):
    before = core_state(origin["store"])
    preview = origin["manager"].preview(origin["project"]["id"], worktree=str(origin["root"]))
    assert preview["counts"]["records"] >= 3
    assert core_state(origin["store"]) == before
    assert not (origin["root"] / ".memory" / "devbrain" / "manifest.json").exists()
    published = origin["manager"].snapshot(origin["project"]["id"])
    store, root, manager = destination(tmp_path, origin)
    before = core_state(store)
    result = manager.import_snapshot(head_path(published), root, dry_run=True)
    assert result["status"] == "dry_run"
    assert core_state(store) == before


def test_roundtrip_preserves_identity_revision_evidence_and_selected_worktree_plan(origin, tmp_path):
    store, project = origin["store"], origin["project"]
    other_root = repository(tmp_path / "other-worktree")
    store.add_project_root(project["id"], other_root)
    other_plan = PlanManager(store).save(project["id"], {"title": "Other root", "goal": "Keep selection scoped"},
        [{"key": "other", "content": "Do not select this task in the first root"}],
        "other-root-plan", worktree=str(other_root), activate=True)
    published = origin["manager"].snapshot(project["id"], worktree=str(origin["root"]))
    assert published["status"] == "created" and published["active_plan_id"] == origin["plan"]["id"]
    assert published["active_plan_id"] != other_plan["plan"]["id"]
    assert inspect_snapshot(head_path(published))["status"] == "valid"
    exported = objects(head_path(published))
    assert not {"sessions", "resume_packets", "resume_acks", "jobs", "import_cursors", "config",
                "embeddings", "vector_indexes", "chunks"}.intersection(exported)
    assert origin["store"].settings.token not in json.dumps(exported)
    target, root, manager = destination(tmp_path, origin)
    result = manager.import_snapshot(head_path(published), root)
    assert result["status"] == "imported" and result["project"]["id"] == project["id"]
    assert Path(result["project"]["root"]) == root.resolve()
    restored = next(row for row in target.list_records(project["id"]) if row["id"] == origin["record"]["id"])
    assert restored["version"] == 2 and restored["content"] == origin["record"]["content"]
    assert [row["version"] for row in target.record_history(restored["id"])] == [1, 2]
    for evidence in (origin["first"], origin["second"], origin["code"]):
        imported = target.get_source(evidence["id"])
        assert {key: imported[key] for key in ("id", "uri", "content", "sha256")} == {
            key: evidence[key] for key in ("id", "uri", "content", "sha256")}
    assert PlanManager(target).next_task(project["id"], worktree=str(root))["plan"]["id"] == origin["plan"]["id"]
    neighbors = target.query("SELECT expand(out('edges')) FROM nodes WHERE id=:id",
                             {"id": f"record:{restored['id']}"})
    assert f"source:{origin['second']['id']}" in {row["id"] for row in neighbors}
    assert {row["path"] for row in target.query("SELECT path FROM project_roots WHERE project_id=:id",
                                               {"id": project["id"]})} == {str(root.resolve())}
    assert target.query("SELECT FROM chunks WHERE project_id=:id", {"id": project["id"]})
    before = core_state(target)
    repeated = manager.import_snapshot(head_path(published), root)
    assert repeated["status"] == "unchanged" and core_state(target) == before


def test_operational_receipts_and_remote_machine_check_authority_do_not_transfer(origin, tmp_path):
    store, project, root = origin["store"], origin["project"], origin["root"]
    profiles = root / ".devbrain"
    profiles.mkdir()
    (profiles / "checks.toml").write_text('[checks.unit]\nargv = ' + json.dumps([
        sys.executable, "-c", "print('controlled portable fixture check')"]) + '\ncwd = "."\ntimeout_seconds = 10\n')
    git(root, "add", ".devbrain/checks.toml")
    git(root, "commit", "-qm", "add fixture profile")
    task = store.remember(project["id"], {"kind": "task", "key": "machine-task", "content": "Verify fixture",
        "metadata": {"requires_test": True, "required_profiles": ["unit"], "state": "needs_test"}}, [origin["second"]["id"]])
    run = run_check(store, store.settings, project["id"], "unit", task["id"])
    assert run["passed"] and verification_status(store, task)["status"] == "fresh"
    prepared = SessionManager(store, store.settings).prepare(project["id"], "codex", timeout=0)
    assert prepared["status"] == "ready"
    packet = prepared["packet"]
    readback = {key: ({"id": packet[field]["id"], "version": packet[field]["version"]} if packet.get(field) else None)
                for key, field in (("plan", "active_plan"), ("task", "active_task"))}
    SessionManager(store, store.settings).ack(prepared["session_id"], packet["id"], packet["hash"], readback)
    job_id = store.enqueue("extract", project["id"], {"source_id": origin["second"]["id"]}, "foreign-work")
    published = origin["manager"].snapshot(project["id"])
    target, local_root, manager = destination(tmp_path, origin)
    result = manager.import_snapshot(head_path(published), local_root)
    imported = target.query("SELECT FROM check_runs WHERE id=:id", {"id": run["id"]})[0]
    assert imported["origin"] == "portable_import"
    imported_task = next(row for row in target.list_records(project["id"]) if row["id"] == task["id"])
    assert verification_status(target, imported_task, local_root)["status"] != "fresh"
    assert result["verification"] == "historical"
    for table in ("sessions", "resume_packets", "resume_acks", "import_cursors"):
        assert target.query(f"SELECT FROM {table}") == []
    assert not target.query("SELECT id FROM jobs WHERE id=:id", {"id": job_id})


@pytest.mark.parametrize("damage", ["object_bytes", "source_hash", "foreign_project", "historical_source_closure",
                                    "revision_chain"])
def test_bad_snapshot_is_rejected_before_any_destination_knowledge_mutation(origin, tmp_path, damage):
    published = origin["manager"].snapshot(origin["project"]["id"])
    path = bundle_copy(tmp_path, published)
    if damage == "object_bytes":
        data = manifest(path)
        entry = data["objects"]["sources"][0]
        (path.parent / entry["path"]).write_bytes(b"{}")
    elif damage == "source_hash":
        alter_object(path, "sources", lambda row: row["id"] == origin["first"]["id"],
                     lambda row: row.update(content="Changed bytes with an old source SHA"))
    elif damage == "foreign_project":
        alter_object(path, "records", lambda row: row["id"] == origin["record"]["id"],
                     lambda row: row.update(project_id=str(uuid4())))
    elif damage == "revision_chain":
        alter_object(path, "records", lambda row: row["id"] == origin["record"]["id"],
                     lambda row: row.update(version=9))
    else:
        data = manifest(path)
        data["objects"]["sources"] = [entry for entry in data["objects"]["sources"]
                                           if entry["id"] != origin["first"]["id"]]
        data["counts"]["sources"] -= 1
        rewrite_consistent_manifest(path, data)
    target, root, manager = destination(tmp_path, origin)
    unrelated_root = repository(tmp_path / "unrelated-repository")
    unrelated = target.add_project("unrelated", unrelated_root)
    evidence = target.snapshot(unrelated["id"], "note://keep", "Existing data must survive.", "note")
    target.remember(unrelated["id"], {"kind": "fact", "content": "Retain existing data."}, [evidence["id"]])
    before = core_state(target)
    with pytest.raises(ValueError):
        manager.import_snapshot(path, root)
    assert core_state(target) == before


def test_strict_fast_forward_import_and_local_divergence_do_not_overwrite_memory(origin, tmp_path):
    first = origin["manager"].snapshot(origin["project"]["id"])
    target, root, manager = destination(tmp_path, origin)
    manager.import_snapshot(head_path(first), root)
    second = advance(origin)
    assert second["parent_id"] == first["snapshot_id"]
    updated = manager.import_snapshot(head_path(second), root)
    assert updated["status"] == "imported"
    row = next(row for row in target.list_records(origin["project"]["id"]) if row["id"] == origin["record"]["id"])
    assert row["version"] == 3
    evidence = target.snapshot(origin["project"]["id"], "note://local-only", "A newer local decision.", "note")
    local = target.remember(origin["project"]["id"], {"kind": "decision", "key": "local-only",
                                                     "content": "Preserve local divergence."}, [evidence["id"]])
    third = advance(origin)
    before = core_state(target)
    with pytest.raises(MemoryConflict):
        manager.import_snapshot(head_path(third), root)
    assert core_state(target) == before
    assert any(row["id"] == local["id"] for row in target.list_records(origin["project"]["id"]))


def test_external_manifest_changes_are_not_overwritten_by_a_new_snapshot(origin):
    published = origin["manager"].snapshot(origin["project"]["id"])
    path = head_path(published)
    external = b'{"user_owned":"manual manifest modification"}\n'
    path.write_bytes(external)
    with pytest.raises(ValueError):
        origin["manager"].snapshot(origin["project"]["id"])
    assert path.read_bytes() == external


def test_failed_publication_preserves_the_latest_manifest_and_retained_history(origin, monkeypatch):
    origin["manager"].snapshot(origin["project"]["id"])
    for _ in range(5):
        published = advance(origin)
    path = head_path(published)
    latest = path.read_bytes()
    history = {file.name: file.read_bytes() for file in (path.parent / "manifests").glob("*.json")}
    assert len(history) == 5
    original = portable._atomic_write

    def fail_latest(destination, data):
        if Path(destination) == path:
            raise OSError("Controlled publication failure")
        return original(destination, data)

    monkeypatch.setattr(portable, "_atomic_write", fail_latest)
    with pytest.raises((OSError, ValueError)):
        advance(origin)
    assert path.read_bytes() == latest
    assert all((path.parent / "manifests" / name).read_bytes() == data for name, data in history.items())
    assert inspect_snapshot(path)["snapshot_id"] == published["snapshot_id"]


def test_legitimate_large_memory_record_preserves_complete_content_and_evidence(origin, tmp_path):
    text = "A legitimate project constraint must remain complete across devices. " * 100
    assert len(text) > 4096
    store, project = origin["store"], origin["project"]
    evidence = store.snapshot(project["id"], "note://long-constraint", text, "note")
    record = store.remember(project["id"], {"kind": "constraint", "key": "long-constraint", "content": text},
                            [evidence["id"]])
    published = origin["manager"].snapshot(project["id"])
    assert inspect_snapshot(head_path(published))["status"] == "valid"
    target, root, manager = destination(tmp_path, origin)
    assert manager.import_snapshot(head_path(published), root)["status"] == "imported"
    imported = next(row for row in target.list_records(project["id"]) if row["id"] == record["id"])
    assert imported["content"] == text and imported["source_ids"] == [evidence["id"]]
    assert target.get_source(evidence["id"])["content"] == text


def test_auto_tick_of_unchanged_committed_memory_does_not_republish(origin):
    manager, project = origin["manager"], origin["project"]
    published = manager.snapshot(project["id"])
    path = head_path(published)
    before = path.read_bytes(), path.stat().st_mtime_ns
    manager.configure_auto(project["id"], True, interval_minutes=15, worktree=str(origin["root"]))
    moment = time.time() + 901
    manager.tick(now=moment)
    manager.tick(now=moment + 901)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    current = manager.status(project["id"])["auto"]
    assert current["enabled"] is True and current["interval_minutes"] == 15
    assert current["last_checked_at"] and current["last_success_at"] and current["error"] is None
    manager.configure_auto(project["id"], False)
    assert manager.status(project["id"])["auto"]["enabled"] is False


def test_the_objects_of_a_snapshot_share_one_directory_sync_before_the_manifest(origin, monkeypatch):
    # #158: a snapshot of a large project synced the objects directory once per object.
    manager, project = origin["manager"], origin["project"]
    synced, real = [], portable.os.fsync

    def fsync(descriptor):
        if portable.os.path.isdir(f"/proc/self/fd/{descriptor}"):
            synced.append(portable.os.readlink(f"/proc/self/fd/{descriptor}"))
        return real(descriptor)

    monkeypatch.setattr(portable.os, "fsync", fsync)
    published = manager.snapshot(project["id"])
    objects = sum(len(rows) for rows in json.loads(Path(published["path"]).read_bytes())["objects"].values())
    folder = Path(published["path"]).parent
    assert objects > 5 and synced.count(str(folder / "objects")) == 1
    # The objects are synced before the manifests that name them.
    assert synced.index(str(folder / "objects")) < synced.index(str(folder / "manifests")) < synced.index(str(folder))
    assert inspect_snapshot(published["path"])["snapshot_id"] == published["snapshot_id"]


def test_objects_left_by_a_killed_attempt_are_synced_before_the_manifest_that_names_them(origin, monkeypatch):
    # #158 review: an attempt killed after its objects were renamed into place, but before their directory was
    # synced, left entries that the next attempt reused without a sync, then published a manifest naming them.
    manager, project = origin["manager"], origin["project"]
    real_write, real_fsync = portable._atomic_write, portable.os.fsync

    def killed(destination, data):
        if Path(destination).parent.name == "manifests":
            raise OSError("Killed before the manifest")
        return real_write(destination, data)

    monkeypatch.setattr(portable, "_atomic_write", killed)
    with pytest.raises(OSError):
        manager.snapshot(project["id"])
    monkeypatch.setattr(portable, "_atomic_write", real_write)
    synced = []

    def fsync(descriptor):
        if portable.os.path.isdir(f"/proc/self/fd/{descriptor}"):
            synced.append(portable.os.readlink(f"/proc/self/fd/{descriptor}"))
        return real_fsync(descriptor)

    monkeypatch.setattr(portable.os, "fsync", fsync)
    published = manager.snapshot(project["id"])
    folder = Path(published["path"]).parent
    assert synced.count(str(folder / "objects")) == 1
    assert synced.index(str(folder / "objects")) < synced.index(str(folder / "manifests"))
    assert inspect_snapshot(published["path"])["snapshot_id"] == published["snapshot_id"]


def test_a_repeated_snapshot_reads_the_objects_of_kept_history_only_by_name(origin, monkeypatch):
    # #158: retention loaded, hashed and validated every object of each of the five kept manifests.
    origin["manager"].snapshot(origin["project"]["id"])
    for _ in range(6):
        published = advance(origin)
    loaded, real = [], portable._load_snapshot
    monkeypatch.setattr(portable, "_load_snapshot", lambda path: loaded.append(path) or real(path))
    published = advance(origin)
    path = head_path(published)
    assert loaded == [path]  # only the previous manifest, before it is replaced
    kept = [json.loads(file.read_bytes()) for file in (path.parent / "manifests").glob("*.json")]
    named = {d["sha256"] for manifest in kept for rows in manifest["objects"].values() for d in rows}
    assert len(kept) == 5 and {file.stem for file in (path.parent / "objects").glob("*.json")} == named
