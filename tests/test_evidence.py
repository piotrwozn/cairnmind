"""Evidence state of records: derived when read, from the real store, Git and file index."""
from __future__ import annotations

import json
import subprocess
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from devbrain import cli
from devbrain.evidence import evidence_states, unanchored
from devbrain.ingest import current_source, index_project
from devbrain.inspector import Inspector
from devbrain.inspector_api import attach_inspector_routes
from devbrain.plans import PlanManager
from devbrain.sessions import MAX_PACKET_BYTES, SessionManager
from devbrain.store import Store

PASSAGE = "LOGIN_ERRORS = {'expired': 401, 'locked': 423}"
ORIGINAL = f"# login errors\n{PASSAGE}\n"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(root):
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "errors.py").write_text(ORIGINAL)
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def workspace(tmp_path):
    root = repository(tmp_path / "repo")
    store = Store(tmp_path / "memory")
    project = store.add_project("evidence", root)
    index_project(store, project["id"])
    return store, project, root


def file_source(store, project, path):
    rows = store.query("SELECT FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                       {"project": project["id"], "uri": str(path)})
    assert len(rows) == 1
    return rows[0]


def decision(store, project, root, content="Login errors map to HTTP codes in errors.py", *, quote=PASSAGE,
             name="errors.py", **metadata):
    source = file_source(store, project, root / name)
    if quote:
        metadata["evidence"] = quote
    return store.remember(project["id"], {"kind": "decision", "content": content, "metadata": metadata}, [source["id"]])


def verdict(state, survives=None, *, current=0, replaced=0, gone=0, unverified=0):
    return {"evidence_state": state, "quote_survives": survives, "sources_current": current,
            "sources_replaced": replaced, "sources_gone": gone, "sources_unverified": unverified}


def state(store, project, record):
    row = store.query("SELECT FROM records WHERE id=:id", {"id": record["id"]})[0]
    return evidence_states(store, project["id"], [row])[record["id"]]


def normalized_export(store):
    result = store.export()
    result.pop("exported_at", None)
    return result


def test_deleting_the_only_supporting_file_orphans_the_record_and_restoring_the_passage_replaces_it(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    assert state(store, project, record) == verdict("anchored", current=1)
    (root / "errors.py").unlink()
    # Until the files are indexed again the store cannot know; the state is as fresh as the last indexing.
    assert state(store, project, record)["evidence_state"] == "anchored"
    index_project(store, project["id"])
    assert state(store, project, record) == verdict("orphaned", gone=1)
    (root / "errors.py").write_text(f"# rewritten\nimport http\n{PASSAGE}\n")
    index_project(store, project["id"])
    assert state(store, project, record) == verdict("evidence_replaced", True, replaced=1)
    (root / "errors.py").write_text("# the mapping moved to another service\n")
    index_project(store, project["id"])
    assert state(store, project, record) == verdict("evidence_replaced", False, replaced=1)
    (root / "errors.py").write_text(ORIGINAL)
    index_project(store, project["id"])
    # The supporting version is current again, although it is a new snapshot row.
    assert state(store, project, record) == verdict("anchored", current=1)


def test_a_moved_file_with_identical_content_keeps_its_records_anchored(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    (root / "core").mkdir()
    (root / "errors.py").rename(root / "core" / "errors.py")
    index_project(store, project["id"])
    assert state(store, project, record) == verdict("anchored", current=1)
    (root / "core" / "errors.py").write_text("# edited after the move\n")
    index_project(store, project["id"])
    # Different content at a different address is not this record's evidence any more.
    assert state(store, project, record) == verdict("orphaned", gone=1)


def test_survival_is_reported_only_for_a_quote_that_occurs_in_the_cited_version(workspace):
    store, project, root = workspace
    unquoted = decision(store, project, root, "No quote", quote=None)
    invented = decision(store, project, root, "Invented quote", quote="import http")
    (root / "errors.py").write_text("import http\n# changed\n")
    index_project(store, project["id"])
    assert state(store, project, unquoted) == verdict("evidence_replaced", None, replaced=1)
    # "import http" occurs in the new file but never occurred in the version the record cites.
    assert state(store, project, invented) == verdict("evidence_replaced", None, replaced=1)
    rows = store.list_records(project["id"])
    assert {value["quote_survives"] for value in evidence_states(store, project["id"], rows, quotes=False).values()} == {None}


def test_one_standing_source_anchors_a_record_and_the_others_are_counted(workspace):
    store, project, root = workspace
    sources = [file_source(store, project, root / name)["id"] for name in ("errors.py", "app.py")]
    record = store.remember(project["id"], {"kind": "fact", "content": "Two files agree"}, sources)
    (root / "errors.py").unlink()
    (root / "app.py").write_text("VALUE = 2\n")
    index_project(store, project["id"])
    assert state(store, project, record) == verdict("evidence_replaced", replaced=1, gone=1)
    (root / "app.py").write_text("VALUE = 1\n")
    index_project(store, project["id"])
    assert state(store, project, record) == verdict("anchored", current=1, gone=1)


@pytest.mark.parametrize("kind, uri", [("manual", "manual://note"), ("transcript", "transcript://session-1"),
                                       ("plan", "plan://p1"), ("check_output", "check://run/stdout")])
def test_a_superseded_statement_still_anchors_its_record(workspace, kind, uri):
    store, project, _ = workspace
    first = store.snapshot(project["id"], uri, "first wording", kind)
    record = store.remember(project["id"], {"kind": "constraint", "content": "Keep the public API."}, [first["id"]])
    store.snapshot(project["id"], uri, "second wording", kind)
    assert store.get_source(first["id"])["is_current"] is False
    assert state(store, project, record) == verdict("anchored", current=1)


def test_plan_steps_and_checkpoints_are_anchored(workspace):
    store, project, root = workspace
    manager = PlanManager(store)
    saved = manager.save(project["id"], {"title": "Plan", "goal": "First"}, [{"key": "a", "content": "Step A"}],
                         "one", worktree=str(root), activate=True)
    manager.save(project["id"], {"id": saved["plan"]["id"], "goal": "Second"}, [{"key": "a", "content": "Step A"}],
                 "two", expected_version=saved["plan"]["version"],
                 task_versions={task["id"]: task["version"] for task in saved["tasks"]})
    store.checkpoint(project["id"], {"summary": "saved"})
    records = store.list_records(project["id"])
    assert len(records) == 2 and unanchored(store, project["id"], records) == []


def test_a_review_note_does_not_anchor_a_record(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    reviewed = Inspector(store).review(project["id"], record["id"], "confirm", record["version"])
    assert len(reviewed["record"]["source_ids"]) == 2 and reviewed["record"]["status"] == "confirmed"
    (root / "errors.py").unlink()
    index_project(store, project["id"])
    assert state(store, project, reviewed["record"]) == verdict("orphaned", gone=1)
    only_audit = store.remember(project["id"], {"kind": "fact", "content": "Rests on a review note alone"},
                                [reviewed["audit_source_id"]])
    assert state(store, project, only_audit) == verdict("orphaned")
    # A note that merely uses the review address is an ordinary statement.
    plain = store.snapshot(project["id"], "review://written-by-a-client", "a note", "manual")
    noted = store.remember(project["id"], {"kind": "fact", "content": "Rests on a note"}, [plain["id"]])
    assert state(store, project, noted) == verdict("anchored", current=1)


def test_a_missing_source_row_counts_as_gone(workspace):
    store, project, root = workspace
    record = decision(store, project, root)
    store.command("DELETE FROM sources WHERE id=:id", {"id": record["source_ids"][0]})
    assert state(store, project, record) == verdict("orphaned", gone=1)


def imported(store, project, origin, name, content):
    path = origin / name
    source = store.snapshot(project["id"], str(path), content, "code",
                            {"root": str(origin), "path": str(path), "relative_path": name})
    store.command("UPDATE sources SET is_current=false,current_key=null WHERE id=:id", {"id": source["id"]})
    store.command("INSERT INTO memory_imports CONTENT :row", {"row": {
        "id": str(uuid4()), "project_id": project["id"], "source_ids": [source["id"]]}})
    return source


def test_imported_file_evidence_is_unverified_until_local_files_are_indexed(tmp_path):
    root = repository(tmp_path / "repo")
    store = Store(tmp_path / "memory")
    project = store.add_project("imported", root)
    origin = tmp_path / "foreign-host"
    store.command("INSERT INTO memory_bindings CONTENT :row", {"row": {
        "id": str(uuid4()), "project_id": project["id"], "origin_root": str(origin), "local_root": str(root)}})
    same = imported(store, project, origin, "errors.py", ORIGINAL)
    changed = imported(store, project, origin, "app.py", "VALUE = 0\n")
    absent = imported(store, project, origin, "removed.py", "GONE = True\n")
    records = {name: store.remember(project["id"], {"kind": "fact", "content": name,
                                                    "metadata": {"evidence": quote}}, [source["id"]])
               for name, source, quote in (("same", same, PASSAGE), ("changed", changed, "VALUE = 0"),
                                           ("absent", absent, "GONE"))}
    before = {name: state(store, project, record) for name, record in records.items()}
    assert set(map(json.dumps, before.values())) == {json.dumps(verdict("unverified", unverified=1))}
    index_project(store, project["id"])
    assert state(store, project, records["same"]) == verdict("anchored", current=1)
    assert state(store, project, records["changed"]) == verdict("evidence_replaced", False, replaced=1)
    assert state(store, project, records["absent"]) == verdict("orphaned", gone=1)


def test_a_local_snapshot_is_not_judged_by_another_worktree_through_a_binding(workspace, tmp_path):
    store, project, root = workspace
    second = repository(tmp_path / "second")
    store.add_project_root(project["id"], second)
    store.command("INSERT INTO memory_bindings CONTENT :row", {"row": {
        "id": str(uuid4()), "project_id": project["id"], "origin_root": str(root), "local_root": str(second)}})
    (second / "errors.py").write_text("# different in the second worktree\n")
    index_project(store, project["id"])
    record = decision(store, project, root)
    (root / "errors.py").unlink()
    index_project(store, project["id"])
    assert state(store, project, record) == verdict("orphaned", gone=1)


def test_an_unavailable_root_does_not_turn_its_records_into_orphans(workspace, tmp_path):
    store, project, root = workspace
    record = decision(store, project, root)
    root.rename(tmp_path / "unmounted")
    index_project(store, project["id"])
    refreshed = current_source(store, record["source_ids"][0])
    assert "unavailable" in refreshed["freshness_warning"] and "deleted" not in refreshed
    assert state(store, project, record) == verdict("anchored", current=1)


def test_reading_the_state_stores_nothing_and_needs_a_fixed_number_of_queries(workspace, monkeypatch):
    store, project, root = workspace
    for index in range(12):
        decision(store, project, root, f"Decision {index}")
        decision(store, project, root, f"App fact {index}", quote="VALUE = 1", name="app.py")
    (root / "errors.py").write_text(f"# changed\n{PASSAGE}\n")
    (root / "app.py").unlink()
    index_project(store, project["id"])
    before = normalized_export(store)
    rows = store.list_records(project["id"])
    queries, bodies = [], []
    original_query, original_source = store.query, store.get_source
    monkeypatch.setattr(store, "query", lambda *a, **k: queries.append(a[0]) or original_query(*a, **k))
    monkeypatch.setattr(store, "get_source", lambda *a, **k: bodies.append(a[0]) or original_source(*a, **k))
    found = unanchored(store, project["id"], rows)
    assert [row["evidence_state"] for row in found] == ["orphaned"] * 12 + ["evidence_replaced"] * 12
    assert all(row["quote_survives"] is True for row in found[12:])
    # The cited rows, the current versions, the moved-content check and the import receipts: no query per record.
    assert len(queries) <= 6 and len(bodies) == 2
    monkeypatch.undo()
    assert normalized_export(store) == before
    assert all("evidence_state" not in row["metadata"] for row in rows)


def test_inspector_lists_and_details_carry_the_state(workspace):
    store, project, root = workspace
    kept = decision(store, project, root, "Kept decision")
    lost = decision(store, project, root, "app value", quote="VALUE = 1", name="app.py")
    (root / "app.py").unlink()
    index_project(store, project["id"])
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + store.settings.token}
    listed = client.get("/inspector/records", params={"project": project["id"]}, headers=headers).json()["items"]
    assert {item["id"]: item["evidence_state"] for item in listed} == {kept["id"]: "anchored", lost["id"]: "orphaned"}
    detail = client.get(f"/inspector/records/{lost['id']}", params={"project": project["id"]}, headers=headers).json()
    assert detail["evidence"] == verdict("orphaned", gone=1)
    assert client.get(f"/inspector/records/{kept['id']}", params={"project": project["id"]},
                      headers=headers).json()["evidence"] == verdict("anchored", current=1)


def test_resume_packet_warns_about_unanchored_records_without_blocking_the_resume(workspace):
    store, project, root = workspace
    manager = SessionManager(store, store.settings)
    record = decision(store, project, root)
    changed = decision(store, project, root, "App value", quote="VALUE = 1", name="app.py")
    clean = manager.prepare(project["id"], "codex", timeout=0)
    assert clean["status"] == "ready" and "warnings" not in clean["packet"]
    assert all("evidence_state" not in row for row in clean["packet"]["records"])
    git(root, "rm", "-q", "errors.py")
    (root / "app.py").write_text("VALUE = 2\n")
    git(root, "commit", "-qam", "remove the mapping")
    index_project(store, project["id"])
    packet = manager.prepare(project["id"], "claude", timeout=0)
    assert packet["status"] == "ready" and packet["unknowns"] == []
    assert packet["packet"]["warnings"] == [
        "As of the last indexing of the project's files, 2 memory records in this scope have no supporting source "
        "that still stands (1 orphaned, 1 with a changed file). Treat them as unverified; "
        f"list them with `cairnmind memory orphans {project['id']}`."]
    previews = {row["id"]: row.get("evidence_state") for row in packet["packet"]["records"]}
    assert previews == {record["id"]: "orphaned", changed["id"]: "evidence_replaced"}
    runner = CliRunner()
    listed = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "orphans", project["id"]])
    assert listed.exit_code == 0 and "Orphaned: no supporting source has a current version (1)" in listed.output


def test_a_change_of_evidence_alone_keeps_an_acknowledged_packet_valid(workspace):
    store, project, root = workspace
    manager = SessionManager(store, store.settings)
    note = store.snapshot(project["id"], "note://design", "first wording", "document")
    store.remember(project["id"], {"kind": "decision", "content": "Follow the design note"}, [note["id"]])
    ready = manager.prepare(project["id"], "codex", timeout=0)
    packet = ready["packet"]
    manager.ack(ready["session_id"], packet["id"], packet["hash"], {"plan": None, "task": None})
    store.snapshot(project["id"], "note://design", "second wording", "document")
    assert unanchored(store, project["id"], store.list_records(project["id"]))[0]["evidence_state"] == "evidence_replaced"
    manager.require_ack(ready["session_id"])
    assert manager.resume_context(ready["session_id"])["packet"]["hash"] == packet["hash"]


def test_advisory_evidence_data_yields_before_a_packet_goes_over_budget(workspace):
    store, project, root = workspace
    manager = SessionManager(store, store.settings)
    filler = store.snapshot(project["id"], "manual://filler", "filler", "manual")
    for index in range(19):
        decision(store, project, root, f"Decision {index} " + "x" * 580)
    size = len(json.dumps(manager.prepare(project["id"], "codex", timeout=0)["packet"]))
    store.remember(project["id"], {"kind": "constraint", "content": "c" * 400,
                                   "metadata": {"note": "y" * max(0, MAX_PACKET_BYTES - size - 1200)}}, [filler["id"]])
    git(root, "rm", "-q", "errors.py")
    git(root, "commit", "-qm", "remove the mapping")
    index_project(store, project["id"])
    packet = manager.prepare(project["id"], "claude", timeout=0)
    assert packet["status"] == "ready", packet["unknowns"]
    assert len(json.dumps(packet["packet"]).encode()) <= MAX_PACKET_BYTES


def test_cli_memory_orphans_lists_the_groups_scoped_to_the_worktree_and_the_session(workspace, tmp_path, monkeypatch):
    store, project, root = workspace
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    monkeypatch.setattr(cli, "store_for", lambda _: store)
    monkeypatch.setattr(cli, "ensure_service", lambda *a, **k: pytest.fail("orphans started the service"))
    runner = CliRunner()
    arguments = ["--data-dir", str(store.data_dir), "memory", "orphans", "evidence"]
    second = repository(tmp_path / "second")
    store.add_project_root(project["id"], second)
    index_project(store, project["id"])
    decision(store, project, root, "Errors decision\nsecond line")
    lost = store.remember(project["id"], {"kind": "fact", "content": "app value\x1b[31m"},
                          [file_source(store, project, root / "app.py")["id"]])
    elsewhere = decision(store, project, second, "Only in the second worktree", worktree=str(second))
    nothing = runner.invoke(cli.app, arguments)
    assert nothing.exit_code == 0, nothing.output or repr(nothing.exception)
    assert nothing.output.splitlines() == [
        f"Evidence of evidence in {root}, as of the last indexing of the project's files",
        "Every record has a supporting source that still stands."]
    (root / "app.py").unlink()
    (root / "errors.py").write_text("# moved\n")
    (second / "errors.py").unlink()
    index_project(store, project["id"])
    shown = runner.invoke(cli.app, arguments)
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert "Orphaned: no supporting source has a current version (1)" in shown.output
    assert f"  [fact] proposed, version 1: app value\\x1b[31m ({lost['id']})" in shown.output
    assert "No supporting version is current and a supporting file changed (1)" in shown.output
    assert "Errors decision (" in shown.output and "); the quoted passage is gone" in shown.output
    assert "\x1b" not in shown.output and "second line" not in shown.output and "second worktree" not in shown.output
    payload = json.loads(runner.invoke(cli.app, [*arguments, "--json"]).output)
    assert (payload["orphaned"], payload["evidence_replaced"], payload["unverified"]) == (1, 1, 0)
    assert [row["evidence_state"] for row in payload["records"]] == ["orphaned", "evidence_replaced"]
    assert payload["records"][1]["quote_survives"] is False
    other = json.loads(runner.invoke(cli.app, [*arguments, "--worktree", str(second), "--json"]).output)
    assert other["worktree"] == str(second) and elsewhere["id"] in {row["id"] for row in other["records"]}

    session = SessionManager(store, store.settings).prepare(project["id"], "codex", timeout=0)["session_id"]
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", session)
    wrong_root = runner.invoke(cli.app, [*arguments, "--worktree", str(second)])
    assert wrong_root.exit_code == 1 and "worktree differs from the session" in str(wrong_root.exception)
    foreign_root = tmp_path / "other"
    foreign_root.mkdir()
    foreign = store.add_project("other", foreign_root)
    refused = runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "orphans", foreign["id"]])
    assert refused.exit_code == 1 and "project differs from the session" in str(refused.exception)
