"""Deleting a project's whole memory is a person's act of the workspace, and it leaves a trace (#347). Engine tests."""
import json
import subprocess
from pathlib import Path

import pytest

from cairnmind import answer_log, client_runtime, project_forget, project_removal as removal
from cairnmind.confirmation import ConfirmationRefused
from cairnmind.ingest import index_project
from cairnmind.memory import MemoryManager
from cairnmind.person_proof import FORGET_REFUSED
from cairnmind.sessions import SessionManager
from cairnmind.sql import insert_document
from cairnmind.store import DOCUMENT_TYPES, EXPORT_TYPES, Store
from cairnmind.store_writer import StoreNotWriter

from conftest import as_person, person_unproven

NOT_COUNTED = {"projects", "project_roots", "journal_state", "journal_events"}


def git_folder(folder: Path, name: str, text: str) -> None:
    folder.mkdir()
    (folder / name).write_text(text)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "first"]):
        subprocess.run([*git, *command], cwd=folder, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(folder), "config", "user.name", "t"], check=True)


@pytest.fixture
def world(tmp_path):
    """A store with a project `app` that owns something of every kind, and a second project `other`."""
    store = Store(tmp_path / "data")
    root, other_root = tmp_path / "app", tmp_path / "other"
    git_folder(root, "a.py", "x = 1\n")
    git_folder(other_root, "c.py", "z = 3\n")
    project, other = store.add_project("app", root), store.add_project("other", other_root)
    for each in (project, other):
        index_project(store, each["id"])
    pid, oid = project["id"], other["id"]
    source = store.query("SELECT id FROM sources WHERE project_id=:p LIMIT 1", {"p": pid})[0]["id"]
    confirmed = as_person(store, pid, {"kind": "decision", "key": "k", "content": "Use X."}, [source])
    osource = store.query("SELECT id FROM sources WHERE project_id=:p LIMIT 1", {"p": oid})[0]["id"]
    record = store.remember(oid, {"kind": "decision", "key": "o", "content": "Use Z."}, [osource])
    from cairnmind.inspector import Inspector
    Inspector(store).review(oid, record["id"], "confirm", record["version"])
    store.enqueue("embed", pid, {"chunk_id": "c"}, "job-1")
    SessionManager(store, store.settings).prepare(pid, "api", timeout=0)
    with store.transaction() as tx:
        insert_document(tx, "import_cursors", {"id": "cursor-1", "project_id": pid, "path": "/nowhere.jsonl",
                                               "client": "claude"})
    return store, project, other, root, confirmed


def counts(store, pid):
    return {name: store._count(name, pid) for name in EXPORT_TYPES if name not in NOT_COUNTED}


def leave_answer_log(store, pid):
    path = answer_log.log_path(store.settings, pid)
    path.parent.mkdir(parents=True)
    path.write_text('{"id": "x"}\n')
    return path


def test_delete_removes_every_row_of_the_project_and_its_answer_log_and_no_backup(world, tmp_path):
    store, project, other, root, confirmed = world
    pid, oid = project["id"], other["id"]
    before, others = counts(store, pid), counts(store, oid)
    assert before["records"] and before["sources"] and before["chunks"] and before["jobs"] and before["sessions"]
    log = leave_answer_log(store, pid)
    backup = Path(store.backup(tmp_path / "before.cairnmind.json.gz"))
    kept = backup.read_bytes()
    result = project_forget.delete(store.settings, store, pid)
    assert result["deleted"] == pid and result["answer_log_deleted"] is True and "answer_log_error" not in result
    assert result["backups_unchanged"] is True and result["deleted_counts"]["records"] == before["records"]
    assert result["project"] == {"id": pid, "name": "app", "root": str(root), "removed_at": None}
    assert not log.parent.exists() and (store.data_dir / answer_log.DIRECTORY).is_dir()
    assert not store.query("SELECT FROM projects WHERE id=:p", {"p": pid})
    for collection in DOCUMENT_TYPES:
        if collection not in NOT_COUNTED:
            assert store._count(collection, pid) == 0, collection
    for collection in ("nodes", "edges"):
        assert not store.query(f"SELECT FROM {collection} WHERE project_id=:p", {"p": pid})
    assert counts(store, oid) == others  # another project is not touched
    assert backup.read_bytes() == kept  # a backup is never touched


