"""After an edit, indexing reads the edited file only and writes only the graph rows that differ (issue #62).

The constraint: the index must stay exactly what a run that reads every file and replaces the
whole graph would produce. Every scenario here ends by comparing the two.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from cairnmind.config import Settings
from cairnmind.scip import ingest
from cairnmind.scip.ingest import forget_index_signature, index_project, index_project_if_changed
from cairnmind.storage.arcade import ArcadeClient
from cairnmind.storage.journal import Journal, JournalTransaction
from cairnmind.storage.sql import clean_row
from cairnmind.store import Store


@pytest.fixture(autouse=True)
def trusted_timestamps(monkeypatch):
    """The tests index files they wrote a moment ago; the racy-clean rule has its own test."""
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", -10**12)


@pytest.fixture
def workspace(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "auth.py").write_text(
        "from pkg.limits import LIMIT, allowed\n\n\ndef authenticate(token):\n    return allowed(token) and LIMIT > 0\n")
    (root / "pkg" / "limits.py").write_text("LIMIT = 3\n\n\ndef allowed(token):\n    return token == 'valid'\n",
                                            encoding="utf-8", newline="\n")
    (root / "app.py").write_text(
        "import missing_module\nfrom pkg.auth import authenticate\n\n\ndef main():\n    return authenticate('valid')\n")
    (root / "test_app.py").write_text("from app import main\n\n\ndef test_main():\n    assert main()\n")
    (root / "notes.md").write_text("# Notes\n\nLogin goes through pkg/auth.py.\n")
    (root / "logo.bin").write_bytes(b"\x00\x01\x02 binary")
    (root / ".gitignore").write_text("hidden.py\n")
    (root / "hidden.py").write_text("HIDDEN = 1\n")
    for index in range(12):
        (root / "pkg" / f"module_{index:02d}.py").write_text(f"VALUE_{index} = {index}\n")
    subprocess.run(["git", "-C", str(root), "init", "-q"], capture_output=True, check=True)
    store = Store(settings.data_dir)
    project = store.add_project("incremental", root)
    forget_index_signature(store)
    return store, project["id"], root


def index_state(store, pid):
    """Everything indexing writes: current sources and chunks, and every node and edge of the project."""
    def rows(sql):
        return sorted((clean_row(row) for row in store.query(sql, {"project": pid})), key=lambda row: str(row.get("id")))

    nodes = rows("SELECT id,kind,label,source_id,metadata,is_current FROM nodes WHERE project_id=:project")
    edges = rows("SELECT id,from_id,to_id,kind,source_id,status,is_current FROM edges WHERE project_id=:project")
    sources = rows("SELECT id,uri,sha256,kind,is_current FROM sources WHERE project_id=:project")
    chunks = rows("SELECT id,source_id,is_current FROM chunks WHERE project_id=:project")
    marker = rows("SELECT id,graph_fingerprint,graph_nodes,graph_edges FROM projects WHERE id=:project")
    jobs = sorted(row["dedupe_key"] for row in store.query(
        "SELECT dedupe_key FROM jobs WHERE project_id=:project", {"project": pid}))
    return {"nodes": nodes, "edges": edges, "sources": sources, "chunks": chunks, "marker": marker, "jobs": jobs}


def full_run(store, pid):
    """Index with nothing remembered and with the stored graph declared unknown."""
    forget_index_signature(store, pid)
    store.command("UPDATE projects SET graph_fingerprint=null WHERE id=:project", {"project": pid})
    result = index_project(store, pid)
    assert result["graph_unchanged"] is False and result["read"] >= result["files"]
    return result


def assert_same_as_a_full_run(store, pid):
    incremental = index_state(store, pid)
    result = full_run(store, pid)
    assert result["changed"] == 0 and result["deleted"] == 0  # the incremental run left nothing to index
    assert index_state(store, pid) == incremental


def reads_under(root, monkeypatch):
    read, original = [], Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: read.append(self.relative_to(root).as_posix())
                        or original(self))
    return read


def test_after_one_edit_only_the_edited_file_is_read(workspace, monkeypatch):
    store, pid, root = workspace
    first = index_project(store, pid)
    assert first["files"] == 19 and first["read"] == 20 and not first["errors"]  # the binary file is read once too
    (root / "pkg" / "limits.py").write_text("LIMIT = 4\n\n\ndef allowed(token):\n    return token == 'valid'\n")
    read = reads_under(root, monkeypatch)
    lookups = []
    original = ingest._source_before
    monkeypatch.setattr(ingest, "_source_before", lambda *a: lookups.append(a[2]) or original(*a))
    edited = index_project_if_changed(store, pid)
    monkeypatch.undo()
    assert read == ["pkg/limits.py"]
    # Not even the edited file is looked up: the read before the walk already says its content changed (#102).
    assert lookups == []
    assert (edited["files"], edited["changed"], edited["unchanged"], edited["read"]) == (19, 1, 18, 1)
    assert_same_as_a_full_run(store, pid)


def test_after_one_edit_only_rows_of_that_file_are_written_to_the_graph(workspace, monkeypatch):
    store, pid, root = workspace
    first = index_project(store, pid)
    (root / "pkg" / "module_03.py").write_text("VALUE_3 = 33\n")
    nodes, edges = [], []
    # Every graph row written, grouped or one by one, passes the checks of its upsert once (#167).
    checked_node, checked_edge = store._checked_node, store._checked_edge
    monkeypatch.setattr(store, "_checked_node", lambda tx, item: nodes.append(item["kind"]) or checked_node(tx, item))
    monkeypatch.setattr(store, "_checked_edge", lambda tx, item: edges.append(item["kind"]) or checked_edge(tx, item))
    edited = index_project_if_changed(store, pid)
    monkeypatch.undo()
    # The file's own node, its new snapshot node and the one constant it defines, which now names
    # the new snapshot: the snapshot edge and the edge that defines the constant.
    assert sorted(nodes) == ["file", "source", "symbol"] and sorted(edges) == ["defines", "snapshot_of"]
    assert edited["graph_written"] == {"nodes": 3, "edges": 2}
    assert (edited["nodes"], edited["edges"]) == (first["nodes"], first["edges"])
    assert_same_as_a_full_run(store, pid)


STEPS = {
    "edit a body": lambda root: (root / "pkg" / "limits.py").write_text(
        "LIMIT = 9\n\n\ndef allowed(token):\n    return bool(token)\n"),
    "rename a function that another file calls": lambda root: (root / "pkg" / "limits.py").write_text(
        "LIMIT = 3\n\n\ndef permitted(token):\n    return token == 'valid'\n"),
    "move a definition down, so its line changes": lambda root: (root / "pkg" / "limits.py").write_text(
        "LIMIT = 3\n\n\n\n\ndef allowed(token):\n    return token == 'valid'\n"),
    "add the module an import could not find": lambda root: (root / "missing_module.py").write_text("FOUND = True\n"),
    "delete a file that others import": lambda root: (root / "pkg" / "limits.py").unlink(),
    "delete a file nobody imports": lambda root: (root / "pkg" / "module_05.py").unlink(),
    "add a file": lambda root: (root / "pkg" / "extra.py").write_text(
        "from pkg.auth import authenticate\n\n\ndef extra():\n    return authenticate('x')\n"),
    "edit a document": lambda root: (root / "notes.md").write_text("# Notes\n\nLogin moved to pkg/extra.py.\n"),
    "turn a text file into a binary one": lambda root: (root / "notes.md").write_bytes(b"\x00\x01"),
    "turn a binary file into text": lambda root: (root / "logo.bin").write_text("now text\n"),
    "ignore a file that was indexed": lambda root: (root / ".gitignore").write_text("pkg/module_07.py\n"),
    "stop ignoring a file": lambda root: (root / ".gitignore").write_text(""),
    "remove an import": lambda root: (root / "app.py").write_text(
        "from pkg.auth import authenticate\n\n\ndef main():\n    return authenticate('valid')\n"),
    "rename a file": lambda root: (root / "pkg" / "module_09.py").rename(root / "pkg" / "renamed.py"),
    "replace a file with a directory of the same name": lambda root: (
        (root / "pkg" / "module_10.py").unlink(), (root / "pkg" / "module_10.py").mkdir(),
        (root / "pkg" / "module_10.py" / "inner.py").write_text("INNER = 1\n")),
}


@pytest.mark.parametrize("step", list(STEPS))
def test_each_kind_of_change_leaves_the_index_a_full_run_would_produce(workspace, step):
    store, pid, root = workspace
    index_project(store, pid)
    STEPS[step](root)
    result = index_project_if_changed(store, pid)
    assert not result["errors"] and "skipped" not in result
    assert result["read"] < result["files"] or result["files"] < 3
    assert_same_as_a_full_run(store, pid)


def test_many_changes_in_a_row_without_a_full_run_in_between(workspace):
    store, pid, root = workspace
    index_project(store, pid)
    source = store.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                         {"project": pid, "uri": str(root / "pkg" / "auth.py")})[0]["id"]
    # A decision whose evidence is a code snapshot shares that snapshot's node with the graph.
    store.remember(pid, {"kind": "decision", "key": "login", "content": "Login goes through authenticate."}, [source])
    for step, change in STEPS.items():
        change(root)
        result = index_project_if_changed(store, pid)
        assert not result["errors"], step
    (root / "pkg" / "auth.py").write_text("def authenticate(token):\n    return True\n")
    index_project_if_changed(store, pid)
    assert_same_as_a_full_run(store, pid)


def test_a_file_that_cannot_be_indexed_is_not_opened_again(workspace, monkeypatch):
    store, pid, root = workspace
    (root / "latin.txt").write_bytes("zażółć".encode("cp1250"))
    index_project(store, pid)
    (root / "app.py").write_text("def main():\n    return 1\n")
    read = reads_under(root, monkeypatch)
    index_project_if_changed(store, pid)
    monkeypatch.undo()
    assert read == ["app.py"]  # neither the binary file nor the one that is not UTF-8
    (root / "latin.txt").write_text("teraz czytelny\n")
    assert index_project_if_changed(store, pid)["changed"] == 1
    assert_same_as_a_full_run(store, pid)


def test_a_source_replaced_behind_the_process_is_read_again(workspace):
    store, pid, root = workspace
    index_project(store, pid)
    path = root / "pkg" / "limits.py"
    # Another process stored different content under the same address; the file itself did not change.
    store.snapshot(pid, str(path), "LIMIT = 100\n", kind="code", metadata={"path": str(path), "root": str(root)})
    repaired = index_project(store, pid)
    assert (repaired["read"], repaired["changed"]) == (1, 1)
    current = store.query("SELECT content FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                          {"project": pid, "uri": str(path)})
    assert [row["content"] for row in current] == [path.read_text()]
    assert_same_as_a_full_run(store, pid)


@pytest.mark.parametrize("damage, missing", [
    ("DELETE FROM edges WHERE project_id=:project AND kind='defines' LIMIT 1", 1),
    ("UPDATE projects SET graph_fingerprint='written-by-another-process' WHERE id=:project", 0),
    ("UPDATE projects SET graph_edges=1 WHERE id=:project", 0),
])
def test_a_graph_changed_behind_the_process_is_written_again_where_it_differs(workspace, damage, missing):
    store, pid, root = workspace
    first = index_project(store, pid)
    store.command(damage, {"project": pid})
    (root / "pkg" / "module_03.py").write_text("VALUE_3 = 33\n")
    repaired = index_project_if_changed(store, pid)
    assert repaired["read"] == 1  # the files are still trusted; the graph is not
    assert repaired["graph_written"]["nodes"] == first["nodes"]  # every vertex is written again
    # An edge that is stored as it would be written stays (#385): the 2 edges of the changed file and the one the damage took are new.
    assert repaired["graph_written"]["edges"] == 2 + missing
    assert_same_as_a_full_run(store, pid)


def test_forgetting_the_index_or_a_new_parser_reads_every_file(workspace, monkeypatch):
    store, pid, root = workspace
    first = index_project(store, pid)
    assert index_project(store, pid)["read"] == 0
    forget_index_signature(store, pid)
    assert index_project(store, pid)["read"] == first["read"]
    assert index_project(store, pid)["read"] == 0
    monkeypatch.setattr(ingest, "parser_identity", lambda: "another-parser")
    again = index_project(store, pid)
    assert again["read"] == first["read"] and again["graph_unchanged"] is False


def test_a_failed_run_remembers_nothing(workspace, monkeypatch):
    store, pid, root = workspace
    index_project(store, pid)
    (root / "app.py").write_text("def main():\n    return 2\n")
    with monkeypatch.context() as patch:
        # The request that writes the graph's edges (in groups since #167) fails.
        real = JournalTransaction.script
        patch.setattr(JournalTransaction, "script", lambda self, statements, *rest: (_ for _ in ()).throw(
            RuntimeError("engine went away")) if statements[0].startswith("CREATE EDGE") else real(self, statements, *rest))
        with pytest.raises(RuntimeError, match="engine went away"):
            index_project(store, pid)
    repaired = index_project(store, pid)
    assert repaired["read"] == 20 and repaired["graph_unchanged"] is False
    assert_same_as_a_full_run(store, pid)


def test_a_file_written_a_moment_ago_is_read_again_next_time(workspace, monkeypatch):
    store, pid, root = workspace
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", 10**18)  # every file is young, however slow the run
    first = index_project(store, pid)
    # Every file is younger than the window: its times cannot yet prove that it did not change.
    assert index_project(store, pid)["read"] == first["read"]
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", -10**12)
    assert index_project(store, pid)["read"] == first["read"]  # nothing was remembered by the runs above
    assert index_project(store, pid)["read"] == 0


def test_what_is_remembered_belongs_to_one_store_and_to_existing_projects(workspace, tmp_path):
    store, pid, root = workspace
    index_project(store, pid)
    key = (str(store.data_dir), pid)
    assert set(ingest._REMEMBERED) >= {key}
    # The same project identifier in another data directory is another key.
    ingest.forget_missing_projects(Store(tmp_path / "other-memory"), [])
    assert key in ingest._REMEMBERED and key in ingest._INDEXED
    ingest.forget_missing_projects(store, [pid])
    assert key in ingest._REMEMBERED
    # A project deleted by another process: the service drops it at its next scan.
    ingest.forget_missing_projects(store, [])
    assert key not in ingest._REMEMBERED and key not in ingest._INDEXED


def test_a_stored_graph_is_remembered_only_after_it_was_compared(workspace):
    store, pid, root = workspace
    first = index_project(store, pid)
    # A new process: nothing is remembered, and the stored fingerprint matches.
    forget_index_signature(store, pid)
    cold = index_project(store, pid)
    assert cold["graph_unchanged"] is True and cold["read"] == first["read"]
    (root / "pkg" / "module_03.py").write_text("VALUE_3 = 33\n")
    assert index_project(store, pid)["graph_written"] == {"nodes": 3, "edges": 2}  # the compared graph was trusted
    assert_same_as_a_full_run(store, pid)


@pytest.mark.parametrize("damage, edges", [
    ("UPDATE nodes SET label='changed elsewhere' WHERE project_id=:project AND kind='symbol' LIMIT 1", 0),
    ("UPDATE nodes SET is_current=false WHERE project_id=:project AND kind='symbol' LIMIT 1", 0),
    ("UPDATE edges SET status='proposed' WHERE project_id=:project AND kind='defines' LIMIT 1", 1),
    ("UPDATE edges SET is_current=false WHERE project_id=:project AND kind='imports' LIMIT 1", 1),
    ("DELETE FROM edges WHERE project_id=:project AND kind='calls' LIMIT 1", 1),
    ("UPDATE edges SET to_id=from_id WHERE project_id=:project AND kind='calls' LIMIT 1", 1),
    ("UPDATE edges SET kind='references' WHERE project_id=:project AND kind='calls' LIMIT 1", 1),
    ("UPDATE edges SET source_id='elsewhere' WHERE project_id=:project AND kind='defines' LIMIT 1", 1),
])
def test_a_stored_graph_that_differs_from_its_fingerprint_is_replaced(workspace, damage, edges):
    store, pid, root = workspace
    first = index_project(store, pid)
    forget_index_signature(store, pid)  # a new process
    store.command(damage, {"project": pid})
    repaired = index_project(store, pid)
    assert repaired["graph_unchanged"] is False
    # Every vertex is written again. An edge stored as it would be written stays (#385); the damaged one is written.
    assert repaired["graph_written"] == {"nodes": first["nodes"], "edges": edges}
    (root / "pkg" / "module_04.py").write_text("VALUE_4 = 44\n")
    assert index_project(store, pid)["graph_written"] == {"nodes": 3, "edges": 2}
    assert_same_as_a_full_run(store, pid)


def test_a_pass_after_an_edit_walks_the_tree_once(workspace, monkeypatch):
    store, pid, root = workspace
    index_project(store, pid)
    (root / "pkg" / "limits.py").write_text("LIMIT = 4\n\n\ndef allowed(token):\n    return token == 'valid'\n")
    walks, original = [], ingest._files
    monkeypatch.setattr(ingest, "_files", lambda *a, **k: walks.append(a[0]) or original(*a, **k))
    edited = index_project_if_changed(store, pid)
    monkeypatch.undo()
    assert edited["changed"] == 1 and len(walks) == 1  # it was three: the check, the signature, the indexing (#409)
    assert_same_as_a_full_run(store, pid)


def stray_node(store, pid):
    with store.transaction() as tx:
        Journal.touch(tx, pid, "graph", op="reset")
        store.write_graph(tx, pid, [{"id": "stray-node", "project_id": pid, "kind": "symbol", "label": "stray"}], [])


@pytest.mark.parametrize("damage", [
    lambda store, pid: stray_node(store, pid),
    lambda store, pid: store.command("DELETE FROM nodes WHERE project_id=:project AND kind='symbol' LIMIT 1", {"project": pid}),
    lambda store, pid: store.command("DELETE FROM edges WHERE project_id=:project AND kind='calls' LIMIT 1", {"project": pid}),
    lambda store, pid: store.command("UPDATE projects SET graph_fingerprint=null WHERE id=:project", {"project": pid}),
    lambda store, pid: store.command("UPDATE projects SET graph_fingerprint='restored-from-a-backup' WHERE id=:project", {"project": pid}),
], ids=["a node added", "a node taken", "an edge taken", "marked as not built", "a fingerprint of another graph"])
def test_a_node_or_an_edge_changed_behind_the_process_is_noticed_and_every_node_is_written_again(workspace, damage):
    # The remembered graph is trusted only when the fingerprint, the markers, the count of edges and the count of
    # nodes say that the stored graph is the one this process wrote (#426); else the stored rows are compared.
    store, pid, root = workspace
    first = index_project(store, pid)
    damage(store, pid)
    (root / "pkg" / "module_03.py").write_text("VALUE_3 = 33\n")
    repaired = index_project_if_changed(store, pid)
    assert repaired["read"] == 1 and repaired["graph_written"]["nodes"] == first["nodes"]
    assert_same_as_a_full_run(store, pid)
    assert not store.query("SELECT id FROM nodes WHERE id='stray-node'")


def test_a_graph_another_process_wrote_after_this_one_is_noticed(workspace):
    store, pid, root = workspace
    index_project(store, pid)
    key = (str(store.data_dir), pid)
    memory = dict(ingest._REMEMBERED[key]["graph"])
    (root / "pkg" / "limits.py").write_text("LIMIT = 4\n\n\ndef allowed(token):\n    return token == 'valid'\n")
    index_project_if_changed(store, pid)  # the other process
    ingest._REMEMBERED[key]["graph"] = memory  # what this one still remembers
    ingest._INDEXED.pop(key, None)
    (root / "pkg" / "module_03.py").write_text("VALUE_3 = 33\n")
    repaired = index_project_if_changed(store, pid)
    assert repaired["graph_written"]["nodes"] == repaired["nodes"]
    assert_same_as_a_full_run(store, pid)


def test_a_record_linked_to_a_code_node_that_moves_keeps_every_later_pass_incremental(workspace):
    # A symbol's id holds its line, so a line added above it leaves the old vertex in the store when a knowledge
    # record's edge names it; the count of stored nodes then holds that vertex too (#426).
    store, pid, root = workspace
    index_project(store, pid)
    allowed = store.query("SELECT id FROM nodes WHERE project_id=:project AND kind='symbol' AND label LIKE '%allowed%' LIMIT 1",
                          {"project": pid})[0]["id"]
    source = store.query("SELECT id FROM sources WHERE project_id=:project AND is_current=true LIMIT 1", {"project": pid})[0]["id"]
    with store.transaction() as tx:
        store.write_graph(tx, pid, [{"id": "record:linked", "project_id": pid, "kind": "decision", "label": "linked"}],
                          [{"id": "linked-edge", "project_id": pid, "from_id": "record:linked", "to_id": allowed,
                            "kind": "relates_to", "source_id": source, "status": "proposed", "is_current": True}])
    limits = root / "pkg" / "limits.py"
    limits.write_text("# moved\n" + limits.read_text())
    moved = index_project_if_changed(store, pid)
    assert store.query("SELECT id FROM nodes WHERE id=:id", {"id": allowed})  # kept, an edge names it
    assert moved["graph_written"]["nodes"] < moved["nodes"]
    for step in (4, 5, 6):
        (root / "pkg" / f"module_{step:02d}.py").write_text(f"VALUE_{step} = {step + 40}\n")
        later = index_project_if_changed(store, pid)
        assert later["changed"] == 1 and later["graph_written"]["nodes"] <= 4 < later["nodes"] / 4, later
    assert_same_as_a_full_run(store, pid)


def test_a_pass_after_one_edit_reads_no_node_ids_and_deletes_edges_by_their_ids(workspace, monkeypatch):
    store, pid, root = workspace
    index_project(store, pid)
    (root / "pkg" / "limits.py").write_text("LIMIT = 4\n\n\ndef allowed(token):\n    return token == 'valid'\n")
    seen = []
    for method in ("query", "command"):
        real = getattr(JournalTransaction, method)
        monkeypatch.setattr(JournalTransaction, method, lambda self, sql, params=None, real=real: seen.append(sql) or real(self, sql, params))
    edited = index_project_if_changed(store, pid)
    monkeypatch.undo()
    assert edited["changed"] == 1
    assert not [sql for sql in seen if sql.startswith("SELECT id FROM nodes WHERE project_id")]  # one count instead
    assert [sql for sql in seen if sql.startswith("SELECT count(*) AS total FROM nodes WHERE project_id")]
    deletes = [sql for sql in seen if sql.startswith("DELETE FROM edges")]
    assert deletes and all(sql == "DELETE FROM edges WHERE id IN :ids" for sql in deletes)
    assert_same_as_a_full_run(store, pid)


def test_a_pass_after_one_edit_asks_the_database_and_computes_graph_rows_in_proportion_to_the_edit(workspace, monkeypatch):
    store, pid, root = workspace
    first = index_project(store, pid)
    (root / "pkg" / "limits.py").write_text("LIMIT = 4\n\n\ndef allowed(token):\n    return token == 'valid'\n")
    asked, digested = [], []
    original, digest = ArcadeClient._request, ingest._node_digest
    monkeypatch.setattr(ArcadeClient, "_request", lambda self, method, path, **kw: asked.append(path) or original(self, method, path, **kw))
    monkeypatch.setattr(ingest, "_node_digest", lambda item: digested.append(item["id"]) or digest(item))
    index_project_if_changed(store, pid)
    monkeypatch.undo()
    # 62 requests for 19 files; a pass that walked and looked the project up three times asked 64 (#409), and none of
    # them grows with the project. Digests are made of the nodes that were computed, not of every node.
    assert len(asked) <= 63, len(asked)
    assert 0 < len(digested) <= 8 < first["nodes"] / 4, (len(digested), first["nodes"])


def test_a_definition_that_stops_being_doubled_changes_the_calls_of_its_importers(workspace):
    store, pid, root = workspace
    (root / "dup.py").write_text("x = 1; x = 2\n")
    (root / "user.py").write_text("from dup import x\n\n\ndef run():\n    return x()\n")
    index_project(store, pid)
    for text in ("x = 1\n", "x = 1; x = 2\n"):
        (root / "dup.py").write_text(text)
        assert index_project_if_changed(store, pid)["changed"] == 1
        assert_same_as_a_full_run(store, pid)  # it leaves a graph remembered for the next edit


def test_a_walk_made_before_waiting_for_another_indexing_is_not_used(workspace, monkeypatch):
    store, pid, root = workspace
    index_project(store, pid)
    listing = {}
    walk = (0, time.time_ns(), ingest._tree_state(store, pid, listing), listing)
    (root / "pkg" / "fresh.py").write_text("FRESH = 1\n")
    blocked, real, results = threading.Event(), ingest.lock_exclusive, []

    def lock_exclusive(file_or_fd, *, blocking=True):
        try:
            return real(file_or_fd, blocking=blocking)
        except BlockingIOError:
            blocked.set()
            raise
    monkeypatch.setattr(ingest, "lock_exclusive", lock_exclusive)
    with ingest._ingestion_lock(store, "project", pid):
        waiting = threading.Thread(target=lambda: results.append(index_project(store, pid, walk)))
        waiting.start()
        assert blocked.wait(10)
        ingest._index_project_locked(store, pid, None, {})  # the other caller sees the file that the walk did not
    waiting.join(30)
    assert results[0]["deleted"] == 0
    assert store.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                       {"project": pid, "uri": str(root / "pkg" / "fresh.py")})


def test_an_edit_after_an_edge_was_deleted_behind_the_process_replaces_what_differs(workspace):
    store, pid, root = workspace
    index_project(store, pid)
    # This process still remembers the graph it wrote: the count of the stored edges says that it is not stored.
    store.command("DELETE FROM edges WHERE project_id=:project AND kind='calls' LIMIT 1", {"project": pid})
    (root / "pkg" / "limits.py").write_text("LIMIT = 4\n\n\ndef allowed(token):\n    return token == 'valid'\n")
    edited = index_project_if_changed(store, pid)
    assert edited["changed"] == 1
    assert_same_as_a_full_run(store, pid)


def test_a_memory_package_kept_deeper_in_the_tree_is_not_indexed_as_project_text(tmp_path):
    root = tmp_path / "repo"
    for folder in ("examples/sample/.memory/cairnmind/objects", "examples/sample/.memory/devbrain", "examples/sample/src",
                   "docs/.memory/other"):
        (root / folder).mkdir(parents=True)
    (root / "examples/sample/.memory/cairnmind/manifest.json").write_text("{}")
    (root / "examples/sample/.memory/cairnmind/objects/row.json").write_text('{"content": "a stored row"}')
    (root / "examples/sample/.memory/devbrain/manifest.json").write_text("{}")
    (root / "examples/sample/src/app.py").write_text("VALUE = 1\n")
    (root / "docs/.memory/other/note.md").write_text("an ordinary file\n")
    found = sorted(path.relative_to(root).as_posix() for path in ingest._files(root))
    assert found == ["docs/.memory/other/note.md", "examples/sample/src/app.py"]


def relink(path, target):
    if path.is_symlink():
        path.unlink()
    path.symlink_to(target)


def lead_to_user(root):
    (root / "pkg2").mkdir()
    (root / "pkg2" / "limits.py").write_text("LIMIT = 5\n")
    (root / "user.py").write_text(
        "from pkg.alias import LIMIT\nfrom lib.limits import LIMIT as OTHER\n\n\ndef use():\n    return LIMIT + OTHER\n")


# A symlink is left out of the walk and an import is resolved through it, so a link that is moved, added or removed
# moves an import though no file changed (#451). (what is done first, what is done alone, whether the graph moves)
LINKS = {
    "retargeted": (lambda root: relink(root / "pkg" / "alias.py", "limits.py"),
                   lambda root: relink(root / "pkg" / "alias.py", "module_00.py"), True),
    "added": (lambda root: None, lambda root: relink(root / "pkg" / "alias.py", "limits.py"), True),
    "removed": (lambda root: relink(root / "pkg" / "alias.py", "limits.py"),
                lambda root: (root / "pkg" / "alias.py").unlink(), True),
    "to a directory, retargeted": (lambda root: relink(root / "lib", "pkg"), lambda root: relink(root / "lib", "pkg2"), True),
    "to a directory, removed": (lambda root: relink(root / "lib", "pkg"), lambda root: (root / "lib").unlink(), True),
    "in a chain, its second link retargeted": (
        lambda root: (relink(root / "pkg" / "via.py", "limits.py"), relink(root / "pkg" / "alias.py", "via.py")),
        lambda root: relink(root / "pkg" / "via.py", "module_00.py"), True),
    "broken, then broken elsewhere": (lambda root: relink(root / "pkg" / "alias.py", "gone.py"),
                                      lambda root: relink(root / "pkg" / "alias.py", "gone_too.py"), False),
    "to a file outside the root, retargeted": (
        lambda root: (root.parent.joinpath("one.py").write_text("ONE = 1\n"), root.parent.joinpath("two.py").write_text("TWO = 2\n"),
                      relink(root / "pkg" / "alias.py", root.parent / "one.py")),
        lambda root: relink(root / "pkg" / "alias.py", root.parent / "two.py"), False),
    "to a directory named like an excluded one, retargeted": (
        lambda root: (relink(root / "vendor", "pkg"), root.joinpath("uses_vendor.py").write_text("from vendor.limits import LIMIT\n")),
        lambda root: relink(root / "vendor", "pkg2"), True),
    "into a loop": (lambda root: relink(root / "pkg" / "alias.py", "limits.py"),
                    lambda root: (relink(root / "pkg" / "alias.py", "loop.py"), relink(root / "pkg" / "loop.py", "alias.py")), True),
}


@pytest.mark.parametrize("name", list(LINKS))
def test_a_link_changed_alone_is_noticed_by_a_running_indexer(workspace, monkeypatch, name):
    store, pid, root = workspace
    first, change, moves = LINKS[name]
    lead_to_user(root)
    first(root)
    index_project(store, pid)
    before = index_state(store, pid)
    change(root)  # nothing else: no file is written
    passes = []
    original = ingest._index_project_locked
    monkeypatch.setattr(ingest, "_index_project_locked", lambda *a: passes.append(1) or original(*a))
    result = index_project_if_changed(store, pid)
    monkeypatch.undo()
    assert passes == [1] and "skipped" not in result and not result["errors"]
    assert result["changed"] == 0 and result["read"] == 0
    # Where the imports lead is written from the remembered graph, the rows that moved: not the whole graph.
    assert (index_state(store, pid) != before) is moves
    assert not moves or 0 < result["graph_written"]["edges"] < result["edges"] / 4
    assert_same_as_a_full_run(store, pid)


def test_a_round_in_which_nothing_changed_does_no_pass_and_no_write_though_links_are_there(workspace, monkeypatch):
    store, pid, root = workspace
    lead_to_user(root)
    relink(root / "lib", "pkg")
    relink(root / "pkg" / "alias.py", "limits.py")
    index_project(store, pid)
    asked = []
    original = ArcadeClient._request
    monkeypatch.setattr(ArcadeClient, "_request", lambda self, method, path, **kw: asked.append(path) or original(self, method, path, **kw))
    monkeypatch.setattr(ingest, "_index_project_locked", lambda *a: pytest.fail("An unchanged tree was indexed"))
    assert index_project_if_changed(store, pid)["skipped"] is True
    assert asked and all("/query/" in path for path in asked) and len(asked) <= 4, asked  # the project is looked up, nothing written


def test_the_walk_notes_the_symlinks_it_leaves_out_and_where_they_lead(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "pkg" / "real.py").write_text("X = 1\n")
    (tmp_path / "outside.py").write_text("Y = 1\n")
    (root / ".gitignore").write_text("ignored.py\n")
    for name, target in (("pkg/file_link.py", "real.py"), ("lib", "pkg"), ("pkg/broken.py", "gone.py"),
                         ("pkg/out.py", tmp_path / "outside.py"), ("pkg/a.py", "b.py"), ("pkg/b.py", "a.py"),
                         ("ignored.py", "pkg/real.py"), ("node_modules/dep/link.py", "../../pkg/real.py"),
                         ("vendor", "pkg")):
        (root / name).symlink_to(target)
    links: list = []
    found = sorted(path.relative_to(root).as_posix() for path in ingest._files(root, links=links))
    assert found == [".gitignore", "pkg/real.py"]  # as before: no link is a file of the project
    assert sorted(links) == sorted((str(root / name), target) for name, target in (
        ("lib", "pkg"), ("pkg/a.py", "b.py"), ("pkg/b.py", "a.py"), ("pkg/broken.py", "gone.py"),
        ("pkg/file_link.py", "real.py"), ("pkg/out.py", str(tmp_path / "outside.py")),
        ("ignored.py", "pkg/real.py"), ("vendor", "pkg"))), links  # one in a directory the walk leaves out (node_modules) is not noted


def test_a_link_replaced_under_the_walk_is_noted_and_does_not_fail_it(tmp_path, monkeypatch):
    (tmp_path / "a.py").symlink_to("b.py")
    monkeypatch.setattr(ingest.os, "readlink", lambda item: (_ for _ in ()).throw(FileNotFoundError(item)))
    links: list = []
    assert list(ingest._files(tmp_path, links=links)) == []
    assert links == [(str(tmp_path / "a.py"), None)]
