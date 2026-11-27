"""A folder this machine cannot read changes nothing of its project (#371).

The trial of 22 November: a project indexed on one machine lost its whole code graph when the knowledge service
of another machine, which has no such folder, scanned it. Two installations share one store here, as in
tests/test_store_writer.py, and the project's rows are counted before and after.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairnmind.memory import project_folders
from cairnmind.storage.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.memory.evidence import evidence_states
from cairnmind.scip.ingest import GRAPH_EDGE_KINDS, GRAPH_NODE_KINDS, index_project, index_project_if_changed
from cairnmind.agents.retrieval import Retriever
from cairnmind.service import create_app
from cairnmind.store import Store
from cairnmind.storage.store_writer import take_over

from test_service import LocalModelFixture


@pytest.fixture(autouse=True)
def identified_machine(monkeypatch):
    """Whatever the test machine is (a container without a machine id, a Mac whose ioreg failed), these tests run on
    one that can be told from another; the host-name case patches its own identity."""
    from cairnmind.storage import store_writer
    monkeypatch.setattr(store_writer, "machine_identity", lambda: "machine-id:fixture")


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
    if state == "unreadable" and (not hasattr(os, "geteuid") or os.geteuid() == 0):
        pytest.skip("requires POSIX chmod(0) enforcement")
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
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("requires POSIX chmod(0) enforcement")
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
    again = index_project_if_changed(store, pid)  # only the folder that is away holds it: nothing can change, so the round skips
    assert again["skipped"] is True and any(error.startswith("The code graph of project cairnmind is kept") for error in again["errors"])


@pytest.mark.parametrize("failure", [ValueError("the index of this project broke"),
                                     ArcadeError("ArcadeDB HTTP 500: the index of this project broke", status_code=500)])
def test_one_project_that_fails_does_not_stop_the_round(tmp_path, monkeypatch, failure):
    from cairnmind.scip import ingest
    store = Store(tmp_path / "data")
    for name in ("aaa-fails", "bbb-works"):
        make_repo(tmp_path / name)
        store.add_project(name, tmp_path / name)
    failing = store.resolve_project("aaa-fails")["id"]
    working = store.resolve_project("bbb-works")["id"]
    real = ingest.index_project_if_changed

    def scan(scanned, project_id):
        if project_id == failing:
            raise failure
        return real(scanned, project_id)
    monkeypatch.setattr(ingest, "index_project_if_changed", scan)
    settings = Settings.load(store.data_dir)
    settings.poll_seconds = 0.05
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=LocalModelFixture())) as client:
        for _ in range(400):
            background = client.get("/status", headers=headers).json()["background"]
            if background.get("last_scan_at"):
                break
            time.sleep(0.05)
    assert f"aaa-fails: {failure}" in background["scan_errors"]  # an answer of the database: this project's error
    assert counts(store, working)["graph_nodes"] > 0 and counts(store, failing)["graph_nodes"] == 0


def test_a_store_that_does_not_answer_is_one_error_of_the_round(tmp_path, monkeypatch):
    from cairnmind.scip import ingest
    store = Store(tmp_path / "data")
    for name in ("aaa-down", "bbb-after"):
        make_repo(tmp_path / name)
        store.add_project(name, tmp_path / name)
    asked = []

    def scan(scanned, project_id):
        asked.append(project_id)
        raise ArcadeError("Local ArcadeDB is unavailable (ConnectError)")
    monkeypatch.setattr(ingest, "index_project_if_changed", scan)
    settings = Settings.load(store.data_dir)
    settings.poll_seconds = 0.05
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=LocalModelFixture())) as client:
        for _ in range(400):
            if client.get("/status", headers=headers).json()["background"].get("scanner_error"):
                break
            time.sleep(0.05)
        background = client.get("/status", headers=headers).json()["background"]
    assert "unavailable" in background["scanner_error"]
    assert set(asked) == {store.resolve_project("aaa-down")["id"]}  # the round ends at the first, not one trace each


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


def lost_folder(tmp_path):
    """A store whose only project has its folder removed, as on a machine that never had it."""
    store, pid, root, record = indexed(tmp_path)
    shutil.rmtree(root)
    return store, pid, root


def test_status_doctor_and_index_name_the_unavailable_folder(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cairnmind.agents import api_runtime
    from cairnmind import cli
    from cairnmind.cli_cmds import doctor_attention
    from cairnmind.memory import integrations
    from cairnmind.storage import store_writer
    from test_first_start_round2 import healthy
    store, pid, root = lost_folder(tmp_path)
    sentence = project_folders.message(store.resolve_project(pid), {"root": str(root), "state": "missing"})
    entries = project_folders.unavailable(store)
    assert [(e["project"], e["root"], e["state"], e["message"]) for e in entries] == [
        ("cairnmind", str(root), "missing", sentence)]
    # The service says it in /status (every project, or the one asked for) and in /project-roots.
    settings = Settings.load(store.data_dir)
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=LocalModelFixture())) as client:
        assert client.get("/status", headers=headers).json()["unavailable_folders"] == entries
        assert client.get("/status", params={"project": "cairnmind"}, headers=headers).json()[
            "unavailable_folders"] == entries
        assert client.get("/project-roots", params={"project": pid}, headers=headers).json()["unavailable"] == entries
    # doctor: a note under "Notes:", the full report carries the entries, and the exit code does not change.
    monkeypatch.setattr(cli, "service_alive", lambda s: False)
    monkeypatch.setattr(integrations, "doctor", lambda s: dict(healthy()))
    monkeypatch.setattr(store_writer, "inspect", lambda s: {"role": "writer"})
    monkeypatch.setattr(api_runtime, "coding_profiles_status", lambda s: [])
    monkeypatch.setattr(cli, "store_for_read", lambda s: store)
    for terminal in (True, False):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        shown = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "doctor"])
        assert shown.exit_code == 0, shown.output
        if terminal:
            lines = shown.output.splitlines()
            assert lines[lines.index(doctor_attention.NOTES) + 1] == f"  - {sentence}"
        else:
            assert json.loads(shown.output)["project_folders"] == entries
    monkeypatch.setattr(cli, "store_for_read", lambda s: (_ for _ in ()).throw(ValueError("no store")))
    unreadable = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "doctor", "--json"])
    assert json.loads(unreadable.output)["project_folders"] is None
    # index: the JSON as before with the entries, each sentence on stderr, exit code 1.
    answer = {"files": 0, "errors": [sentence], "unavailable": entries}
    monkeypatch.setattr(cli, "request", lambda *args, **kwargs: answer)
    indexed_run = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "index", "cairnmind"])
    assert indexed_run.exit_code == 1 and json.loads(indexed_run.stdout)["unavailable"] == entries
    assert f"Cairnmind: {sentence}" in indexed_run.stderr
    monkeypatch.setattr(cli, "request", lambda *args, **kwargs: {"files": 4, "errors": []})
    assert CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "index", "cairnmind"]).exit_code == 0


def test_the_folder_notice_reads_in_english_and_polish():
    project = {"name": "cairnmind"}
    missing = {"root": "/Users/me/proj-b", "state": "missing", "host": None, "error": None}
    unreadable = {"root": "/Users/me/proj-c", "state": "unreadable", "host": None, "error": "Permission denied"}
    tail_en = (" Nothing of the project is changed for it: its files are not counted as deleted and its code graph "
               "is kept.")
    tail_pl = (" Nic w projekcie nie zmienia się z jego powodu: jego pliki nie są liczone jako usunięte, a graf kodu "
               "zostaje zachowany.")
    for folder, reason_en, reason_pl in (
            (missing, "it does not exist here", "nie istnieje tutaj"),
            (unreadable, "it cannot be read here (Permission denied)", "nie da się go tu odczytać (Permission denied)")):
        item = project_folders.entry(project, folder)
        english = (f"The folder {folder['root']} of project cairnmind is unavailable on this machine: "
                   f"{reason_en}.{tail_en}")
        assert item["message"] == project_folders.folder_notice([item]) == english
        assert project_folders.folder_notice([item], "pl") == (
            f"Folder {folder['root']} projektu cairnmind jest niedostępny na tym komputerze: {reason_pl}.{tail_pl}")
    both = [project_folders.entry(project, missing), project_folders.entry(project, unreadable)]
    assert project_folders.folder_notice(both) == " ".join(item["message"] for item in both)
    assert project_folders.folder_notice([]) == ""


# --- a folder belongs to the machine that registered it ---------------------------------------------------------

def machine(monkeypatch, name):
    """This test process acts as another machine; the stamps written before were written by the real one."""
    monkeypatch.setattr(project_folders, "this_machine", lambda: name)


def stamped(store, pid):
    return [(row.get("machine"), row.get("host")) for row in store.query(
        "SELECT machine,host FROM project_roots WHERE project_id=:p", {"p": pid})]


def test_a_folder_of_another_machine_is_not_indexed_even_when_the_path_exists_here(tmp_path, monkeypatch):
    from cairnmind.scip.ingest import current_source
    from cairnmind.storage.store_writer import label
    store, pid, root, record = indexed(tmp_path)
    before = counts(store, pid, [record])
    source = next(row["id"] for row in store.query(
        "SELECT id FROM sources WHERE project_id=:p AND uri=:u AND is_current=true",
        {"p": pid, "u": str(root / "app.py")}))
    machine(monkeypatch, "b")
    (root / "app.py").write_text("def handler():\n    return 'changed on this machine'\n")  # the same path, other files
    result = index_project(store, pid)
    assert result["files"] == 0 and result["changed"] == 0 and result["deleted"] == 0
    (item,) = result["unavailable"]
    assert item["state"] == "elsewhere" and item["host"] == label(socket.gethostname())
    assert "belongs to" in item["message"] and "only that machine indexes it" in item["message"]
    assert f"cairnmind project root cairnmind {root}" in item["message"]  # how the person moves it, by name
    assert counts(store, pid, [record]) == before
    assert index_project_if_changed(store, pid)["skipped"] is True
    # A cited file is not refreshed from a same-path folder of another machine, and nothing is deactivated.
    cited = current_source(store, source)
    assert "unavailable" in cited["freshness_warning"] and cited.get("is_current") is not False
    assert counts(store, pid, [record]) == before


def test_a_folder_without_a_machine_is_stamped_by_its_first_complete_index_here(tmp_path, monkeypatch):
    store, pid, root, record = indexed(tmp_path)
    here = project_folders.this_machine()
    assert [machine_id for machine_id, _ in stamped(store, pid)] == [here]  # a new registration is stamped
    store.command("UPDATE project_roots REMOVE machine, host WHERE project_id=:p", {"p": pid})  # as before #371
    assert stamped(store, pid) == [(None, None)]
    machine(monkeypatch, "b")
    away = root.with_name("proj-b.away")
    root.rename(away)
    index_project(store, pid)  # the folder is not here: not claimed
    assert stamped(store, pid) == [(None, None)]
    away.rename(root)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        (root / "pkg").chmod(0)
        try:
            index_project(store, pid)  # a walk that could not list everything is not complete: not claimed
        finally:
            (root / "pkg").chmod(0o755)
        assert stamped(store, pid) == [(None, None)]
    result = index_project(store, pid)
    assert "unavailable" not in result and result["errors"] == []
    assert [machine_id for machine_id, _ in stamped(store, pid)] == ["b"]
    # Claimed: another machine now leaves it alone.
    machine(monkeypatch, "c")
    assert index_project(store, pid)["unavailable"][0]["state"] == "elsewhere"


def test_project_root_moves_a_folder_to_this_machine_by_name(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cairnmind import cli
    from cairnmind.storage.store_writer import label
    store, pid, root, record = indexed(tmp_path)
    machine(monkeypatch, "b")
    assert index_project(store, pid)["unavailable"][0]["state"] == "elsewhere"
    (root / "app.py").write_text("def handler():\n    return 'moved'\n")
    monkeypatch.setattr(cli, "store_for", lambda settings: store)
    moved = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "project", "root", "cairnmind", str(root)])
    assert moved.exit_code == 0, moved.output
    assert json.loads(moved.stdout) == {"project": pid, "root": str(root.resolve()),
                                        "moved_from": label(socket.gethostname())}
    assert [machine_id for machine_id, _ in stamped(store, pid)] == ["b"]
    result = index_project(store, pid)  # now the folder is read here, and files that differ replace the indexed ones
    assert "unavailable" not in result and result["changed"] == 1 and result["errors"] == []
    # Registering a folder that already is this machine's, or a new one, says nothing of a move.
    assert store.add_project_root(pid, root) == {}


def test_a_backup_restored_on_another_machine_keeps_the_old_machines_folders(tmp_path, monkeypatch):
    store, pid, root, record = indexed(tmp_path)
    backup = store.backup(tmp_path / "old.cairnmind.json.gz")
    machine(monkeypatch, "b")  # the disaster-recovery case: a new store on another machine
    target = Store(tmp_path / "target")
    target.restore(backup)
    assert [machine_id for machine_id, _ in stamped(target, pid)] == [stamped(store, pid)[0][0]] != ["b"]
    before = counts(target, pid)
    assert before["graph_nodes"] > 0
    (root / "app.py").write_text("def handler():\n    return 'another file'\n")
    result = index_project(target, pid)
    assert result["unavailable"][0]["state"] == "elsewhere" and result["changed"] == 0
    assert counts(target, pid) == before  # worse than reading it here? no: nothing is replaced, and it is named
    target.add_project_root(pid, root)  # the person moves it, by name
    result = index_project(target, pid)
    assert "unavailable" not in result and result["changed"] == 1
def test_restore_on_another_machine_names_the_folders_it_leaves_with_the_old_one(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from cairnmind.terminal import person_proof
    from cairnmind.cli import app
    store, pid, root, record = indexed(tmp_path)
    backup = store.backup(tmp_path / "old.cairnmind.json.gz")
    machine(monkeypatch, "b")
    target = Store(tmp_path / "target")
    monkeypatch.setattr("cairnmind.cli.service_alive", lambda settings: False)
    monkeypatch.setattr(person_proof, "restore_proof", lambda settings: "key")
    result = CliRunner().invoke(app, ["--data-dir", str(target.data_dir), "restore", str(backup)])
    assert result.exit_code == 0, (result.output, result.exception)
    assert [item["state"] for item in json.loads(result.stdout)["unavailable_folders"]] == ["elsewhere"]
    assert "cairnmind project root" in result.stderr


def test_a_mac_whose_ioreg_fails_is_not_asked_by_every_folder_check(monkeypatch):
    from cairnmind.storage import store_writer
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("no ioreg")
    monkeypatch.undo()  # the module's fixture stands in for the identity here
    monkeypatch.setattr(store_writer.sys, "platform", "darwin")
    monkeypatch.setattr(store_writer, "_known_platform_uuid", None)
    monkeypatch.setattr(store_writer, "_ioreg_failed_at", float("-inf"))
    monkeypatch.setattr(store_writer.subprocess, "run", fail)
    assert project_folders.this_machine() is None and project_folders.this_machine() is None
    assert len(calls) == 1


def test_the_machine_of_a_folder_is_not_in_a_snapshot_or_its_state_hash(tmp_path, monkeypatch):
    from cairnmind.memory.memory import MemoryManager
    store, pid, root, record = indexed(tmp_path)
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid"), ("add", "."), ("commit", "-qm", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    manager, here = MemoryManager(store), project_folders.stamp()
    before = manager.preview(pid)
    store.command("UPDATE project_roots SET machine='another-machine',host='another-host' WHERE project_id=:p",
                  {"p": pid})
    after = manager.preview(pid)
    assert before["state_hash"] == after["state_hash"] and "another-host" not in json.dumps(after)
    with pytest.raises(ValueError, match="belongs to another-host"):
        manager.snapshot(pid)  # never a package into a same-path folder of another machine
    store.command("UPDATE project_roots SET machine=:machine,host=:host WHERE project_id=:p", {**here, "p": pid})
    published = Path(manager.snapshot(pid)["manifest_path"])
    written = "".join(file.read_text() for file in published.parent.rglob("*.json"))
    assert here["machine"] not in written and '"machine"' not in written and "project_roots" not in written
    assert json.loads(published.read_text())["state_hash"] == before["state_hash"]


def test_an_acknowledgement_holds_while_another_machine_scans_the_project(tmp_path, monkeypatch):
    from cairnmind.memory.sessions import SessionManager
    from test_sessions import acknowledge, git
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", "app.py")
    git(root, "commit", "-m", "initial")
    store = Store(tmp_path / "memory")
    project = store.add_project("session-fixture", root)
    index_project(store, project["id"])
    manager = SessionManager(store, store.settings)
    initial = manager.prepare(project["id"], "codex", timeout=0)
    acknowledge(manager, initial)
    machine(monkeypatch, "b")  # another machine scans the project: its folder is elsewhere for it
    assert index_project(store, project["id"])["unavailable"][0]["state"] == "elsewhere"
    store.command("UPDATE project_roots SET machine='c' WHERE project_id=:p", {"p": project["id"]})
    manager.require_ack(initial["session_id"], initial["packet"]["id"])
    again = manager.resume_context(initial["session_id"])
    assert again["packet"]["hash"] == initial["packet"]["hash"] and again["epoch"] == initial["epoch"]


# --- review of the first build ------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["elsewhere", "unreadable"])
def test_a_folder_reached_through_a_symlink_keeps_its_sources_when_it_is_away(tmp_path, monkeypatch, state):
    if state == "unreadable" and (not hasattr(os, "geteuid") or os.geteuid() == 0):
        pytest.skip("requires POSIX chmod(0) enforcement")
    store, pid, root, record = indexed(tmp_path)
    before = counts(store, pid, [record])
    real = tmp_path / "mac.real"
    (tmp_path / "mac").rename(real)
    (tmp_path / "mac").symlink_to(real)  # the registered path now spells differently from the folder it resolves to
    if state == "elsewhere":
        machine(monkeypatch, "b")
    else:
        (real / "proj-b").chmod(0)
    try:
        result = index_project(store, pid)
    finally:
        (real / "proj-b").chmod(0o755)
    assert [item["state"] for item in result["unavailable"]] == [state]
    assert result["deleted"] == 0 and counts(store, pid, [record]) == before


def test_project_add_of_a_folder_of_another_machine_names_it_and_fails(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cairnmind import cli
    store, pid, root, record = indexed(tmp_path)
    before = counts(store, pid, [record])
    machine(monkeypatch, "b")
    monkeypatch.setattr(cli, "store_for", lambda settings: store)
    added = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "project", "add", "cairnmind", str(root)])
    assert added.exit_code == 1
    assert "belongs to" in added.output and (str(root) in added.output or json.dumps(str(root)) in added.output) and "checked again" not in added.output
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)  # the sentences a person reads, not the JSON
    added = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "project", "add", "cairnmind", str(root)])
    assert added.exit_code == 1 and "belongs to" in added.output and "checked again" not in added.output
    assert "project root cairnmind" in " ".join(added.output.split())  # the command that moves it, by name
    assert counts(store, pid, [record]) == before


def test_the_elsewhere_notice_reads_in_english_and_polish():
    project = {"name": "cairnmind"}
    root = "/Users/me/proj-b"
    move_en = (f" If the project's files are on this machine now, at this path, cairnmind project root cairnmind "
               f"{root} records that, and the next indexing reads them here; files that differ from the indexed ones "
               f"then replace them.")
    move_pl = (f" Jeśli pliki projektu są teraz na tym komputerze, pod tą ścieżką, cairnmind project root cairnmind "
               f"{root} to zapisuje, a następne indeksowanie czyta je tutaj; pliki różne od zaindeksowanych zastąpią "
               f"wtedy tamte.")
    tail_en = " Nothing of the project is changed for it: its files are not counted as deleted and its code graph is kept."
    tail_pl = (" Nic w projekcie nie zmienia się z jego powodu: jego pliki nie są liczone jako usunięte, a graf kodu "
               "zostaje zachowany.")
    for host, reason_en, reason_pl in (
            ("mac", "it belongs to mac, the machine that added it, and only that machine indexes it",
             "należy do mac, komputera, który go dodał, i tylko tamten komputer go indeksuje"),
            (None, "it belongs to the machine that added it, and only that machine indexes it",
             "należy do komputera, który go dodał, i tylko tamten komputer go indeksuje")):
        item = project_folders.entry(project, {"root": root, "state": "elsewhere", "host": host, "error": None})
        assert item["message"] == (f"The folder {root} of project cairnmind is unavailable on this machine: "
                                   f"{reason_en}.{tail_en}{move_en}")
        assert project_folders.folder_notice([item], "pl") == (
            f"Folder {root} projektu cairnmind jest niedostępny na tym komputerze: {reason_pl}.{tail_pl}{move_pl}")


@pytest.mark.parametrize("identity", ["host:laptop", "machine-id:uninitialized"])
def test_a_machine_known_only_by_its_host_name_neither_claims_nor_refuses_a_folder(tmp_path, monkeypatch, identity):
    from cairnmind.storage import store_writer
    store, pid, root, record = indexed(tmp_path)  # stamped by an identifiable machine, as a Mac that could read ioreg
    assert stamped(store, pid)[0][0]
    monkeypatch.setattr(store_writer, "machine_identity", lambda: identity)
    assert project_folders.this_machine() is None and project_folders.stamp() == {}
    # A folder stamped by another machine is not refused: a host name that changed or is shared proves nothing.
    (root / "app.py").write_text("def handler():\n    return 'edited'\n")
    result = index_project(store, pid)
    assert "unavailable" not in result and result["changed"] == 1
    # Registering writes no stamp; the first index of an unstamped folder does not claim it.
    other = tmp_path / "mac" / "other"
    make_repo(other)
    store.add_project_root(pid, other)
    make_repo(tmp_path / "mac" / "second")
    store.add_project("second", tmp_path / "mac" / "second")
    store.command("UPDATE project_roots REMOVE machine, host WHERE project_id=:p", {"p": pid})
    index_project(store, pid)
    assert all(row == (None, None) for row in stamped(store, pid))
    assert [row[0] for row in stamped(store, store.resolve_project("second")["id"])] == [None]
    assert store.add_project_root(pid, other) == {}  # nor does `project root` move anything


def test_status_doctor_and_a_poll_that_skips_never_stamp_a_folder(tmp_path):
    store, pid, root, record = indexed(tmp_path)
    time.sleep(2.2)  # outside the racy window (2 s of the newest change time), so the next index records its signature
    index_project(store, pid)
    store.command("UPDATE project_roots REMOVE machine, host WHERE project_id=:p", {"p": pid})  # as before #371
    project = store.resolve_project(pid)
    assert project_folders.unavailable(store, pid) == [] and project_folders.unavailable(store) == []
    assert project_folders.folder_states(store, project)[0]["adopt"] is True  # only a complete index acts on it
    assert index_project_if_changed(store, pid).get("skipped") is True  # the poll found nothing to read
    assert stamped(store, pid) == [(None, None)]
    index_project(store, pid)
    assert stamped(store, pid)[0][0] == project_folders.this_machine()