def test_delete_leaves_a_deleted_projects_entry_and_the_history_is_capped(world, monkeypatch):
    store, project, other, root, confirmed = world
    assert project_forget.history(store) == []
    project_forget.delete(store.settings, store, project["id"])
    (entry,) = project_forget.history(store)
    assert {key: entry[key] for key in ("project_id", "name", "root", "removed_at", "route")} == {
        "project_id": project["id"], "name": "app", "root": str(root), "removed_at": None, "route": "workspace"}
    assert entry["deleted_at"] and entry["host"] and set(entry["counts"]) == set(project_forget.KEPT_COUNTS)
    assert entry["counts"]["records"] >= 1
    monkeypatch.setattr(project_forget, "HISTORY_LIMIT", 2)
    first = store.add_project("again", root)
    (root.parent / "third").mkdir()
    second = store.add_project("third", root.parent / "third")
    for each in (first, second):
        project_forget.delete(store.settings, store, each["id"])
    assert [item["project_id"] for item in project_forget.history(store)] == [first["id"], second["id"]]


def test_without_the_proof_nothing_is_deleted_and_nothing_is_recorded(world):
    store, project, other, root, confirmed = world
    pid = project["id"]
    before = counts(store, pid)
    for attempt in (lambda: project_forget.delete(store.settings, store, pid), lambda: store.forget_project(pid)):
        with person_unproven(), pytest.raises(ConfirmationRefused) as refused:  # a token holder's request
            attempt()
        assert str(refused.value) == FORGET_REFUSED
    assert counts(store, pid) == before and project_forget.history(store) == []
    assert store.query("SELECT FROM projects WHERE id=:p", {"p": pid})


def test_the_proof_is_checked_before_anything_is_read(monkeypatch):
    def read(*args, **kwargs):
        raise AssertionError("the project was read before the proof was checked")
    monkeypatch.setattr(project_forget, "plan", read)
    with person_unproven(), pytest.raises(ConfirmationRefused) as refused:
        project_forget.delete(None, None, "p")
    assert str(refused.value) == FORGET_REFUSED


def test_a_live_run_or_a_job_in_work_refuses_and_deletes_nothing(world, monkeypatch):
    store, project, other, root, confirmed = world
    pid = project["id"]
    before = counts(store, pid)
    store.command("DELETE FROM jobs WHERE project_id=:p", {"p": other["id"]})  # the claim below must be this project's
    assert store.claim_job("w", lease_seconds=300, kinds=["embed"])["project_id"] == pid
    for call in (project_forget.plan, project_forget.delete):
        with pytest.raises(removal.Refused, match=r"Jobs of app are in work now \(embed\)"):
            call(store.settings, store, pid)
    store.command("UPDATE jobs SET state='completed' WHERE project_id=:p", {"p": pid})
    runtime = store.data_dir / "runtime"
    runtime.mkdir()
    registry = runtime / "runs.json"
    registry.write_text(json.dumps({"version": 1, "runs": [{
        "id": "r1", "client": "claude", "lifecycle": "starting", "project_id": pid, "tmux_session": "s", "generation": 1}]}))
    registry.chmod(0o600)
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: "tmux")
    for call in (project_forget.plan, project_forget.delete):
        with pytest.raises(removal.Refused, match=r"app has a session running in the workspace \(claude starting\)"):
            call(store.settings, store, pid)
    assert counts(store, pid) == before and project_forget.history(store) == []


