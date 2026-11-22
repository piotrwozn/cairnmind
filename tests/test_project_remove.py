"""A project is removed from the registry and keeps its memory; the same name and folder bring it back (#327)."""
import subprocess

import pytest

from cairnmind import project_removal as removal
from cairnmind.ingest import index_pending_sources, index_project, poll_imports
from cairnmind.memory import MemoryConflict, MemoryManager
from cairnmind.record_kinds import FIELD, doctor_view
from cairnmind.service import schedule_embeddings
from cairnmind.sessions import SessionManager
from cairnmind.sql import insert_document
from cairnmind.store import EXPORT_TYPES, Store

from conftest import as_person, plain_words

NOT_COUNTED = {"projects", "project_roots", "journal_state", "journal_events"}


@pytest.fixture
def world(tmp_path):
    """A store with one project that owns something of every kind the issue lists."""
    store = Store(tmp_path / "data")
    root, extra = tmp_path / "app", tmp_path / "app-wt"
    for folder in (root, extra):
        folder.mkdir()
    (root / "a.py").write_text("x = 1\n")
    (extra / "b.py").write_text("y = 2\n")
    project = store.add_project("app", root)
    store.add_project_root(project["id"], extra)
    index_project(store, project["id"])
    pid = project["id"]
    source = store.query("SELECT id FROM sources WHERE project_id=:p LIMIT 1", {"p": pid})[0]["id"]
    confirmed = as_person(store, pid, {"kind": "decision", "key": "k", "content": "Use X."}, [source])
    store.enqueue("embed", pid, {"chunk_id": "c"}, "job-1")
    SessionManager(store, store.settings).prepare(pid, "api", timeout=0)  # a session with its resume packet
    with store.transaction() as tx:
        insert_document(tx, "import_cursors", {"id": "cursor-1", "project_id": pid, "path": "/nowhere.jsonl",
                                               "client": "claude"})
    return store, project, root, extra, confirmed


def remove(store, project, folder=None):
    plan = removal.plan(store.settings, store, store.resolve_project(project["id"]), folder)
    return removal.apply(store, plan)


def counts(store, pid):
    return {name: store._count(name, pid) for name in EXPORT_TYPES if name not in NOT_COUNTED}


def test_remove_keeps_every_row_and_frees_the_name_and_folder(world, tmp_path):
    store, project, root, extra, confirmed = world
    before = counts(store, project["id"])
    assert before["records"] and before["sources"] and before["chunks"] and before["jobs"] and before["sessions"]
    result = remove(store, project)
    assert result["applied"] and result["removed_at"]
    assert counts(store, project["id"]) == before
    assert store.list_projects() == []
    assert not store.query("SELECT FROM project_roots WHERE project_id=:p", {"p": project["id"]})
    row = store.query("SELECT FROM projects WHERE id=:p", {"p": project["id"]})[0]
    assert row["removed_name"] == "app" and row["removed_root"] == str(root) and row["removed_roots"] == [str(extra)]
    assert "name" not in row and "root" not in row
    for value in ("app", project["id"], str(root), str(extra / "b.py")):
        with pytest.raises(ValueError, match=r"^Unknown project: .*was removed from the registry"):
            store.resolve_project(value)
    other = tmp_path / "other"
    other.mkdir()
    assert store.add_project("app", other)["id"] != project["id"]  # the name is free
    assert store.add_project("another", root)["id"] != project["id"]  # the folder is free


def test_add_with_the_same_name_and_folder_brings_back_the_same_project(world):
    store, project, root, extra, confirmed = world
    remove(store, project)
    back = store.add_project("app", root)
    assert back["id"] == project["id"] and back["name"] == "app" and back["root"] == str(root)
    row = store.query("SELECT FROM projects WHERE id=:p", {"p": project["id"]})[0]
    assert not any(field in row for field in removal.REMOVED_FIELDS)
    assert [r["path"] for r in store.query("SELECT FROM project_roots WHERE project_id=:p", {"p": project["id"]})] \
        == [str(root)]  # the extra folder is not added back
    assert store.resolve_project("app")["id"] == project["id"]
    assert store.query("SELECT status FROM records WHERE id=:id", {"id": confirmed["id"]})[0]["status"] == "confirmed"


def test_a_backup_after_removal_restores_with_the_project_still_removed(world, tmp_path):
    store, project, root, extra, confirmed = world
    remove(store, project)
    target = Store(tmp_path / "target")
    target.restore(store.backup(tmp_path / "removed.cairnmind.json.gz"))
    assert target.list_projects() == []
    assert [r["id"] for r in removal.removed_rows(target)] == [project["id"]]
    assert target.query("SELECT status FROM records WHERE id=:id", {"id": confirmed["id"]})[0]["status"] == "confirmed"


