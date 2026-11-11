"""After an edit, indexing reads the edited file only and writes only the graph rows that differ (issue #62).

The constraint: the index must stay exactly what a run that reads every file and replaces the
whole graph would produce. Every scenario here ends by comparing the two.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from cairnmind import ingest
from cairnmind.config import Settings
from cairnmind.ingest import forget_index_signature, index_project, index_project_if_changed
from cairnmind.store import Store, _clean


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
    (root / "pkg" / "limits.py").write_text("LIMIT = 3\n\n\ndef allowed(token):\n    return token == 'valid'\n")
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
        return sorted((_clean(row) for row in store.query(sql, {"project": pid})), key=lambda row: str(row.get("id")))

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
    assert lookups == [str(root / "pkg" / "limits.py")]
    assert (edited["files"], edited["changed"], edited["unchanged"], edited["read"]) == (19, 1, 18, 1)
    assert_same_as_a_full_run(store, pid)


def test_after_one_edit_only_rows_of_that_file_are_written_to_the_graph(workspace, monkeypatch):
    store, pid, root = workspace
    first = index_project(store, pid)
    (root / "pkg" / "module_03.py").write_text("VALUE_3 = 33\n")
    nodes, edges = [], []
    upsert_node, upsert_edge = store.upsert_node, store.upsert_edge
    monkeypatch.setattr(store, "upsert_node", lambda item, tx=None: nodes.append(item["kind"]) or upsert_node(item, tx=tx))
    monkeypatch.setattr(store, "upsert_edge", lambda item, tx=None: edges.append(item["kind"]) or upsert_edge(item, tx=tx))
    edited = index_project_if_changed(store, pid)
    monkeypatch.undo()
    # The file's own node and its new snapshot node, joined by one edge: the file defines nothing.
    assert sorted(nodes) == ["file", "source"] and edges == ["snapshot_of"]
    assert edited["graph_written"] == {"nodes": 2, "edges": 1}
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


@pytest.mark.parametrize("damage", [
    "DELETE FROM edges WHERE project_id=:project AND kind='defines' LIMIT 1",
    "UPDATE projects SET graph_fingerprint='written-by-another-process' WHERE id=:project",
    "UPDATE projects SET graph_edges=1 WHERE id=:project",
])
def test_a_graph_changed_behind_the_process_is_replaced_in_full(workspace, damage):
    store, pid, root = workspace
    first = index_project(store, pid)
    store.command(damage, {"project": pid})
    (root / "pkg" / "module_03.py").write_text("VALUE_3 = 33\n")
    repaired = index_project_if_changed(store, pid)
    assert repaired["read"] == 1  # the files are still trusted; the graph is not
    assert repaired["graph_written"] == {"nodes": first["nodes"], "edges": first["edges"]}
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
    original = store.upsert_edge
    monkeypatch.setattr(store, "upsert_edge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("engine went away")))
    with pytest.raises(RuntimeError, match="engine went away"):
        index_project(store, pid)
    monkeypatch.setattr(store, "upsert_edge", original)
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
    assert index_project(store, pid)["graph_written"] == {"nodes": 2, "edges": 1}  # the compared graph was trusted
    # The same, but a stored row differs from what the fingerprint promises.
    forget_index_signature(store, pid)
    store.command("UPDATE nodes SET label='changed elsewhere' WHERE project_id=:project AND kind='symbol' LIMIT 1",
                  {"project": pid})
    assert index_project(store, pid)["graph_unchanged"] is True
    (root / "pkg" / "module_04.py").write_text("VALUE_4 = 44\n")
    repaired = index_project(store, pid)
    assert repaired["graph_written"] == {"nodes": first["nodes"], "edges": first["edges"]}
    assert_same_as_a_full_run(store, pid)
