"""What a second session of the same conversation (the cairnmind-recorder subagent) does to the gate (#376).

Real HTTP service, ArcadeDB and Git; no model is asked. The recorder is an ordinary external session.
"""
import json

from fastapi.testclient import TestClient

from cairnmind.inspector import Inspector
from cairnmind.mcp_server import _saved_record_view
from cairnmind.memory_published import PUBLICATION_SWITCH, Publisher
from cairnmind.packet_origin import EXTERNAL
from cairnmind.recorder_agent import RECORDER_REPLY
from cairnmind.service import create_app
from cairnmind.store import ORIGIN_METADATA, Store

from conftest import as_person

from test_ack_under_extraction import ack_body
from test_external_session_writes import fixture as fixture  # noqa: F401 - a fixture
from test_external_session_writes import post
from test_short_resume import read, sample_project, size

WORDS = "We keep the old reader working until March. " * 14


def start(fixture, client, acknowledge=True):  # noqa: F811
    started = post(fixture, "/sessions/prepare", {"project": fixture.project["id"], "client": client,
                                                  "reason": "startup"})
    assert started["status"] == "ready", started
    if acknowledge:
        post(fixture, "/sessions/ack", ack_body(started))
    return {"session_id": started["session_id"], "packet_id": started["packet"]["id"]}


def renew(fixture, bound):  # noqa: F811
    """What a session does when its ACK went stale: read the whole new packet, acknowledge it."""
    whole = read(fixture, bound)
    post(fixture, "/sessions/ack", ack_body(bound, whole["packet"]))
    return {"session_id": bound["session_id"], "packet_id": whole["packet"]["id"]}


def write(fixture, bound, content, kind="decision", **record):  # noqa: F811
    return fixture.client.post("/remember", json={"project": fixture.project["id"], "sources": [], **bound,
                                                  "record": {"kind": kind, "content": content, **record}})


def test_a_second_session_writes_on_its_own_ack_under_its_own_origin(fixture):  # noqa: F811
    main = start(fixture, "codex")
    recorder = start(fixture, "external")
    saved = write(fixture, recorder, "Quoted by the main conversation")
    assert saved.status_code == 200, saved.text
    row = fixture.record(saved.json()["id"])
    assert row["status"] == "proposed"
    assert {key: row["metadata"][key] for key in ORIGIN_METADATA} == {
        "origin_client": "external", "origin_session_id": recorder["session_id"],
        "origin_history": "unarchived", "origin_evidence": "degraded"}
    assert main["session_id"] not in json.dumps(row["metadata"])


def test_a_second_sessions_decision_stales_the_first_sessions_ack_and_shows_in_its_next_packet(fixture):  # noqa: F811
    main = start(fixture, "codex")
    recorder = start(fixture, "external")
    assert read(fixture, main, short=True)["unchanged"] is True
    saved = write(fixture, recorder, "Quoted by the main conversation").json()
    answer = read(fixture, main, short=True)
    assert "unchanged" not in answer
    assert {row["id"]: row["origin"] for row in answer["packet"]["records"]}[saved["id"]] == EXTERNAL
    refused = fixture.client.post("/remember", json={
        "project": fixture.project["id"], "sources": [fixture.note["id"]], **main,
        "record": {"kind": "decision", "content": "The main conversation's own"}})
    assert refused.status_code == 409 and "resume_required" in refused.text
    main = renew(fixture, main)
    post(fixture, "/remember", {"project": fixture.project["id"], "sources": [fixture.note["id"]], **main,
                                "record": {"kind": "decision", "content": "The main conversation's own"}})
    # A fact stales nobody: the same ACK still holds for the main conversation.
    main = renew(fixture, main)
    recorder = renew(fixture, recorder)
    assert write(fixture, recorder, "A fact the recorder was given", kind="fact").status_code == 200
    assert read(fixture, main, short=True)["unchanged"] is True


def test_a_second_session_without_its_own_ack_is_refused_in_one_sentence(fixture):  # noqa: F811
    start(fixture, "codex")
    recorder = start(fixture, "external", acknowledge=False)
    refused = write(fixture, recorder, "Before its own ACK")
    assert refused.status_code == 409
    first = refused.json()["detail"].split(". ")[0]
    assert first.startswith("resume_required") and len(first) <= 200, first
    assert not fixture.store.query("SELECT FROM records WHERE content='Before its own ACK'")
    recorder = renew(fixture, recorder)
    confirmed = write(fixture, recorder, "Asking to be confirmed", status="confirmed")
    assert confirmed.status_code == 400
    assert not fixture.store.query("SELECT FROM records WHERE content='Asking to be confirmed'")


