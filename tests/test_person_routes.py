"""A person's routes need the open workspace's key, and the store counts a person's act only with it (#214).

Real HTTP service and ArcadeDB. The workspace is stood in for by `workspace_person` (conftest): the tmux that
holds the key answers with the test's key, and the headers are what the workspace's panels send.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from cairnmind.memory import confirmation
from cairnmind.terminal import person_proof
from cairnmind.terminal.cockpit import create_cockpit_app
from cairnmind.memory.confirmation import ConfirmationRefused, VerdictKept, confirmation_of
from cairnmind.memory.memory import MemoryManager
from cairnmind.memory.plans import PlanManager
from cairnmind.memory.record_kinds import act_as_person

from conftest import TEST_KEY, as_person, person_proven
from test_cockpit_server import ORIGIN, Runtime, login
from test_confirmed_by_person import fixture  # noqa: F401 (the fixture)
from test_confirmed_elsewhere import rows, two_computers  # noqa: F401 (a fixture and a helper)

KIND = {"client": "claude", "kind": "future-claude-record"}


def state(fixture):  # noqa: F811
    """What a refused person's route must leave exactly as it was."""
    query = fixture.store.query
    return {"records": sorted((row["id"], row["status"], row["version"]) for row in query(
                "SELECT id,status,version FROM records WHERE project_id=:p", {"p": fixture.pid})),
            "sources": query("SELECT count(*) AS n FROM sources")[0]["n"],
            "kinds": query("SELECT accepted_record_kinds AS a, withdrawn_record_kinds AS w FROM projects "
                           "WHERE id=:p", {"p": fixture.pid})}


def conflict_pair(fixture):  # noqa: F811
    existing = as_person(fixture.store, fixture.pid, {"kind": "decision", "key": "ttl",
                                                      "content": "Sessions last a day."}, [fixture.note["id"]])
    candidate = fixture.store.remember(fixture.pid, {
        "kind": "decision", "key": "ttl~a", "content": "Sessions last an hour.",
        "metadata": {"possible_conflicts": [existing["id"]], "suggested_key": "ttl"}}, [fixture.note["id"]])
    return existing, candidate


def saved_plan(fixture):  # noqa: F811
    return PlanManager(fixture.store).save(fixture.pid, {"title": "Ship", "goal": "Ship it", "constraints": []},
                                           [{"key": "one", "content": "Step one"}], "ship")


def person_requests(fixture):  # noqa: F811
    """Every route that can end in a person's act: (name, path, body), built on the fixture's records."""
    pid, proposed = fixture.pid, fixture.proposed("A decision that waits for a person")
    existing, candidate = conflict_pair(fixture)
    plan = saved_plan(fixture)
    act_as_person(fixture.store, pid, "claude", "future-withdraw-me", "Because", route="inspector_review")
    finding = {"project": pid, "content": "Use WAL", "kind": "constraint"}
    review = f"/inspector/records/{proposed['id']}/review"
    resolve = {"project": pid, "record_id": candidate["id"], "other_id": existing["id"],
               "expected_version": candidate["version"], "other_expected_version": existing["version"]}
    imports = {"replace_confirmed": True}
    replaceable = fixture.proposed("A decision to replace")
    legacy = fixture.store.remember(pid, {"kind": "task", "key": "old", "content": "An old task", "metadata": {
        "plan_id": "old-group", "step_index": 1, "state": "todo"}}, [fixture.note["id"]])
    return {
        "legacy adopt": ("/inspector/legacy-groups/adopt", {
            "project": pid, "group": "old-group", "task_versions": {legacy["id"]: legacy["version"]}}),
        "review confirm": (review, {"project": pid, "action": "confirm", "expected_version": 1}),
        "review dispute": (review, {"project": pid, "action": "dispute", "expected_version": 1, "reason": "No"}),
        "replace": (f"/inspector/records/{replaceable['id']}/replace",
                    {"project": pid, "content": "Use WAL mode", "expected_version": 1}),
        "conflict supersede": ("/inspector/conflicts/resolve", {**resolve, "action": "supersede",
                                                                "keep": candidate["id"]}),
        "conflict dismiss": ("/inspector/conflicts/resolve", {**resolve, "action": "dismiss"}),
        "plan confirm": (f"/inspector/plans/{plan['plan']['id']}/confirm",
                         {"project": pid, "expected_plan_version": plan["plan"]["version"]}),
        "record kind accept": ("/inspector/record-kinds/accept", {"project": pid, **KIND, "note": "Bookkeeping"}),
        "record kind withdraw": ("/inspector/record-kinds/withdraw",
                                 {"project": pid, "client": "claude", "kind": "future-withdraw-me"}),
        "finding proposed": ("/findings", {**finding, "status": "proposed", "key": str(uuid4())}),
        "finding confirmed": ("/findings", {**finding, "status": "confirmed", "key": str(uuid4())}),
        "import archive": ("/memory/import", {"path": "/nowhere", "root": str(fixture.root), **imports}),
        "import published": ("/memory/import-published", {"project": pid, "path": "/nowhere", **imports}),
        "import commit": ("/memory/import-commit", {"project": pid, "revision": "HEAD", **imports}),
        "project forget": ("/inspector/projects/forget", {"project": pid}),
    }


