"""Fewer database requests per indexed file, with the same rows and the same journal events (issue #71)."""
from __future__ import annotations

import collections
import json

import pytest

from cairnmind import ingest
from cairnmind.arcade import ArcadeClient
from cairnmind.store import Store
from cairnmind.sql import clean_row


def tree(root, files=24):
    for index in range(files):
        folder = root / f"pkg{index % 3}"
        folder.mkdir(parents=True, exist_ok=True)
        imported = f"from pkg{(index + 1) % 3}.module_{(index + 1) % files:03d} import function_{(index + 1) % files}\n"
        (folder / f"module_{index:03d}.py").write_text(
            f"{imported}\nVALUE_{index} = {index}\n\n\ndef function_{index}(argument):\n    return argument + VALUE_{index}\n")
    (root / "README.md").write_text("# Notes\n\nA document, so that an extraction job is queued.\n")
    # Several chunks; one constant, so that its graph rows are those of an ordinary file.
    (root / "pkg0" / "long.py").write_text("LINES = [\n" + "".join(f"    {n},\n" for n in range(400)) + "]\n")


def dump(store, pid):
    """Everything indexing stores, with the identifiers that differ between two databases replaced by what they name."""
    sources = {row["id"]: row for row in map(clean_row, store.query("SELECT FROM sources WHERE project_id=:p", {"p": pid}))}
    chunks = {row["id"]: row for row in map(clean_row, store.query("SELECT FROM chunks WHERE project_id=:p", {"p": pid}))}
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

    nodes = {row["id"]: row for row in map(clean_row, store.query("SELECT FROM nodes WHERE project_id=:p", {"p": pid}))}

    def node(identifier):
        row = nodes[identifier]
        return named([row["kind"], row["label"], row.get("source_id"), row.get("is_current"), row.get("metadata")])

    return {
        "sources": sorted(named(without(row, "id", "created_at")) for row in sources.values()),
        "chunks": sorted(named(without(row, "id")) for row in chunks.values()),
        "jobs": sorted(named(without(row, "id", "created_at", "updated_at"))
                       for row in map(clean_row, store.query("SELECT FROM jobs WHERE project_id=:p", {"p": pid}))),
        "nodes": sorted(node(identifier) for identifier in nodes),
        "edges": sorted(named([row["kind"], node(row["from_id"]), node(row["to_id"]), row.get("source_id"), row.get("is_current"),
                               row.get("status"), row.get("metadata")])
                        for row in map(clean_row, store.query("SELECT FROM edges WHERE project_id=:p", {"p": pid}))),
        "events": sorted(collections.Counter(
            (row["entity_type"], row["op"]) for row in store.query("SELECT FROM journal_events WHERE project_id=:p", {"p": pid})
        ).items()),
    }


def row_by_row(monkeypatch):
    """Indexing as it was before: every new or replaced file in its own three transactions, every deleted file in
    one of its own, every graph row looked up singly."""
    monkeypatch.setattr(ingest, "_write_files", lambda store, project_id, batch: [None] * len(batch))
    monkeypatch.setattr(ingest, "_deactivate_sources",
                        lambda store, source_ids: [ingest._deactivate_source(store, item) for item in source_ids])
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
    # The first group of eight is stored whole; of the group that failed, files nine to eleven, nothing is.
    assert {row["uri"] for row in sources} == set(calls[:ingest.FILES_PER_TRANSACTION])
    chunks = store.query("SELECT id,source_id FROM chunks WHERE project_id=:p", {"p": pid})
    assert {row["source_id"] for row in chunks} == {row["id"] for row in sources}  # no source without its chunks
    jobs = store.query("SELECT payload FROM jobs WHERE project_id=:p AND kind='embed'", {"p": pid})
    assert sorted(clean_row(row)["payload"]["chunk_id"] for row in jobs) == sorted(row["id"] for row in chunks)

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
    sources = ingest._write_files(store, pid, [item("/repo/taken.py", "NEW = 2\n"), item("/repo/free.py", "FREE = 1\n")])
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