def test_a_recorders_record_is_held_from_publishing_and_survives_a_backup_and_restore(fixture, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    start(fixture, "codex")
    recorder = start(fixture, "external")
    saved = write(fixture, recorder, "Quoted by the main conversation").json()
    Inspector(fixture.store).review(fixture.project["id"], saved["id"], "confirm", saved["version"])
    held = Publisher(fixture.store).publish(fixture.project["id"], preview=True)
    assert held["records"] == 0 and held["held"]["items"][0]["reason"].endswith("(degraded origin)")
    target = Store(tmp_path / "target")
    target.restore(fixture.store.backup(tmp_path / "round-trip.cairnmind.json.gz"))
    restored = target.query("SELECT FROM records WHERE id=:id", {"id": saved["id"]})[0]
    assert {key: restored["metadata"][key] for key in ORIGIN_METADATA} == {
        "origin_client": "external", "origin_session_id": recorder["session_id"],
        "origin_history": "unarchived", "origin_evidence": "degraded"}


def costs(fixture):  # noqa: F811
    """What the main conversation spends on one decision, written directly or handed to the recorder."""
    main = start(fixture, "codex")
    sent = {"topic": "storage"}
    arguments = {"project": fixture.project["id"], "sources": [fixture.note["id"]],
                 "record": {"kind": "decision", "content": WORDS, "metadata": sent}}
    saved = post(fixture, "/remember", {**arguments, **main})
    direct_current = size(arguments) + size(_saved_record_view(saved, sent))
    renewed = read(fixture, main)
    acknowledged = post(fixture, "/sessions/ack", ack_body(main, renewed["packet"]))
    ack_args = {"packet_id": renewed["packet"]["id"], "packet_hash": renewed["packet"]["hash"],
                "readback": ack_body(main, renewed["packet"])["readback"]}
    direct_stale = direct_current + size(renewed) + size(ack_args) + size(acknowledged)
    recorder = post(fixture, "/sessions/prepare", {"project": fixture.project["id"], "client": "external",
                                                   "reason": "startup"})
    recorder_ack = post(fixture, "/sessions/ack", ack_body(recorder))
    bound = {"session_id": recorder["session_id"], "packet_id": recorder["packet"]["id"]}
    given = {"project": fixture.project["id"], "sources": [], "record": {"kind": "decision", "content": WORDS}}
    handed = post(fixture, "/remember", {**given, **bound})
    reply = RECORDER_REPLY.format(kind="decision", id=handed["id"], version=handed["version"],
                                  session=recorder["session_id"])
    delegated = size('Record this decision, word for word: "' + WORDS + '"') + size(reply)
    recorder_own = (size(recorder) + size(ack_body(recorder)) + size(recorder_ack) + size(given)
                    + size(_saved_record_view(handed, None)))
    return {"direct_current": direct_current, "direct_stale": direct_stale, "delegated": delegated,
            "recorder_own": recorder_own, "packet": size(renewed)}


def test_recorder_costs_in_the_main_context(tmp_path):
    fixture = sample_project(tmp_path)  # noqa: F811
    with TestClient(create_app(fixture.settings, start_worker=False, ollama=object()),
                    headers={"Authorization": f"Bearer {fixture.settings.token}"}) as client:
        fixture.client = client
        sizes = costs(fixture)
        print("h376 sizes:", *(f"{key}={value}" for key, value in sizes.items()))
        assert sizes["delegated"] * 5 < sizes["direct_stale"]  # the stale case only: with a current ACK it is about even
        for number in range(20):  # a store that fills the packet's budget
            as_person(fixture.store, fixture.project["id"],
                      {"kind": "decision", "key": f"budget-{number}", "content": f"Decision {number}. " + "x" * 580},
                      [fixture.note["id"]])
        budget = costs(fixture)
        print("h376 sizes (full budget):", *(f"{key}={value}" for key, value in budget.items()))
        assert budget["delegated"] * 20 < budget["direct_stale"]
