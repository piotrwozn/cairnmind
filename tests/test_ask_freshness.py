"""A repeated question must not re-read an unchanged tree or scan collections (issue #23)."""
from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest

from cairnmind import ingest
from cairnmind.config import Settings
from cairnmind.ingest import forget_index_signature, index_project, index_project_if_changed, tree_signature
from cairnmind.journal import Journal
from cairnmind.plans import PlanManager
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import Store


class LocalModel:
    def __init__(self):
        self.calls = 0

    async def models(self):
        return {"models": []}

    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        evidence = json.loads(messages[1]["content"])["initial_evidence"][0]
        return {"message": {"content": json.dumps({
            "answer": "The authenticate function handles login.",
            "citations": [{"source_id": evidence["source_id"], "quote": "def authenticate"}], "unknowns": []})}}


@pytest.fixture(autouse=True)
def trusted_timestamps(monkeypatch):
    """The tests index files they wrote a moment ago; the racy-clean rule has its own test."""
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", -10**12)


@pytest.fixture
def workspace(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "auth.py").write_text("def authenticate(token):\n    return token == 'valid'\n")
    (root / "pkg" / "util.py").write_text("LIMIT = 3\n")
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "cache.bin").write_text("first")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    store = Store(settings.data_dir)
    project = store.add_project("demo", root)
    forget_index_signature(store)
    return settings, store, project, root


def test_tree_signature_changes_with_every_change_that_indexing_would_see(workspace):
    _, store, project, root = workspace
    pid = project["id"]
    first = tree_signature(store, pid)
    assert tree_signature(store, pid) == first
    (root / "ignored" / "cache.bin").write_text("second, and longer")
    assert tree_signature(store, pid) == first  # an ignored file is not indexed
    seen = {first}

    def changed():
        value = tree_signature(store, pid)
        assert value not in seen
        seen.add(value)

    target = root / "pkg" / "util.py"
    before = target.stat()
    target.write_text("LIMIT = 4\n")  # the same size
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))  # a tool restores the modification time
    assert target.stat().st_mtime_ns == before.st_mtime_ns and target.stat().st_size == before.st_size
    changed()
    edited = tree_signature(store, pid)
    (root / "new.py").write_text("NEW = 1\n")
    changed()
    (root / "new.py").rename(root / "renamed.py")
    changed()
    (root / "renamed.py").unlink()
    assert tree_signature(store, pid) == edited  # the tree is as it was before the file was added
    (root / ".gitignore").write_text("ignored/\npkg/\n")
    changed()
    second = root.parent / "second"
    second.mkdir()
    (second / "extra.py").write_text("EXTRA = 1\n")
    store.add_project_root(pid, second)
    changed()


def test_an_unchanged_tree_is_not_read_or_looked_up_again(workspace, monkeypatch):
    _, store, project, root = workspace
    pid = project["id"]
    first = index_project_if_changed(store, pid)
    assert first["files"] == 3 and "skipped" not in first
    queries, opened = [], []
    original_query, original_open = store.query, io.open
    monkeypatch.setattr(store, "query", lambda *a, **k: queries.append(a[0]) or original_query(*a, **k))
    monkeypatch.setattr(store, "command", lambda *a, **k: pytest.fail("An unchanged tree wrote to the store"))
    monkeypatch.setattr(ingest, "_index_project_locked", lambda *a: pytest.fail("An unchanged tree was indexed"))

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and Path(file).resolve().is_relative_to(root):
            opened.append(Path(file).name)
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    skipped = index_project_if_changed(store, pid)
    monkeypatch.undo()
    # The guard really saw the walk: the ignore file is read, and nothing else under the root.
    assert opened == [".gitignore"]
    assert skipped == {"files": 0, "changed": 0, "unchanged": 0, "deleted": 0, "chunks": 0, "errors": [], "skipped": True}
    # Resolving the project and its roots: the cost does not grow with the number of files.
    assert len(queries) <= 3 and not any("sources" in sql or "chunks" in sql for sql in queries)


def test_an_edit_a_new_file_and_a_deletion_are_indexed(workspace):
    _, store, project, root = workspace
    pid = project["id"]
    index_project_if_changed(store, pid)
    (root / "auth.py").write_text("def authenticate(token):\n    return token == 'other'\n")
    edited = index_project_if_changed(store, pid)
    assert (edited["changed"], edited["unchanged"], edited.get("skipped")) == (1, 2, None)
    assert index_project_if_changed(store, pid)["skipped"] is True
    (root / "added.py").write_text("ADDED = 1\n")
    assert index_project_if_changed(store, pid)["changed"] == 1
    (root / "added.py").unlink()
    assert index_project_if_changed(store, pid)["deleted"] == 1
    assert index_project_if_changed(store, pid)["skipped"] is True


