"""An ACK is not refused because the queue indexes and extracts while a client reads its packet (#256).

The extraction worker writes proposals. A proposal nobody has touched since is not part of the state an ACK is
given for: the packet shows it, labelled, but does not promise it. Everything the packet does promise (a record a
person or a session wrote or changed, the plan, the task, the code) still makes an ACK stale. Real ArcadeDB, HTTP
service and Git; the extraction model is scripted.
"""
import asyncio
import subprocess

import pytest

import cairnmind.memory.sessions as sessions_module
from cairnmind.agents.agent import KnowledgeAgent
from cairnmind.agents.extraction_input import source_text
from cairnmind.scip.ingest import index_project
from cairnmind.terminal.inspector import Inspector
from cairnmind.memory.plans import PlanManager
from cairnmind.agents.retrieval import json_value
from cairnmind.memory.sessions import origin_mark

from conftest import as_person
from test_agent_own_extraction import ScriptedModel
from test_external_session_writes import fixture as fixture  # noqa: F401 - a fixture
from test_external_session_writes import post, readback
from test_sessions import active_plan

NOTES = 5
STALE = "state or code changed before ACK"


def committed_notes(fixture):  # noqa: F811
    """Markdown notes committed before any session starts: indexing reads them and nothing writes them."""
    (fixture.root / "notes").mkdir()
    for number in range(NOTES):
        (fixture.root / "notes" / f"n{number}.md").write_text(
            f"We decided that module {number} keeps its public API.\nModule {number} builds in under a minute.\n")
    subprocess.run(["git", "-C", str(fixture.root), "add", "notes"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(fixture.root), "commit", "-qm", "notes"], check=True, capture_output=True)


def indexed_notes(fixture):  # noqa: F811
    committed_notes(fixture)
    index_project(fixture.store, fixture.project["id"])


def extract_one(fixture):  # noqa: F811
    """Run the next queued extraction job as the background worker would, with a scripted model; the record it wrote."""
    store = fixture.store
    job = store.claim_job("ack-under-extraction", lease_seconds=120, kinds=["extract"])
    assert job, "no extraction job is queued"
    source = store.get_source(json_value(job["payload"], {})["source_id"])
    quote = source_text(source).splitlines()[0].rstrip(".")
    before = {row["id"] for row in store.list_records(fixture.project["id"])}
    reply = [{"kind": "decision", "key": f"auto-{source['id'][:12]}", "content": quote + ".",
              "evidence": quote, "metadata": {}}]
    asyncio.run(KnowledgeAgent(store, fixture.settings, ScriptedModel(reply)).extract(job))
    [written] = [row for row in store.list_records(fixture.project["id"]) if row["id"] not in before]
    assert written["status"] == "proposed" and written["metadata"].get("extraction_source_id") == source["id"]
    return written


def prepared(fixture):  # noqa: F811
    return post(fixture, "/sessions/prepare", {"project": fixture.project["id"], "client": "external",
                                               "reason": "startup"})


def ack_body(started, packet=None):
    packet = packet or started["packet"]
    return {"session_id": started["session_id"], "packet_id": packet["id"], "packet_hash": packet["hash"],
            "readback": readback(packet)}