def test_jobs_of_a_removed_project_are_not_claimed_until_it_is_back(world):
    store, project, root, extra, confirmed = world
    for job in store.query("SELECT id FROM jobs WHERE project_id=:p", {"p": project["id"]}):
        store.command("UPDATE jobs SET state='completed' WHERE id=:id AND dedupe_key!=:keep",
                      {"id": job["id"], "keep": "job-1"})
    store.enqueue("other", None, {}, "unowned")
    remove(store, project)
    assert store.claim_job("w", kinds=["embed"]) is None
    assert store.claim_job("w", kinds=["other"])["project_id"] is None  # a job of no project is still claimed
    store.add_project("app", root)
    assert store.claim_job("w", kinds=["embed"])["project_id"] == project["id"]


def test_jobs_in_work_refuse_and_change_nothing(world):
    store, project, root, extra, confirmed = world
    plan = removal.plan(store.settings, store, store.resolve_project("app"))
    assert store.claim_job("w", lease_seconds=300, kinds=["embed"])["project_id"] == project["id"]
    with pytest.raises(removal.Refused, match="Jobs of app are in work now \\(embed\\)"):
        removal.apply(store, plan)
    assert [p["id"] for p in store.list_projects()] == [project["id"]]
    with pytest.raises(removal.Refused, match="in work"):
        removal._jobs_in_work(store, project)


def test_dropping_an_extra_root_deletes_only_that_row(world):
    store, project, root, extra, confirmed = world
    sources = store._count("sources", project["id"])
    result = remove(store, project, extra)
    assert result["applied"] and "removed_at" not in result
    assert [r["path"] for r in store.query("SELECT FROM project_roots WHERE project_id=:p", {"p": project["id"]})] \
        == [str(root)]
    assert store.resolve_project("app")["id"] == project["id"] and "removed_at" not in store.resolve_project("app")
    index_project(store, project["id"])
    current = {r["uri"]: r["is_current"] for r in store.query("SELECT uri,is_current FROM sources WHERE kind='code'")}
    assert store._count("sources", project["id"]) == sources  # the rows stay as evidence
    assert [uri for uri, is_current in current.items() if is_current and uri.endswith("b.py")] == []


def test_forget_deletes_a_removed_project_by_id(world):
    store, project, root, extra, confirmed = world
    remove(store, project)
    assert removal.forget_target(store, project["id"])["id"] == project["id"]
    with pytest.raises(ValueError, match=rf"app was removed .* forget takes its id: .*cairnmind forget {project['id']} "
                                         r"--delete-project"):
        removal.forget_target(store, "app")  # by name it is refused, and the refusal names the id form
    store.forget_project(project["id"])
    assert not store.query("SELECT FROM projects WHERE id=:p", {"p": project["id"]})
    assert store._count("records", project["id"]) == 0


def test_restore_refuses_when_the_folder_is_now_another_projects_root(world, tmp_path):
    store, project, root, extra, confirmed = world
    remove(store, project)
    # Another project took the folder as an extra root while this one was removed.
    taker = store.add_project("taker", tmp_path / "taker-home")
    store.add_project_root(taker["id"], root)
    with pytest.raises(ValueError, match="is now a folder of the project taker"):
        store.add_project("app", root)
    assert store.list_projects() == [store.resolve_project("taker")]
    assert [r["id"] for r in removal.removed_rows(store)] == [project["id"]]


def test_the_status_counts_registered_and_removed_projects(world):
    store, project, root, extra, confirmed = world
    assert (store.status()["projects"], store.status()["removed_projects"]) == (1, 0)
    remove(store, project)
    assert (store.status()["projects"], store.status()["removed_projects"]) == (0, 1)


def test_transcripts_and_snapshots_of_a_removed_project_are_not_polled(world):
    store, project, root, extra, confirmed = world
    pid = project["id"]
    with store.transaction() as tx:
        insert_document(tx, "memory_settings", {"id": pid, "project_id": pid, "enabled": True, "interval_minutes": 1,
                                                "worktree": str(root), "updated_at": "2026-11-22T10:00:00+00:00"})
    remove(store, project)
    assert poll_imports(store)["errors"] == []  # the cursor of its transcript is kept, not read
    ticked = MemoryManager(store).tick()
    assert ticked["projects"] == [] and ticked["errors"] == []
    assert "last_checked_at" not in store.query("SELECT FROM memory_settings WHERE id=:id", {"id": pid})[0]
    store.add_project("app", root)
    assert poll_imports(store)["errors"]  # back: the cursor is read again (its file is not there)


def test_no_embedding_job_is_queued_for_a_removed_project(world):
    store, project, root, extra, confirmed = world
    assert schedule_embeddings(store, "test-namespace") > 0
    for job in store.query("SELECT id FROM jobs WHERE kind='embed'"):
        store.command("DELETE FROM jobs WHERE id=:id", {"id": job["id"]})
    remove(store, project)
    assert schedule_embeddings(store, "test-namespace") == 0