def test_a_file_too_large_to_share_a_transaction_keeps_its_named_journal_events(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for index in range(3):
        (root / f"a{index}.py").write_text(f"SMALL_{index} = {index}\n")
    (root / "big.py").write_text("".join(f"LINE_{n} = {n}\n" for n in range(9000)))
    (root / "z.py").write_text("LAST = 1\n")
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    seen = counted(monkeypatch)
    result = ingest.index_project(store, pid)
    chunks = store.query("SELECT id,source_id FROM chunks WHERE project_id=:p", {"p": pid})
    big = store.query("SELECT id FROM sources WHERE project_id=:p AND uri=:uri", {"p": pid, "uri": str(root / "big.py")})[0]["id"]
    assert sum(row["source_id"] == big for row in chunks) > 100 and result["chunks"] == len(chunks)
    events = collections.Counter((row["entity_type"], row["op"], row["entity_id"] is not None)
                                 for row in store.query("SELECT FROM journal_events WHERE project_id=:p", {"p": pid}))
    # Every source, chunk and job is named in the journal: none was folded into "everything of this kind changed".
    assert events[("chunk", "upsert", True)] == len(chunks) and events[("source", "upsert", True)] == 5
    assert not [key for key in events if key[1] == "reset" and key[0] in ("source", "chunk", "job")]
    # The small files before and after it still share transactions; the large one took the ordinary path.
    assert sum(text.startswith("SELECT FROM sources WHERE current_key=:key") for text in seen) == 1


def test_a_group_never_holds_more_events_than_its_limit(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for index in range(6):
        (root / f"m{index}.py").write_text("".join(f"LINE_{index}_{n} = {n}\n" for n in range(1500)))
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    groups, real = [], ingest._write_files

    def recorded(store, project_id, batch):
        groups.append(sum(2 + 2 * len(item["chunks"]) for item in batch))
        return real(store, project_id, batch)
    monkeypatch.setattr(ingest, "_write_files", recorded)
    ingest.index_project(store, pid)
    assert len(groups) > 1 and max(groups) <= ingest.EVENTS_PER_TRANSACTION


def rewrite(root, stamp="changed"):
    """Every file gets new content: each one now replaces a current source (issue #102)."""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            path.write_text(path.read_text() + f"\n# {stamp}\n")


def test_replaced_files_need_few_requests_per_file(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    tree(root, files=40)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    rewrite(root)
    ingest.forget_index_signature(store)
    seen = counted(monkeypatch)
    result = ingest.index_project(store, pid)
    assert result["changed"] == result["files"] == 42 and not result["errors"]
    # 65 per file before #102 in this tree: every replacement took three transactions of its own, the journal
    # read each updated row before and after and wrote one request per event.
    assert len(seen) / result["files"] < 20, collections.Counter(text[:60] for text in seen).most_common(12)
    assert sum(text == "begin" for text in seen) < result["files"] / 4
    assert not [text for text in seen if text.startswith("SELECT FROM sources WHERE project_id=:project AND uri=:uri")]


def embed_everything(store, pid):
    """An embedding row per current chunk, as the embedding worker writes it, so that a replacement deletes them."""
    for row in store.query("SELECT id,source_id FROM chunks WHERE project_id=:p AND is_current=true", {"p": pid}):
        store.command("INSERT INTO embeddings CONTENT :row", {"row": {
            "id": f"embedding:{row['id']}", "project_id": pid, "source_id": row["source_id"], "chunk_id": row["id"]}})


def test_replacing_and_deleting_together_stores_what_row_by_row_stores(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    tree(root, files=30)
    grouped, single = Store(tmp_path / "grouped"), Store(tmp_path / "single")
    first, second = grouped.add_project("p", root)["id"], single.add_project("p", root)["id"]
    ingest.index_project(grouped, first)
    with monkeypatch.context() as patch:
        row_by_row(patch)
        ingest.index_project(single, second)
    embed_everything(grouped, first)
    embed_everything(single, second)
    for version in ("edited", "edited again"):
        rewrite(root, version)
        for index in range(0, 30, 7 if version == "edited" else 11):
            path = root / f"pkg{index % 3}" / f"module_{index:03d}.py"
            if path.exists():
                path.unlink()
        ingest.forget_index_signature(grouped)
        ingest.forget_index_signature(single)
        result = ingest.index_project(grouped, first)
        with monkeypatch.context() as patch:
            row_by_row(patch)
            reference = ingest.index_project(single, second)
        assert {key: result[key] for key in ("files", "changed", "unchanged", "deleted", "chunks", "nodes", "edges")} == \
            {key: reference[key] for key in ("files", "changed", "unchanged", "deleted", "chunks", "nodes", "edges")}
        assert result["deleted"] and result["changed"] == result["files"]
        one, other = dump(grouped, first), dump(single, second)
        for part in one:
            assert one[part] == other[part], (version, part)
        assert ("embedding", "delete") in dict(one["events"])
        assert grouped.query("SELECT count(*) AS n FROM embeddings")[0]["n"] == 0
        assert single.query("SELECT count(*) AS n FROM embeddings")[0]["n"] == 0


def test_a_group_of_replacements_is_checked_against_what_it_actually_wrote(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    # About 60 chunks each for a to c, 120 for d: with their embeddings, a group of two of the first three holds more
    # events than its limit, d alone does too (but less than the journal's own limit), e and f are small.
    sizes = {"a.py": 4100, "b.py": 4100, "c.py": 4100, "d.py": 8200, "e.py": 10, "f.py": 10}
    for name, lines in sizes.items():
        (root / name).write_text("".join(f"# old line {n}\n" for n in range(lines)))
    grouped, single = Store(tmp_path / "grouped"), Store(tmp_path / "single")
    first, second = grouped.add_project("p", root)["id"], single.add_project("p", root)["id"]
    ingest.index_project(grouped, first)
    with monkeypatch.context() as patch:
        row_by_row(patch)
        ingest.index_project(single, second)
    embed_everything(grouped, first)
    embed_everything(single, second)
    for name in sizes:  # small now: what the new versions write is known, what the old ones leave behind is not
        (root / name).write_text(f"NEW_{name[0]} = 1\n")
    ingest.forget_index_signature(grouped)
    committed, real = [], ingest._within_limit

    def recorded(tx):
        real(tx)
        committed.append(len(tx.pending()))
    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_within_limit", recorded)
        ingest.index_project(grouped, first)
    with monkeypatch.context() as patch:
        row_by_row(patch)
        ingest.index_project(single, second)
    # a, b and c each alone after the group of six was halved twice, then e and f together; d took the ordinary path.
    assert len(committed) == 4 and max(committed) <= ingest.EVENTS_PER_TRANSACTION
    one, other = dump(grouped, first), dump(single, second)
    for part in one:
        assert one[part] == other[part], part

    # Deleted files: a group of large sources is deactivated source by source instead.
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("".join(f"# old line {n}\n" for n in range(4100)))
    for store, pid in ((grouped, first), (single, second)):
        ingest.forget_index_signature(store)
        with monkeypatch.context() as patch:
            if store is single:
                row_by_row(patch)
            ingest.index_project(store, pid)
        embed_everything(store, pid)
    for name in ("a.py", "b.py", "c.py", "e.py"):
        (root / name).unlink()
    del committed[:]
    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_within_limit", recorded)
        assert ingest.index_project(grouped, first)["deleted"] == 4
    with monkeypatch.context() as patch:
        row_by_row(patch)
        assert ingest.index_project(single, second)["deleted"] == 4
    assert committed == []  # the one group of four was too large and rolled back
    one, other = dump(grouped, first), dump(single, second)
    for part in one:
        assert one[part] == other[part], part
    events = collections.Counter((row["entity_type"], row["op"], row["entity_id"] is not None)
                                 for row in grouped.query("SELECT FROM journal_events WHERE project_id=:p", {"p": first}))
    # No group was folded into "everything of this kind changed": every row is still named.
    assert not [key for key in events if key[1] == "reset" and key[0] in ("source", "chunk", "job", "embedding")]


def test_a_failure_in_a_group_of_replacements_leaves_every_file_of_it_as_it_was(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    tree(root, files=12)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    ingest.index_project(store, pid)
    with monkeypatch.context() as patch:
        row_by_row(patch)
        single = Store(tmp_path / "single")
        second = single.add_project("p", root)["id"]
        ingest.index_project(single, second)
    old = {row["uri"]: row["id"] for row in store.query(
        "SELECT id,uri FROM sources WHERE project_id=:p AND is_current=true", {"p": pid})}
    rewrite(root)
    ingest.forget_index_signature(store)
    real, calls = Store.new_source, []

    def failing(self, tx, project_id, uri, *args, **kwargs):
        calls.append(uri)
        if len(calls) == 11:  # the eleventh replacement: its old source is already deactivated in this transaction
            raise RuntimeError("the disk is full")
        return real(self, tx, project_id, uri, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Store, "new_source", failing)
        with pytest.raises(RuntimeError, match="disk is full"):
            ingest.index_project(store, pid)
    current = {row["uri"]: row for row in map(clean_row, store.query(
        "SELECT id,uri,sha256 FROM sources WHERE project_id=:p AND is_current=true", {"p": pid}))}
    replaced = set(calls[:ingest.FILES_PER_TRANSACTION])
    assert set(current) == set(old)
    # The first group is replaced whole; every file of the group that failed keeps its old source, current.
    assert {uri for uri, row in current.items() if row["id"] != old[uri]} == replaced
    for uri in calls[ingest.FILES_PER_TRANSACTION:]:
        assert current[uri]["id"] == old[uri]
    stale = store.query("SELECT id FROM sources WHERE project_id=:p AND is_current=false", {"p": pid})
    assert {row["id"] for row in stale} == {old[uri] for uri in replaced}
    chunks = store.query("SELECT source_id,is_current FROM chunks WHERE project_id=:p", {"p": pid})
    assert all(row["is_current"] == (row["source_id"] not in {r["id"] for r in stale}) for row in chunks)
    assert {row["source_id"] for row in chunks} == {row["id"] for row in current.values()} | {row["id"] for row in stale}

    ingest.index_project(store, pid)  # the next run completes what failed
    with monkeypatch.context() as patch:
        row_by_row(patch)
        ingest.index_project(single, second)
    one, other = dump(store, pid), dump(single, second)
    for part in ("sources", "chunks", "jobs", "nodes", "edges"):
        assert one[part] == other[part], part


def test_a_graph_reset_skips_reading_the_graph_rows_it_updates(tmp_path, monkeypatch):
    from cairnmind.journal import Journal
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", tmp_path)["id"]
    for identifier in ("a", "b"):
        store.upsert_node({"id": identifier, "project_id": pid, "kind": "module", "label": identifier})
    store.upsert_edge({"id": "e", "project_id": pid, "from_id": "a", "to_id": "b", "kind": "imports"})
    seen = counted(monkeypatch)
    with store.transaction() as tx:
        Journal.touch(tx, pid, "graph", op="reset")
        store.upsert_node({"id": "a", "project_id": pid, "kind": "module", "label": "renamed"}, tx=tx)
        store.upsert_edge({"id": "e", "project_id": pid, "from_id": "a", "to_id": "b", "kind": "imports", "line": 2}, tx=tx)
        store.upsert_edge({"id": "e", "project_id": pid, "from_id": "b", "to_id": "a", "kind": "imports"}, tx=tx)
    # Only the lookups of the upserts themselves; the journal read no row before or after its update.
    assert not [text for text in seen if "journal_ids" in text or text.startswith(("SELECT FROM `nodes`", "SELECT FROM `edges`"))]
    rows = {row["id"]: clean_row(row) for row in store.query("SELECT FROM nodes WHERE project_id=:p", {"p": pid})}
    assert rows["a"]["label"] == "renamed"
    edge = clean_row(store.query("SELECT FROM edges WHERE id='e'")[0])
    assert (edge["from_id"], edge["to_id"]) == ("b", "a")
    # Without a reset, the journal still names the node it updated.
    cursor = Journal(store).cursor()
    store.upsert_node({"id": "b", "project_id": pid, "kind": "module", "label": "named"})
    events = Journal(store).read(cursor)["events"]
    assert [(event["entity_type"], event["entity_id"]) for event in events] == [("node", "b")]


def test_a_file_under_two_registered_roots_is_written_and_counted_once(tmp_path):
    root = tmp_path / "repo"
    tree(root, files=6)
    store = Store(tmp_path / "memory")
    pid = store.add_project("p", root)["id"]
    store.add_project_root(pid, root / "pkg0")
    result = ingest.index_project(store, pid)
    sources = store.query("SELECT uri FROM sources WHERE project_id=:p AND is_current=true", {"p": pid})
    assert len(sources) == len({row["uri"] for row in sources}) == 8
    twice = len(list((root / "pkg0").glob("*.py")))
    assert (result["changed"], result["unchanged"]) == (8, twice)