def test_a_failed_or_forgotten_indexing_is_never_skipped(workspace, tmp_path):
    settings, store, project, root = workspace
    pid = project["id"]
    index_project(store, pid)
    assert index_project_if_changed(store, pid)["skipped"] is True
    forget_index_signature(store, pid)
    assert "skipped" not in index_project_if_changed(store, pid)
    missing = tmp_path / "gone"
    missing.mkdir()
    store.add_project_root(pid, missing)
    missing.rmdir()
    failed = index_project_if_changed(store, pid)
    assert failed["errors"] and "skipped" not in failed
    # A run with errors is not a complete index, so the next question indexes again.
    assert "skipped" not in index_project_if_changed(store, pid)


def test_the_signature_of_one_store_does_not_skip_indexing_in_another(workspace, tmp_path):
    _, store, project, root = workspace
    index_project(store, project["id"])
    other = Store(tmp_path / "other-memory")
    same = other.add_project("demo", root)
    assert "skipped" not in index_project_if_changed(other, same["id"])


def test_cache_stamp_moves_only_for_writes_that_can_change_an_answer(workspace):
    settings, store, project, root = workspace
    pid = project["id"]
    other = store.add_project("other", root.parent / "other-repo")
    stamp = store.cache_stamp(pid)
    assert store.cache_stamp(pid) == stamp
    seen = {stamp}

    def moved():
        value = store.cache_stamp(pid)
        assert value not in seen
        seen.add(value)

    source = store.snapshot(pid, "manual://note", "A note", "manual")
    moved()
    record = store.remember(pid, {"kind": "fact", "content": "A fact"}, [source["id"]])
    moved()
    current = store.cache_stamp(pid)
    # Rewriting identical values, queue jobs, sessions and another project's writes do not move it.
    store.enqueue("extract", pid, {"source_id": source["id"]}, "extract:test")
    SessionManager(store, settings).prepare(pid, "codex", timeout=0)
    foreign = store.snapshot(other["id"], "manual://other", "Elsewhere", "manual")
    store.remember(other["id"], {"kind": "fact", "content": "Elsewhere"}, [foreign["id"]])
    assert store.cache_stamp(pid) == current
    assert store.cache_stamp(other["id"]) != store.cache_stamp(pid)
    store.remember(pid, {"id": record["id"], "expected_version": record["version"], "status": "confirmed"}, [source["id"]])
    moved()
    PlanManager(store).save(pid, {"title": "Plan", "goal": "Goal"}, [{"key": "a", "content": "Step"}], "one",
                            worktree=str(root), activate=True)
    moved()
    index_project(store, pid)
    moved()


def test_cache_stamp_reads_only_new_journal_events(workspace, monkeypatch):
    _, store, project, root = workspace
    pid = project["id"]
    source = store.snapshot(pid, "manual://note", "A note", "manual")
    other = store.add_project("other", root.parent / "other-repo")
    foreign = store.snapshot(other["id"], "manual://other", "Elsewhere", "manual")
    other_stamp = store.cache_stamp(other["id"])
    store.cache_stamp(pid)
    queries = []
    original = store.query
    monkeypatch.setattr(store, "query", lambda *a, **k: queries.append(a[0]) or original(*a, **k))
    store.cache_stamp(pid)
    assert queries == ["SELECT epoch,seq,first_seq FROM journal_state WHERE id='current'"]
    monkeypatch.undo()
    for index in range(1203):
        store.remember(pid, {"kind": "fact", "content": f"Fact {index}"}, [source["id"]])
        if index == 0:
            first = store.cache_stamp(pid)
        if index == 1201:
            # Written after more than one page of 1,000 events: only a full read sees it.
            store.remember(other["id"], {"kind": "fact", "content": "Late fact"}, [foreign["id"]])
    assert store.cache_stamp(pid) != first
    assert store.cache_stamp(other["id"]) != other_stamp
    reads = []
    plain_query = store.query
    monkeypatch.setattr(store, "query", lambda *a, **k: reads.append(a) or plain_query(*a, **k))
    monkeypatch.setattr(store, "transaction", lambda: pytest.fail("The stamp took the store's writer lock"))
    monkeypatch.setattr(store, "_lock", lambda: pytest.fail("The stamp took the store's writer lock"))
    before = store.cache_stamp(pid)
    monkeypatch.undo()
    store.remember(pid, {"kind": "fact", "content": "One more"}, [source["id"]])
    monkeypatch.setattr(store, "query", lambda *a, **k: reads.append(a) or plain_query(*a, **k))
    monkeypatch.setattr(store, "transaction", lambda: pytest.fail("The stamp took the store's writer lock"))
    del reads[:]
    assert store.cache_stamp(pid) != before
    # The journal state and one page of events for one new write, not the whole history.
    assert len(reads) == 2 and len(plain_query(*reads[1])) < 20
    monkeypatch.undo()
    stamp = store.cache_stamp(pid)
    with store.transaction() as tx:
        Journal.reset(tx, reason="restore")
    assert store.cache_stamp(pid) != stamp  # a new journal epoch invalidates everything


