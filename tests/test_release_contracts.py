"""Cross-release upgrade, legacy file compatibility, and frozen JSON/MCP contract tests (#249)."""
from __future__ import annotations

import asyncio
import copy
import gzip
import json
from contextlib import closing
from pathlib import Path

import pytest
from contract_store_fixture import (
    assert_matches_frozen_example,
    build_contract_environment,
    capture_cli_json_contracts,
    capture_mcp_tool_contracts,
    run_contract_service,
)

from cairnmind.config import Settings
from cairnmind.memory.memory import MemoryManager, inspect_snapshot
from cairnmind.memory.memory_published_check import check_directory
from cairnmind.memory.memory_published_import import PublishedImporter
from cairnmind.storage.arcade import ArcadeClient
from cairnmind.storage.sql import insert_document, quote_name
from cairnmind.store import EXPORT_TYPES, SCHEMA_VERSION, Store

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"
RELEASES_DIR = FIXTURES_ROOT / "releases"
CONTRACTS_DIR = FIXTURES_ROOT / "contracts"


def _release_dirs() -> list[Path]:
    return sorted(p for p in RELEASES_DIR.iterdir() if p.is_dir() and (p / "manifest.json").is_file())


def _published_release_dirs() -> list[Path]:
    return [p for p in _release_dirs() if (p / "backup.cairnmind.json.gz").is_file()]


