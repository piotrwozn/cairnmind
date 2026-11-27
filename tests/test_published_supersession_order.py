"""A replacement and the record it replaces reach an empty store in either order of their IDs (#239).

The importer writes the graph of its records in sorted ID order. When the replacement's ID sorts first, its
supersedes edge was written before the replaced record's node existed, and the whole import failed.
"""
from __future__ import annotations

import pytest

from cairnmind.terminal.inspector import Inspector
from cairnmind.memory.memory import MemoryManager
from cairnmind.memory.memory_published import Publisher
from cairnmind.memory.memory_published_import import PublishedImporter
from cairnmind.store import Store

from conftest import person_edit
from test_published_memory import ada, commit, folder_of, teammate  # noqa: F401 (ada is a fixture)

LOW, HIGH = "00000000-0000-4000-8000-000000000001", "ffffffff-ffff-4fff-bfff-fffffffffffe"
ORDERS = pytest.mark.parametrize("old_id,new_id", [(HIGH, LOW), (LOW, HIGH)],
                                 ids=["replacement-sorts-first", "replacement-sorts-last"])


def supersession(store, pid, old_id, new_id, monkeypatch):
    """A reviewed decision, and a person's reviewed replacement of it, with the IDs given.

    A client cannot choose the ID of a new record, so the store's own UUIDs are replaced for a new record only.
    """
    import sys
    import uuid

    import cairnmind.store as store_module
    wanted = [uuid.UUID(new_id), uuid.UUID(old_id)]

    def chosen():
        return wanted.pop() if wanted and sys._getframe(1).f_code.co_name == "_remember" else uuid.uuid4()

    monkeypatch.setattr(store_module, "uuid4", chosen)
    note = store.snapshot(pid, "note://ttl", "Sessions last a day.", "note")
    old = store.remember(pid, {"kind": "decision", "key": "ttl", "content": "Sessions last a day."}, [note["id"]])
    Inspector(store).review(pid, old["id"], "confirm", old["version"])
    source = store.snapshot(pid, "note://replacement", "Sessions last an hour.", "note")
    new = person_edit(store, pid, {"kind": "decision", "key": "ttl-2", "content": "Sessions last an hour.",
                                   "supersedes": old["id"]}, [source["id"]])
    Inspector(store).review(pid, new["id"], "confirm", new["version"])
    monkeypatch.setattr(store_module, "uuid4", uuid.uuid4)
    assert (old["id"], new["id"]) == (old_id, new_id)


def supersedes_edges(store, pid):
    return {(row["from_id"], row["to_id"]) for row in store.query(
        "SELECT from_id,to_id FROM edges WHERE project_id=:project AND kind='supersedes'", {"project": pid})}


def statuses(store, pid):
    return {row["id"]: row["status"] for row in store.list_records(pid, include_history=True)}


@ORDERS
def test_import_published_into_an_empty_store_keeps_the_supersession_whatever_the_order_of_the_ids(
        ada, old_id, new_id, monkeypatch):  # noqa: F811
    store, pid, root = ada["store"], ada["project"], ada["root"]
    supersession(store, pid, old_id, new_id, monkeypatch)
    assert Publisher(store).publish(pid)["held"]["total"] == 0
    commit(root, "Replace the day-long sessions")
    bo = teammate(ada, "bo", "Bo Lee")
    importer = PublishedImporter(bo["store"])
    importer.import_published(folder_of(bo["root"]), bo["project"], dry_run=True)
    importer.import_published(folder_of(bo["root"]), bo["project"])
    assert statuses(bo["store"], bo["project"]) == {old_id: "superseded", new_id: "confirmed"}
    assert supersedes_edges(bo["store"], bo["project"]) == {(f"record:{new_id}", f"record:{old_id}")}


@ORDERS
def test_an_archive_import_and_a_restore_keep_the_supersession_whatever_the_order_of_the_ids(
        ada, old_id, new_id, monkeypatch):  # noqa: F811
    store, pid = ada["store"], ada["project"]
    supersession(store, pid, old_id, new_id, monkeypatch)
    edge = {(f"record:{new_id}", f"record:{old_id}")}
    assert supersedes_edges(store, pid) == edge
    package = MemoryManager(store).snapshot(pid)["path"]
    elsewhere = ada["tmp"] / "elsewhere"
    elsewhere.mkdir()
    target = Store(ada["tmp"] / "archive-database")
    imported = MemoryManager(target).import_snapshot(package, elsewhere)
    target_pid = imported["project"]["id"]
    assert statuses(target, target_pid) == {old_id: "superseded", new_id: "confirmed"}
    assert supersedes_edges(target, target_pid) == edge
    backup = store.backup(ada["tmp"] / "team.cairnmind.json.gz")
    restored = Store(ada["tmp"] / "restored-database")
    restored.restore(backup)
    assert statuses(restored, pid) == {old_id: "superseded", new_id: "confirmed"}
    assert supersedes_edges(restored, pid) == edge
