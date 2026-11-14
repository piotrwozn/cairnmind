"""Upgrade real schema-1 databases and archives without inventing verification."""
from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from cairnmind.arcade import ArcadeClient
from cairnmind.store import LEGACY_EXPORT_TYPES, SCHEMA_VERSION, V03_DOCUMENT_TYPES, Store
from cairnmind.sql import json_text, update_document
from cairnmind.verification import verification_status


def read_envelope(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def schema_one_fixture(tmp_path):
    """Represent an actual pre-runner task; direct DAL writes are fixture setup."""
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    project = store.add_project("legacy", root)
    evidence = store.snapshot(project["id"], "legacy://review", "A model reported tests passed", "transcript")
    task = store.remember(project["id"], {"kind": "task", "key": "legacy-step", "content": "Legacy completed work",
        "metadata": {"state": "done", "plan_id": "release alpha", "test_passed": True,
                     "test_source_ids": [evidence["id"]]}}, [evidence["id"]])
    # Schema 1 accepted reported booleans as completion evidence. Construct that
    # historical state directly, without using a production completion bypass.
    task["metadata"]["requires_test"] = True
    with store.transaction() as tx:
        update_document(tx, "records", {"metadata": task["metadata"]}, "id=:id", {"id": task["id"]})
        update_document(tx, "record_revisions", {"snapshot": task}, "record_id=:id", {"id": task["id"]})
        store._record_graph(tx, task)
    for collection in reversed(V03_DOCUMENT_TYPES):
        store.command(f"DROP TYPE {collection}")
    store.command("UPDATE devbrain_meta SET value=1 WHERE name='schema_version'")
    return store, project, task, evidence


def legacy_archive(store, destination):
    with store.transaction() as tx:
        payload = store._export_snapshot(tx, None, LEGACY_EXPORT_TYPES, 2)
    return Store._write_backup(destination, payload)


def assert_legacy_preserved(store, project, task, evidence):
    current = next(row for row in store.list_records(project["id"]) if row["id"] == task["id"])
    assert current == task
    assert store.get_source(evidence["id"]) == evidence
    assert store.record_history(task["id"]) == [task]
    assert current["metadata"]["plan_id"] == "release alpha"
    assert verification_status(store, current)["status"] == "reported"
    assert not store.query("SELECT FROM plans")
    assert not store.query("SELECT FROM check_runs")
    assert store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION


def test_schema_one_backup_precedes_new_ddl_and_preserves_legacy(tmp_path, monkeypatch):
    original, project, task, evidence = schema_one_fixture(tmp_path)
    command = ArcadeClient.command
    observed = []

    def inspect_ddl(client, sql, params=None):
        if sql.startswith("CREATE DOCUMENT TYPE plans "):
            backups = list((original.data_dir / "backups").glob("before-schema-1-to-4-*.gz"))
            assert len(backups) == 1
            envelope = read_envelope(backups[0])
            assert envelope["sha256"] == hashlib.sha256(json_text(envelope["payload"]).encode()).hexdigest()
            assert envelope["payload"]["format_version"] == 2
            assert not set(V03_DOCUMENT_TYPES).intersection(envelope["payload"])
            assert envelope["payload"]["records"] == [task]
            assert client.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == 1
            observed.append(True)
        return command(client, sql, params)

    monkeypatch.setattr(ArcadeClient, "command", inspect_ddl)
    migrated = Store(original.data_dir)
    assert observed == [True]
    assert_legacy_preserved(migrated, project, task, evidence)
    assert migrated.export()["format_version"] == 5
    for collection in V03_DOCUMENT_TYPES:
        assert migrated.export()[collection] == []
    monkeypatch.setattr(ArcadeClient, "command", command)
    Store(original.data_dir)
    assert len(list((original.data_dir / "backups").glob("before-schema-1-to-4-*.gz"))) == 1


def test_interrupted_additive_migration_restarts_with_version_written_last(tmp_path, monkeypatch):
    original, project, task, evidence = schema_one_fixture(tmp_path)
    command = ArcadeClient.command

    def interrupt(client, sql, params=None):
        if sql.startswith("CREATE PROPERTY plans.project_id "):
            raise RuntimeError("simulated process interruption between additive DDL statements")
        return command(client, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(ArcadeClient, "command", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            Store(original.data_dir)
    assert original.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == 1
    backups = list((original.data_dir / "backups").glob("before-schema-1-to-4-*.gz"))
    assert len(backups) == 1
    migrated = Store(original.data_dir)
    assert_legacy_preserved(migrated, project, task, evidence)
    assert len(list((original.data_dir / "backups").glob("before-schema-1-to-4-*.gz"))) == 2
    assert migrated.query("SELECT count(*) AS total FROM records")[0]["total"] == 1


def test_backup_failure_prevents_additive_ddl_and_version_change(tmp_path, monkeypatch):
    original, project, task, evidence = schema_one_fixture(tmp_path)
    command = ArcadeClient.command
    ddl = []

    def record_ddl(client, sql, params=None):
        if sql.startswith("CREATE DOCUMENT TYPE plans "):
            ddl.append(sql)
        return command(client, sql, params)

    def fail_backup(destination, payload):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(ArcadeClient, "command", record_ddl)
        patch.setattr(Store, "_write_backup", staticmethod(fail_backup))
        with pytest.raises(OSError, match="disk full"):
            Store(original.data_dir)
    assert ddl == []
    assert original.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == 1
    assert_legacy_preserved(Store(original.data_dir), project, task, evidence)


def test_legacy_payload_two_restore_preserves_groups_flags_and_evidence(tmp_path):
    original, project, task, evidence = schema_one_fixture(tmp_path)
    archive = legacy_archive(original, tmp_path / "legacy.devbrain.json.gz")
    target = Store(tmp_path / "restored")
    target.restore(archive)
    assert_legacy_preserved(target, project, task, evidence)
    for collection in V03_DOCUMENT_TYPES:
        assert target.export()[collection] == []
    # Conversion in memory cannot change the original envelope or its checksum.
    assert read_envelope(archive)["payload"]["format_version"] == 2


def test_legacy_payload_checksum_checked_before_conversion_and_mutation(tmp_path):
    original, project, task, evidence = schema_one_fixture(tmp_path)
    archive = legacy_archive(original, tmp_path / "legacy-tampered.devbrain.json.gz")
    envelope = read_envelope(archive)
    envelope["payload"]["records"][0]["metadata"]["plan_id"] = "tampered group"
    with gzip.open(archive, "wt", encoding="utf-8") as stream:
        json.dump(envelope, stream)
    target = Store(tmp_path / "restored")
    sentinel = target.add_project("keep", tmp_path / "keep")
    with pytest.raises(ValueError, match="checksum"):
        target.restore(archive)
    assert target.list_projects() == [sentinel]
    assert not (target.data_dir / "backups").exists()
    assert original.get_source(evidence["id"])["content"] == evidence["content"]


def test_fresh_schema_four_needs_no_migration_backup_and_exports_new_collections(tmp_path):
    store = Store(tmp_path / "fresh")
    assert store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION
    payload = store.export()
    assert payload["format_version"] == 5
    assert all(payload[collection] == [] for collection in V03_DOCUMENT_TYPES)
    assert not (store.data_dir / "backups").exists()

