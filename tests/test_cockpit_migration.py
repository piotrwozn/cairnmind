"""Schema-3 migration captures the old backup before any journal DDL."""
from __future__ import annotations

import gzip
import json

import pytest

from cairnmind.arcade import ArcadeClient
from cairnmind.journal import Journal
from cairnmind.store import SCHEMA_VERSION, V04_EXPORT_TYPES, V05_DOCUMENT_TYPES, Store


def schema_three(tmp_path):
    store = Store(tmp_path / "data")
    project = store.add_project("preserve", tmp_path / "repo")
    source = store.snapshot(project["id"], "manual://original", "Unchanged evidence")
    record = store.remember(project["id"], {"kind": "fact", "content": "Unchanged evidence"}, [source["id"]])
    for table in V05_DOCUMENT_TYPES:
        store.command(f"DROP TYPE {table}")
    store.command("UPDATE devbrain_meta SET value=3 WHERE name='schema_version'")
    return store, project, source, record


def test_schema_three_backup_precedes_additive_ddl_and_initializes_cursor(tmp_path, monkeypatch):
    original, project, source, record = schema_three(tmp_path)
    command, observed = ArcadeClient.command, []

    def inspect(client, sql, params=None):
        if sql.startswith("CREATE DOCUMENT TYPE journal_state "):
            archives = list((original.data_dir / "backups").glob("before-schema-3-to-4-*.gz"))
            assert len(archives) == 1
            with gzip.open(archives[0], "rt") as stream:
                payload = json.load(stream)["payload"]
            assert payload["format_version"] == 4
            assert not set(V05_DOCUMENT_TYPES).intersection(payload)
            assert payload["records"] == [record]
            assert client.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == 3
            observed.append(True)
        return command(client, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(ArcadeClient, "command", inspect)
        store = Store(original.data_dir)
    assert observed == [True]
    assert store.get_source(source["id"]) == source
    assert store.list_records(project["id"]) == [record]
    assert Journal(store).cursor()["seq"] == 0
    assert store.export()["format_version"] == 5
    assert store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION


def test_interrupted_ddl_restarts_without_domain_loss(tmp_path, monkeypatch):
    original, project, source, record = schema_three(tmp_path)
    command = ArcadeClient.command

    def interrupt(client, sql, params=None):
        if sql.startswith("CREATE PROPERTY journal_events.project_id "):
            raise RuntimeError("interrupted journal migration")
        return command(client, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(ArcadeClient, "command", interrupt)
        with pytest.raises(RuntimeError, match="interrupted"):
            Store(original.data_dir)
    assert original.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == 3
    migrated = Store(original.data_dir)
    assert migrated.get_source(source["id"]) == source
    assert migrated.list_records(project["id"]) == [record]
    assert Journal(migrated).cursor()["seq"] == 0


def test_old_format_four_backup_is_validated_without_modifying_original(tmp_path):
    store = Store(tmp_path / "data")
    with store.transaction() as tx:
        old = store._export_snapshot(tx, None, V04_EXPORT_TYPES, 4)
    archive = Store._write_backup(tmp_path / "old.gz", old)
    before = archive.read_bytes()
    converted = Store._validated_backup(archive)
    assert converted["format_version"] == 5
    assert all(converted[table] == [] for table in V05_DOCUMENT_TYPES)
    assert archive.read_bytes() == before
