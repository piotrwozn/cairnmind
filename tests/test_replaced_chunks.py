"""Chunks of a replaced source are deleted with it; a one-off clean-up removes those older releases kept (#186).

The source itself stays, text and hash, so evidence that cites an old version can still be read and checked.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from typer.testing import CliRunner

from cairnmind import ingest
from cairnmind.cli import app
from cairnmind.ingest import _write_chunks
from cairnmind.replaced_chunks import drop_replaced_chunks
from cairnmind.retrieval import Retriever
from cairnmind.sql import clean_row
from cairnmind.store import Store
from tests.test_index_batching import rewrite, tree
from tests.test_portable_memory import destination, head_path, origin  # noqa: F401 (origin is a fixture)


class Vectors:
    async def embed(self, texts):
        return [[0.25, 0.5, 0.75, 1.0] for _ in texts]

    async def model_identity(self, name):
        return "embedder@digest"


def stale_chunks(store, pid=None):
    """Chunks whose source is not current (or not found): what nothing searches."""
    where = " WHERE project_id=:p" if pid else ""
    current = {row["id"] for row in store.query("SELECT id FROM sources WHERE is_current=true")}
    return [row for row in store.query(f"SELECT id,source_id,is_current FROM chunks{where}", {"p": pid})
            if row["source_id"] not in current or row.get("is_current") is False]


def sources(store, pid):
    return {row["id"]: clean_row(row) for row in store.query(
        "SELECT id,uri,content,sha256,is_current FROM sources WHERE project_id=:p", {"p": pid})}


def embed_all(store, pid):
    retriever = Retriever(store, store.settings, Vectors())
    for row in store.query("SELECT id FROM chunks WHERE project_id=:p AND is_current=true", {"p": pid}):
        asyncio.run(retriever.embed_pending({"project_id": pid, "payload": {"chunk_id": row["id"]}}))
    return retriever


def vector_rows(store, source_ids):
    total = len(store.query("SELECT id FROM embeddings WHERE source_id IN :ids", {"ids": source_ids}))
    for row in store.query("SELECT type_name FROM vector_indexes"):
        total += len(store.query(f"SELECT id FROM {row['type_name']} WHERE source_id IN :ids", {"ids": source_ids}))
    return total


def legacy(store, pid, uri, lines=500):
    """A replaced source as releases before #186 left it: not current, its chunks kept and flagged."""
    old = store.snapshot(pid, uri, "".join(f"old line {n}\n" for n in range(lines)), "document")
    _write_chunks(store, pid, old["id"], old["content"])
    with store.transaction() as tx:
        tx.command("UPDATE sources SET is_current=false,current_key=null WHERE id=:id", {"id": old["id"]})
        tx.command("UPDATE chunks SET is_current=false WHERE source_id=:id", {"id": old["id"]})
    new = store.snapshot(pid, uri, "new text\n", "document")
    _write_chunks(store, pid, new["id"], new["content"])
    return old, new


def test_an_edited_and_reindexed_file_leaves_no_chunk_of_a_replaced_source(tmp_path):
    root = tmp_path / "repo"
    tree(root, files=12)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    before = sources(store, pid)
    rewrite(root)
    ingest.forget_index_signature(store)
    ingest.index_project(store, pid)
    after = sources(store, pid)
    replaced = [row for row in after.values() if not row["is_current"]]
    assert replaced and {row["id"] for row in replaced} == set(before)
    assert stale_chunks(store, pid) == []
    # Every current source still has its chunks; every old version keeps its text and its hash.
    chunked = {row["source_id"] for row in store.query("SELECT source_id FROM chunks WHERE project_id=:p", {"p": pid})}
    assert chunked == {row["id"] for row in after.values() if row["is_current"]}
    for row in replaced:
        assert row["content"] == before[row["id"]]["content"]
        assert hashlib.sha256(row["content"].encode()).hexdigest() == row["sha256"]


def test_a_deleted_file_keeps_its_source_but_not_its_chunks(tmp_path):
    root = tmp_path / "repo"
    tree(root, files=6)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    gone = root / "pkg0" / "long.py"
    old = store.query("SELECT id,content FROM sources WHERE project_id=:p AND uri=:u AND is_current=true",
                      {"p": pid, "u": str(gone.resolve())})
    assert old, "the fixture file was not indexed under its path"
    gone.unlink()
    ingest.forget_index_signature(store)
    ingest.index_project(store, pid)
    assert stale_chunks(store, pid) == []
    assert not store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": old[0]["id"]})
    assert store.get_source(old[0]["id"])["content"] == old[0]["content"]