def test_a_memory_package_of_a_removed_project_is_refused(world, tmp_path):
    store, project, root, extra, confirmed = world
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "first"]):
        subprocess.run([*git, *command], cwd=root, check=True, capture_output=True)
    snapshot = MemoryManager(store).snapshot(project["id"], worktree=str(root))
    remove(store, project)
    destination = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(root), str(destination)], check=True, capture_output=True)
    with pytest.raises(MemoryConflict, match=r"removed from the registry on .*cairnmind project add app .* Nothing was "
                                             r"imported"):
        MemoryManager(store).import_snapshot(snapshot["manifest_path"], destination)
    assert store.list_projects() == []


def test_the_workspace_and_doctor_list_registered_projects_only(world):
    store, project, root, extra, confirmed = world
    entry = {"client": "claude", "kind": "tool_note", "note": "technical", "accepted_at": "2026-11-22T10:00:00+00:00"}
    store.command(f"UPDATE projects SET {FIELD}=:entries WHERE id=:id", {"entries": [entry], "id": project["id"]})
    assert [row["project_id"] for row in doctor_view(store)] == [project["id"]]  # the filter, not the data, hides it
    remove(store, project)
    assert doctor_view(store) == []
    assert store.list_projects() == []


def test_sources_waiting_to_be_indexed_are_not_chunked_for_a_removed_project(world):
    store, project, root, extra, confirmed = world
    store.snapshot(project["id"], "manual://waiting", "A note that nobody chunked yet.", "manual")
    remove(store, project)
    assert index_pending_sources(store, limit=1000) == {"sources": 0, "chunks": 0, "errors": []}
    store.add_project("app", root)
    assert index_pending_sources(store, limit=1000)["sources"] >= 1  # back: it waited, nothing was lost


def test_a_session_of_a_removed_project_names_the_removal_on_every_call(world):
    store, project, root, extra, confirmed = world
    sessions = SessionManager(store, store.settings)
    session_id = store.query("SELECT id FROM sessions WHERE project_id=:p LIMIT 1", {"p": project["id"]})[0]["id"]
    remove(store, project)
    for call in (lambda: sessions.resume_context(session_id), lambda: sessions.ack(session_id, "x", "y", {})):
        with pytest.raises(ValueError, match=r"^Unknown project: .*was removed from the registry"):
            call()
    store.add_project("app", root)
    sessions.resume_context(session_id)  # back: the session answers again


def test_the_workspace_route_says_a_removed_project_came_back_and_honours_a_separate_identity(world):
    from fastapi.testclient import TestClient

    from cairnmind.service import create_app
    store, project, root, extra, confirmed = world
    remove(store, project)
    headers = {"Authorization": "Bearer " + store.settings.token}
    with TestClient(create_app(store.settings, start_worker=False, ollama=object()), headers=headers) as client:
        fresh = client.post("/projects", json={"name": "app", "root": str(root), "fresh": True})
        assert fresh.status_code >= 400 and "keeps its memory" in fresh.text and "another name" in fresh.text
        assert store.list_projects() == []  # nothing was brought back, nothing was created
        back = client.post("/projects", json={"name": "app", "root": str(root)})
        assert back.status_code == 200, back.text
        assert back.json()["project"]["id"] == project["id"] and back.json()["restored"]["removed_at"]
        new = client.post("/projects", json={"name": "second", "root": str(extra)})
        assert new.status_code == 200 and "restored" not in new.json()


def test_cli_remove_and_add_again_end_to_end(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from cairnmind import cli, ingest
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    folder = tmp_path / "mistake"
    folder.mkdir()
    (folder / "a.py").write_text("x = 1\n")

    def invoke(*arguments):
        return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "data"), *arguments])
    assert invoke("project", "add", "mistake", str(folder)).exit_code == 0
    dry = invoke("project", "remove", "mistake")
    assert dry.exit_code == 1 and "Nothing was changed; this is what cairnmind project remove mistake --yes" in dry.stdout
    assert "Nothing in the folder is changed." in dry.stdout and "1 sources" in dry.stdout
    assert "mistake" in invoke("project", "list").stdout
    done = invoke("project", "remove", "mistake", "--yes")
    assert done.exit_code == 0 and done.stdout.startswith("Removed mistake from the registry")
    listed = invoke("project", "list").stdout
    assert "mistake:" not in listed and "Removed projects whose memory is kept: 1" in listed
    assert "- mistake: " + str(folder) in invoke("project", "list", "--removed").stdout
    named = invoke("brief", "mistake")
    assert named.exit_code != 0 and "was removed from the registry" in plain_words(named.output + str(named.exception))
    back = invoke("project", "add", "mistake", str(folder))
    assert back.exit_code == 0 and back.stdout.startswith(f"Brought back mistake in {folder.resolve()} with its memory")
