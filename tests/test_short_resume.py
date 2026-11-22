"""Reading the packet again costs the assistant little while the session's ACK of it still holds (#325).

Real HTTP service, ArcadeDB and Git; no model is asked. The short answer is never an acknowledgement.
"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cairnmind.config import Settings
from cairnmind.inspector import Inspector
from cairnmind.mcp_server import _saved_record_view
from cairnmind.memory import MemoryManager
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import Store

from conftest import as_person

from test_ack_under_extraction import (NEWER, ack_body, extract_one, indexed_notes, prepared,  # noqa: F401
                                       session_view)
from test_external_session_writes import fixture as fixture  # noqa: F401 - a fixture
from test_external_session_writes import post, readback

SHORT_KEYS = {"session_id", "epoch", "status", "unchanged", "acknowledged", "packet_id", "packet_hash", NEWER, "note"}


def acknowledged_external(fixture):  # noqa: F811
    started = prepared(fixture)
    post(fixture, "/sessions/ack", ack_body(started))
    return started


def read(fixture, started, **extra):  # noqa: F811
    return post(fixture, "/sessions/context", {"session_id": started["session_id"], **extra})


def session_row(fixture, session_id):  # noqa: F811
    return fixture.store.query("SELECT FROM sessions WHERE id=:id", {"id": session_id})[0]


def remember(fixture, started, text):  # noqa: F811
    return fixture.client.post("/remember", json={
        "project": fixture.project["id"], "sources": [fixture.note["id"]], "session_id": started["session_id"],
        "packet_id": started["packet"]["id"], "record": {"kind": "decision", "content": text}})


def test_an_acknowledged_unchanged_packet_comes_back_short_and_the_ack_still_holds(fixture):  # noqa: F811
    started = acknowledged_external(fixture)
    packet = started["packet"]
    before = session_row(fixture, started["session_id"])
    short = read(fixture, started, short=True)
    assert set(short) == SHORT_KEYS | {"history", "evidence"}
    assert short["unchanged"] is True and short["acknowledged"] is True and short["status"] == "ready"
    assert (short["packet_id"], short["packet_hash"]) == (packet["id"], packet["hash"])
    assert short[NEWER] == 0 and "packet" not in short
    assert (short["history"], short["evidence"]) == ("unarchived", "degraded")
    assert len(fixture.store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": started["session_id"]})) == 1
    after = session_row(fixture, started["session_id"])
    assert {key: after[key] for key in ("epoch", "ack_id", "packet_id")} == \
        {key: before[key] for key in ("epoch", "ack_id", "packet_id")}
    view = session_view(fixture, started["session_id"])
    assert view["acknowledged"] is True and view["stale"] is False
    assert remember(fixture, started, "Short reads keep the gate").status_code == 200


def test_after_a_relevant_change_the_whole_new_packet_comes_back_and_must_be_acknowledged(fixture):  # noqa: F811
    started = acknowledged_external(fixture)
    assert remember(fixture, started, "A fact the packet will show").status_code == 200
    renewed = read(fixture, started, short=True)
    assert "unchanged" not in renewed and renewed["status"] == "ready"
    assert renewed["packet"]["id"] != started["packet"]["id"]
    refused = remember(fixture, {**started, "packet": renewed["packet"]}, "Written before the new ACK")
    assert refused.status_code == 409 and "resume_required" in refused.text
    post(fixture, "/sessions/ack", ack_body(started, renewed["packet"]))
    assert remember(fixture, {**started, "packet": renewed["packet"]}, "Written after it").status_code == 200
    # that write changed the state again: the next read is a new packet, then short once acknowledged
    again = read(fixture, started, short=True)
    assert "packet" in again
    post(fixture, "/sessions/ack", ack_body(started, again["packet"]))
    last = read(fixture, started, short=True)
    assert last["unchanged"] is True and last["packet_id"] == again["packet"]["id"]


def test_an_unacknowledged_session_always_gets_the_whole_packet(fixture):  # noqa: F811
    for client in ("external", "codex"):
        started = post(fixture, "/sessions/prepare", {"project": fixture.project["id"], "client": client,
                                                      "reason": "startup"})
        for _ in range(2):
            answer = read(fixture, started, short=True)
            assert "unchanged" not in answer and answer["packet"]["id"] == started["packet"]["id"]


def test_the_whole_packet_is_returned_on_request_and_without_short(fixture):  # noqa: F811
    started = acknowledged_external(fixture)
    for extra in ({}, {"short": False}):
        answer = read(fixture, started, **extra)
        assert answer["packet"]["id"] == started["packet"]["id"] and "unchanged" not in answer
        assert answer["packet"]["hash"] == started["packet"]["hash"]
    assert session_view(fixture, started["session_id"])["acknowledged"] is True


def test_newer_automatic_proposals_are_counted_in_the_short_answer(fixture):  # noqa: F811
    indexed_notes(fixture)
    started = acknowledged_external(fixture)
    extract_one(fixture)
    extract_one(fixture)
    short = read(fixture, started, short=True)
    assert short[NEWER] == 2 and short["packet_id"] == started["packet"]["id"]
    assert session_view(fixture, started["session_id"])["acknowledged"] is True
    Inspector(fixture.store).review(fixture.project["id"], extract_one(fixture)["id"], "confirm", 1)
    renewed = read(fixture, started, short=True)
    assert "unchanged" not in renewed and renewed["packet"]["id"] != started["packet"]["id"]


def test_short_is_refused_with_an_exact_packet_read(fixture):  # noqa: F811
    started = acknowledged_external(fixture)
    body = {"session_id": started["session_id"], "packet_id": started["packet"]["id"]}
    refused = fixture.client.post("/sessions/context", json={**body, "short": True})
    assert refused.status_code == 400 and "short" in refused.text
    assert post(fixture, "/sessions/context", body)["packet"]["id"] == started["packet"]["id"]


def test_a_native_short_answer_has_no_degraded_marks(fixture):  # noqa: F811
    native = fixture.session("codex")
    started = {"session_id": native["session_id"]}
    short = read(fixture, started, short=True)
    assert short["unchanged"] is True and set(short) == SHORT_KEYS
    external = read(fixture, acknowledged_external(fixture), short=True)
    assert {"history", "evidence"} <= set(external)


def test_a_short_answer_after_the_epoch_moved_is_the_whole_packet(fixture):  # noqa: F811
    native = fixture.session("codex")
    fixture.manager.note_event({"cairnmind_session_id": native["session_id"], "session_id": "native-1",
                                "hook_event_name": "PreCompact", "resume_reason": "compact",
                                "cwd": str(fixture.root), "event_id": "compact-1"}, "codex")
    answer = read(fixture, native, short=True)
    assert "unchanged" not in answer and answer["status"] == "ready"
    assert answer["packet"]["id"] != native["packet_id"]
    assert not fixture.manager.status(native["session_id"])["acknowledged"]
    assert readback(answer["packet"]) is not None


STORED_RECORD_KEYS = {"id", "project_id", "kind", "key", "content", "status", "source_ids", "version", "supersedes",
                      "metadata", "created_at", "updated_at", "active_key"}


def test_only_the_bridges_answer_names_the_metadata_a_write_removed(fixture):  # noqa: F811
    """The service computes it for that answer alone; the stored record and every other answer stay as on main."""
    pid, note = fixture.project["id"], fixture.note["id"]

    def write(record, **extra):
        return post(fixture, "/remember", {"project": pid, "sources": [note], "record": record, **extra})

    task = write({"kind": "task", "content": "T", "metadata": {
        "acceptance_criteria": [{"text": "A", "check": "unit", "argv": ["first"]}]}}, name_dropped=True)
    assert set(task) == STORED_RECORD_KEYS  # a new record removed nothing
    plain = write({"id": task["id"], "expected_version": 1, "metadata": {
        "acceptance_criteria": [{"text": "A", "check": "lint"}]}})
    assert set(plain) == STORED_RECORD_KEYS and "check_proposals" not in plain["metadata"]
    sent = {"acceptance_criteria": ["A"]}
    asked = write({"id": task["id"], "expected_version": 2, "metadata": sent}, name_dropped=True)
    assert asked["metadata_dropped"] == ["criterion_checks"]
    history = fixture.store.record_history(task["id"])
    assert [plain, {key: value for key, value in asked.items() if key != "metadata_dropped"}] == history[1:]
    assert all("metadata_dropped" not in json.dumps(row) for row in history)
    view = _saved_record_view(asked, sent)
    assert view["metadata_dropped"] == ["criterion_checks"] and "metadata_not_stored_as_sent" not in view
    refused = fixture.client.post("/remember", json={"project": pid, "sources": [note], "name_dropped": "yes",
                                                     "record": {"kind": "fact", "content": "F"}})
    assert refused.status_code == 422
    for state in ({"summary": "one", "next": "x"}, {"summary": "two"}):
        checkpoint = post(fixture, "/checkpoint", {"project": pid, "state": state, "name_dropped": True})
        assert set(checkpoint) == STORED_RECORD_KEYS  # a checkpoint's metadata accumulates: nothing removed


SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample-project"


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def sample_project(tmp_path):
    """The sample project's memory imported into a repository of its own, served over HTTP."""
    root = tmp_path / "trail-notes"
    shutil.copytree(SAMPLE, root, ignore=shutil.ignore_patterns(".memory"))
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.invalid"),
                 ("config", "user.name", "T"), ("add", "."), ("commit", "-qm", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    imported = MemoryManager(store).import_snapshot(SAMPLE / ".memory" / "cairnmind" / "manifest.json", root)
    project = store.resolve_project(imported["project"]["id"])
    return SimpleNamespace(root=root, settings=settings, store=store, project=project,
                           manager=SessionManager(store, settings),
                           note=store.snapshot(project["id"], "manual://editor-note", "A note a record cites"))


def measured(fixture):  # noqa: F811
    """The size in bytes of each answer before (the route as it was) and after (what the MCP tool now returns)."""
    started = prepared(fixture)
    post(fixture, "/sessions/ack", ack_body(started))
    full = read(fixture, started)
    short = read(fixture, started, short=True)
    assert full["packet"]["id"] == short["packet_id"] and short["unchanged"] is True
    text = "Keep every note readable by the older reader, as decided in March. " * 9
    sent = {"topic": "storage"}
    body = {"project": fixture.project["id"], "sources": [fixture.note["id"]], "session_id": started["session_id"],
            "packet_id": started["packet"]["id"], "record": {"kind": "decision", "content": text, "metadata": sent}}
    saved = post(fixture, "/remember", body)
    renewed = read(fixture, started)
    post(fixture, "/sessions/ack", ack_body(started, renewed["packet"]))
    state = {"summary": "Sizes measured. " * 40, "next": "Write the docs"}
    checkpoint = post(fixture, "/checkpoint", {"project": fixture.project["id"], "session_id": started["session_id"],
                                               "packet_id": renewed["packet"]["id"], "state": state})
    after_remember, after_checkpoint = _saved_record_view(saved, sent), _saved_record_view(checkpoint, state)
    for answer, view in ((saved, after_remember), (checkpoint, after_checkpoint)):
        assert size(view) < size(answer) and "content" not in view and "metadata" not in view
    assert text not in json.dumps(after_remember) and "Sizes measured" not in json.dumps(after_checkpoint)
    return {"packet_full": size(full), "packet_short": size(short), "remember_full": size(saved),
            "remember_short": size(after_remember), "checkpoint_full": size(checkpoint),
            "checkpoint_short": size(after_checkpoint)}


def test_sample_project_answers_before_and_after(tmp_path):
    fixture = sample_project(tmp_path)
    with TestClient(create_app(fixture.settings, start_worker=False, ollama=object()),
                    headers={"Authorization": f"Bearer {fixture.settings.token}"}) as client:
        fixture.client = client
        sizes = measured(fixture)
        print("h325 sizes:", *(f"{key}={value}" for key, value in sizes.items()))
        assert sizes["packet_short"] < 1024 and sizes["packet_short"] < sizes["packet_full"]
        for number in range(20):  # a store that fills the packet's budget
            as_person(fixture.store, fixture.project["id"],
                      {"kind": "decision", "key": f"budget-{number}", "content": f"Decision {number}. " + "x" * 580},
                      [fixture.note["id"]])
        budget = measured(fixture)
        print("h325 sizes (full budget):", *(f"{key}={value}" for key, value in budget.items()))
        assert budget["packet_short"] < 1024 and budget["packet_short"] * 5 < budget["packet_full"]
