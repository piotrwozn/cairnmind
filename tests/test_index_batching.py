"""Fewer database requests per indexed file, with the same rows and the same journal events (issue #71)."""
from __future__ import annotations

import collections
import json

import pytest

from cairnmind import ingest
from cairnmind.arcade import ArcadeClient
from cairnmind.store import Store, _clean


def tree(root, files=24):
    for index in range(files):
        folder = root / f"pkg{index % 3}"
        folder.mkdir(parents=True, exist_ok=True)
        imported = f"from pkg{(index + 1) % 3}.module_{(index + 1) % files:03d} import function_{(index + 1) % files}\n"
        (folder / f"module_{index:03d}.py").write_text(
            f"{imported}\nVALUE_{index} = {index}\n\n\ndef function_{index}(argument):\n    return argument + VALUE_{index}\n")
    (root / "README.md").write_text("# Notes\n\nA document, so that an extraction job is queued.\n")
    (root / "pkg0" / "long.py").write_text("".join(f"LINE_{n} = {n}\n" for n in range(400)))  # several chunks


def dump(store, pid):
    """Everything indexing stores, with the identifiers that differ between two databases replaced by what they name."""
    sources = {row["id"]: row for row in map(_clean, store.query("SELECT FROM sources WHERE project_id=:p", {"p": pid}))}
    chunks = {row["id"]: row for row in map(_clean, store.query("SELECT FROM chunks WHERE project_id=:p", {"p": pid}))}
    names = {pid: "PROJECT"}
    for row in sources.values():
        names[row["id"]] = f"source:{row['uri']}:{row['sha256'][:8]}"
    for row in chunks.values():
        names[row["id"]] = f"chunk:{names[row['source_id']]}:{row['line_start']}-{row['line_end']}"

    def named(value):
        text = json.dumps(value, sort_keys=True, default=str)
        for identifier, name in names.items():
            text = text.replace(identifier, name)
        return text

    def without(row, *keys):
        return {key: value for key, value in row.items() if key not in keys and not key.startswith("@")}

    nodes = {row["id"]: row for row in map(_clean, store.query("SELECT FROM nodes WHERE project_id=:p", {"p": pid}))}

    def node(identifier):
        row = nodes[identifier]
        return named([row["kind"], row["label"], row.get("source_id"), row.get("is_current"), row.get("metadata")])

    return {
        "sources": sorted(named(without(row, "id", "created_at")) for row in sources.values()),
        "chunks": sorted(named(without(row, "id")) for row in chunks.values()),
        "jobs": sorted(named(without(row, "id", "created_at", "updated_at"))
                       for row in map(_clean, store.query("SELECT FROM jobs WHERE project_id=:p", {"p": pid}))),
        "nodes": sorted(node(identifier) for identifier in nodes),
        "edges": sorted(named([row["kind"], node(row["from_id"]), node(row["to_id"]), row.get("source_id"), row.get("is_current"),
                               row.get("status"), row.get("metadata")])
                        for row in map(_clean, store.query("SELECT FROM edges WHERE project_id=:p", {"p": pid}))),
        "events": sorted(collections.Counter(
            (row["entity_type"], row["op"]) for row in store.query("SELECT FROM journal_events WHERE project_id=:p", {"p": pid})
        ).items()),
    }


def row_by_row(monkeypatch):
    """Indexing as it was before: every file in its own three transactions, every graph row looked up singly."""
    monkeypatch.setattr(ingest, "_write_new_files", lambda store, project_id, batch: [None] * len(batch))
    monkeypatch.setattr(Store, "prefetch_graph", lambda self, tx, nodes, edges: None)


def counted(monkeypatch):
    seen = []
    real = ArcadeClient._request

    def request(self, method, path, **kwargs):
        seen.append((kwargs.get("json") or {}).get("command") or path.split("/")[3])
        return real(self, method, path, **kwargs)
    monkeypatch.setattr(ArcadeClient, "_request", request)
    return seen


def change(root):
    (root / "pkg1" / "module_001.py").write_text("VALUE_1 = 100\n\n\ndef function_1(argument):\n    return argument\n")
    (root / "pkg2" / "module_002.py").unlink()
    (root / "pkg0" / "added.py").write_text("from pkg1.module_001 import function_1\n\nADDED = function_1(1)\n")
    (root / "README.md").write_text("# Notes\n\nChanged.\n")


