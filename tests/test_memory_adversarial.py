"""Valid-checksum packages must still preserve history, identity and atomicity."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from devbrain import memory as portable
from devbrain.memory import MemoryConflict, MemoryManager, inspect_snapshot
from devbrain.plans import PlanManager
from devbrain.store import Store


@pytest.fixture
def fixture(tmp_path):
    origin_root = tmp_path / "origin"
    origin_root.mkdir()
    origin = Store(tmp_path / "origin-data")
    project = origin.add_project("source-project", origin_root)
    evidence = origin.snapshot(project["id"], "note://decision", "Choose cookies.", "note")
    first = origin.remember(project["id"], {
        "kind": "decision", "key": "auth", "content": "Choose cookies.",
    }, [evidence["id"]])
    second = origin.remember(project["id"], {
        "id": first["id"], "expected_version": 1, "content": "Choose secure cookies.",
    }, [evidence["id"]])
    # An empty plan makes plan omission independently testable without changing tasks.
    plan = PlanManager(origin).save(project["id"], {"title": "Recorded plan"}, [], "empty-plan")
    manager = MemoryManager(origin)
    initial = manager.snapshot(project["id"])
    destination_root = tmp_path / "destination"
    destination_root.mkdir()
    destination = Store(tmp_path / "destination-data")
    importer = MemoryManager(destination)
    importer.import_snapshot(initial["path"], destination_root)
    return {"source": origin, "destination": destination, "manager": manager, "importer": importer,
            "root": destination_root, "project": project, "first": first, "second": second,
            "plan": plan["plan"], "initial": initial}


def database_state(store):
    state = store.export()
    state.pop("exported_at", None)
    for key, rows in state.items():
        if isinstance(rows, list):
            rows.sort(key=lambda row: str(row.get("id")))
    return state


def successor(fixture):
    current = fixture["second"]
    fixture["source"].remember(fixture["project"]["id"], {
        "id": current["id"], "expected_version": current["version"], "content": "Choose HttpOnly cookies.",
    }, current["source_ids"])
    return fixture["manager"].snapshot(fixture["project"]["id"])


def rewrite_bundle(tmp_path, published, mutation):
    """Recompute all transport checksums, so tests exercise semantic validation."""
    source = Path(published["path"])
    root = tmp_path / "modified-package"
    shutil.copytree(source.parent, root)
    path = root / "manifest.json"
    manifest = portable._parse(path.read_bytes())
    payload = {
        table: [portable._parse((root / entry["path"]).read_bytes()) for entry in entries]
        for table, entries in manifest["objects"].items()
    }
    mutation(manifest, payload)
    manifest["objects"] = {}
    for table, rows in payload.items():
        descriptors = []
        for row in rows:
            raw = portable._bytes(row)
            digest = portable._digest(row)
            relative = f"objects/{digest}.json"
            (root / relative).write_bytes(raw)
            descriptors.append({"id": row["id"], "sha256": digest, "path": relative, "bytes": len(raw)})
        manifest["objects"][table] = descriptors
    manifest["counts"] = {table: len(rows) for table, rows in payload.items()}
    manifest["state_hash"] = portable._state_hash(payload)
    manifest["snapshot_id"] = portable._manifest_id(manifest)
    path.write_bytes(portable._bytes(manifest))
    return path


@pytest.mark.parametrize("damage", ["regressed_record", "omitted_plan", "rewritten_revision", "reused_source"])
def test_valid_checksum_fast_forward_cannot_rewrite_or_omit_history(fixture, tmp_path, damage):
    published = successor(fixture)

    def mutate(manifest, payload):
        if damage == "regressed_record":
            identifier = fixture["first"]["id"]
            head = next(row for row in payload["records"] if row["id"] == identifier)
            original = next(row["snapshot"] for row in payload["record_revisions"]
                            if row["record_id"] == identifier and row["version"] == 1)
            head.clear()
            head.update(deepcopy(original))
            # Keep the forged package internally coherent: this case must
            # reach the destination-history guard, rather than fail the graph
            # consistency check during standalone inspection.
            node = next(row for row in payload["nodes"] if row["id"] == f"record:{identifier}")
            node.update(kind=head["kind"], label=head["content"][:1000], source_id=head["source_ids"][0],
                        metadata={**head["metadata"], "record_id": identifier, "status": head["status"]})
            payload["record_revisions"] = [row for row in payload["record_revisions"]
                                           if row["record_id"] != identifier or row["version"] == 1]
        elif damage == "omitted_plan":
            payload["plans"] = []
            payload["plan_revisions"] = []
            removed = {row["id"] for row in payload["nodes"] if row["kind"] == "plan"}
            payload["nodes"] = [row for row in payload["nodes"] if row["id"] not in removed]
            payload["edges"] = [row for row in payload["edges"]
                                if row["from_id"] not in removed and row["to_id"] not in removed]
            manifest["active_plan_id"] = None
        elif damage == "rewritten_revision":
            row = next(row for row in payload["record_revisions"] if row["version"] == 1)
            row["snapshot"]["content"] = "A different historical decision."
        else:
            row = next(row for row in payload["sources"] if row["id"] == fixture["first"]["source_ids"][0])
            row["content"] = "Changed source with a recalculated raw-content hash."
            row["sha256"] = hashlib.sha256(row["content"].encode()).hexdigest()

    path = rewrite_bundle(tmp_path, published, mutate)
    assert inspect_snapshot(path)["status"] == "valid"
    before = database_state(fixture["destination"])
    with pytest.raises(MemoryConflict):
        fixture["importer"].import_snapshot(path, fixture["root"])
    assert database_state(fixture["destination"]) == before


@pytest.mark.parametrize("damage", ["foreign_node", "missing_endpoint", "foreign_edge_source"])
def test_valid_checksum_invalid_graph_references_reject_before_import(fixture, tmp_path, damage):
    published = successor(fixture)

    def mutate(manifest, payload):
        if damage == "foreign_node":
            payload["nodes"][0]["project_id"] = str(uuid4())
        elif damage == "missing_endpoint":
            payload["edges"][0]["to_id"] = "node-from-another-project"
        else:
            payload["edges"][0]["source_id"] = str(uuid4())

    path = rewrite_bundle(tmp_path, published, mutate)
    before = database_state(fixture["destination"])
    with pytest.raises(ValueError):
        fixture["importer"].import_snapshot(path, fixture["root"])
    assert database_state(fixture["destination"]) == before


@pytest.mark.parametrize("damage", ["invalid_state", "dependency_cycle"])
def test_valid_checksum_task_metadata_cannot_poison_restored_workflow(fixture, tmp_path, damage):
    source, project_id = fixture["source"], fixture["project"]["id"]
    first = source.remember(project_id, {"kind": "task", "key": "a", "content": "Task A"},
                            fixture["first"]["source_ids"])
    second = source.remember(project_id, {"kind": "task", "key": "b", "content": "Task B"},
                             fixture["first"]["source_ids"])
    published = fixture["manager"].snapshot(project_id)

    def mutate(manifest, payload):
        for identifier, dependency in ((first["id"], second["id"]), (second["id"], first["id"])):
            row = next(row for row in payload["records"] if row["id"] == identifier)
            if damage == "invalid_state":
                row["metadata"]["state"] = "invented_undefined_state"
            else:
                row["metadata"]["depends_on"] = [dependency]
            revision = next(item for item in payload["record_revisions"]
                            if item["record_id"] == identifier and item["version"] == row["version"])
            revision["snapshot"] = deepcopy(row)

    path = rewrite_bundle(tmp_path, published, mutate)
    before = database_state(fixture["destination"])
    with pytest.raises(ValueError):
        fixture["importer"].import_snapshot(path, fixture["root"])
    assert database_state(fixture["destination"]) == before


def test_repeat_snapshot_cannot_silently_ignore_a_different_target_root(fixture, tmp_path):
    alternate = tmp_path / "alternate-target"
    alternate.mkdir()
    before = database_state(fixture["destination"])
    with pytest.raises(MemoryConflict):
        fixture["importer"].import_snapshot(fixture["initial"]["path"], alternate)
    assert database_state(fixture["destination"]) == before


def test_alternating_computers_advance_the_latest_shared_lineage(fixture):
    project_id = fixture["project"]["id"]
    source, destination = fixture["source"], fixture["destination"]
    record_id = fixture["first"]["id"]

    def edit(store, text):
        record = next(row for row in store.list_records(project_id) if row["id"] == record_id)
        return store.remember(project_id, {
            "id": record_id, "expected_version": record["version"], "content": text,
        }, record["source_ids"])

    edit(destination, "Decision made on computer B.")
    second = fixture["importer"].snapshot(project_id)
    assert second["parent_id"] == fixture["initial"]["snapshot_id"]
    assert fixture["manager"].import_snapshot(second["path"], fixture["project"]["root"])["status"] == "imported"
    edit(source, "Following decision made on computer A.")
    third = fixture["manager"].snapshot(project_id)
    assert third["parent_id"] == second["snapshot_id"]
    assert fixture["importer"].import_snapshot(third["path"], fixture["root"])["status"] == "imported"
    restored = next(row for row in destination.list_records(project_id) if row["id"] == record_id)
    assert restored["content"] == "Following decision made on computer A."
    assert restored["version"] == 4


def test_fast_forward_removes_obsolete_dependency_and_source_edges(fixture):
    source, target = fixture["source"], fixture["destination"]
    project_id = fixture["project"]["id"]
    evidence = fixture["first"]["source_ids"]
    first = source.remember(project_id, {"kind": "task", "key": "first", "content": "First task"}, evidence)
    second = source.remember(project_id, {"kind": "task", "key": "second", "content": "Second task",
        "metadata": {"depends_on": [first["id"]]}}, evidence)
    initial = fixture["manager"].snapshot(project_id)
    fixture["importer"].import_snapshot(initial["path"], fixture["root"])
    replacement = source.snapshot(project_id, "note://new-evidence", "An independent task with new evidence.", "note")
    source.remember(project_id, {"id": second["id"], "expected_version": second["version"],
        "metadata": {"depends_on": []}}, [replacement["id"]])
    updated = fixture["manager"].snapshot(project_id)
    fixture["importer"].import_snapshot(updated["path"], fixture["root"])
    scope = {"id": f"record:{second['id']}"}
    assert target.query("SELECT id FROM edges WHERE from_id=:id AND kind='depends_on'", scope) == []
    links = target.query("SELECT to_id FROM edges WHERE from_id=:id AND kind='supported_by'", scope)
    assert {row["to_id"] for row in links} == {f"source:{replacement['id']}"}


def test_fast_forward_archived_plan_step_is_not_reactivated(fixture):
    source, target = fixture["source"], fixture["destination"]
    project_id = fixture["project"]["id"]
    plans = PlanManager(source)
    saved = plans.save(project_id, {"title": "Plan with removable step"}, [
        {"key": "keep", "content": "Keep this step"},
        {"key": "remove", "content": "Archive this step"},
    ], "removal-v1", activate=True)
    initial = fixture["manager"].snapshot(project_id)
    fixture["importer"].import_snapshot(initial["path"], fixture["root"])
    removed = next(row for row in saved["tasks"] if row["metadata"]["step_key"] == "remove")
    plans.save(project_id, {"id": saved["plan"]["id"], "title": "Plan after removal"}, [
        {"key": "keep", "content": "Keep this step"},
    ], "removal-v2", expected_version=saved["plan"]["version"],
        task_versions={row["id"]: row["version"] for row in saved["tasks"]})
    updated = fixture["manager"].snapshot(project_id)
    fixture["importer"].import_snapshot(updated["path"], fixture["root"])
    assert target.query("SELECT id FROM edges WHERE kind='has_step' AND to_id=:id",
                        {"id": f"record:{removed['id']}"}) == []
    node = target.query("SELECT is_current FROM nodes WHERE id=:id", {"id": f"record:{removed['id']}"})[0]
    assert node["is_current"] is False
    archived = target.query("SELECT status FROM records WHERE id=:id AND project_id=:project",
                            {"id": removed["id"], "project": project_id})[0]
    assert archived["status"] == "superseded"


def test_automatic_publication_stops_at_external_valid_manifest(fixture, tmp_path):
    origin, manager, project = fixture["source"], fixture["manager"], fixture["project"]
    manager.configure_auto(project["id"], True, interval_minutes=15)
    now = portable.datetime.now(portable.timezone.utc).timestamp()
    first_tick = manager.tick(now=now)
    assert not first_tick["errors"]
    head = Path(fixture["initial"]["path"])
    manifest = portable._parse(head.read_bytes())
    manifest["external_label"] = "Different, syntactically valid package from another checkout."
    manifest["snapshot_id"] = portable._manifest_id(manifest)
    head.write_bytes(portable._bytes(manifest))
    assert inspect_snapshot(head)["status"] == "valid"
    external = head.read_bytes()
    current = next(row for row in origin.list_records(project["id"]) if row["id"] == fixture["second"]["id"])
    origin.remember(project["id"], {
        "id": current["id"], "expected_version": current["version"], "content": "New local decision.",
    }, current["source_ids"])
    result = manager.tick(now=now + 901)
    assert result["errors"]
    assert head.read_bytes() == external
    assert manager.status(project["id"])["auto"]["error"]


def test_interrupted_manifest_switch_keeps_last_complete_snapshot(fixture, monkeypatch):
    manager, project = fixture["manager"], fixture["project"]
    path = Path(fixture["initial"]["path"])
    previous = path.read_bytes()
    original_write = portable._atomic_write

    def fail_head(target, raw):
        if target == path:
            raise OSError("Simulated interrupted final pointer switch")
        return original_write(target, raw)

    current = fixture["second"]
    fixture["source"].remember(project["id"], {
        "id": current["id"], "expected_version": current["version"], "content": "New unsaved state.",
    }, current["source_ids"])
    monkeypatch.setattr(portable, "_atomic_write", fail_head)
    with pytest.raises(OSError, match="interrupted"):
        manager.snapshot(project["id"])
    assert path.read_bytes() == previous
    assert inspect_snapshot(path)["snapshot_id"] == fixture["initial"]["snapshot_id"]


def test_published_snapshot_without_receipt_requires_explicit_safe_adoption(fixture, monkeypatch):
    source, manager = fixture["source"], fixture["manager"]
    project_id = fixture["project"]["id"]
    current = fixture["second"]
    source.remember(project_id, {
        "id": current["id"], "expected_version": current["version"],
        "content": "A new decision published immediately before a simulated crash.",
    }, current["source_ids"])
    path = Path(fixture["initial"]["path"])

    def fail_receipt(*args, **kwargs):
        raise OSError("Simulated crash after manifest publication and before receipt")

    before = database_state(source)
    with monkeypatch.context() as patch:
        patch.setattr(manager, "_receipt", fail_receipt)
        with pytest.raises(OSError, match="before receipt"):
            manager.snapshot(project_id)
    published = inspect_snapshot(path)
    assert published["snapshot_id"] != fixture["initial"]["snapshot_id"]
    assert database_state(source) == before
    with pytest.raises(MemoryConflict):
        manager.snapshot(project_id)
    assert inspect_snapshot(path)["snapshot_id"] == published["snapshot_id"]
    assert database_state(source) == before

    result = manager.import_snapshot(path, fixture["project"]["root"])
    assert result["status"] == "imported"
    after = database_state(source)
    for table in ("records", "record_revisions", "plans", "plan_revisions", "sources", "check_runs",
                  "sessions", "resume_packets", "resume_acks", "jobs", "import_cursors"):
        assert after[table] == before[table]
    assert manager.snapshot(project_id)["status"] == "unchanged"
    assert inspect_snapshot(path)["snapshot_id"] == published["snapshot_id"]


def test_divergent_manual_source_heads_conflict_without_record_changes(fixture):
    source, remote = fixture["source"], fixture["destination"]
    project_id = fixture["project"]["id"]
    record_heads = deepcopy(source.list_records(project_id))
    local = source.snapshot(project_id, "note://decision", "A new source revision only on computer A.", "note")
    remote.snapshot(project_id, "note://decision", "A different source revision only on computer B.", "note")
    published = fixture["importer"].snapshot(project_id)
    assert source.list_records(project_id) == record_heads
    before = database_state(source)
    with pytest.raises(MemoryConflict):
        fixture["manager"].import_snapshot(published["path"], fixture["project"]["root"])
    assert database_state(source) == before
    heads = source.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                         {"project": project_id, "uri": "note://decision"})
    assert [row["id"] for row in heads] == [local["id"]]


def test_local_active_plan_focus_change_blocks_remote_focus_overwrite(fixture):
    source, remote = fixture["source"], fixture["destination"]
    project_id = fixture["project"]["id"]
    plans = PlanManager(source)
    primary = plans.save(project_id, {"title": "Initially selected plan"}, [], "primary-plan", activate=True)
    baseline = fixture["manager"].snapshot(project_id)
    fixture["importer"].import_snapshot(baseline["path"], fixture["root"])
    assert baseline["active_plan_id"] == primary["plan"]["id"]
    unchanged_records = deepcopy(source.list_records(project_id))
    plans.activate(project_id, fixture["plan"]["id"], fixture["project"]["root"])
    assert source.list_records(project_id) == unchanged_records

    record = next(row for row in remote.list_records(project_id) if row["id"] == fixture["first"]["id"])
    remote.remember(project_id, {"id": record["id"], "expected_version": record["version"],
        "content": "A remote continuation that retains its original plan focus."}, record["source_ids"])
    published = fixture["importer"].snapshot(project_id)
    assert published["active_plan_id"] == primary["plan"]["id"]
    before = database_state(source)
    with pytest.raises(MemoryConflict):
        fixture["manager"].import_snapshot(published["path"], fixture["project"]["root"])
    assert database_state(source) == before
    focus = source.query("SELECT active_plan_id FROM worktree_states WHERE project_id=:project AND worktree=:root",
                         {"project": project_id, "root": fixture["project"]["root"]})[0]
    assert focus["active_plan_id"] == fixture["plan"]["id"]