def test_a_repeated_question_does_not_index_again_and_is_served_from_the_cache(workspace, monkeypatch):
    settings, store, project, root = workspace
    model = LocalModel()
    headers = {"Authorization": f"Bearer {settings.token}"}
    body = {"project": "demo", "question": "Where is authenticate?"}
    with TestClient(create_app(settings, start_worker=False, ollama=model)) as client:
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is False
        indexed = []
        original = ingest._index_project_locked
        monkeypatch.setattr(ingest, "_index_project_locked", lambda *a: indexed.append(a) or original(*a))
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is True
        assert client.post("/ask", json=body, headers=headers).json()["cached"] is True
        assert indexed == [] and model.calls == 1
        (root / "auth.py").write_text("def authenticate(token):\n    return token == 'changed'\n")
        answer = client.post("/ask", json=body, headers=headers).json()
        assert answer["cached"] is False and len(indexed) == 1 and model.calls == 2


def test_files_touched_just_before_indexing_are_indexed_once_more(workspace, monkeypatch):
    _, store, project, root = workspace
    pid = project["id"]
    # A window longer than any test run: the files count as written a moment ago however slow the
    # machine is. A second write inside one coarse timestamp tick could not be told apart, so this
    # indexing is not trusted as a baseline.
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", 10**18)
    index_project(store, pid)
    assert "skipped" not in index_project_if_changed(store, pid)
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", 2_000_000_000)
    monkeypatch.setattr(ingest.time, "time_ns", lambda: 10**19)  # long after every file time
    index_project(store, pid)
    assert index_project_if_changed(store, pid)["skipped"] is True


def test_an_indexing_that_raises_leaves_no_signature_behind(workspace, monkeypatch):
    _, store, project, root = workspace
    pid = project["id"]
    index_project(store, pid)
    (root / "temporary.py").write_text("TEMPORARY = 1\n")
    original = ingest._build_graph
    monkeypatch.setattr(ingest, "_build_graph", lambda *a: (_ for _ in ()).throw(RuntimeError("engine went away")))
    with pytest.raises(RuntimeError):
        index_project_if_changed(store, pid)
    monkeypatch.setattr(ingest, "_build_graph", original)
    (root / "temporary.py").unlink()
    # The tree is as it was at the last complete indexing, but the store holds the half-written run.
    repaired = index_project_if_changed(store, pid)
    assert "skipped" not in repaired and repaired["deleted"] == 1


def test_a_source_deactivated_while_answering_is_indexed_again_when_the_file_returns(workspace):
    _, store, project, root = workspace
    pid = project["id"]
    index_project(store, pid)
    signature = tree_signature(store, pid)
    source = store.query("SELECT FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                         {"project": pid, "uri": str(root / "pkg" / "util.py")})[0]
    (root / "pkg").rename(root / "pkg-away")
    assert ingest.current_source(store, source["id"])["deleted"] is True
    (root / "pkg-away").rename(root / "pkg")
    assert tree_signature(store, pid) == signature  # renaming a parent away and back changes no file time
    restored = index_project_if_changed(store, pid)
    assert "skipped" not in restored and restored["changed"] == 1
    assert store.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                       {"project": pid, "uri": str(root / "pkg" / "util.py")})


def test_a_directory_that_cannot_be_listed_is_an_error_and_deactivates_nothing(workspace, monkeypatch):
    _, store, project, root = workspace
    pid = project["id"]
    index_project(store, pid)
    original = os.scandir

    def scandir(path="."):
        if Path(path).name == "pkg":
            raise PermissionError(13, "Permission denied", str(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    forget_index_signature(store, pid)
    result = index_project_if_changed(store, pid)
    assert result["deleted"] == 0 and any("Cannot list" in error and "pkg" in error for error in result["errors"])
    assert store.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                       {"project": pid, "uri": str(root / "pkg" / "util.py")})
    assert "skipped" not in index_project_if_changed(store, pid)  # an incomplete walk is never a baseline
    monkeypatch.undo()
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", -10**12)
    index_project(store, pid)
    assert index_project_if_changed(store, pid)["skipped"] is True


def test_memory_import_makes_the_next_question_index_again(workspace, monkeypatch):
    settings, store, project, root = workspace
    from cairnmind.memory import MemoryManager
    # The import itself is covered elsewhere; here only what the route does after it matters.
    monkeypatch.setattr(MemoryManager, "import_snapshot",
                        lambda self, *args, **kwargs: {"status": "imported", "project": {"id": project["id"]}})
    note = store.snapshot(project["id"], "manual://memory", "The release identifier is zephyr_214.", "manual")
    store.remember(project["id"], {"kind": "fact", "content": note["content"]}, [note["id"]])
    index_project(store, project["id"])
    saved = MemoryManager(store).snapshot(project["id"])
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModel())) as client:
        assert index_project_if_changed(store, project["id"])["skipped"] is True
        imported = client.post("/memory/import", json={"path": saved["path"], "root": str(root)}, headers=headers)
        assert imported.status_code == 200, imported.text
        assert "skipped" not in index_project_if_changed(store, project["id"])