NAMES = ["review confirm", "review dispute", "replace", "conflict supersede", "conflict dismiss", "plan confirm",
         "record kind accept", "record kind withdraw", "finding proposed", "finding confirmed", "import archive",
         "import published", "import commit", "legacy adopt", "project forget"]


@pytest.mark.parametrize("name", NAMES)
def test_every_person_route_refuses_the_service_token_alone(fixture, name):  # noqa: F811
    path, body = person_requests(fixture)[name]
    before = state(fixture)
    response = fixture.client.post(path, json=body)
    expected = (person_proof.IMPORT_REPLACE if path.startswith("/memory/")
                else person_proof.FORGET_REFUSED if path.endswith("/projects/forget") else person_proof.PROOF_MISSING)
    assert response.status_code == 403 and response.json()["detail"] == expected, (name, response.text)
    assert state(fixture) == before, name  # nothing written: no record, no audit note, no acceptance
    assert fixture.store.query("SELECT id FROM projects WHERE id=:p", {"p": fixture.pid}), name


@pytest.mark.parametrize("name", NAMES)
def test_a_wrong_key_is_refused_by_every_person_route_too(fixture, workspace_person, name):  # noqa: F811
    path, body = person_requests(fixture)[name]
    before = state(fixture)
    forged = {**workspace_person, person_proof.KEY_HEADER: "cd" * 32}
    response = fixture.client.post(path, json=body, headers=forged)
    assert response.status_code == 403 and response.json()["detail"] == person_proof.PROOF_REFUSED, (name, response.text)
    assert state(fixture) == before, name


