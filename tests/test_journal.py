"""Same-transaction durable invalidations on the real ArcadeDB engine."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from cairnmind import journal as module
from cairnmind.arcade import ArcadeClient
from cairnmind.journal import Journal
from cairnmind.memory import MemoryManager
from cairnmind.store import Store


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store = Store(tmp_path / "data")
    project = store.add_project("journal", root)
    return store, project, root, Journal(store)


def test_domain_and_direct_sql_changes_are_coalesced_and_metadata_only(workspace):
    store, project, _, journal = workspace
    before = journal.cursor()
    with store.transaction() as tx:
        tx.command("UPDATE projects SET note=:note WHERE id=:id", {"id": project["id"], "note": "PRIVATE_CANARY"})
        tx.command("UPDATE projects SET note=:note WHERE id=:id", {"id": project["id"], "note": "PRIVATE_SECOND"})
        assert journal.cursor(tx) == before  # Nothing is published before the commit.
    changes = journal.read(before)
    assert changes["reset_required"] is False
    assert [(event["entity_type"], event["entity_id"], event["op"]) for event in changes["events"]] == [
        ("project", project["id"], "upsert")]
    assert "PRIVATE" not in json.dumps(changes)
    assert changes["cursor"]["seq"] == before["seq"] + 1
    assert Journal(Store(store.data_dir)).cursor() == journal.cursor()


def test_rollback_reads_and_final_noops_emit_nothing(workspace):
    store, project, _, journal = workspace
    before = journal.cursor()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.snapshot(project["id"], "manual://rollback", "not committed")
            raise RuntimeError("abort")
    assert journal.cursor() == before
    assert not store.query("SELECT FROM sources")
    with store.transaction() as tx:
        tx.query("SELECT FROM projects")
        tx.command("UPDATE projects SET name=:name WHERE id=:id", {"id": project["id"], "name": project["name"]})
        with store.transaction() as nested:
            assert nested is tx
    assert journal.cursor() == before
    with store.transaction() as tx:
        tx.command("UPDATE projects SET name='temporary' WHERE id=:id", {"id": project["id"]})
        tx.command("UPDATE projects SET name=:name WHERE id=:id", {"id": project["id"], "name": project["name"]})
    assert journal.cursor() == before


def test_outbox_failure_rolls_back_the_domain_and_cursor(workspace, monkeypatch):
    store, project, _, journal = workspace
    before, command = journal.cursor(), ArcadeClient.command

    def fail_event(client, sql, params=None):
        if sql.startswith("INSERT INTO journal_events "):
            raise RuntimeError("outbox unavailable")
        return command(client, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(ArcadeClient, "command", fail_event)
        with pytest.raises(RuntimeError, match="outbox"):
            store.snapshot(project["id"], "manual://atomic", "must roll back")
    assert journal.cursor() == before
    assert not store.query("SELECT id FROM sources WHERE uri='manual://atomic'")


def test_concurrent_store_writers_keep_a_single_monotonic_sequence(workspace):
    store, project, _, journal = workspace
    before = journal.cursor()

    def write(index):
        writer = Store(store.data_dir)
        with writer.transaction() as tx:
            Journal.touch(tx, project["id"], "record", f"parallel-{index}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(write, range(9)))
    events = journal.read(before)["events"]
    assert [e["seq"] for e in events] == list(range(before["seq"] + 1, before["seq"] + 10))
    assert len({e["entity_id"] for e in events}) == 9


def test_record_revision_source_and_job_paths_are_covered(workspace):
    store, project, _, journal = workspace
    before = journal.cursor()
    source = store.snapshot(project["id"], "manual://decision", "Keep source evidence")
    record = store.remember(project["id"], {"kind": "decision", "content": "Keep evidence"}, [source["id"]])
    job = store.enqueue("extract", project["id"], {"source_id": source["id"]}, "job-one")
    claimed = store.claim_job("journal-test", kinds=("extract",))
    store.finish_job(claimed["id"], worker=claimed["worker"], expected_attempts=claimed["attempts"])
    changes = journal.read(before)["events"]
    assert any(e["entity_type"] == "source" and e["entity_id"] == source["id"] for e in changes)
    assert sum(e["entity_type"] == "record" and e["entity_id"] == record["id"] for e in changes) == 1
    assert any(e["entity_type"] == "job" and e["entity_id"] == job for e in changes)
    assert all(e["project_id"] == project["id"] for e in changes)


def test_cas_and_state_predicates_emit_updates_instead_of_false_deletes(workspace):
    store, project, _, journal = workspace
    source = store.snapshot(project["id"], "manual://cas", "Evidence")
    record = store.remember(project["id"], {"kind": "fact", "content": "First"}, [source["id"]])
    job = store.enqueue("extract", project["id"], {"source_id": source["id"]}, "cas-job")
    before = journal.cursor()
    store.remember(project["id"], {"id": record["id"], "expected_version": 1, "content": "Second"}, [source["id"]])
    store.command("UPDATE jobs SET state='running' WHERE id=:id AND state='pending'", {"id": job})
    events = journal.read(before)["events"]
    assert any(e["entity_type"] == "record" and e["entity_id"] == record["id"] and e["op"] == "upsert" for e in events)
    assert any(e["entity_type"] == "job" and e["entity_id"] == job and e["op"] == "upsert" for e in events)
    assert not any(e["op"] == "delete" and e["entity_type"] in {"record", "job"} for e in events)


def test_delete_tombstones_and_insert_set_are_covered(workspace):
    store, project, _, journal = workspace
    source = store.snapshot(project["id"], "manual://chunks", "literal text")
    before = journal.cursor()
    store.command("INSERT INTO chunks SET id=:id,project_id=:project,source_id=:source,text=:text,is_current=true",
                  {"id": "chunk-set", "project": project["id"], "source": source["id"], "text": "literal text"})
    store.command("DELETE FROM chunks WHERE id=:id", {"id": "chunk-set"})
    events = journal.read(before)["events"]
    assert [(e["entity_type"], e["entity_id"], e["op"]) for e in events] == [
        ("chunk", "chunk-set", "upsert"), ("chunk", "chunk-set", "delete")]


def test_pagination_project_filter_gaps_and_retention(workspace, monkeypatch):
    store, project, root, journal = workspace
    other = store.add_project("other", root.parent / "other")
    before = journal.cursor()
    with store.transaction() as tx:
        for index in range(5):
            journal.touch(tx, project["id"], "record", f"r-{index}")
        journal.touch(tx, other["id"], "record", "other-row")
    first = journal.read(f"{before['epoch']}:{before['seq']}", project["id"], limit=2)
    second = journal.read(first["cursor"], project["id"], limit=2)
    third = journal.read(second["cursor"], project["id"], limit=2)
    assert len(first["events"]) == len(second["events"]) == 2 and len(third["events"]) == 1
    assert third["cursor"] == journal.cursor()
    assert not journal.read(third["cursor"], project["id"])["events"]
    assert journal.read({**before, "seq": journal.cursor()["seq"] + 1})["reset_required"]
    monkeypatch.setattr(module, "MAX_EVENTS", 3)
    with store.transaction() as tx:
        for index in range(4):
            journal.touch(tx, project["id"], "record", f"later-{index}")
    assert journal.read(before)["reset_required"]


def test_graph_rebuild_is_one_reset_and_unchanged_scan_is_quiet(workspace):
    from cairnmind.ingest import index_project
    store, project, root, journal = workspace
    for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Journal")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "app.py").write_text("def function():\n    return 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True, capture_output=True)
    before = journal.cursor()
    index_project(store, project["id"])
    events = journal.read(before)["events"]
    assert sum(e["entity_type"] == "graph" and e["op"] == "reset" for e in events) == 1
    assert not any(e["entity_type"] in {"node", "edge"} for e in events)
    before = journal.cursor()
    index_project(store, project["id"])
    assert journal.cursor() == before


def test_portable_import_and_forget_rotate_epoch_without_transferring_runtime(workspace, tmp_path):
    store, project, _, journal = workspace
    source = store.snapshot(project["id"], "manual://memory", "Portable fact")
    store.remember(project["id"], {"kind": "fact", "content": "Portable fact"}, [source["id"]])
    result = MemoryManager(store).snapshot(project["id"])
    manifest = json.loads(Path(result["path"]).read_text())
    assert not set(module.JOURNAL_TYPES).intersection(manifest["objects"])
    target = Store(tmp_path / "target-data")
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    target_journal = Journal(target)
    before = target_journal.cursor()
    MemoryManager(target).import_snapshot(result["path"], target_root)
    after = target_journal.cursor()
    assert after["epoch"] != before["epoch"] and after["seq"] == 1
    assert target_journal.read(before)["reset_required"]
    MemoryManager(target).import_snapshot(result["path"], target_root)
    assert target_journal.cursor() == after
    target.forget_project(project["id"])
    assert target_journal.cursor()["epoch"] != after["epoch"]


def test_restore_rotates_epoch_and_backup_five_roundtrips_domain_rows(workspace, tmp_path):
    store, project, _, journal = workspace
    source = store.snapshot(project["id"], "manual://backup", "Exact archived bytes")
    backup = store.backup(tmp_path / "backup.gz")
    before = journal.cursor()
    assert Store._validated_backup(backup)["format_version"] == 5
    store.restore(backup)
    assert store.get_source(source["id"]) == source
    assert journal.cursor()["epoch"] != before["epoch"]
    assert journal.read(before)["reset_required"]


@pytest.mark.parametrize("after", [{"epoch": "not-an-epoch", "seq": 0}, {"epoch": "bad", "seq": True}, "bad", 12])
def test_invalid_cursors_are_rejected(workspace, after):
    with pytest.raises(ValueError):
        workspace[3].read(after)
