"""A folder this machine cannot read changes nothing of its project (#371).

The trial of 22 November: a project indexed on one machine lost its whole code graph when the knowledge service
of another machine, which has no such folder, scanned it. Two installations share one store here, as in
tests/test_store_writer.py, and the project's rows are counted before and after.
"""
import asyncio
import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from cairnmind import project_folders
from cairnmind.config import Settings
from cairnmind.evidence import evidence_states
from cairnmind.ingest import GRAPH_EDGE_KINDS, GRAPH_NODE_KINDS, index_project, index_project_if_changed
from cairnmind.retrieval import Retriever
from cairnmind.service import create_app
from cairnmind.store import Store
from cairnmind.store_writer import take_over

from test_service import LocalModelFixture


class Vectors:
    async def embed(self, texts):
        return [[0.25, 0.5, 0.75, 1.0] for _ in texts]

    async def model_identity(self, name):
        return "embedder@digest"


def embed_all(store):
    retriever = Retriever(store, store.settings, Vectors())
    while job := store.claim_job("test", kinds=["embed"]):
        asyncio.run(retriever.embed_pending(job))
        store.finish_job(job["id"], worker=job["worker"], expected_attempts=job["attempts"])


def second_installation(tmp_path, first, name="second"):
    """A data directory of another installation whose configuration names the first one's database."""
    data_dir = tmp_path / name
    data_dir.mkdir(mode=0o700)
    (data_dir / "config.json").write_text(json.dumps({"arcade_database": first.settings.arcade_database}))
    return data_dir


def make_repo(root):
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "auth.py").write_text("def authenticate(token):\n    return check(token)\n\n"
                                          "def check(token):\n    return token == 'valid'\n")
    (root / "app.py").write_text("from pkg.auth import authenticate\n\ndef handler(t):\n    return authenticate(t)\n")
    (root / "README.md").write_text("# Demo\n\nThe login keeps signed tokens.\n")


def ids(store, table, pid):
    return {row["id"] for row in store.query(f"SELECT id FROM {table} WHERE project_id=:p", {"p": pid})}


def counts(store, pid, records=()):
    """Everything of a project that indexing or a scan could touch, as values that compare."""
    params = {"p": pid, "nk": list(GRAPH_NODE_KINDS), "ek": list(GRAPH_EDGE_KINDS)}

    def n(sql):
        return store.query(sql, params)[0]["n"]
    markers = store.query("SELECT graph_fingerprint,graph_nodes,graph_edges FROM projects WHERE id=:p", params)[0]
    return {
        "graph_nodes": n("SELECT count(*) AS n FROM nodes WHERE project_id=:p AND kind IN :nk"),
        "graph_edges": n("SELECT count(*) AS n FROM edges WHERE project_id=:p AND kind IN :ek"),
        "source_nodes": n("SELECT count(*) AS n FROM nodes WHERE project_id=:p AND kind='source'"),
        "supported_by": n("SELECT count(*) AS n FROM edges WHERE project_id=:p AND kind='supported_by'"),
        "sources": {row["id"]: row["is_current"] for row in
                    store.query("SELECT id,is_current FROM sources WHERE project_id=:p", params)},
        "chunks": ids(store, "chunks", pid), "jobs": ids(store, "jobs", pid),
        "embeddings": n("SELECT count(*) AS n FROM embeddings WHERE project_id=:p"),
        "records": {row["id"]: row["version"] for row in
                    store.query("SELECT id,version FROM records WHERE project_id=:p", params)},
        "revisions": ids(store, "record_revisions", pid),
        "evidence": {rec["id"]: evidence_states(store, pid, [rec]) for rec in records},
        "markers": (markers.get("graph_fingerprint"), markers.get("graph_nodes"), markers.get("graph_edges")),
    }


def indexed(tmp_path, name="cairnmind", folder="proj-b"):
    """A store with a project of four files, indexed and embedded, and a record that cites one of them."""
    store = Store(tmp_path / "first")
    root = tmp_path / "mac" / folder
    make_repo(root)
    pid = store.add_project(name, root)["id"]
    index_project(store, pid)
    embed_all(store)
    source = store.query("SELECT id FROM sources WHERE project_id=:p AND uri=:u AND is_current=true",
                         {"p": pid, "u": str(root / "pkg" / "auth.py")})[0]["id"]
    record = store.remember(pid, {"kind": "decision", "key": "login", "content": "Keep login behavior",
                                  "metadata": {"evidence": "def authenticate"}}, [source])
    return store, pid, root, record


