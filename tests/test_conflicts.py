"""The conflict inbox: pending pairs and the three audited decisions, against the real store."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairnmind import conflicts
from cairnmind.evidence import evidence_states
from cairnmind.inspector import Inspector, InspectorNotFound
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.store import Store


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "database")
    project = store.add_project("conflicts", root)
    return store, project, root


def pair(store, project, *, kind="decision", key="session-ttl", old="Sessions last 24 hours.",
         new="Sessions expire after one hour.", old_time="2026-03-01T10:00:00+00:00",
         new_time="2026-06-01T10:00:00+00:00", **metadata):
    """An existing finding and a later one that extraction marked as a possible conflict."""
    first = store.snapshot(project["id"], f"transcript://{key}/old", old, "transcript",
                           {"source_timestamp": old_time} if old_time else {})
    existing = store.remember(project["id"], {"kind": kind, "key": key, "content": old, "status": "confirmed"},
                              [first["id"]])
    second = store.snapshot(project["id"], f"transcript://{key}/new", new, "transcript",
                            {"source_timestamp": new_time} if new_time else {})
    candidate = store.remember(project["id"], {"kind": kind, "key": f"{key}~abc", "content": new, "metadata": {
        "possible_conflicts": [existing["id"]], "suggested_key": key, "evidence": new, **metadata}}, [second["id"]])
    return candidate, existing


def pending(store, project):
    return conflicts.list_conflicts(store, project["id"], store.list_records(project["id"]))


def resolve(store, project, candidate, existing, action, **options):
    return conflicts.resolve_conflict(store, project["id"], candidate["id"], existing["id"], action,
                                      candidate["version"], existing["version"], **options)


def record(store, identifier):
    return store.query("SELECT FROM records WHERE id=:id", {"id": identifier})[0]


def normalized_export(store):
    result = store.export()
    result.pop("exported_at", None)
    return result


def test_a_pending_pair_is_listed_with_dates_scope_sources_and_the_order_of_evidence(workspace):
    store, project, root = workspace
    candidate, existing = pair(store, project, worktree=str(root))
    listed = pending(store, project)
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["id"] == f"{candidate['id']}:{existing['id']}" and item["kind"] == "conflict"
    first, second = item["candidate"], item["existing"]
    assert (first["id"], first["status"], first["content"]) == (candidate["id"], "proposed", "Sessions expire after one hour.")
    assert (second["id"], second["status"], second["content"]) == (existing["id"], "confirmed", "Sessions last 24 hours.")
    assert first["evidence_latest"].startswith("2026-06-01") and first["evidence_basis"] == "stated"
    assert second["evidence_latest"].startswith("2026-03-01")
    assert first["scope"] == str(root) and second["scope"] is None
    assert first["sources"] == ["transcript://session-ttl/new"] and second["sources"] == ["transcript://session-ttl/old"]
    assert first["evidence_state"] == second["evidence_state"] == "anchored"
    assert (item["newer_evidence"], item["newer_evidence_basis"]) == (candidate["id"], "stated")


def test_the_order_of_evidence_is_not_called_reliable_when_a_time_was_only_observed(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project, new_time=None)
    item = pending(store, project)["items"][0]
    # The marked finding was snapshotted now, which only bounds its evidence from above.
    assert (item["newer_evidence"], item["newer_evidence_basis"]) == (candidate["id"], "observed")
    assert item["candidate"]["evidence_basis"] == "observed"


def test_a_real_contradiction_is_resolved_by_superseding_and_every_source_is_kept(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    sources_before = {row["id"] for row in store.query("SELECT id FROM sources WHERE project_id=:p", {"p": project["id"]})}
    result = resolve(store, project, candidate, existing, "supersede", keep=candidate["id"], reason="Policy changed in June")
    kept, replaced = record(store, candidate["id"]), record(store, existing["id"])
    assert result["record"]["id"] == kept["id"] and result["superseded"]["id"] == replaced["id"]
    assert (kept["status"], kept["supersedes"], kept["key"]) == ("confirmed", existing["id"], "session-ttl")
    assert replaced["status"] == "superseded" and replaced["source_ids"] == existing["source_ids"]
    assert kept["source_ids"] == [*candidate["source_ids"], result["audit_source_id"]]
    assert kept["metadata"]["possible_conflicts"] == [] and kept["metadata"]["conflicts_resolved"] == [existing["id"]]
    assert kept["metadata"]["conflict_decisions"] == [f"{existing['id']}:supersede"]
    assert "supersede_warning" not in kept["metadata"]
    audit = store.get_source(result["audit_source_id"])
    note = json.loads(audit["content"])
    assert audit["uri"].startswith("review://") and audit["metadata"]["origin"] == "local_user_review"
    assert (note["decision"], note["kept_record_id"], note["reason"]) == ("supersede", candidate["id"], "Policy changed in June")
    assert note["versions"] == {candidate["id"]: 1, existing["id"]: 1} and note["actor"] == "local_user"
    after = {row["id"] for row in store.query("SELECT id FROM sources WHERE project_id=:p", {"p": project["id"]})}
    assert after == sources_before | {result["audit_source_id"]}
    assert pending(store, project)["items"] == []
    history = store.record_history(existing["id"])
    assert [row["status"] for row in history] == ["confirmed", "superseded"]


def test_keeping_the_existing_finding_supersedes_the_new_one_and_warns_when_its_evidence_is_older(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    resolve(store, project, candidate, existing, "supersede", keep=existing["id"])
    kept, replaced = record(store, existing["id"]), record(store, candidate["id"])
    assert (kept["status"], kept["supersedes"], kept["key"]) == ("confirmed", candidate["id"], "session-ttl")
    assert replaced["status"] == "superseded"
    # The user may keep the older finding; the store still says that its evidence is older.
    assert kept["metadata"]["supersede_warning"] == "anachronistic_supersede"
    assert pending(store, project)["items"] == []


def test_two_valid_scopes_keep_both_findings_and_need_a_description(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    before = normalized_export(store)
    with pytest.raises(ValueError, match="description of the two scopes"):
        resolve(store, project, candidate, existing, "different_scopes", reason="  ")
    assert normalized_export(store) == before
    result = resolve(store, project, candidate, existing, "different_scopes", reason="One hour for the API, a day for the web")
    first, second = record(store, candidate["id"]), record(store, existing["id"])
    assert (first["status"], second["status"]) == ("proposed", "confirmed")
    assert first["supersedes"] is None and second["version"] == existing["version"]
    assert first["metadata"]["conflict_decisions"] == [f"{existing['id']}:different_scopes"]
    assert first["key"] == "session-ttl~abc" and result["superseded"] is None
    assert json.loads(store.get_source(result["audit_source_id"])["content"])["reason"].startswith("One hour for the API")
    assert pending(store, project)["items"] == []


def test_a_false_alarm_is_dismissed_with_an_audit_note_and_changes_no_status(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    result = resolve(store, project, candidate, existing, "dismiss")
    first = record(store, candidate["id"])
    assert (first["status"], record(store, existing["id"])["status"]) == ("proposed", "confirmed")
    assert first["source_ids"][-1] == result["audit_source_id"] and first["version"] == 2
    assert first["metadata"]["conflict_decisions"] == [f"{existing['id']}:dismiss"]
    assert pending(store, project)["items"] == []
    # The audit note documents the decision; it is not evidence, and the finding's own source still anchors it.
    assert evidence_states(store, project["id"], [first])[first["id"]]["sources_current"] == 1


def test_stale_versions_wrong_pairs_and_bad_requests_change_nothing(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    other, _ = pair(store, project, key="cache", old="Cache for a day.", new="Cache for an hour.")
    before = normalized_export(store)
    cases = [
        (dict(action="merge"), "supersede, different_scopes or dismiss"),
        (dict(action="supersede"), "ID of the finding to keep"),
        (dict(action="supersede", keep=other["id"]), "ID of the finding to keep"),
        (dict(action="dismiss", keep=candidate["id"]), "Only superseding"),
        (dict(action="dismiss", reason="x" * 4001), "at most 4000"),
    ]
    for options, message in cases:
        with pytest.raises(ValueError, match=message):
            resolve(store, project, candidate, existing, options.pop("action"), **options)
    with pytest.raises(ValueError, match="version conflict"):
        conflicts.resolve_conflict(store, project["id"], candidate["id"], existing["id"], "dismiss", 1, 2)
    with pytest.raises(ValueError, match="exact version"):
        conflicts.resolve_conflict(store, project["id"], candidate["id"], existing["id"], "dismiss", True, 1)
    with pytest.raises(ValueError, match="not a pending conflict"):
        resolve(store, project, existing, candidate, "dismiss")
    with pytest.raises(ValueError, match="not a pending conflict"):
        resolve(store, project, candidate, other, "dismiss")
    with pytest.raises(LookupError):
        conflicts.resolve_conflict(store, project["id"], candidate["id"], "missing", "dismiss", 1, 1)
    assert normalized_export(store) == before
    resolve(store, project, candidate, existing, "dismiss")
    with pytest.raises(ValueError, match="version conflict"):
        resolve(store, project, candidate, existing, "dismiss")
    with pytest.raises(ValueError, match="not a pending conflict"):
        resolve(store, project, record(store, candidate["id"]), existing, "dismiss")


def test_only_facts_and_decisions_in_scope_are_listed(workspace):
    store, project, root = workspace
    pair(store, project, kind="constraint", key="limits", old="No limit.", new="A limit of ten.")
    listed_candidate, listed_existing = pair(store, project, kind="fact", key="port", old="Port 80.", new="Port 8080.")
    elsewhere = tmp_other = root.parent / "other-root"
    tmp_other.mkdir()
    store.add_project_root(project["id"], elsewhere)
    pair(store, project, key="scoped", old="Old.", new="New.", worktree=str(elsewhere))
    # The marked finding is project-wide but its counterpart belongs to the other worktree.
    bound = store.snapshot(project["id"], "manual://bound", "Bound to the other worktree.", "manual")
    counterpart = store.remember(project["id"], {"kind": "fact", "key": "bound", "content": "Bound to the other worktree.",
                                                 "metadata": {"worktree": str(elsewhere)}}, [bound["id"]])
    store.remember(project["id"], {"kind": "fact", "key": "bound~x", "content": "Project-wide claim.",
                                   "metadata": {"possible_conflicts": [counterpart["id"]]}}, [bound["id"]])
    inspector = Inspector(store)
    here = conflicts.inbox(inspector, project["id"], root)
    assert [item["id"] for item in here["items"]] == [f"{listed_candidate['id']}:{listed_existing['id']}"]
    assert len(conflicts.inbox(inspector, project["id"], elsewhere)["items"]) == 3
    assert len(conflicts.inbox(inspector, project["id"])["items"]) == 3
    with pytest.raises(InspectorNotFound):
        conflicts.resolve(inspector, project["id"], listed_candidate["id"], "missing", "dismiss", 1, 1)


def test_http_routes_list_and_resolve_with_validation_and_authentication(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + store.settings.token}
    assert client.get("/inspector/conflicts", params={"project": project["id"]}).status_code == 401
    listed = client.get("/inspector/conflicts", params={"project": project["id"]}, headers=headers)
    assert listed.status_code == 200 and listed.json()["total"] == 1
    body = {"project": project["id"], "record_id": candidate["id"], "other_id": existing["id"], "action": "supersede",
            "expected_version": 1, "other_expected_version": 1, "keep": candidate["id"], "reason": "Decided"}
    before = normalized_export(store)
    assert client.post("/inspector/conflicts/resolve", json=body).status_code == 401
    assert client.post("/inspector/conflicts/resolve", json={**body, "session_id": "forged"}, headers=headers).status_code == 422
    assert client.post("/inspector/conflicts/resolve", json={**body, "action": "merge"}, headers=headers).status_code == 422
    assert client.post("/inspector/conflicts/resolve", json={**body, "expected_version": 7},
                       headers=headers).status_code == 409
    assert client.post("/inspector/conflicts/resolve", json={**body, "other_id": "missing"},
                       headers=headers).status_code == 404
    assert normalized_export(store) == before
    saved = client.post("/inspector/conflicts/resolve", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["record"]["status"] == "confirmed" and saved.json()["superseded"]["status"] == "superseded"
    assert client.get("/inspector/conflicts", params={"project": project["id"]}, headers=headers).json()["items"] == []


def test_a_review_note_does_not_move_the_time_of_a_finding_s_evidence(workspace):
    store, project, _ = workspace
    _, existing = pair(store, project)
    reviewed = Inspector(store).review(project["id"], existing["id"], "dispute", 1, reason="Needs a second look")["record"]
    assert reviewed["metadata"]["evidence_basis"] == "stated"
    assert reviewed["metadata"]["evidence_latest"].startswith("2026-03-01")


def test_a_pair_follows_its_counterpart_to_whatever_replaced_it(workspace):
    store, project, _ = workspace
    first, existing = pair(store, project)
    second_source = store.snapshot(project["id"], "transcript://session-ttl/third", "Sessions expire after two hours.",
                                   "transcript", {"source_timestamp": "2026-07-01T10:00:00+00:00"})
    second = store.remember(project["id"], {"kind": "decision", "key": "session-ttl~def",
                                            "content": "Sessions expire after two hours.",
                                            "metadata": {"possible_conflicts": [existing["id"]], "suggested_key": "session-ttl"}},
                            [second_source["id"]])
    assert pending(store, project)["total"] == 2
    resolve(store, project, first, existing, "supersede", keep=first["id"])
    # The second finding was marked against a record that is now replaced: the question
    # stays open, against the finding that replaced it.
    listed = pending(store, project)["items"]
    assert [item["id"] for item in listed] == [f"{second['id']}:{first['id']}"]
    winner = record(store, first["id"])
    result = conflicts.resolve_conflict(store, project["id"], second["id"], first["id"], "dismiss",
                                        second["version"], winner["version"])
    saved = result["record"]["metadata"]
    assert saved["possible_conflicts"] == [] and set(saved["conflicts_resolved"]) == {existing["id"], first["id"]}
    assert json.loads(store.get_source(result["audit_source_id"])["content"])["marked_as"] == [existing["id"]]
    assert pending(store, project)["items"] == []
    # A replacement made by an ordinary write is followed as well.
    third, other = pair(store, project, kind="fact", key="region", old="EU only.", new="EU and US.")
    note = store.snapshot(project["id"], "manual://replacement", "EU, US and Asia.", "manual")
    replacement = store.remember(project["id"], {"kind": "fact", "key": "region-new", "content": "EU, US and Asia.",
                                                 "supersedes": other["id"]}, [note["id"]])
    assert [item["id"] for item in pending(store, project)["items"]] == [f"{third['id']}:{replacement['id']}"]


def test_a_winner_that_already_replaced_something_keeps_that_link(workspace):
    store, project, _ = workspace
    first, original = pair(store, project)
    resolve(store, project, first, original, "supersede", keep=first["id"])
    winner = record(store, first["id"])
    assert winner["supersedes"] == original["id"]
    later_source = store.snapshot(project["id"], "transcript://session-ttl/later", "Sessions never expire.", "transcript",
                                  {"source_timestamp": "2026-08-01T10:00:00+00:00"})
    later = store.remember(project["id"], {"kind": "decision", "key": "session-ttl~later", "content": "Sessions never expire.",
                                           "metadata": {"possible_conflicts": [winner["id"]], "suggested_key": "session-ttl"}},
                           [later_source["id"]])
    result = conflicts.resolve_conflict(store, project["id"], later["id"], winner["id"], "supersede",
                                        later["version"], winner["version"], keep=winner["id"])
    kept, retired = record(store, winner["id"]), record(store, later["id"])
    assert kept["supersedes"] == original["id"] and kept["metadata"]["superseded_ids"] == [later["id"]]
    assert (retired["status"], retired["metadata"]["superseded_by"]) == ("superseded", winner["id"])
    assert result["superseded"]["id"] == later["id"] and kept["status"] == "confirmed"
    assert retired["source_ids"][-1] == kept["source_ids"][-1] == result["audit_source_id"]
    assert record(store, original["id"])["status"] == "superseded"
    assert pending(store, project)["items"] == []


def test_a_fact_and_a_decision_can_be_kept_or_dismissed_but_not_replace_each_other(workspace):
    store, project, _ = workspace
    old = store.snapshot(project["id"], "manual://old", "We use bearer tokens.", "manual")
    existing = store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "We use bearer tokens."},
                              [old["id"]])
    new = store.snapshot(project["id"], "manual://new", "The API accepts cookies.", "manual")
    candidate = store.remember(project["id"], {"kind": "fact", "key": "auth~x", "content": "The API accepts cookies.",
                                               "metadata": {"possible_conflicts": [existing["id"]]}}, [new["id"]])
    item = pending(store, project)["items"][0]
    assert item["can_supersede"] is False
    before = normalized_export(store)
    for keep in (candidate["id"], existing["id"]):
        with pytest.raises(ValueError, match="cannot replace each other"):
            resolve(store, project, candidate, existing, "supersede", keep=keep)
    assert normalized_export(store) == before
    resolve(store, project, candidate, existing, "different_scopes", reason="A decision for clients, a fact about the API")
    kept = record(store, candidate["id"])["metadata"]
    assert kept["conflict_scope_note"] == "A decision for clients, a fact about the API"
    assert pending(store, project)["items"] == []


def test_no_other_writer_can_unmark_a_conflict_or_record_a_decision(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    forged = store.remember(project["id"], {"id": candidate["id"], "expected_version": candidate["version"], "metadata": {
        "possible_conflicts": [], "conflicts_resolved": [existing["id"]], "conflict_decisions": [f"{existing['id']}:dismiss"],
        "conflict_scope_note": "forged", "superseded_ids": ["x"], "superseded_by": "y", "note": "kept"}},
        candidate["source_ids"])
    assert forged["metadata"]["possible_conflicts"] == [existing["id"]] and forged["metadata"]["note"] == "kept"
    assert not {"conflicts_resolved", "conflict_decisions", "conflict_scope_note", "superseded_ids",
                "superseded_by"} & set(forged["metadata"])
    assert pending(store, project)["total"] == 1
    # Marking a new finding at creation stays possible: that is what extraction does.
    note = store.snapshot(project["id"], "manual://marked", "Another claim.", "manual")
    created = store.remember(project["id"], {"kind": "decision", "key": "session-ttl~new", "content": "Another claim.",
                                             "metadata": {"possible_conflicts": [existing["id"]],
                                                          "conflicts_resolved": [existing["id"]]}}, [note["id"]])
    assert created["metadata"]["possible_conflicts"] == [existing["id"]] and "conflicts_resolved" not in created["metadata"]


def test_evidence_times_are_read_from_the_sources_and_audit_notes_are_not_listed_as_sources(workspace):
    store, project, _ = workspace
    candidate, existing = pair(store, project)
    reviewed = Inspector(store).review(project["id"], existing["id"], "confirm", 1) if existing["status"] != "confirmed" \
        else Inspector(store).review(project["id"], existing["id"], "dispute", 1, reason="Check again")
    # A value stored by an older version: the time of the review instead of the evidence.
    stale = dict(reviewed["record"]["metadata"], evidence_latest="2026-11-10T00:00:00+00:00", evidence_basis="mixed")
    store.command("UPDATE records SET metadata=:metadata WHERE id=:id", {"metadata": stale, "id": existing["id"]})
    item = pending(store, project)["items"][0]
    assert item["existing"]["evidence_latest"].startswith("2026-03-01") and item["existing"]["evidence_basis"] == "stated"
    assert item["existing"]["sources"] == ["transcript://session-ttl/old"] and item["existing"]["sources_total"] == 1
    assert (item["newer_evidence"], item["newer_evidence_basis"]) == (candidate["id"], "stated")


def test_the_order_is_reliable_exactly_when_the_later_time_was_stated(workspace):
    store, project, _ = workspace
    later_stated, _ = pair(store, project, key="a", old_time=None, new_time="2099-01-01T00:00:00+00:00")
    _, later_observed = pair(store, project, key="b", old_time=None, new_time="2020-01-01T00:00:00+00:00")
    found = {item["candidate"]["key"]: item for item in pending(store, project)["items"]}
    assert (found["a~abc"]["newer_evidence"], found["a~abc"]["newer_evidence_basis"]) == (later_stated["id"], "stated")
    assert (found["b~abc"]["newer_evidence"], found["b~abc"]["newer_evidence_basis"]) == (later_observed["id"], "observed")


def test_a_long_finding_is_shown_whole_up_to_the_extraction_limit(workspace):
    store, project, _ = workspace
    long_text = "x" * 3990 + " differs at the end"
    pair(store, project, new=long_text[:4000])
    item = pending(store, project)["items"][0]
    assert item["candidate"]["content"] == long_text[:4000] and item["candidate"]["content_truncated"] is False