def _load_raw_release_database(settings: Settings, release_dir: Path) -> dict:
    """Recreate an older release's exact raw ArcadeDB state into an empty test database."""
    raw = json.loads(gzip.decompress((release_dir / "raw_store.json.gz").read_bytes()).decode("utf-8"))
    tables: dict[str, list[dict]] = raw["tables"]
    with closing(ArcadeClient(settings)) as client:
        client.ensure_database()
        existing = set(tables)
        stmts = ["CREATE DOCUMENT TYPE devbrain_meta IF NOT EXISTS"]
        for name in sorted(existing - {"devbrain_meta", "nodes", "edges"}):
            stmts.append(f"CREATE DOCUMENT TYPE {quote_name(name)} IF NOT EXISTS")
        if "nodes" in existing:
            stmts.append("CREATE VERTEX TYPE nodes IF NOT EXISTS")
        if "edges" in existing:
            stmts.append("CREATE EDGE TYPE edges IF NOT EXISTS")
        for collection in sorted(existing & set(EXPORT_TYPES)):
            if collection in {"nodes", "edges"}:
                stmts.append(f"CREATE PROPERTY {collection}.id IF NOT EXISTS STRING")
                stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}(id) UNIQUE NULL_STRATEGY SKIP")
            else:
                stmts.append(f"CREATE PROPERTY {collection}.id IF NOT EXISTS STRING")
                stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}(id) UNIQUE NULL_STRATEGY SKIP")
            if collection != "projects":
                stmts.append(f"CREATE PROPERTY {collection}.project_id IF NOT EXISTS STRING")
                stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}(project_id) NOTUNIQUE NULL_STRATEGY SKIP")
        for collection, field in (("projects", "name"), ("projects", "root"), ("project_roots", "path"),
                                  ("sources", "current_key"), ("records", "active_key"), ("jobs", "job_key"),
                                  ("import_cursors", "path")):
            if collection in existing:
                stmts.append(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) UNIQUE NULL_STRATEGY SKIP")
        for collection in ("sources", "chunks", "nodes", "edges"):
            if collection in existing:
                stmts.append(f"CREATE PROPERTY {collection}.is_current IF NOT EXISTS BOOLEAN")
        for collection, field in (("chunks", "text"), ("records", "content")):
            if collection in existing:
                stmts.append(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                stmts.append(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) FULL_TEXT METADATA {{similarity:'BM25'}}")
        if "journal_events" in existing:
            stmts.append("CREATE PROPERTY journal_events.epoch IF NOT EXISTS STRING")
            stmts.append("CREATE PROPERTY journal_events.seq IF NOT EXISTS LONG")
            stmts.append("CREATE INDEX IF NOT EXISTS ON journal_events(epoch,seq) UNIQUE")
        client.script(";\n".join(stmts))

        with client.transaction() as tx:
            for table, rows in tables.items():
                if table in {"nodes", "edges"} or not rows:
                    continue
                for row in rows:
                    insert_document(tx, table, row)
            for node in tables.get("nodes") or []:
                tx.command(
                    "CREATE VERTEX nodes SET " + ", ".join(f"{quote_name(k)}=:{k}" for k in node),
                    node,
                )
            for edge in tables.get("edges") or []:
                props = {k: v for k, v in edge.items() if k not in {"from_id", "to_id"}}
                sets = ", ".join(f"{quote_name(k)}=:{k}" for k in props)
                tx.command(
                    "CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:from_id) "
                    "TO (SELECT FROM nodes WHERE id=:to_id)"
                    + (f" SET {sets}" if sets else ""),
                    edge,
                )
    return raw


def _normalize_export_rows(exported: dict) -> dict[str, dict[str, dict]]:
    """Index exported rows by collection and stable row ID for lossless comparison across upgrades."""
    by_table: dict[str, dict[str, dict]] = {}
    for collection in EXPORT_TYPES:
        rows = exported.get(collection) or []
        by_table[collection] = {row["id"]: row for row in rows if isinstance(row, dict) and "id" in row}
    return by_table


@pytest.mark.parametrize("release_dir", _published_release_dirs(), ids=lambda p: p.name)
def test_old_release_portable_and_published_trees_open_cleanly(release_dir: Path):
    """Fast store-free check: portable memory packages and published directories from older releases open cleanly."""
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    inspected = inspect_snapshot(release_dir / "portable_memory")
    assert inspected["status"] == "valid"
    assert inspected["format_version"] == 1
    assert inspected["project"]["id"] == manifest["project_id"]
    assert inspected["counts"]["records"] >= 2

    if manifest["has_published_memory"]:
        tree, errors = check_directory(release_dir / "published_memory")
        assert errors == [], (release_dir.name, errors)
        assert tree["records"]


@pytest.mark.arcade
@pytest.mark.no_schema_template
@pytest.mark.parametrize("release_dir", _release_dirs(), ids=lambda p: p.name)
def test_upgrade_from_published_release_preserves_every_row_and_records_backup(tmp_path, release_dir: Path):
    """Upgrade stores created by every published release (and schema_v3) without losing data or repeating work."""
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    data_dir = tmp_path / "data"
    settings = Settings.load(data_dir, create=True)
    _load_raw_release_database(settings, release_dir)

    store = Store(data_dir)
    meta = {
        row["name"]: row["value"]
        for row in store.query("SELECT FROM devbrain_meta WHERE name IN ['schema_version','lookup_indexes']")
    }
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["lookup_indexes"] == 1

    # Schema upgrades from older schema versions must create exactly one safety backup before altering DDL.
    before_backups = list((data_dir / "backups").glob("before-schema-*.cairnmind.json.gz")) if (data_dir / "backups").exists() else []
    if manifest["schema_version"] < SCHEMA_VERSION:
        assert len(before_backups) == 1
        validated = Store._validated_backup(before_backups[0])
        assert validated["projects"]

    # Export after upgrade must retain every row present in expected_export.json.
    expected = json.loads((release_dir / "expected_export.json").read_text(encoding="utf-8"))
    actual = store.export()
    exp_by_table = _normalize_export_rows(expected)
    act_by_table = _normalize_export_rows(actual)
    for collection in EXPORT_TYPES:
        missing_ids = sorted(set(exp_by_table[collection]) - set(act_by_table[collection]))
        assert not missing_ids, f"{release_dir.name} lost rows in {collection}: {missing_ids}"
        if collection == "records":
            for rid, exp_row in exp_by_table[collection].items():
                act_row = act_by_table[collection][rid]
                for field in ("id", "project_id", "kind", "key", "content", "status", "source_ids", "version"):
                    assert act_row[field] == exp_row[field], (release_dir.name, rid, field)

    # Opening a second time upgrades nothing again and creates no duplicate safety backup.
    store2 = Store(data_dir)
    assert store2.confirmations_before_rule is None
    if (data_dir / "backups").exists():
        after_backups = list((data_dir / "backups").glob("before-schema-*.cairnmind.json.gz"))
        assert len(after_backups) == len(before_backups)


@pytest.mark.arcade
@pytest.mark.parametrize("release_dir", _published_release_dirs(), ids=lambda p: p.name)
def test_old_release_backup_portable_and_published_files_import_into_current_tree(tmp_path, release_dir: Path):
    """Logical backups, portable .memory packages, and published team memory from older releases load into main."""
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    expected = json.loads((release_dir / "expected_export.json").read_text(encoding="utf-8"))
    expected_records = _normalize_export_rows(expected)["records"]

    # 1. Restore logical backup into a fresh store
    backup_dir = tmp_path / "restore-data"
    restored_store = Store(backup_dir)
    restored_store.restore(release_dir / "backup.cairnmind.json.gz")
    actual_records = _normalize_export_rows(restored_store.export())["records"]
    assert set(expected_records) <= set(actual_records)

    # 2. Import portable memory snapshot into an empty matching project folder
    portable_data = tmp_path / "portable-data"
    portable_store = Store(portable_data)
    target_root = tmp_path / "imported-repo"
    target_root.mkdir()
    imported = MemoryManager(portable_store).import_snapshot(
        release_dir / "portable_memory",
        root=target_root,
        name="restored-portable",
    )
    assert imported["counts"]["records"] >= len(expected_records)

    # 3. Import published team memory (0.11.0+)
    if manifest["has_published_memory"]:
        pub_data = tmp_path / "pub-data"
        pub_store = Store(pub_data)
        pub_root = tmp_path / "pub-repo"
        pub_root.mkdir()
        proj = pub_store.add_project("pub-proj", pub_root)
        pub_res = PublishedImporter(pub_store).import_published(
            release_dir / "published_memory",
            proj["id"],
            bind=True,
        )
        assert len(pub_res["inserted"]) >= 1


@pytest.mark.arcade
def test_frozen_cli_json_and_mcp_tool_examples_match_contract(tmp_path):
    """CLI --json outputs and MCP tool responses on a fixed store match frozen contract examples (#249)."""
    cli_dir = CONTRACTS_DIR / "cli_json"
    mcp_dir = CONTRACTS_DIR / "mcp_tools"
    cli_files = sorted(cli_dir.glob("*.json"))
    mcp_files = sorted(mcp_dir.glob("*.json"))
    assert cli_files, "Frozen CLI JSON examples are missing"
    assert len(mcp_files) == 9, f"Expected 9 frozen MCP tool captures (8 tools + short resume), found {len(mcp_files)}"

    env = build_contract_environment(tmp_path)
    with run_contract_service(env["settings"]):
        cli_outputs = capture_cli_json_contracts(env)
        mcp_outputs = asyncio.run(capture_mcp_tool_contracts(env))

    assert {p.stem for p in cli_files} == set(cli_outputs)
    assert {p.stem for p in mcp_files} == set(mcp_outputs)

    for name, payload in cli_outputs.items():
        assert_matches_frozen_example(payload, cli_dir / f"{name}.json", env, command_name=name)
    for name, payload in mcp_outputs.items():
        assert_matches_frozen_example(payload, mcp_dir / f"{name}.json", env, command_name=name)


def test_removing_field_from_cli_json_or_mcp_answer_fails(tmp_path):
    """Self-test required by #249: removing a field from a frozen CLI JSON or MCP answer fails the check."""
    fake_env = {
        "tmp_path": tmp_path,
        "data_dir": tmp_path / "data",
        "repo": tmp_path / "repo",
        "converter": tmp_path / "conv.py",
        "foreign_log": tmp_path / "log.acme",
        "settings": Settings.load(tmp_path / "data"),
    }

    cli_golden = CONTRACTS_DIR / "cli_json" / "status.json"
    cli_sample = copy.deepcopy(json.loads(cli_golden.read_text(encoding="utf-8")))
    assert_matches_frozen_example(cli_sample, cli_golden, fake_env, command_name="status")
    dropped_cli_key = next(iter(cli_sample))
    cli_sample.pop(dropped_cli_key)
    with pytest.raises(AssertionError, match=f"removed keys.*{dropped_cli_key}"):
        assert_matches_frozen_example(cli_sample, cli_golden, fake_env, command_name="status")

    mcp_golden = CONTRACTS_DIR / "mcp_tools" / "ask.json"
    mcp_sample = copy.deepcopy(json.loads(mcp_golden.read_text(encoding="utf-8")))
    assert_matches_frozen_example(mcp_sample, mcp_golden, fake_env, command_name="ask")
    dropped_mcp_key = next(iter(mcp_sample))
    mcp_sample.pop(dropped_mcp_key)
    with pytest.raises(AssertionError, match=f"removed keys.*{dropped_mcp_key}"):
        assert_matches_frozen_example(mcp_sample, mcp_golden, fake_env, command_name="ask")
