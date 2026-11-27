"""The network drops in the middle of an indexing run, a restore or a schema upgrade (#68).

The database is reached through a relay that cuts the connection at one chosen request: before the
server sees it, or after the server answered and before the answer arrives (a commit the client
cannot know about). After each cut the store is read through a second, direct connection: it holds
whole transactions only, and the same command run again finishes the work.
"""
from __future__ import annotations

from contextlib import closing
import hashlib

import pytest

from cairnmind.scip import ingest
from cairnmind.storage.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings
from cairnmind.storage.sql import clean_row
from cairnmind.storage.restore_staged import RestoreFailed, RestoreUnfinished
from cairnmind.store import SCHEMA_VERSION, Store
from cairnmind.storage.store_readonly import StoreNotReady
from database_relay import Relay
from test_connect_only import connect, data_directory, no_engine_files, test_engine  # noqa: F401 - a fixture
from test_index_batching import dump, tree
from test_migrations import assert_legacy_preserved, schema_one_fixture


@pytest.fixture
def relay(test_engine, monkeypatch):  # noqa: F811
    port, secret = test_engine
    with Relay(port) as running:
        connect(monkeypatch, running.url, secret)
        running.engine_port, running.secret = port, secret
        yield running


def direct(relay, store) -> ArcadeClient:
    """A connection that does not pass through the relay, to see what the server really holds."""
    return ArcadeClient(Settings(data_dir=store.data_dir, arcade_url=f"http://127.0.0.1:{relay.engine_port}",
                                 arcade_password=relay.secret, arcade_database=store.settings.arcade_database))


def whole_transactions_only(client, pid) -> list[str]:
    """What must hold after any cut: every current source has all its chunks, and the journal head is the last event."""
    sources = [clean_row(row) for row in client.query("SELECT FROM sources WHERE project_id=:p AND is_current=true",
                                                      {"p": pid})]
    for source in sources:
        expected = len(list(ingest._chunks(source["content"])))
        found = client.query("SELECT count(*) AS n FROM chunks WHERE source_id=:s", {"s": source["id"]})[0]["n"]
        assert found == expected, (source["uri"], found, expected)
        assert hashlib.sha256(source["content"].encode()).hexdigest() == source["sha256"]
    head = client.query("SELECT FROM journal_state WHERE id='current'")[0]
    last = client.query("SELECT max(seq) AS seq FROM journal_events WHERE epoch=:e", {"e": head["epoch"]})[0]["seq"]
    assert head["seq"] == (last or 0), (head["seq"], last)
    return sorted(source["uri"] for source in sources)


def without_events(snapshot: dict) -> dict:
    # The graph is reported as reset by every indexing run, so a second run adds events and nothing else.
    return {key: value for key, value in snapshot.items() if key != "events"}


@pytest.mark.parametrize("after_server", [False, True], ids=["commit never arrives", "commit applied, answer lost"])
def test_indexing_cut_at_a_commit_leaves_whole_files_and_the_next_run_finishes(tmp_path, relay, after_server):
    root = tmp_path / "repo"
    tree(root)
    store = Store(data_directory(tmp_path))
    pid = store.add_project("p", root)["id"]
    relay.cut_at(2, matching=b"/api/v1/commit/", after_server=after_server)
    with pytest.raises(ArcadeError, match="is unavailable"):
        ingest.index_project(store, pid)
    with closing(direct(relay, store)) as client:
        indexed = whole_transactions_only(client, pid)
    # Two groups of files were sent: the second is there only when the server got its commit.
    assert 0 < len(indexed) < 26
    assert not client.query("SELECT FROM nodes WHERE project_id=:p AND kind='function'", {"p": pid})  # no graph yet
    relay.heal()
    result = ingest.index_project(store, pid)  # the same store object goes on
    assert not result["errors"] and result["changed"] == 26 - len(indexed)

    clean = Store(data_directory(tmp_path, "clean"))
    clean_pid = clean.add_project("p", root)["id"]
    ingest.index_project(clean, clean_pid)
    assert without_events(dump(store, pid)) == without_events(dump(clean, clean_pid))
    with closing(direct(relay, store)) as client:
        assert len(whole_transactions_only(client, pid)) == 26
    assert no_engine_files(store.data_dir)


def test_indexing_cut_in_the_middle_of_a_transaction_writes_nothing_of_it(tmp_path, relay):
    root = tmp_path / "repo"
    tree(root)
    store = Store(data_directory(tmp_path))
    pid = store.add_project("p", root)["id"]
    relay.cut_at(3, matching=b"INSERT INTO chunks")
    with pytest.raises(ArcadeError, match="is unavailable"):
        ingest.index_project(store, pid)
    with closing(direct(relay, store)) as client:
        assert whole_transactions_only(client, pid) == []
        assert client.query("SELECT count(*) AS n FROM chunks WHERE project_id=:p", {"p": pid})[0]["n"] == 0
    relay.heal()
    assert ingest.index_project(store, pid)["changed"] == 26


def contents(client) -> dict:
    return {table: sorted((row.get("id") or "", row.get("sha256") or row.get("content") or row.get("name") or "")
                          for row in client.query(f"SELECT FROM {table}"))
            for table in ("projects", "sources", "chunks", "records", "nodes")}


MARK_ROW = "SELECT FROM devbrain_meta WHERE name='restore_unfinished'"