def test_the_service_of_a_machine_without_the_folder_changes_nothing_of_the_project(tmp_path):
    first, pid, mac, record = indexed(tmp_path)
    before = counts(first, pid, [record])
    assert before["graph_nodes"] > 0 and before["graph_edges"] > 0 and before["embeddings"] > 0
    assert before["evidence"][record["id"]], "the record's evidence is read"
    away = mac.with_name("proj-b.away")
    mac.rename(away)  # the folder is on the other machine
    second_dir = second_installation(tmp_path, first)
    second = Store(second_dir)
    take_over(second, "trial")
    second = Store(second_dir)
    copy = tmp_path / "wsl" / "proj"
    make_repo(copy)
    other = second.add_project("wsl-repo", copy)["id"]
    index_project(second, other)
    index_project(second, other)
    assert counts(second, pid, [record]) == before
    settings = Settings.load(second_dir)
    settings.poll_seconds = 0.05
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=LocalModelFixture())) as client:
        for _ in range(400):
            background = client.get("/status", headers=headers).json()["background"]
            if background.get("last_scan_at"):
                break
            time.sleep(0.05)
    assert background.get("last_scan_at"), "the service scanned"
    assert counts(second, pid, [record]) == before
    sentence = next(error for error in background["scan_errors"] if error.startswith("cairnmind: "))
    assert str(mac) in sentence and "unavailable on this machine" in sentence
    assert counts(second, other)["graph_nodes"] > 0  # the project of this machine is indexed as usual
    # The folder is back on the first machine, which takes the writer's role over again.
    away.rename(mac)
    take_over(first, "back")
    first = Store(first.data_dir)
    result = index_project(first, pid)
    assert result["graph_unchanged"] is True and "unavailable" not in result
    assert counts(first, pid, [record]) == before


@pytest.mark.parametrize("state", ["missing", "unreadable"])
def test_a_missing_or_unreadable_root_changes_nothing_and_is_named(tmp_path, state):
    if state == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads every folder")
    store, pid, root, record = indexed(tmp_path)
    before = counts(store, pid, [record])
    if state == "missing":
        shutil.rmtree(root)
    else:
        root.chmod(0)
    try:
        result = index_project(store, pid)
        assert result["deleted"] == 0 and result["graph_unchanged"] is True
        assert [item["state"] for item in result["unavailable"]] == [state]
        assert result["unavailable"][0]["root"] == str(root.resolve())
        assert result["errors"] == [item["message"] for item in result["unavailable"]]
        assert ("does not exist here" if state == "missing" else "cannot be read here") in result["errors"][0]
        assert counts(store, pid, [record]) == before
        # A project that is not here costs one stat per round and is still named every round.
        again = index_project_if_changed(store, pid)
        assert again["skipped"] is True and again["errors"] == result["errors"]
        assert again["unavailable"] == result["unavailable"]
    finally:
        root.chmod(0o755) if root.exists() else None


def test_a_subfolder_that_cannot_be_listed_keeps_its_files_in_the_graph(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root lists every folder")
    store, pid, root, record = indexed(tmp_path)
    before = counts(store, pid, [record])
    (root / "pkg").chmod(0)
    try:
        result = index_project(store, pid)
    finally:
        (root / "pkg").chmod(0o755)
    assert any(error.startswith("Cannot list") for error in result["errors"])
    assert result["deleted"] == 0 and "unavailable" not in result
    assert counts(store, pid, [record]) == before


def test_an_emptied_root_and_a_deleted_file_are_still_deletions(tmp_path):
    store, pid, root, record = indexed(tmp_path)
    (root / "README.md").unlink()
    result = index_project(store, pid)
    assert result["deleted"] == 1 and "unavailable" not in result and result["errors"] == []
    after = counts(store, pid)
    assert sum(after["sources"].values()) == sum(1 for _ in root.rglob("*.py")) and after["graph_nodes"] > 0
    for item in list(root.iterdir()):
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    result = index_project(store, pid)  # the folder opens and is empty: its files were deleted
    assert result["deleted"] == 3 and "unavailable" not in result
    after = counts(store, pid)
    assert not any(after["sources"].values()) and after["graph_nodes"] == 0 and after["graph_edges"] == 0


def test_a_graph_with_files_of_an_older_parser_that_cannot_be_read_here_is_kept(tmp_path):
    store, pid, root, record = indexed(tmp_path)
    old = store.query("SELECT id FROM sources WHERE project_id=:p AND uri=:u AND is_current=true",
                      {"p": pid, "u": str(root / "app.py")})[0]["id"]
    store.command("UPDATE sources SET metadata.analysis_version='old' WHERE id=:id", {"id": old})
    before = counts(store, pid, [record])
    shutil.move(str(root), str(root) + ".away")
    result = index_project(store, pid)
    assert result["graph_unchanged"] is True and "graph_written" not in result
    assert any(error.startswith("The code graph of project cairnmind is kept as it was") and "files of it" in error
               and error.endswith("rebuilds it.") and ": 1." in error for error in result["errors"])
    assert counts(store, pid, [record]) == before
    assert not index_project_if_changed(store, pid).get("skipped")  # not recorded: named until rebuilt


def test_two_projects_from_the_same_files_share_no_rows(tmp_path):
    store, pid, root, record = indexed(tmp_path)
    before = counts(store, pid, [record])
    copy = tmp_path / "wsl" / "proj"
    make_repo(copy)
    other = store.add_project("wsl-repo", copy)["id"]
    index_project(store, other)
    index_project_if_changed(store, other)
    index_project(store, other)
    assert counts(store, pid, [record]) == before
    for table in ("sources", "chunks", "nodes", "edges", "jobs"):
        assert ids(store, table, other) and not ids(store, table, pid) & ids(store, table, other), table
    assert project_folders.unavailable(store) == []
