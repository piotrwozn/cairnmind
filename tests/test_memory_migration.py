"""Additive portable-memory migration and old administrative archive support."""
from __future__ import annotations

import gzip
import json

import pytest

from cairnmind.arcade import ArcadeClient
from cairnmind.store import SCHEMA_VERSION, V03_EXPORT_TYPES, V04_DOCUMENT_TYPES, Store


def schema_two(tmp_path):
    store = Store(tmp_path / "data")
    project = store.add_project("keep", tmp_path / "repo")
    source = store.snapshot(project["id"], "manual://old", "Preserve original evidence")
    record = store.remember(project["id"], {"kind": "decision", "content": "Use original evidence"}, [source["id"]])
    for collection in V04_DOCUMENT_TYPES:
        store.command(f"DROP TYPE {collection}")
    store.command("UPDATE devbrain_meta SET value=2 WHERE name='schema_version'")
    return store, project, source, record


def test_schema_two_snapshot_precedes_ddl_and_restores_old_archive(tmp_path, monkeypatch):
    original, project, source, record = schema_two(tmp_path)
    command = ArcadeClient.command
    observed = []

    def before_ddl(client, sql, params=None):
        if sql.startswith("CREATE DOCUMENT TYPE memory_settings "):
            backups = list((original.data_dir / "backups").glob("before-schema-2-to-4-*.gz"))
            assert len(backups) == 1
            with gzip.open(backups[0], "rt") as stream:
                payload = json.load(stream)["payload"]
            assert payload["format_version"] == 3
            assert not set(V04_DOCUMENT_TYPES).intersection(payload)
            assert payload["records"] == [record]
            assert client.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == 2
            observed.append(True)
        return command(client, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(ArcadeClient, "command", before_ddl)
        migrated = Store(original.data_dir)
    assert observed == [True]
    assert migrated.get_source(source["id"]) == source
    assert migrated.list_records(project["id"]) == [record]
    assert all(migrated.export()[collection] == [] for collection in V04_DOCUMENT_TYPES)
    backup = next((original.data_dir / "backups").glob("before-schema-2-to-4-*.gz"))
    migrated.restore(backup)
    assert migrated.get_source(source["id"]) == source
    assert migrated.list_records(project["id"]) == [record]
    assert migrated.export()["format_version"] == 5
    assert migrated.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION


def test_interrupted_schema_two_upgrade_keeps_original_rows(tmp_path, monkeypatch):
    original, project, source, record = schema_two(tmp_path)
    command = ArcadeClient.command

    def interrupt(client, sql, params=None):
        if sql.startswith("CREATE PROPERTY memory_imports.project_id "):
            raise RuntimeError("interrupted portable migration")
        return command(client, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(ArcadeClient, "command", interrupt)
        with pytest.raises(RuntimeError, match="interrupted"):
            Store(original.data_dir)
    assert original.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == 2
    migrated = Store(original.data_dir)
    assert migrated.list_records(project["id"]) == [record]
    assert migrated.get_source(source["id"]) == source
    assert migrated.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION


def test_schema_two_archive_conversion_does_not_modify_archive(tmp_path):
    store = Store(tmp_path / "store")
    with store.transaction() as tx:
        payload = store._export_snapshot(tx, None, V03_EXPORT_TYPES, 3)
    archive = Store._write_backup(tmp_path / "old.gz", payload)
    before = archive.read_bytes()
    assert Store._validated_backup(archive)["format_version"] == 5
    assert archive.read_bytes() == before