@pytest.mark.parametrize("cut, marked, lands", [
    # The restore writes its mark first (commit 2, after the safety copy), then deletes and writes in transactions.
    ({"number": 2, "matching": b"/api/v1/commit/"}, False, "later"),
    ({"number": 2, "matching": b"/api/v1/commit/", "after_server": True}, True, "later"),
    ({"number": 3, "matching": b"DELETE FROM"}, True, "later"),
    # Rows go in groups since #219: one request is one group of rows of one type.
    ({"number": 3, "matching": b"INSERT INTO"}, True, None),
    ({"number": 2, "matching": b"INSERT INTO", "after_server": True}, True, None),
    ({"number": 1, "matching": b"CREATE VERTEX nodes CONTENT :rows"}, True, None),
    ({"number": 1, "matching": b"CREATE VERTEX nodes CONTENT :rows", "after_server": True}, True, None),
    ({"number": 1, "matching": b"CREATE EDGE edges", "after_server": True}, True, None),
    # Every row is written; the mark is not yet removed.
    ({"number": 1, "matching": b"DELETE FROM devbrain_meta WHERE", "after_server": True}, True, "saved"),
], ids=["mark never arrives", "mark applied, answer lost", "while deleting", "while inserting",
        "a group of documents applied, answer lost", "a group of vertices", "a group of vertices applied, answer lost",
        "a group of edges applied, answer lost", "the last commit never arrives"])
def test_a_restore_cut_anywhere_leaves_the_old_data_or_a_refused_store_the_next_restore_finishes(
        tmp_path, relay, cut, marked, lands):
    root = tmp_path / "repo"
    tree(root, files=6)
    data = data_directory(tmp_path)
    store = Store(data)
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    backup = store.backup(tmp_path / "saved.cairnmind.json.gz")
    with closing(direct(relay, store)) as client:
        saved = contents(client)
    (root / "pkg0" / "module_000.py").write_text("CHANGED = 'after the backup'\n")
    (root / "extra.py").write_text("def extra():\n    return 1\n")
    ingest.index_project(store, pid)
    with closing(direct(relay, store)) as client:
        later = contents(client)
    assert later != saved

    # The restore's own safety copy is the first commit; the cut comes after it. The undo meets the same cut.
    relay.cut_at(**cut)
    with pytest.raises((ArcadeError, RestoreFailed), match="is unavailable"):
        store.restore(backup)
    with closing(direct(relay, store)) as client:
        found = contents(client)
        if not marked:
            assert found == later and not client.query(MARK_ROW)
        else:
            # Part of the backup may be there: the store carries the mark, and no command opens it (#362).
            assert client.query(MARK_ROW)
            expected = {"later": later, "saved": saved}.get(lands)
            assert expected is None or found == expected
        head = client.query("SELECT FROM journal_state WHERE id='current'")[0]
        assert head["writer"] == store.installation.id
    # The safety copy of what the restore was about to replace is a whole file on this machine.
    copies = list((data / "backups").glob("before-restore-*"))
    assert len(copies) == 1 and Store._validated_backup(copies[0])["sources"]
    assert not list((data / "backups").glob(".*"))  # and no partial one

    relay.heal()
    if marked:
        with pytest.raises(RestoreUnfinished, match="did not finish"):
            Store(data)
    Store(data, for_restore=True).restore(backup)
    with closing(direct(relay, store)) as client:
        assert contents(client) == saved and not client.query(MARK_ROW)
    assert len(list((data / "backups").glob("before-restore-*"))) == (1 if marked else 2)


def test_a_schema_upgrade_cut_between_statements_keeps_the_old_version_and_restarts(tmp_path, relay):
    original, project, task, evidence = schema_one_fixture(tmp_path)
    version = "SELECT value FROM devbrain_meta WHERE name='schema_version'"
    relay.cut_at(matching=b"CREATE PROPERTY plans.project_id ")
    with pytest.raises(ArcadeError, match="is unavailable"):
        Store(original.data_dir)
    with closing(direct(relay, original)) as client:
        assert client.query(version)[0]["value"] == 1
    # The backup written before the first statement is a whole file on this machine.
    assert len(list((original.data_dir / "backups").glob("before-schema-1-to-*.gz"))) == 1
    relay.heal()
    migrated = Store(original.data_dir)
    assert_legacy_preserved(migrated, project, task, evidence)


def test_a_schema_upgrade_whose_last_commit_is_applied_but_not_answered_is_complete(tmp_path, relay):
    original, project, task, evidence = schema_one_fixture(tmp_path)
    seen = {"version_written": False}

    def the_commit_after_the_version(request: bytes) -> bool:
        if b"UPDATE" in request and b"name='schema_version'" in request:
            seen["version_written"] = True
            return False
        return seen["version_written"] and b"/api/v1/commit/" in request

    relay.cut_when(the_commit_after_the_version, after_server=True)
    with pytest.raises(ArcadeError, match="is unavailable"):
        Store(original.data_dir)
    with closing(direct(relay, original)) as client:
        # The version is written last, in the commit that was applied: every statement before it ran.
        assert client.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION
    relay.heal()
    assert_legacy_preserved(Store(original.data_dir), project, task, evidence)


@pytest.mark.no_schema_template
def test_a_new_store_cut_while_its_schema_is_created_is_not_ready_and_the_next_start_finishes(tmp_path, relay):
    data = data_directory(tmp_path)
    settings = Settings.load(data, create=True)
    # A connect-only installation creates no database: the one it was given exists and is empty.
    root = Settings(data_dir=data, arcade_url=f"http://127.0.0.1:{relay.engine_port}", arcade_password=relay.secret,
                    arcade_database=settings.arcade_database)
    with closing(ArcadeClient(root)) as client:
        client.ensure_database()
    relay.cut_at(2, matching=b"/api/v1/command/")
    with pytest.raises(ArcadeError, match="is unavailable"):
        Store(data)
    relay.heal()
    with pytest.raises(StoreNotReady, match="no schema yet"):
        Store.open_readonly(Settings.load(data))
    store = Store(data)
    assert store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION
    assert store.writer_state()["role"] == "writer"
    assert store.add_project("p", tmp_path)["name"] == "p"