def test_a_workspace_without_a_key_another_socket_or_no_tmux_is_refused_with_its_own_sentence(
        fixture, workspace_person, monkeypatch):  # noqa: F811
    path, body = "/inspector/records/{}/review".format(fixture.proposed()["id"]), {
        "project": fixture.pid, "action": "confirm", "expected_version": 1}
    before = state(fixture)

    def refused(headers, sentence):
        response = fixture.client.post(path, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == sentence, response.text
        assert state(fixture) == before
    refused({**workspace_person, person_proof.SOCKET_HEADER: "/tmp/elsewhere/tmux.sock"}, person_proof.PROOF_REFUSED)
    refused({person_proof.KEY_HEADER: TEST_KEY}, person_proof.PROOF_REFUSED)  # a key with no socket
    monkeypatch.setattr(person_proof, "read_key", lambda *args, **kwargs: ("no_key", None))
    refused(workspace_person, person_proof.NO_WORKSPACE_KEY)
    monkeypatch.setattr(person_proof, "read_key", lambda *args, **kwargs: ("unavailable", None))
    refused(workspace_person, person_proof.PROOF_REFUSED)
    monkeypatch.setattr(person_proof, "IS_WINDOWS", False)
    monkeypatch.setattr(person_proof, "_tmux", lambda settings: None)
    refused(workspace_person, person_proof.TMUX_MISSING)


def test_the_workspace_key_lets_the_person_act(fixture):  # noqa: F811
    requests = person_requests(fixture)
    person = fixture.person

    def post(name):
        path, body = requests[name]
        response = fixture.client.post(path, json=body, headers=person)
        assert response.status_code == 200, (name, response.text)
        return response.json()
    reviewed = post("review confirm")["record"]
    assert reviewed["status"] == "confirmed" and confirmation_of(reviewed)["route"] == "inspector_review"
    assert confirmation_of(reviewed)["by"] == "person"
    replaced = post("replace")["record"]
    assert replaced["status"] == "confirmed" and confirmation_of(replaced)["route"] == "inspector_review"
    decided = post("conflict supersede")
    assert decided["record"]["status"] == "confirmed" and confirmation_of(decided["record"])["route"] == "conflict_decision"
    planned = post("plan confirm")
    assert {task["status"] for task in planned["tasks"]} == {"confirmed"}
    assert post("record kind accept")["status"] == "accepted"
    assert post("record kind withdraw")["status"] == "withdrawn"
    adopted = post("legacy adopt")
    assert adopted["plan"]["title"] == "old-group" and {task["status"] for task in adopted["tasks"]} == {"proposed"}
    form = post("finding confirmed")["record"]
    assert form["status"] == "confirmed" and confirmation_of(form)["route"] == "workspace_form"
    assert post("finding proposed")["record"]["status"] == "proposed"
    # The import's route gate: the person's own archive, replacing nothing, is imported as before.
    snapshot = MemoryManager(fixture.store).snapshot(fixture.pid)
    path, body = requests["import archive"]
    imported = fixture.client.post(path, json={**body, "path": snapshot["path"], "expected_replaced": []},
                                  headers=person)  # the list the person was shown: none (#333)
    assert imported.status_code == 200 and imported.json()["status"] == "unchanged", imported.text
    refused = fixture.client.post(path, json={**body, "path": snapshot["path"]})
    assert refused.status_code == 403


def test_the_workspace_key_lets_the_person_delete_a_project(fixture):  # noqa: F811
    path, body = person_requests(fixture)["project forget"]
    listed = fixture.client.get("/inspector/projects/deletable")
    assert listed.status_code == 200 and [item["id"] for item in listed.json()["items"]] == [fixture.pid]
    shown = fixture.client.get("/inspector/projects/forget", params={"project": fixture.pid})  # reads, needs no key
    assert shown.status_code == 200 and shown.json()["deletes"]["records"] >= 1, shown.text
    assert fixture.client.get("/inspector/projects/forget", params={"project": "nope"}).status_code == 400
    sent = fixture.client.post(path, json={**body, "session": "x"}, headers=fixture.person)
    assert sent.status_code == 422  # a person's act names no session
    response = fixture.client.post(path, json=body, headers=fixture.person)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == fixture.pid and response.json()["backups_unchanged"] is True
    assert not fixture.store.query("SELECT id FROM projects WHERE id=:p", {"p": fixture.pid})
    assert fixture.store._count("records", fixture.pid) == 0
    entries = fixture.client.get("/status").json()["deleted_projects"]
    assert [entry["project_id"] for entry in entries] == [fixture.pid]
    again = fixture.client.post(path, json=body, headers=fixture.person)
    assert again.status_code == 400 and "Unknown project" in again.json()["detail"]  # gone: nothing to delete


def test_a_dispute_with_the_key_is_stored_and_without_it_is_not(fixture):  # noqa: F811
    requests = person_requests(fixture)
    path, body = requests["review dispute"]
    assert fixture.client.post(path, json=body).status_code == 403
    response = fixture.client.post(path, json=body, headers=fixture.person)
    assert response.status_code == 200 and response.json()["record"]["status"] == "disputed"


def test_the_proof_reaches_the_store_thread_and_no_other_request(fixture):  # noqa: F811
    requests = person_requests(fixture)
    path, body = requests["review confirm"]
    assert fixture.client.post(path, json=body, headers=fixture.person).status_code == 200
    # The next request on the same client, without the headers, is a token holder's again.
    path, body = requests["conflict supersede"]
    before = state(fixture)
    assert fixture.client.post(path, json=body).status_code == 403
    assert state(fixture) == before
    # A proven form does not let a following /remember ask for the status confirmed.
    path, body = requests["finding confirmed"]
    assert fixture.client.post(path, json=body, headers=fixture.person).status_code == 200
    asked = fixture.client.post("/remember", json={
        "project": fixture.pid, "sources": [fixture.note["id"]],
        "record": {"kind": "constraint", "status": "confirmed", "content": "Asked after a proven request"}})
    assert asked.status_code == 400 and confirmation.REFUSED in asked.json()["detail"], asked.text
    assert not fixture.store.query("SELECT id FROM records WHERE content=:c",
                                   {"c": "Asked after a proven request"})


@pytest.mark.unproven  # the door of the tests is shut: only `require` can give the store the proof
def test_the_route_alone_gives_the_store_the_proof(fixture):  # noqa: F811
    with person_proven():
        requests = person_requests(fixture)
    path, body = requests["review confirm"]
    assert fixture.client.post(path, json=body).status_code == 403
    response = fixture.client.post(path, json=body, headers=fixture.person)
    assert response.status_code == 200, response.text
    assert confirmation_of(response.json()["record"])["by"] == "person"
    path, body = requests["finding confirmed"]
    form = fixture.client.post(path, json=body, headers=fixture.person)
    assert form.status_code == 200 and confirmation_of(form.json()["record"])["route"] == "workspace_form"


def test_a_session_still_cannot_use_a_persons_proof_for_a_finding(fixture):  # noqa: F811
    # A request that names a session is a session's finding: the proof does not turn it into the person's form.
    bound = fixture.session("codex")
    body = {"project": fixture.pid, "content": "Use WAL", "kind": "constraint", "status": "confirmed",
            "key": str(uuid4()), **bound}
    response = fixture.client.post("/findings", json=body, headers=fixture.person)
    assert response.status_code == 400 and confirmation.REFUSED in response.json()["detail"], response.text


def test_a_broker_session_from_the_token_does_not_carry_a_person(fixture, tmp_path, monkeypatch):  # noqa: F811
    """The documented sequence through the broker: a ticket and an exchange with the token, a forged Origin."""
    def upstream(request):
        reply = fixture.client.request(request.method, request.url.raw_path.decode(), content=request.content,
                                       headers={name: value for name, value in request.headers.items()
                                                if name.lower() in {"authorization", "content-type"}
                                                or name.lower().startswith("x-cairnmind-")})
        return httpx.Response(reply.status_code, content=reply.content, headers={"Content-Type": "application/json"})
    monkeypatch.setattr("cairnmind.agents.client_auth.probe_client", lambda client, **kwargs: {})
    app = create_cockpit_app(fixture.settings, runtime=Runtime(), proxy_transport=httpx.MockTransport(upstream),
                             public_url=ORIGIN)
    requests = person_requests(fixture)
    before = state(fixture)
    with TestClient(app, base_url=ORIGIN) as browser:
        headers = login(app, browser)
        review, review_body = requests["review confirm"]
        conflict, conflict_body = requests["conflict supersede"]
        for path, body in ((review, review_body), (conflict, conflict_body)):
            sent = browser.post("/api" + path, json=body, headers=headers)
            assert sent.status_code == 403 and sent.json()["detail"] == person_proof.PROOF_MISSING, sent.text
        # With a key it invented: the same refusal as a wrong key.
        forged = {person_proof.KEY_HEADER: "ef" * 32, person_proof.SOCKET_HEADER: "/tmp/cairnmind-ui-1-x/tmux.sock"}
        sent = browser.post("/api" + review, json=review_body, headers={**headers, **forged})
        assert sent.status_code == 403 and sent.json()["detail"] == person_proof.PROOF_REFUSED
        finding, finding_body = requests["finding confirmed"]
        sent = browser.post("/api" + finding, json=finding_body, headers=headers)
        assert sent.status_code == 403
        # The panel's own proof, passed on by the broker, is what counts.
        sent = browser.post("/api" + review, json=review_body, headers={**headers, **fixture.person})
        assert sent.status_code == 200, sent.text
    assert state(fixture) != before  # only that last request changed anything


def test_the_key_is_in_no_log_or_error(fixture, caplog):  # noqa: F811
    requests = person_requests(fixture)
    path, body = requests["review confirm"]
    with caplog.at_level(logging.DEBUG):
        refused = fixture.client.post(path, json=body, headers={**fixture.person,
                                                                person_proof.KEY_HEADER: "cd" * 32})
        accepted = fixture.client.post(path, json=body, headers=fixture.person)
    assert refused.status_code == 403 and accepted.status_code == 200
    for shown in (refused.text, accepted.text, caplog.text):
        assert TEST_KEY not in shown and "cd" * 32 not in shown


# The store: one gate for every confirmation.

@pytest.mark.unproven
def test_the_store_counts_no_act_without_the_proof(fixture):  # noqa: F811
    store, pid, note = fixture.store, fixture.pid, fixture.note
    saved = fixture.proposed("A decision a person might confirm")
    before = state(fixture)
    audit = confirmation.person_source(store, pid, "confirm", route="inspector_review")
    with store.person_act("inspector_review", audit["id"]):
        assert store.open_person_act() is None  # an act that is not proven is not the person's
        with pytest.raises(ConfirmationRefused) as refused:
            store.remember(pid, {"id": saved["id"], "expected_version": 1, "status": "confirmed"},
                           [note["id"], audit["id"]])
        assert str(refused.value) == confirmation.NOT_PROVEN
        with pytest.raises(ConfirmationRefused, match=confirmation.NOT_PROVEN):
            store.remember(pid, {"kind": "fact", "content": "New and confirmed", "status": "confirmed"},
                           [note["id"], audit["id"]])
    assert fixture.record(saved["id"])["status"] == "proposed"
    assert {key: state(fixture)[key] for key in ("records", "kinds")} == {key: before[key] for key in ("records", "kinds")}
    with person_proven(), store.person_act("inspector_review", audit["id"]):
        assert store.open_person_act() == {"route": "inspector_review", "source_id": audit["id"]}
        stored = store.remember(pid, {"id": saved["id"], "expected_version": 1, "status": "confirmed"},
                                [note["id"], audit["id"]])
    assert stored["status"] == "confirmed" and confirmation_of(stored)["by"] == "person"
    # A person's verdict is kept against an act that is not proven: it is a write like a session's.
    with store.person_act("inspector_review", audit["id"]), pytest.raises(VerdictKept):
        store.remember(pid, {"id": saved["id"], "expected_version": stored["version"], "status": "disputed"},
                       stored["source_ids"])
    with store.person_act("conflict_decision", audit["id"]), pytest.raises(VerdictKept):
        store.remember(pid, {"kind": "decision", "content": "Another text", "supersedes": saved["id"]},
                       [note["id"]])
    assert fixture.record(saved["id"])["status"] == "confirmed"


@pytest.mark.unproven
def test_a_record_kind_acceptance_needs_the_proof_too(fixture):  # noqa: F811
    from cairnmind.memory.record_kinds import AcceptanceRefused
    with pytest.raises(AcceptanceRefused):
        act_as_person(fixture.store, fixture.pid, "claude", "future-claude-record", "Because",
                      route="inspector_review")
    assert state(fixture)["kinds"][0]["a"] in (None, [], "[]")


@pytest.mark.unproven
def test_seed_data_needs_no_proof(fixture):  # noqa: F811
    stored = confirmation.remember_as_person(
        fixture.store, fixture.pid, {"kind": "fact", "content": "A fixture fact", "status": "confirmed"},
        [fixture.note["id"]], route=confirmation.SEED_ROUTE)
    assert stored["status"] == "confirmed" and confirmation_of(stored)["by"] == "seed"


@pytest.mark.unproven
def test_imports_replace_a_verdict_only_with_the_proof(two_computers):  # noqa: F811
    from cairnmind.memory.memory import MemoryConflict  # noqa: F401
    from cairnmind.terminal.inspector import Inspector
    source, destination, pid, first, second = two_computers
    with person_proven():
        note = source.snapshot(pid, "manual://note", "Keep the decision across computers", "manual")
        decision = source.remember(pid, {"kind": "decision", "content": "Use the portable decision"}, [note["id"]])
        Inspector(source).review(pid, decision["id"], "confirm", 1)
        MemoryManager(destination).import_snapshot(MemoryManager(source).snapshot(pid)["path"], second)
        there = rows(destination, pid)
        changed = destination.remember(pid, {"id": decision["id"], "expected_version": there[decision["id"]]["version"],
                                             "content": "Use another decision"}, there[decision["id"]]["source_ids"])
        Inspector(destination).review(pid, decision["id"], "confirm", changed["version"])
        later = MemoryManager(destination).snapshot(pid, second)
    mine = rows(source, pid)[decision["id"]]
    with pytest.raises(ConfirmationRefused) as refused:
        MemoryManager(source).import_snapshot(later["path"], first, replace_confirmed=True)
    assert str(refused.value) == person_proof.IMPORT_REPLACE
    assert rows(source, pid)[decision["id"]] == mine  # nothing was imported
    # A dry run shows it; with the proof the person's choice is applied.
    assert MemoryManager(source).import_snapshot(later["path"], first, dry_run=True)["replaces_confirmed_here"]["total"] == 1
    with person_proven():
        back = MemoryManager(source).import_snapshot(later["path"], first, replace_confirmed=True)
    assert back["status"] == "imported"
    assert rows(source, pid)[decision["id"]]["content"] == "Use another decision"