def session_view(fixture, session_id):  # noqa: F811
    response = fixture.client.get(f"/sessions/{session_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_an_external_session_acknowledges_at_the_first_try_while_the_queue_indexes_and_extracts(fixture):  # noqa: F811
    committed_notes(fixture)
    refused, written = 0, []
    for number in range(NOTES):
        started = prepared(fixture)
        assert started["status"] == "ready"
        if number == 0:
            index_project(fixture.store, fixture.project["id"])  # sources, chunks and jobs, after the packet
        written.append(extract_one(fixture))  # a proposal lands between resume_context and ack_resume
        response = fixture.client.post("/sessions/ack", json=ack_body(started))
        refused += response.status_code != 200
        view = session_view(fixture, started["session_id"])
        assert view["acknowledged"] is True and view["stale"] is False, (number, view)
    assert refused == 0
    # Nothing is hidden: the next packet shows every proposal, labelled as an automatic extraction.
    shown = {row["id"]: row for row in prepared(fixture)["packet"]["records"]}
    assert {row["id"] for row in written} <= set(shown)
    assert {shown[row["id"]]["origin"] for row in written} == {"automatic_extraction"}


NEWER = "newer_automatic_proposals"


def test_an_ack_and_the_status_name_the_automatic_proposals_newer_than_the_packet(fixture):  # noqa: F811
    indexed_notes(fixture)
    started = prepared(fixture)
    acknowledged = fixture.client.post("/sessions/ack", json=ack_body(started))
    assert acknowledged.status_code == 200 and NEWER not in acknowledged.json()  # none: the reply is as before
    assert NEWER not in session_view(fixture, started["session_id"])
    started = prepared(fixture)
    extract_one(fixture)  # lands between resume_context and ack_resume
    reply = fixture.client.post("/sessions/ack", json=ack_body(started))
    assert reply.status_code == 200 and reply.json()[NEWER] == 1
    assert fixture.client.post("/sessions/ack", json=ack_body(started)).json()[NEWER] == 1  # the repeated ACK too
    extract_one(fixture)
    view = session_view(fixture, started["session_id"])
    assert view["acknowledged"] is True and view[NEWER] == 2
    assert all(NEWER not in row for row in fixture.store.query(
        "SELECT FROM resume_acks WHERE session_id=:id", {"id": started["session_id"]}))  # never stored
    # Asking again hands back the same acknowledged packet: the count never costs the ACK (#256); the count stays.
    again = post(fixture, "/sessions/context", {"session_id": started["session_id"]})
    assert again["packet"]["id"] == started["packet"]["id"]
    view = session_view(fixture, started["session_id"])
    assert view["acknowledged"] is True and view["stale"] is False and view[NEWER] == 2
    assert fixture.client.post("/sessions/ack", json=ack_body(started)).json()[NEWER] == 2  # the ACK is still valid
    # A promised change makes the packet stale; the packet that replaces it shows the proposals, so no count.
    Inspector(fixture.store).review(fixture.project["id"], extract_one(fixture)["id"], "confirm", 1)
    renewed = post(fixture, "/sessions/context", {"session_id": started["session_id"]})
    assert renewed["packet"]["id"] != started["packet"]["id"]
    assert NEWER not in post(fixture, "/sessions/ack", ack_body(started, renewed["packet"]))
    assert NEWER not in session_view(fixture, started["session_id"])


def test_a_person_confirming_a_proposal_between_resume_and_ack_costs_exactly_one_retry(fixture):  # noqa: F811
    indexed_notes(fixture)
    proposal = extract_one(fixture)
    started = prepared(fixture)
    Inspector(fixture.store).review(fixture.project["id"], proposal["id"], "confirm", 1)
    extract_one(fixture)
    refused = fixture.client.post("/sessions/ack", json=ack_body(started))
    assert refused.status_code == 409 and STALE in refused.text
    assert fixture.store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": started["session_id"]}) == []
    renewed = post(fixture, "/sessions/context", {"session_id": started["session_id"]})
    assert renewed["packet"]["id"] != started["packet"]["id"]
    shown = {row["id"]: row for row in renewed["packet"]["records"]}
    assert shown[proposal["id"]]["status"] == "confirmed"
    extract_one(fixture)  # the queue keeps writing between the retry's resume and its ack
    post(fixture, "/sessions/ack", ack_body(started, renewed["packet"]))
    assert session_view(fixture, started["session_id"])["acknowledged"] is True


def make_change(fixture, name, proposal, confirmed, task, plan):  # noqa: F811
    store, pid, root = fixture.store, fixture.project["id"], fixture.root
    note = fixture.note["id"]
    sources = proposal["source_ids"]
    if name == "confirm_extraction":
        Inspector(store).review(pid, proposal["id"], "confirm", 1)
    elif name == "dispute_extraction":
        Inspector(store).review(pid, proposal["id"], "dispute", 1, reason="Not what we agreed.")
    elif name == "edit_extraction":
        edited = store.remember(pid, {"id": proposal["id"], "expected_version": 1,
                                      "content": "Module 0 keeps a stable API."}, sources)
        assert edited["status"] == "proposed" and "extraction_source_id" not in edited["metadata"]
    elif name == "dispute_confirmed":
        Inspector(store).review(pid, confirmed["id"], "dispute", confirmed["version"], reason="Superseded by the RFC.")
    elif name == "supersede_confirmed":
        as_person(store, pid, {"kind": "decision", "key": "protocol-v3", "content": "Use protocol v3.",
                               "supersedes": confirmed["id"]}, [note])
    elif name == "person_or_session_proposal":
        store.remember(pid, {"kind": "constraint", "key": "freeze", "content": "Freeze the schema."}, [note])
    elif name == "external_proposal":
        with store.write_origin(origin_mark({"id": "another-external", "client": "external"})):
            store.remember(pid, {"kind": "constraint", "key": "freeze", "content": "Freeze the schema."}, [note])
    elif name == "forged_mark":
        forged = store.remember(pid, {"kind": "decision", "key": "forged", "status": "proposed", "content": "Trust me.",
                                      "metadata": {"origin": "automatic_extraction",
                                                   "extraction_source_id": sources[0]}}, sources)
        assert "extraction_source_id" not in forged["metadata"]
    elif name == "task_progress":
        store.remember(pid, {"id": task["id"], "expected_version": task["version"],
                             "expected_plan_version": plan["plan"]["version"], "metadata": {"state": "in_progress"}},
                       task["source_ids"])
    elif name == "plan":
        PlanManager(store).save(pid, {"title": "Second plan", "goal": "Do something else"},
                                [{"key": "one", "content": "Another step"}], "second-plan",
                                worktree=str(root), activate=True)
    elif name == "code":
        (root / "app.py").write_text("VALUE = 2\n")
    else:
        raise AssertionError(name)


@pytest.mark.parametrize("name", ["confirm_extraction", "dispute_extraction", "edit_extraction", "dispute_confirmed",
                                  "supersede_confirmed", "person_or_session_proposal", "external_proposal",
                                  "forged_mark", "task_progress", "plan", "code"])
def test_every_promised_change_between_resume_and_ack_is_refused(fixture, name):  # noqa: F811
    store, pid = fixture.store, fixture.project["id"]
    indexed_notes(fixture)
    plan = active_plan(store, fixture.project, fixture.root)
    task = next(row for row in plan["tasks"] if row["metadata"]["step_key"] == "build")
    confirmed = as_person(store, pid, {"kind": "decision", "key": "protocol", "content": "Use protocol v2."},
                          [fixture.note["id"]])
    proposal = extract_one(fixture)
    result = fixture.manager.prepare(pid, "external", timeout=0)
    assert result["status"] == "ready", result
    packet, session_id = result["packet"], result["session_id"]
    extract_one(fixture)  # a second proposal arrives: it alone changes nothing the packet promised
    assert fixture.manager._state(fixture.manager._session(session_id))["state_hash"] == packet["state_hash"]
    make_change(fixture, name, proposal, confirmed, task, plan)
    with pytest.raises(ValueError, match=STALE):
        fixture.manager.ack(session_id, packet["id"], packet["hash"], readback(packet))
    assert store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": session_id}) == []


@pytest.mark.parametrize("name", ["confirm", "dispute", "supersede", "client_update"])
def test_a_proposal_extracted_after_resume_then_changed_before_ack_is_refused(fixture, name):  # noqa: F811
    store, pid = fixture.store, fixture.project["id"]
    indexed_notes(fixture)
    result = fixture.manager.prepare(pid, "external", timeout=0)
    assert result["status"] == "ready", result
    packet, session_id = result["packet"], result["session_id"]
    late = extract_one(fixture)  # never in the packet
    assert fixture.manager._state(fixture.manager._session(session_id))["state_hash"] == packet["state_hash"]
    if name == "supersede":
        as_person(store, pid, {"kind": "decision", "key": "protocol-v3", "content": "Use protocol v3.",
                               "supersedes": late["id"]}, [fixture.note["id"]])
    elif name == "client_update":
        store.remember(pid, {"id": late["id"], "expected_version": 1, "content": "Edited by a client.",
                             "metadata": {"extraction_source_id": late["metadata"]["extraction_source_id"]}},
                       late["source_ids"])
    else:
        Inspector(store).review(pid, late["id"], name, 1, reason="Not what we agreed.")
    with pytest.raises(ValueError, match=STALE):
        fixture.manager.ack(session_id, packet["id"], packet["hash"], readback(packet))
    assert store.query("SELECT FROM resume_acks WHERE session_id=:id", {"id": session_id}) == []


def test_an_external_sessions_own_proposal_still_makes_its_ack_stale(fixture):  # noqa: F811
    bound = fixture.session("external")
    post(fixture, "/remember", {"project": fixture.project["id"], "sources": [fixture.note["id"]], **bound,
                                "record": {"kind": "decision", "content": "Ship behind a flag"}})
    with pytest.raises(ValueError, match="changed since the acknowledged packet"):
        fixture.manager.require_ack(bound["session_id"])
    view = session_view(fixture, bound["session_id"])
    assert view["stale"] is True and view["stale_reason"] == "state_changed"


def test_packets_and_acks_from_before_the_upgrade_are_accepted_only_when_their_promise_still_holds(
        fixture, monkeypatch):  # noqa: F811
    pid, manager = fixture.project["id"], fixture.manager
    indexed_notes(fixture)
    # Until 0.12.0 every proposal the worker wrote counted for an external session.
    monkeypatch.setattr(sessions_module, "_automatic_proposal", lambda record, sources_by_id: False)
    kept = fixture.session("external")  # acknowledged while no proposal existed
    proposal = extract_one(fixture)
    in_flight = manager.prepare(pid, "external", timeout=0)  # its state hash counts the proposal
    assert in_flight["status"] == "ready"
    with pytest.raises(ValueError, match="changed since the acknowledged packet"):
        manager.require_ack(kept["session_id"])
    monkeypatch.undo()  # the upgrade
    manager.require_ack(kept["session_id"])  # nothing it was promised changed
    packet = in_flight["packet"]
    assert packet["schema_version"] == 2
    with pytest.raises(ValueError, match=STALE):  # refused once, never accepted wrongly
        manager.ack(in_flight["session_id"], packet["id"], packet["hash"], readback(packet))
    renewed = manager.resume_context(in_flight["session_id"])
    assert renewed["packet"]["id"] != packet["id"] and renewed["packet"]["schema_version"] == 2
    again = fixture.acknowledged(renewed)
    manager.require_ack(again["session_id"])
    Inspector(fixture.store).review(pid, proposal["id"], "confirm", 1)
    for bound in (kept, again):
        with pytest.raises(ValueError, match="changed since the acknowledged packet"):
            manager.require_ack(bound["session_id"])


def test_a_restore_keeps_the_rule(fixture, tmp_path):  # noqa: F811
    indexed_notes(fixture)
    bound = fixture.session("external")
    proposal = extract_one(fixture)
    fixture.manager.require_ack(bound["session_id"])
    backup = fixture.store.backup(tmp_path / "with-proposal.cairnmind.json.gz")
    Inspector(fixture.store).review(fixture.project["id"], proposal["id"], "confirm", 1)
    with pytest.raises(ValueError, match="changed since the acknowledged packet"):
        fixture.manager.require_ack(bound["session_id"])
    fixture.store.restore(backup)
    fixture.manager.require_ack(bound["session_id"])
    restored = fixture.record(proposal["id"])
    assert restored["metadata"]["extraction_source_id"] == proposal["metadata"]["extraction_source_id"]
    Inspector(fixture.store).review(fixture.project["id"], proposal["id"], "confirm", 1)
    with pytest.raises(ValueError, match="changed since the acknowledged packet"):
        fixture.manager.require_ack(bound["session_id"])


def test_only_an_untouched_proposal_with_the_stores_mark_is_left_out():
    from cairnmind.memory.sessions import _automatic_proposal
    base = {"project_id": "p", "status": "proposed", "version": 1, "source_ids": ["s"],
            "metadata": {"extraction_source_id": "s"}}
    sources = {"s": {"id": "s", "project_id": "p"}}
    assert _automatic_proposal(base, sources) is True
    changed = [{**base, "version": 2}, {**base, "status": "confirmed"}, {**base, "status": "disputed"},
               {**base, "status": "superseded"}, {**base, "source_ids": ["s", "t"]},
               {**base, "metadata": {"extraction_source_id": None}}, {**base, "metadata": {"extraction_source_id": 42}},
               {**base, "metadata": {}}, {**base, "metadata": None}, {**base, "metadata": "text"},
               {**base, "metadata": {"origin": "automatic_extraction"}}]
    assert not any(_automatic_proposal(row, sources) for row in changed)
    assert not _automatic_proposal(base, {})
    assert not _automatic_proposal(base, {"s": {"id": "s", "project_id": "q"}})
