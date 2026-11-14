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


def remember(fixture, bound, content, sources, kind="decision"):
    return post(fixture, "/remember", {"project": fixture.project["id"], "sources": sources, **(bound or {}),
                                       "record": {"kind": kind, "content": content}})


def test_an_external_record_is_never_verified_wherever_its_evidence_state_is_shown(fixture):
    from cairnmind.evidence import evidence_states, orphans_report
    from cairnmind.inspector import Inspector
    from cairnmind.retrieval import public_record
    from cairnmind.terminal_inspector import EVIDENCE_LABELS

    external = remember(fixture, fixture.session("external"), "Decided in an external editor", [fixture.note["id"]])
    native = remember(fixture, fixture.session("codex"), "Decided in a native client", [fixture.note["id"]])
    rows = [fixture.record(external["id"]), fixture.record(native["id"])]
    # The same current source anchors the native record and not the external one.
    states = evidence_states(fixture.store, fixture.project["id"], rows)
    assert states[native["id"]]["evidence_state"] == "anchored"
    assert states[external["id"]]["evidence_state"] == "degraded"
    inspector = Inspector(fixture.store)
    assert inspector.record(fixture.project["id"], external["id"])["evidence"]["evidence_state"] == "degraded"
    listed = {item["id"]: item["evidence_state"] for item in inspector.page("records", fixture.project["id"])["items"]}
    assert listed[external["id"]] == "degraded" and listed[native["id"]] == "anchored"
    assert EVIDENCE_LABELS[("degraded", None)].endswith("never verified")
    report = orphans_report(fixture.store, fixture.project, fixture.root, rows)
    assert report["degraded"] == 1 and [row["id"] for row in report["records"]] == [external["id"]]
    assert "external client's session; never verified (1)" in report["text"]
    assert public_record(rows[0])["metadata"]["origin_evidence"] == "degraded"
    # Every later resume packet, also a native one, shows it as degraded, never as anchored.
    packet = fixture.manager.prepare(fixture.project["id"], "claude", timeout=0)["packet"]
    shown = {row["id"]: row.get("evidence_state") for row in packet["records"]}
    assert shown[external["id"]] == "degraded" and shown[native["id"]] is None


def test_an_external_source_cannot_anchor_any_record(fixture):
    from cairnmind.evidence import evidence_states

    checkpoint = post(fixture, "/checkpoint", {"project": fixture.project["id"], **fixture.session("external"),
                                               "state": {"summary": "Claimed by an external editor"}})
    statement = checkpoint["source_ids"][0]
    alone = remember(fixture, None, "A person's note resting on the external checkpoint", [statement], kind="fact")
    both = remember(fixture, None, "A person's note resting on it and on the note", [statement, fixture.note["id"]],
                    kind="fact")
    states = evidence_states(fixture.store, fixture.project["id"], [fixture.record(alone["id"]),
                                                                    fixture.record(both["id"])])
    assert states[alone["id"]]["evidence_state"] == "degraded"
    assert states[both["id"]]["evidence_state"] == "anchored"
    assert states[both["id"]]["sources_current"] == 1


def test_an_external_test_claim_is_reported_never_a_machine_check(fixture):
    from cairnmind.verification import verification_status

    bound = fixture.session("external")
    plan = post(fixture, "/plans/save", {"project": fixture.project["id"], **bound, "idempotency_key": "claims",
                                         "plan": {"title": "Claims", "goal": "Check the claims"}, "activate": True,
                                         "steps": [{"key": "one", "content": "Tested step",
                                                    "metadata": {"requires_test": True}}]})
    task = plan["tasks"][0]
    bound = fixture.refresh(bound)
    claim = {"project": fixture.project["id"], "sources": [fixture.note["id"]], **bound,
             "record": {"id": task["id"], "expected_version": task["version"],
                        "expected_plan_version": plan["plan"]["version"],
                        "metadata": {"state": "done", "test_passed": True, "test_source_ids": [fixture.note["id"]]}}}
    refused = fixture.client.post("/remember", json=claim)
    assert refused.status_code == 400 and "fresh machine check evidence" in refused.text
    claim["record"]["metadata"]["state"] = "needs_review"
    saved = post(fixture, "/remember", claim)
    status = verification_status(fixture.store, fixture.record(saved["id"]), str(fixture.root))
    assert status["status"] == "reported" and status["run_ids"] == []


def test_the_brief_says_an_external_session_saved_the_latest_checkpoint(fixture):
    from cairnmind.brief import build_brief

    post(fixture, "/checkpoint", {"project": fixture.project["id"], **fixture.session("external"),
                                  "state": {"summary": "From an external editor"}})
    lines = build_brief(fixture.store, fixture.project["id"])["lines"]
    assert {"basis": "machine", "text": "It was saved by an external client's session (history unarchived, "
            "evidence degraded); nothing in it is verified."} in lines
    post(fixture, "/checkpoint", {"project": fixture.project["id"], **fixture.session("codex"),
                                  "state": {"summary": "From a native client"}})
    assert not any("external client" in line["text"] for line in build_brief(fixture.store, fixture.project["id"])["lines"])


def test_a_full_native_packet_stays_ready_and_never_shows_an_external_record_unmarked(fixture, monkeypatch):
    import cairnmind.sessions as sessions_module

    external = remember(fixture, fixture.session("external"), "Decided in an external editor", [fixture.note["id"]])
    changed = remember(fixture, None, "Rests on a note that changed", [fixture.note["id"]])
    fixture.store.snapshot(fixture.project["id"], "manual://note", "The note was rewritten")
    full = fixture.manager.prepare(fixture.project["id"], "codex", timeout=0)["packet"]
    states = {row["id"]: row.get("evidence_state") for row in full["records"]}
    assert states == {external["id"]: "degraded", changed["id"]: "evidence_replaced"}
    # Over the budget before its hash and status are added: the advisory data and warning yield, the external
    # record leaves the preview list (counted as omitted) rather than being shown unmarked, and the packet is
    # still ready, so a native launch is never blocked by it.
    monkeypatch.setattr(sessions_module, "MAX_PACKET_BYTES", len(sessions_module.json_text(full).encode()) - 120)
    trimmed = fixture.manager.prepare(fixture.project["id"], "claude", timeout=0)
    assert trimmed["status"] == "ready", trimmed["unknowns"]
    states = {row["id"]: row.get("evidence_state") for row in trimmed["packet"]["records"]}
    assert states == {changed["id"]: None}
    assert trimmed["packet"]["omitted_records"] == full["omitted_records"] + 1
