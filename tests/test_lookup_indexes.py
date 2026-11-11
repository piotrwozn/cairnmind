"""Indexes for lookups by a field other than id and project_id (issue #71)."""
from __future__ import annotations

from cairnmind.arcade import ArcadeClient
from cairnmind.store import LOOKUP_INDEX_SET, LOOKUP_INDEXES, SCHEMA_VERSION, Store


def indexed(store):
    """The (type, field) pairs that have an index of their own."""
    rows = store.query("SELECT FROM schema:indexes")
    def flat(value):
        return tuple(part for item in value for part in flat(item)) if isinstance(value, (list, tuple)) else (value,)
    return {(flat(row.get("typeName"))[0], flat(row.get("properties") or ())): row for row in rows}


def marker(store):
    rows = store.query("SELECT value FROM devbrain_meta WHERE name='lookup_indexes'")
    return [row["value"] for row in rows]


def test_a_new_database_has_every_lookup_index(tmp_path):
    store = Store(tmp_path / "memory")
    found = indexed(store)
    missing = [pair for pair in LOOKUP_INDEXES if (pair[0], (pair[1],)) not in found]
    assert not missing, (missing, sorted(found, key=str))
    assert marker(store) == [LOOKUP_INDEX_SET]
    # They are not a schema step: the version an earlier release checks is unchanged.
    assert store.query("SELECT value FROM devbrain_meta WHERE name='schema_version'")[0]["value"] == SCHEMA_VERSION == 4


def test_a_database_of_an_earlier_release_gets_them_when_it_is_opened_and_only_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    project = store.add_project("p", tmp_path)
    source = store.snapshot(project["id"], "note://one", "kept text", "note")
    # As an earlier release left it: no marker and no index on these fields.
    store.command("DELETE FROM devbrain_meta WHERE name='lookup_indexes'")
    found = indexed(store)
    for collection, field in LOOKUP_INDEXES:
        store.command(f"DROP INDEX `{found[(collection, (field,))]['name']}`")
    assert not [pair for pair in LOOKUP_INDEXES if (pair[0], (pair[1],)) in indexed(store)]

    commands = []
    real = ArcadeClient.command

    def recorded(self, sql, params=None):
        commands.append(sql)
        return real(self, sql, params)
    monkeypatch.setattr(ArcadeClient, "command", recorded)
    reopened = Store(tmp_path / "memory")
    assert not [pair for pair in LOOKUP_INDEXES if (pair[0], (pair[1],)) not in indexed(reopened)]
    assert marker(reopened) == [LOOKUP_INDEX_SET]
    assert sum("CREATE INDEX" in sql for sql in commands) == len(LOOKUP_INDEXES)
    assert reopened.get_source(source["id"])["content"] == "kept text"  # the data is untouched
    assert reopened.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri",
                          {"project": project["id"], "uri": "note://one"})[0]["id"] == source["id"]

    del commands[:]
    Store(tmp_path / "memory")
    assert not [sql for sql in commands if "CREATE INDEX" in sql or "CREATE PROPERTY" in sql]  # nothing to do the next time