def test_grouped_writes_store_what_writing_row_by_row_stores(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    tree(root)
    grouped = Store(tmp_path / "grouped")
    first = grouped.add_project("p", root)["id"]
    result = ingest.index_project(grouped, first)
    assert result["files"] == 26 and not result["errors"]
    with monkeypatch.context() as patch:
        row_by_row(patch)
        single = Store(tmp_path / "single")
        second = single.add_project("p", root)["id"]
        reference = ingest.index_project(single, second)
    assert {key: result[key] for key in ("files", "changed", "unchanged", "chunks", "nodes", "edges")} == \
        {key: reference[key] for key in ("files", "changed", "unchanged", "chunks", "nodes", "edges")}
    one, other = dump(grouped, first), dump(single, second)
    for part in one:
        assert one[part] == other[part], part
    assert one["chunks"] and one["edges"] and any('"extract"' in job for job in one["jobs"])

    # And again after an edit, a deletion, a new file and a changed document.
    change(root)
    ingest.forget_index_signature(grouped)
    ingest.forget_index_signature(single)
    ingest.index_project(grouped, first)
    with monkeypatch.context() as patch:
        row_by_row(patch)
        ingest.index_project(single, second)
    one, other = dump(grouped, first), dump(single, second)
    for part in one:
        assert one[part] == other[part], part


def test_first_indexing_needs_few_requests_per_file(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    tree(root, files=40)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    seen = counted(monkeypatch)
    result = ingest.index_project(store, pid)
    assert result["files"] == 42 and not result["errors"]
    # 47 per file before this change. What is left: one lookup and four writes per file, the graph rows,
    # one journal event per row, and the cost of a transaction shared by up to eight files.
    assert len(seen) / result["files"] < 20, collections.Counter(text[:60] for text in seen).most_common(12)
    assert sum(text == "begin" for text in seen) < result["files"]
    assert not [text for text in seen if text.startswith("SELECT FROM sources WHERE project_id=:project AND uri=:uri")]


def test_a_failure_stores_nothing_of_its_group_and_the_next_run_completes_it(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    tree(root, files=12)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    real, calls = Store.snapshot_new, []

    def failing(self, tx, *args, **kwargs):
        source = real(self, tx, *args, **kwargs)
        calls.append(source["uri"])
        if len(calls) == 11:  # the source of the eleventh file is written, then the write fails
            raise RuntimeError("the disk is full")
        return source
    with monkeypatch.context() as patch:
        patch.setattr(Store, "snapshot_new", failing)
        with pytest.raises(RuntimeError, match="disk is full"):
            ingest.index_project(store, pid)
    sources = store.query("SELECT id,uri FROM sources WHERE project_id=:p", {"p": pid})
    # Whole groups of the files before it are stored; of the group that failed, nothing is.
    assert 0 < len(sources) < 11 and calls[10] not in {row["uri"] for row in sources}
    assert {row["uri"] for row in sources} == set(calls[:len(sources)])
    chunks = store.query("SELECT id,source_id FROM chunks WHERE project_id=:p", {"p": pid})
    assert {row["source_id"] for row in chunks} == {row["id"] for row in sources}  # no source without its chunks
    jobs = store.query("SELECT payload FROM jobs WHERE project_id=:p AND kind='embed'", {"p": pid})
    assert sorted(_clean(row)["payload"]["chunk_id"] for row in jobs) == sorted(row["id"] for row in chunks)

    ingest.index_project(store, pid)
    with monkeypatch.context() as patch:
        row_by_row(patch)
        single = Store(tmp_path / "single")
        second = single.add_project("p", root)["id"]
        ingest.index_project(single, second)
    one, other = dump(store, pid), dump(single, second)
    for part in ("sources", "chunks", "jobs", "nodes", "edges"):
        assert one[part] == other[part], part


def test_a_path_that_another_writer_stored_first_is_left_to_the_ordinary_path(tmp_path):
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    taken = store.snapshot(pid, "/repo/taken.py", "OLD = 1\n", "code")

    def item(uri, content):
        return {"uri": uri, "content": content, "kind": "code", "metadata": {}, "chunk_metadata": {"language": "python"},
                "chunks": list(ingest._chunks(content)), "extract": False}
    sources = ingest._write_new_files(store, pid, [item("/repo/taken.py", "NEW = 2\n"), item("/repo/free.py", "FREE = 1\n")])
    assert sources[0] is None and sources[1]["uri"] == "/repo/free.py"
    current = {row["uri"]: row for row in store.query("SELECT FROM sources WHERE project_id=:p AND is_current=true", {"p": pid})}
    assert current["/repo/taken.py"]["id"] == taken["id"] and current["/repo/taken.py"]["content"] == "OLD = 1\n"
    assert not store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": taken["id"]})
    assert len(store.query("SELECT id FROM chunks WHERE source_id=:s", {"s": sources[1]["id"]})) == 1


def test_what_a_transaction_remembers_never_replaces_a_check(tmp_path):
    store = Store(tmp_path / "memory")
    mine = store.add_project("mine", tmp_path / "a")["id"]
    theirs = store.add_project("theirs", tmp_path / "b")["id"]
    source = store.snapshot(mine, "note://a", "a", "note")
    foreign = store.upsert_node({"id": "foreign", "project_id": theirs, "kind": "module", "label": "x"})
    node = {"id": "n1", "project_id": mine, "kind": "module", "label": "n1", "source_id": source["id"]}
    with store.transaction() as tx:
        edges = [{"id": "e1", "project_id": mine, "from_id": "n1", "to_id": "n1", "kind": "imports"},
                 {"id": "e2", "project_id": mine, "from_id": "n1", "to_id": foreign["id"], "kind": "imports"},
                 {"id": "e3", "project_id": mine, "from_id": "n1", "to_id": "missing", "kind": "imports"}]
        store.prefetch_graph(tx, [node], edges)
        assert tx.known["nodes"] == {"n1": None, "foreign": {"id": "foreign", "project_id": theirs}, "missing": None}
        with pytest.raises(ValueError, match="endpoints must exist"):
            store.upsert_edge(edges[0], tx=tx)  # its endpoint is known not to exist yet
        store.upsert_node(node, tx=tx)
        store.upsert_edge(edges[0], tx=tx)      # created in this transaction, and known to be
        for edge in edges[1:]:
            with pytest.raises(ValueError, match="endpoints must exist in the same project"):
                store.upsert_edge(edge, tx=tx)
        with pytest.raises(ValueError, match="belongs to another project"):
            store.upsert_node({**node, "id": "foreign"}, tx=tx)
        assert tx.known is not None
        tx.command("DELETE FROM nodes WHERE id=:id", {"id": "unrelated"})  # a write the upserts did not make
        assert tx.known is None
        store.upsert_edge({**edges[0], "id": "e4"}, tx=tx)  # read from the database again, and still right
    assert {row["id"] for row in store.query("SELECT id FROM edges WHERE project_id=:p", {"p": mine})} == {"e1", "e4"}
