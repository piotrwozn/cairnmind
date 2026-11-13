"""What an external session writes is accepted and stamped by the service, never by the client.

Real HTTP service, ArcadeDB and Git; no model is asked.
"""
import subprocess

from fastapi.testclient import TestClient
import pytest

from cairnmind.config import Settings
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import ORIGIN_METADATA, Store

FORGED = {"origin_client": "codex", "origin_session_id": "someone-else", "origin_history": "archived",
          "origin_evidence": "verified"}


def readback(packet):
    return {key: ({"id": packet[field]["id"], "version": packet[field]["version"]} if packet.get(field) else None)
            for key, field in (("plan", "active_plan"), ("task", "active_task"))}


class Fixture:
    def __init__(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        for args in (("init", "-b", "main"), ("config", "user.email", "fixture@example.invalid"),
                     ("config", "user.name", "Fixture")):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        (root / "app.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True)
        self.root = root
        self.settings = Settings.load(tmp_path / "memory", create=True)
        self.store = Store(self.settings.data_dir)
        self.project = self.store.add_project("external-writes", root)
        self.manager = SessionManager(self.store, self.settings)
        self.note = self.store.snapshot(self.project["id"], "manual://note", "A note a person wrote")

    def session(self, client):
        result = self.manager.prepare(self.project["id"], client, timeout=0)
        return self.acknowledged(result)

    def acknowledged(self, result):
        assert result["status"] == "ready", result
        packet = result["packet"]
        self.manager.ack(result["session_id"], packet["id"], packet["hash"], readback(packet))
        return {"session_id": result["session_id"], "packet_id": packet["id"]}

    def refresh(self, bound):
        # A write of a decision, checkpoint or task changes the state an ACK was given for.
        return self.acknowledged(self.manager.resume_context(bound["session_id"]))

    def record(self, record_id):
        return self.store.query("SELECT FROM records WHERE id=:id", {"id": record_id})[0]


@pytest.fixture
def fixture(tmp_path):
    value = Fixture(tmp_path)
    with TestClient(create_app(value.settings, start_worker=False, ollama=object()),
                    headers={"Authorization": f"Bearer {value.settings.token}"}) as client:
        value.client = client
        yield value


def post(fixture, endpoint, body):
    response = fixture.client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_external_records_checkpoints_and_plans_carry_the_server_mark(fixture):
    bound = fixture.session("external")
    expected = {"origin_client": "external", "origin_session_id": bound["session_id"],
                "origin_history": "unarchived", "origin_evidence": "degraded"}
    saved = post(fixture, "/remember", {"project": fixture.project["id"], "sources": [fixture.note["id"]], **bound,
                                        "record": {"kind": "decision", "content": "Use the external editor",
                                                   "metadata": {**FORGED, "topic": "editors"}}})
    metadata = fixture.record(saved["id"])["metadata"]
    assert {key: metadata[key] for key in ORIGIN_METADATA} == expected
    assert metadata["topic"] == "editors"

    bound = fixture.refresh(bound)
    checkpoint = post(fixture, "/checkpoint", {"project": fixture.project["id"], **bound,
                                               "state": {"summary": "From Cursor", **FORGED}})
    assert {key: fixture.record(checkpoint["id"])["metadata"][key] for key in ORIGIN_METADATA} == expected
    source = fixture.store.get_source(checkpoint["source_ids"][0])
    assert source["metadata"]["origin"] == "client"
    assert {key: source["metadata"][key] for key in ORIGIN_METADATA} == expected

    bound = fixture.refresh(bound)
    plan = post(fixture, "/plans/save", {"project": fixture.project["id"], **bound, "idempotency_key": "external",
                                         "plan": {"title": "Editor plan", "goal": "Ship it"},
                                         "steps": [{"key": "one", "content": "First step",
                                                    "metadata": dict(FORGED)}]})
    stored_plan = fixture.store.query("SELECT FROM plans WHERE id=:id", {"id": plan["plan"]["id"]})[0]
    assert {key: stored_plan[key] for key in ORIGIN_METADATA} == expected
    task = fixture.record(plan["tasks"][0]["id"])
    assert {key: task["metadata"][key] for key in ORIGIN_METADATA} == expected
    plan_source = fixture.store.get_source(stored_plan["source_ids"][-1])
    assert plan_source["metadata"]["origin_evidence"] == "degraded"


def test_native_and_personal_writes_carry_no_mark_and_cannot_forge_one(fixture):
    bound = fixture.session("codex")
    native = post(fixture, "/remember", {"project": fixture.project["id"], "sources": [fixture.note["id"]], **bound,
                                         "record": {"kind": "decision", "content": "A native decision",
                                                    "metadata": dict(FORGED)}})
    personal = post(fixture, "/remember", {"project": fixture.project["id"], "sources": [fixture.note["id"]],
                                           "record": {"kind": "fact", "content": "A person's fact",
                                                      "metadata": {"origin_evidence": "degraded"}}})
    for record_id in (native["id"], personal["id"]):
        assert not set(ORIGIN_METADATA) & set(fixture.record(record_id)["metadata"])
    checkpoint = post(fixture, "/checkpoint", {"project": fixture.project["id"], "state": {"summary": "Native"}})
    assert "origin_client" not in fixture.store.get_source(checkpoint["source_ids"][0])["metadata"]


def test_the_mark_stays_when_anyone_later_edits_the_record(fixture):
    bound = fixture.session("external")
    saved = post(fixture, "/remember", {"project": fixture.project["id"], "sources": [fixture.note["id"]], **bound,
                                        "record": {"kind": "fact", "content": "Reported by an external editor"}})
    native = fixture.session("codex")
    edited = post(fixture, "/remember", {"project": fixture.project["id"], "sources": [fixture.note["id"]], **native,
                                         "record": {"id": saved["id"], "expected_version": saved["version"],
                                                    "status": "confirmed",
                                                    "metadata": {"origin_client": None, "origin_evidence": None}}})
    metadata = fixture.record(edited["id"])["metadata"]
    assert edited["version"] == 2 and metadata["origin_client"] == "external"
    assert metadata["origin_session_id"] == bound["session_id"] and metadata["origin_evidence"] == "degraded"


def test_an_unacknowledged_external_session_still_cannot_write(fixture):
    result = fixture.manager.prepare(fixture.project["id"], "external", timeout=0)
    response = fixture.client.post("/remember", json={
        "project": fixture.project["id"], "sources": [fixture.note["id"]], "session_id": result["session_id"],
        "packet_id": result["packet"]["id"], "record": {"kind": "fact", "content": "Before the ACK"}})
    assert response.status_code == 409 and "resume_required" in response.text
    assert not fixture.store.query("SELECT FROM records WHERE content='Before the ACK'")