def test_a_replacement_deletes_chunks_and_vectors_in_its_own_transaction(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    pid = store.add_project("p", tmp_path / "repo")["id"]
    old = store.snapshot(pid, "manual://note", "first version of the note\n" * 200, "manual")
    _write_chunks(store, pid, old["id"], old["content"])
    embed_all(store, pid)
    chunks = store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": old["id"]})
    assert len(chunks) > 1 and vector_rows(store, [old["id"]]) == 2 * len(chunks)

    # A replacement that fails after the old rows were dropped leaves them all in place.
    import cairnmind.store as store_module
    real = store_module.insert_document

    def failing(tx, table, row):
        if table == "sources":
            raise RuntimeError("the disk is full")
        return real(tx, table, row)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "insert_document", failing)
        with pytest.raises(RuntimeError, match="disk is full"):
            store.snapshot(pid, "manual://note", "second version\n", "manual")
    assert len(store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": old["id"]})) == len(chunks)
    assert store.get_source(old["id"])["is_current"] is True

    new = store.snapshot(pid, "manual://note", "second version\n", "manual")
    assert not store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": old["id"]})
    assert vector_rows(store, [old["id"]]) == 0
    kept = store.get_source(old["id"])
    assert kept["is_current"] is False and kept["content"] == old["content"] and kept["sha256"] == old["sha256"]
    assert new["id"] != old["id"]


def test_no_chunk_is_written_for_a_source_replaced_meanwhile(tmp_path):
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    pid = store.add_project("p", tmp_path / "repo")["id"]
    old = store.snapshot(pid, "manual://note", "one\n", "manual")
    store.snapshot(pid, "manual://note", "two\n", "manual")
    assert _write_chunks(store, pid, old["id"], old["content"]) == 0
    assert not store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": old["id"]})
    assert not store.query("SELECT id FROM jobs WHERE kind='embed'")


def test_the_cleanup_removes_what_older_releases_kept_in_short_resumable_runs(tmp_path):
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    pid = store.add_project("p", tmp_path / "repo")["id"]
    first, current_first = legacy(store, pid, "file://a.py")
    second, _ = legacy(store, pid, "file://b.py", lines=200)
    # A vector an import left on an old source goes with its chunks.
    store.command("INSERT INTO embeddings CONTENT :row", {"row": {
        "id": "left-over", "chunk_id": "x", "source_id": second["id"], "project_id": pid, "namespace": "n",
        "vector": [1.0], "dimension": 1}})
    record = store.remember(pid, {"kind": "fact", "content": "cites the old version"}, [first["id"]])
    stale = stale_chunks(store, pid)
    current = store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": current_first["id"]})
    before = sources(store, pid)
    assert len(stale) > 4 and current
    embed_all(store, pid)
    retriever = Retriever(store, store.settings)

    def hits():
        return [[(hit["id"], hit.get("source_id")) for hit in retriever.search(pid, query, **vector)]
                for query in ("old line", "new text", "line 42")
                for vector in ({}, {"query_vector": [0.25, 0.5, 0.75, 1.0], "embedding_key": "embedder@digest"})]
    found = hits()
    assert any(found)

    partial = drop_replaced_chunks(store, batch=2, max_batches=1)
    assert partial["complete"] is False and partial["removed"]["chunks"] == 2 and partial["transactions"] == 1
    left = stale_chunks(store, pid)
    assert len(left) == len(stale) - 2
    # A stopped run still counts the sources it deleted chunks of.
    gone = {row["id"] for row in left}
    assert partial["sources"] == len({row["source_id"] for row in stale if row["id"] not in gone}) >= 1

    rest = drop_replaced_chunks(store, batch=2)
    assert rest["complete"] is True
    assert partial["removed"]["chunks"] + rest["removed"]["chunks"] == len(stale)
    assert rest["removed"]["embeddings"] == 1 and rest["sources"] == 2
    # Every transaction is counted: the chunk batches and the one that deleted the left-over vector.
    assert rest["transactions"] == -(-len(left) // 2) + 1
    assert stale_chunks(store, pid) == []
    assert hits() == found  # search never read them: the same hits in the same order
    # Nothing that is evidence went: every source with its text, the record that cites the old one, current chunks.
    assert sources(store, pid) == before
    assert store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": current_first["id"]}) == current
    assert store.query("SELECT id FROM records WHERE id=:id", {"id": record["id"]})
    again = drop_replaced_chunks(store)
    assert again["complete"] is True and again["removed"] == {"chunks": 0, "embeddings": 0, "vector_rows": 0}
    with pytest.raises(ValueError):
        drop_replaced_chunks(store, batch=0)
    with pytest.raises(ValueError):
        drop_replaced_chunks(store, max_batches=0)


def test_a_run_whose_last_allowed_batch_empties_the_store_is_complete(tmp_path):
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    pid = store.add_project("p", tmp_path / "repo")["id"]
    legacy(store, pid, "file://a.py")
    stale = len(stale_chunks(store, pid))
    done = drop_replaced_chunks(store, batch=stale, max_batches=1)
    assert done == {"removed": {"chunks": stale, "embeddings": 0, "vector_rows": 0}, "sources": 1,
                    "transactions": 1, "complete": True}


def test_left_over_vectors_go_in_batches_counted_as_transactions(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    pid = store.add_project("p", tmp_path / "repo")["id"]
    old, _ = legacy(store, pid, "file://a.py", lines=3)
    for n in range(5):
        store.command("INSERT INTO embeddings CONTENT :row", {"row": {
            "id": f"left-over-{n}", "chunk_id": f"x{n}", "source_id": old["id"], "project_id": pid,
            "namespace": "n", "vector": [1.0], "dimension": 1}})
    stale = len(stale_chunks(store, pid))
    sizes = []
    real = store.transaction

    def watched():
        context = real()
        tx = context.__enter__()
        command = tx.command

        def counting(sql, params=None):
            if sql.startswith("DELETE FROM"):
                sizes.append(len(params["ids"]))
            return command(sql, params)
        tx.command = counting

        class Wrapped:
            def __enter__(self):
                return tx

            def __exit__(self, *exc):
                return context.__exit__(*exc)
        return Wrapped()
    monkeypatch.setattr(store, "transaction", watched)
    stopped = drop_replaced_chunks(store, batch=2, max_batches=-(-stale // 2) + 1)
    assert stopped["complete"] is False and stopped["removed"]["embeddings"] == 2
    rest = drop_replaced_chunks(store, batch=2)
    assert rest["complete"] is True and rest["removed"]["embeddings"] == 3 and rest["transactions"] == 2
    assert max(sizes) <= 2 and len(sizes) == stopped["transactions"] + rest["transactions"]
    assert vector_rows(store, [old["id"]]) == 0 and stale_chunks(store, pid) == []


def test_the_command_reports_the_rows_it_removed(tmp_path):
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    pid = store.add_project("p", tmp_path / "repo")["id"]
    legacy(store, pid, "file://a.py")
    stale = len(stale_chunks(store, pid))
    runner = CliRunner()
    shown = runner.invoke(app, ["--data-dir", str(store.data_dir), "store", "drop-replaced-chunks", "--batch", "3"])
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    report = json.loads(shown.output)
    assert report["removed"]["chunks"] == stale and report["complete"] is True
    assert stale_chunks(store, pid) == []
    refused = runner.invoke(app, ["--data-dir", str(store.data_dir), "store", "drop-replaced-chunks", "--batch", "0"])
    assert refused.exit_code != 0


def test_backup_and_restore_round_trip_after_the_cleanup(tmp_path):
    store = Store(tmp_path / "memory")
    (tmp_path / "repo").mkdir()
    pid = store.add_project("p", tmp_path / "repo")["id"]
    old, new = legacy(store, pid, "file://a.py")
    embed_all(store, pid)
    drop_replaced_chunks(store)
    exported = store.export()
    backup = store.backup(tmp_path / "after-cleanup.cairnmind.json.gz")
    store.forget_project(pid)
    store.restore(backup)
    restored = store.export()
    def rows(payload, collection):
        return sorted(json.dumps(row, sort_keys=True) for row in payload[collection])
    for collection in ("sources", "chunks", "embeddings", "records"):
        assert rows(exported, collection) == rows(restored, collection), collection
    assert store.get_source(old["id"])["content"] == old["content"]
    assert stale_chunks(store, pid) == []
    hits = Retriever(store, store.settings).search(pid, "new text")
    assert hits and all(hit.get("source_id") == new["id"] for hit in hits if hit.get("source_id"))


def test_an_imported_memory_brings_no_chunk_of_a_replaced_source(origin, tmp_path):  # noqa: F811 (the fixture)
    """The package carries every source version; only the current ones get chunks in the store that imports it."""
    pid = origin["project"]["id"]
    published = origin["manager"].snapshot(pid)
    target, root, manager = destination(tmp_path, origin)
    manager.import_snapshot(head_path(published), root)
    first, second = origin["first"], origin["second"]
    kept = target.get_source(first["id"])
    assert kept["is_current"] is False and kept["content"] == first["content"] and kept["sha256"] == first["sha256"]
    assert not target.query("SELECT id FROM chunks WHERE source_id=:s", {"s": first["id"]})
    assert target.query("SELECT id FROM chunks WHERE source_id=:s", {"s": second["id"]})
    assert stale_chunks(target, pid) == []