def test_a_job_leased_after_the_checks_is_refused_inside_the_transaction(world, monkeypatch):
    store, project, other, root, confirmed = world
    pid = project["id"]
    shown = project_forget.plan(store.settings, store, pid)  # the checks passed: the job comes after them
    before = counts(store, pid)
    store.command("DELETE FROM jobs WHERE project_id=:p", {"p": other["id"]})
    assert store.claim_job("w", lease_seconds=300, kinds=["embed"])["project_id"] == pid
    monkeypatch.setattr(project_forget, "plan", lambda *args: shown)
    with pytest.raises(removal.Refused, match=r"Jobs of app are in work now \(embed\)"):
        project_forget.delete(store.settings, store, pid)
    assert counts(store, pid) == before and project_forget.history(store) == []
    assert store.query("SELECT FROM projects WHERE id=:p", {"p": pid})


def test_a_reader_refuses(world):
    store, project, other, root, confirmed = world
    before = counts(store, project["id"])
    store.not_writer = "Another installation writes this store"
    for call in (project_forget.plan, project_forget.delete):
        with pytest.raises(StoreNotWriter, match="Another installation writes this store"):
            call(store.settings, store, project["id"])
    assert counts(store, project["id"]) == before


def test_a_removed_project_is_deleted_by_its_id_and_a_name_is_not_guessed(world):
    store, project, other, root, confirmed = world
    pid = project["id"]
    removal.apply(store, removal.plan(store.settings, store, store.resolve_project(pid)))
    listed = project_forget.deletable(store)["items"]
    assert [item["id"] for item in listed] == [other["id"], pid]
    assert listed[1]["name"] == "app" and listed[1]["root"] == str(root) and listed[1]["removed_at"]
    with pytest.raises(ValueError, match="^Unknown project: app"):
        project_forget.plan(store.settings, store, "app")
    result = project_forget.delete(store.settings, store, pid)
    assert result["project"]["removed_at"] and result["deleted_counts"]["records"] >= 1
    assert not store.query("SELECT FROM projects WHERE id=:p", {"p": pid}) and store._count("records", pid) == 0
    assert project_forget.history(store)[0]["removed_at"]


def test_a_new_project_with_the_same_name_and_folder_starts_clean(world):
    store, project, other, root, confirmed = world
    project_forget.delete(store.settings, store, project["id"])
    fresh = store.add_project("app", root)
    assert fresh["id"] != project["id"]
    assert all(value == 0 for key, value in counts(store, fresh["id"]).items() if key != "jobs" and key != "projects")
    assert not store.query("SELECT FROM records WHERE id=:id", {"id": confirmed["id"]})
    index_project(store, fresh["id"])  # only the folder's files come back
    assert store._count("records", fresh["id"]) == 0 and store._count("sources", fresh["id"]) > 0
    assert store._count("sessions", fresh["id"]) == 0 and store._count("plans", fresh["id"]) == 0


def test_other_projects_back_up_restore_snapshot_and_publish_unchanged(world, monkeypatch, tmp_path):
    from cairnmind.memory_published import PUBLICATION_SWITCH, Publisher
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    store, project, other, root, confirmed = world
    oid = other["id"]
    before = counts(store, oid)
    status = store.query("SELECT status FROM records WHERE project_id=:p", {"p": oid})
    project_forget.delete(store.settings, store, project["id"])
    target = Store(tmp_path / "restored")
    target.restore(store.backup(tmp_path / "after.cairnmind.json.gz"))
    assert counts(target, oid) == before
    assert target.query("SELECT status FROM records WHERE project_id=:p", {"p": oid}) == status
    assert not target.query("SELECT FROM projects WHERE id=:p", {"p": project["id"]})
    assert project_forget.history(target) == []  # the trace is not carried by a backup
    memory = MemoryManager(store)
    assert Path(memory.snapshot(oid, worktree=other["root"])["manifest_path"]).is_file()
    assert Publisher(store).publish(oid)["status"] == "written"
    assert counts(store, oid)["records"] == before["records"]
