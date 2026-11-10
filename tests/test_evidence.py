"""Evidence state of records: derived when read, from the real store, Git and file index."""
from __future__ import annotations

import json
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from devbrain import cli
from devbrain.evidence import evidence_states, unanchored
from devbrain.ingest import index_project
from devbrain.inspector import Inspector
from devbrain.inspector_api import attach_inspector_routes
from devbrain.plans import PlanManager
from devbrain.sessions import SessionManager
from devbrain.store import Store

PASSAGE = "LOGIN_ERRORS = {'expired': 401, 'locked': 423}"
ORIGINAL = f"# login errors\n{PASSAGE}\n"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "errors.py").write_text(ORIGINAL)
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    store = Store(tmp_path / "memory")
    project = store.add_project("evidence", root)
    index_project(store, project["id"])
    return store, project, root


def file_source(store, project, path):
    rows = store.query("SELECT FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                       {"project": project["id"], "uri": str(path)})
    assert len(rows) == 1
    return rows[0]


def decision(store, project, root, content="Login errors map to HTTP codes in errors.py", *, quote=PASSAGE):
    source = file_source(store, project, root / "errors.py")
    metadata = {"evidence": quote} if quote else {}
    return store.remember(project["id"], {"kind": "decision", "content": content, "metadata": metadata}, [source["id"]])


def state(store, project, record):
    return evidence_states(store, project["id"], [store.query("SELECT FROM records WHERE id=:id", {"id": record["id"]})[0]]
                           )[record["id"]]


def normalized_export(store):
    result = store.export()
    result.pop("exported_at", None)
    return result


def test_deleting_the_only_supporting_file_orphans_the_record_and_restoring_the_passage_replaces_it(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    assert state(store, project, record) == {"evidence_state": "anchored", "quote_survives": None,
                                             "sources_current": 1, "sources_replaced": 0, "sources_gone": 0}
    (root / "errors.py").unlink()
    # Until the files are indexed again the store cannot know; the state is as fresh as the last indexing.
    assert state(store, project, record)["evidence_state"] == "anchored"
    index_project(store, project["id"])
    assert state(store, project, record) == {"evidence_state": "orphaned", "quote_survives": None,
                                             "sources_current": 0, "sources_replaced": 0, "sources_gone": 1}
    (root / "errors.py").write_text(f"# rewritten\nimport http\n{PASSAGE}\n")
    index_project(store, project["id"])
    assert state(store, project, record) == {"evidence_state": "evidence_replaced", "quote_survives": True,
                                             "sources_current": 0, "sources_replaced": 1, "sources_gone": 0}
    (root / "errors.py").write_text("# the mapping moved to another service\n")
    index_project(store, project["id"])
    assert state(store, project, record)["quote_survives"] is False
    (root / "errors.py").write_text(ORIGINAL)
    index_project(store, project["id"])
    # The supporting version is current again, although it is a new snapshot row.
    assert state(store, project, record)["evidence_state"] == "anchored"


def test_a_record_without_a_stored_quote_reports_that_survival_is_unknown(workspace):
    store, project, root = workspace
    record = decision(store, project, root, quote=None)
    (root / "errors.py").write_text("# changed\n")
    index_project(store, project["id"])
    assert state(store, project, record) == {"evidence_state": "evidence_replaced", "quote_survives": None,
                                             "sources_current": 0, "sources_replaced": 1, "sources_gone": 0}


def test_one_current_source_keeps_a_record_anchored_and_the_others_are_counted(workspace):
    store, project, root = workspace
    sources = [file_source(store, project, root / name)["id"] for name in ("errors.py", "app.py")]
    record = store.remember(project["id"], {"kind": "fact", "content": "Two files agree"}, sources)
    (root / "errors.py").unlink()
    (root / "app.py").write_text("VALUE = 2\n")
    index_project(store, project["id"])
    assert state(store, project, record) == {"evidence_state": "evidence_replaced", "quote_survives": None,
                                             "sources_current": 0, "sources_replaced": 1, "sources_gone": 1}
    (root / "app.py").write_text("VALUE = 1\n")
    index_project(store, project["id"])
    assert state(store, project, record) == {"evidence_state": "anchored", "quote_survives": None,
                                             "sources_current": 1, "sources_replaced": 0, "sources_gone": 1}


def test_notes_conversations_and_plan_snapshots_are_never_replaced(workspace):
    store, project, root = workspace
    note = store.snapshot(project["id"], "manual://note", "We keep the public API.", "manual")
    constraint = store.remember(project["id"], {"kind": "constraint", "content": "Keep the public API."}, [note["id"]])
    manager = PlanManager(store)
    saved = manager.save(project["id"], {"title": "Plan", "goal": "First"}, [{"key": "a", "content": "Step A"}],
                         "one", worktree=str(root), activate=True)
    manager.save(project["id"], {"id": saved["plan"]["id"], "goal": "Second"}, [{"key": "a", "content": "Step A"}],
                 "two", expected_version=saved["plan"]["version"],
                 task_versions={task["id"]: task["version"] for task in saved["tasks"]})
    checkpoint = store.checkpoint(project["id"], {"summary": "saved"})
    records = store.list_records(project["id"])
    states = evidence_states(store, project["id"], records)
    assert {states[record["id"]]["evidence_state"] for record in records} == {"anchored"}
    assert {constraint["id"], checkpoint["id"], saved["tasks"][0]["id"]} <= set(states)
    assert unanchored(store, project["id"], records) == []


def test_a_review_note_does_not_anchor_a_record_whose_file_is_gone(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    reviewed = Inspector(store).review(project["id"], record["id"], "confirm", record["version"])["record"]
    assert len(reviewed["source_ids"]) == 2 and reviewed["status"] == "confirmed"
    (root / "errors.py").unlink()
    index_project(store, project["id"])
    assert state(store, project, reviewed) == {"evidence_state": "orphaned", "quote_survives": None,
                                               "sources_current": 0, "sources_replaced": 0, "sources_gone": 1}


def test_a_missing_source_row_counts_as_gone(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    store.command("DELETE FROM sources WHERE id=:id", {"id": record["source_ids"][0]})
    assert state(store, project, record)["evidence_state"] == "orphaned"


def test_reading_the_state_stores_nothing(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    (root / "errors.py").unlink()
    index_project(store, project["id"])
    before = normalized_export(store)
    rows = store.list_records(project["id"])
    assert unanchored(store, project["id"], rows)[0]["record"]["id"] == record["id"]
    assert normalized_export(store) == before
    assert "evidence_state" not in rows[0]["metadata"]


def test_inspector_lists_and_details_carry_the_state(workspace):
    store, project, root = workspace
    kept = decision(store, project, root, "Kept decision")
    lost = store.remember(project["id"], {"kind": "fact", "content": "app value", "metadata": {"evidence": "VALUE = 1"}},
                          [file_source(store, project, root / "app.py")["id"]])
    (root / "app.py").unlink()
    index_project(store, project["id"])
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + store.settings.token}
    listed = client.get("/inspector/records", params={"project": project["id"]}, headers=headers).json()["items"]
    assert {item["id"]: item["evidence_state"] for item in listed} == {kept["id"]: "anchored", lost["id"]: "orphaned"}
    detail = client.get(f"/inspector/records/{lost['id']}", params={"project": project["id"]}, headers=headers).json()
    assert detail["evidence"] == {"evidence_state": "orphaned", "quote_survives": None,
                                  "sources_current": 0, "sources_replaced": 0, "sources_gone": 1}
    assert client.get(f"/inspector/records/{kept['id']}", params={"project": project["id"]},
                      headers=headers).json()["evidence"]["evidence_state"] == "anchored"


def test_resume_packet_warns_about_unanchored_records_without_blocking_the_resume(workspace):
    store, project, root = workspace
    manager = SessionManager(store, store.settings)
    record = decision(store, project, root)
    clean = manager.prepare(project["id"], "codex", timeout=0)
    assert clean["status"] == "ready" and "warnings" not in clean["packet"]
    assert all("evidence_state" not in row for row in clean["packet"]["records"])
    git(root, "rm", "-q", "errors.py")
    git(root, "commit", "-qm", "remove the mapping")
    index_project(store, project["id"])
    packet = manager.prepare(project["id"], "claude", timeout=0)
    assert packet["status"] == "ready" and packet["unknowns"] == []
    assert packet["packet"]["warnings"] == [
        "1 memory records in this scope no longer rest on a current source (1 orphaned, 0 with a changed source). "
        "Treat them as unverified; list them with `cairnmind memory orphans`."]
    preview = next(row for row in packet["packet"]["records"] if row["id"] == record["id"])
    assert preview["evidence_state"] == "orphaned"


def test_cli_memory_orphans_lists_both_groups_and_respects_the_session(workspace, tmp_path, monkeypatch):
    store, project, root = workspace
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    monkeypatch.setattr(cli, "store_for", lambda _: store)
    monkeypatch.setattr(cli, "ensure_service", lambda *a, **k: pytest.fail("orphans started the service"))
    runner = CliRunner()
    arguments = ["--data-dir", str(store.data_dir), "memory", "orphans", "evidence"]
    decision(store, project, root, "Errors decision\nsecond line")
    lost = store.remember(project["id"], {"kind": "fact", "content": "app value\x1b[31m"},
                          [file_source(store, project, root / "app.py")["id"]])
    nothing = runner.invoke(cli.app, arguments)
    assert nothing.exit_code == 0, nothing.output or repr(nothing.exception)
    assert nothing.output.splitlines() == [
        f"Evidence of evidence in {root}, as of the last indexing of the project's files",
        "Every record rests on a current source."]
    (root / "app.py").unlink()
    (root / "errors.py").write_text("# moved\n")
    index_project(store, project["id"])
    shown = runner.invoke(cli.app, arguments)
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert "Orphaned: no supporting source has a current version (1)" in shown.output
    assert f"  [fact] proposed, version 1: app value\\x1b[31m ({lost['id']})" in shown.output
    assert "Source changed since the record was written (1)" in shown.output
    assert "Errors decision (" in shown.output and "); the quoted passage is gone" in shown.output
    assert "\x1b" not in shown.output and "second line" not in shown.output
    payload = json.loads(runner.invoke(cli.app, [*arguments, "--json"]).output)
    assert (payload["orphaned"], payload["evidence_replaced"]) == (1, 1)
    assert [row["evidence_state"] for row in payload["records"]] == ["orphaned", "evidence_replaced"]
    assert payload["records"][1]["quote_survives"] is False

    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("other", other_root)
    session = SessionManager(store, store.settings).prepare(project["id"], "codex", timeout=0)["session_id"]
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", session)
    foreign = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "orphans", other["id"]])
    assert foreign.exit_code == 1 and "differs from the session" in str(foreign.exception)
