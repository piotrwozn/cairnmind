"""A person replaces one record with a new text (#222): one act, the version check, what travels.

Real HTTP service, ArcadeDB and Git; no model is asked.
"""
from __future__ import annotations

import json

import pytest

from cairnmind import conflicts, person_proof, record_replace
from cairnmind.confirmation import ConfirmationRefused, confirmation_of
from cairnmind.inspector import Inspector
from cairnmind.memory_published import Publisher
from cairnmind.memory_published_check import check_directory
from cairnmind.memory_published_format import identity_hash, reviewed_by
from cairnmind.memory_published_import import PublishedImporter
from cairnmind.memory_published_parse import parse_file
from cairnmind.memory import MemoryManager
from cairnmind.store import Store

from conftest import confirmed_before_rule
from test_confirmed_by_person import fixture  # noqa: F401 (the fixture)
from test_published_conflict_decision import EXTERNAL, head
from test_published_memory import ada, commit, folder_of, reviewed, teammate  # noqa: F401 (ada is a fixture)

NEW = "Use WAL mode with a short checkpoint."


def state(fixture):  # noqa: F811
    """What a refused replacement must leave exactly as it was."""
    query = fixture.store.query
    return {"records": sorted((row["id"], row["status"], row["version"]) for row in query(
                "SELECT id,status,version FROM records WHERE project_id=:p", {"p": fixture.pid})),
            "sources": query("SELECT count(*) AS n FROM sources")[0]["n"],
            "revisions": query("SELECT count(*) AS n FROM record_revisions")[0]["n"]}


def replace(fixture, record, content=NEW, headers=True, **extra):  # noqa: F811
    body = {"project": fixture.pid, "content": content, "expected_version": record["version"], **extra}
    return fixture.client.post(f"/inspector/records/{record['id']}/replace", json=body,
                               headers=fixture.person if headers else {})


def test_a_person_replaces_a_proposed_decision_in_one_act(fixture):  # noqa: F811
    old = fixture.proposed("Use rollback journal mode")
    response = replace(fixture, old, reason="  The journal is slower  ")
    assert response.status_code == 200, response.text
    saved = response.json()
    new, replaced = saved["record"], saved["replaced"]
    assert new["id"] != old["id"] and new["status"] == "confirmed" and new["version"] == 1
    assert (new["kind"], new["key"], new["content"], new["supersedes"]) == (old["kind"], old["key"], NEW, old["id"])
    mark = confirmation_of(new)
    assert mark["by"] == "person" and mark["route"] == "inspector_review"
    note, audit = [fixture.store.query("SELECT FROM sources WHERE id=:id", {"id": i})[0] for i in new["source_ids"]]
    assert new["source_ids"] == [saved["note_source_id"], saved["audit_source_id"]]
    assert note["content"] == NEW and note["uri"] == f"workspace://finding/{new['id']}"
    assert note["metadata"]["origin"] == "workspace_form" and note["metadata"]["actor_name"] == "Ada Nowak"
    said = json.loads(audit["content"])
    assert audit["metadata"]["origin"] == "local_user_review" and audit["uri"].startswith("review://")
    assert (said["action"], said["record_id"], said["replaces"], said["replaced_version"]) == (
        "confirm", new["id"], old["id"], 1)
    assert said["reason"] == "The journal is slower" and said["replaced_status"] == "proposed"
    assert said["identity_sha256"] == identity_hash("decision", old["key"], old["id"])
    assert said["evidence_sha256"] is None and said["content_sha256"] != said["replaced_content_sha256"]
    # The old record is superseded and says nothing else: same text, same sources, one more revision.
    assert (replaced["status"], replaced["version"], replaced["content"]) == ("superseded", 2, old["content"])
    assert replaced["source_ids"] == old["source_ids"]
    inspector = Inspector(fixture.store)
    revisions = inspector.revisions(fixture.pid, old["id"])["items"]
    assert [row["version"] for row in revisions] == [1, 2]
    assert revisions[0]["snapshot"]["status"] == "proposed" and revisions[0]["snapshot"]["content"] == old["content"]
    assert inspector.record(fixture.pid, old["id"])["replaced_by"] == new["id"]
    assert "replaced_by" not in inspector.record(fixture.pid, new["id"])
    assert inspector.record(fixture.pid, new["id"])["evidence"]["evidence_state"] == "anchored"


def test_a_record_that_changed_since_it_was_shown_is_refused_and_nothing_is_written(fixture):  # noqa: F811
    old = fixture.proposed("Use rollback journal mode")
    shown = dict(old)
    Inspector(fixture.store).review(fixture.pid, old["id"], "dispute", 1, reason="Wrong")  # version 2
    before = state(fixture)
    response = replace(fixture, shown)
    assert response.status_code == 409 and response.json()["detail"] == record_replace.CHANGED, response.text
    assert state(fixture) == before


@pytest.mark.parametrize("how", ["person", "disputed", "before_rule", "extracted"])
def test_what_a_person_can_replace(fixture, how):  # noqa: F811
    store, pid = fixture.store, fixture.pid
    if how == "extracted":
        stamp = {"version": 1, "prompt_sha256": "0123456789abcdef", "model": "tiny@abc"}
        old = store.remember(pid, {"kind": "fact", "content": "The cache is warm."}, [fixture.note["id"]],
                             extracted_from=fixture.note["id"], extractor=stamp)
        assert old["metadata"]["extraction_source_id"] == fixture.note["id"]
    else:
        old = fixture.proposed("Use rollback journal mode")
        if how == "person":
            old = Inspector(store).review(pid, old["id"], "confirm", 1)["record"]
        elif how == "disputed":
            old = Inspector(store).review(pid, old["id"], "dispute", 1, reason="Wrong")["record"]
        else:
            confirmed_before_rule(store, old["id"])
            old = fixture.record(old["id"])
    response = replace(fixture, old)
    assert response.status_code == 200, (how, response.text)
    new = response.json()["record"]
    assert new["status"] == "confirmed" and confirmation_of(new)["route"] == "inspector_review"
    assert "extraction_source_id" not in new["metadata"] and "extractor" not in new["metadata"]
    assert "lapsed" not in json.dumps(new["metadata"].get("confirmation"))
    assert fixture.record(old["id"])["status"] == "superseded"


def test_what_cannot_be_replaced_says_why_and_writes_nothing(fixture):  # noqa: F811
    store, pid = fixture.store, fixture.pid
    old = fixture.proposed("Use rollback journal mode")
    gone = fixture.proposed("Use a bigger cache")
    assert replace(fixture, gone).status_code == 200
    task = store.remember(pid, {"kind": "task", "key": "loose", "content": "Do a thing"}, [fixture.note["id"]])
    checkpoint = store.checkpoint(pid, {"goal": "x"})
    refusals = [(gone, 400, record_replace.HISTORICAL), (fixture.record(gone["id"]), 400, record_replace.HISTORICAL),
                (task, 400, record_replace.NOT_REPLACEABLE), (checkpoint, 400, record_replace.NOT_REPLACEABLE)]
    for record, code, detail in refusals:
        before = state(fixture)
        response = replace(fixture, record)
        assert (response.status_code, response.json()["detail"]) == (code, detail), response.text
        assert state(fixture) == before
    for content, code in (("  " + old["content"] + "  ", 400), ("", 422), ("x" * 16001, 422)):
        before = state(fixture)
        response = replace(fixture, old, content)
        assert response.status_code == code, (content[:20], response.text)
        if code == 400:
            assert response.json()["detail"] == record_replace.SAME_TEXT
        assert state(fixture) == before
    before = state(fixture)
    missing = fixture.client.post("/inspector/records/nope/replace", headers=fixture.person, json={
        "project": fixture.pid, "content": NEW, "expected_version": 1})
    assert missing.status_code == 404 and state(fixture) == before
    for extra in ({"session_id": "s"}, {"status": "confirmed"}):
        assert replace(fixture, old, **extra).status_code == 422  # no session field, no status field
    assert state(fixture) == before


def test_after_a_replacement_preview_snapshot_publishing_and_backup_restore_work(ada, tmp_path):  # noqa: F811
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    Publisher(store).publish(pid)
    commit(root, "Publish the first decision")
    result = record_replace.replace_record(store, pid, old["id"], "Sessions last an hour.", old["version"])
    new = result["record"]
    assert Publisher(store).publish(pid, preview=True)["held"]["total"] == 0
    published = Publisher(store).publish(pid)
    assert published["held"]["total"] == 0, published["held"]
    folder = folder_of(root)
    last = head(folder, "decision", old["id"])
    assert (last["status"], last["covered"]["by"]) == ("superseded", "superseded")
    first = parse_file((folder / f"decision/{new['id']}.md").read_bytes())
    top = first["revisions"][-1]
    assert (top["status"], top["supersedes"], top["covered"]["by"]) == ("confirmed", old["id"], "review")
    assert reviewed_by(first, first["revisions"])[0]["action"] == "confirm"
    assert check_directory(folder)[1] == []
    commit(root, "Replace the day-long sessions")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    rows = {row["id"]: row for row in bo["store"].list_records(bo["project"], include_history=True)}
    assert rows[new["id"]]["status"] == "confirmed" and rows[old["id"]]["status"] == "superseded"
    manager = MemoryManager(store)
    assert manager.preview(pid)["status"] == "preview"
    assert manager.snapshot(pid)["status"] == "created"
    restored = Store(tmp_path / "restored")
    restored.restore(store.backup(tmp_path / "after-replace.cairnmind.json.gz"))
    mine = {row["id"]: row for row in store.list_records(pid, include_history=True)}
    there = {row["id"]: row for row in restored.list_records(pid, include_history=True)}
    assert {i: row["content"] for i, row in there.items()} == {i: row["content"] for i, row in mine.items()}
    assert there[new["id"]]["status"] == "confirmed" and there[old["id"]]["status"] == "superseded"
    history = restored.record_history(old["id"])  # proposed, reviewed, superseded: every version is still there
    assert [(row["version"], row["status"]) for row in history] == [(1, "proposed"), (2, "confirmed"), (3, "superseded")]
    assert history == store.record_history(old["id"])
    claims = confirmed_claims(restored, pid, new["id"])
    assert [claim["action"] for claim in claims] == ["confirm"]  # one act, one claim (not also its note)


def test_a_fresh_import_takes_a_record_and_its_replacement_with_one_key_in_any_file_order(ada, monkeypatch):  # noqa: F811
    from itertools import count
    from uuid import UUID
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    numbers = count(1)  # the replacement's file sorts before the replaced record's file
    monkeypatch.setattr(record_replace, "uuid4", lambda: UUID(f"00000000-0000-4000-8000-{next(numbers):012d}"))
    new = record_replace.replace_record(store, pid, old["id"], "Sessions last an hour.", old["version"])["record"]
    assert new["id"] < old["id"]
    Publisher(store).publish(pid)
    commit(root, "Publish the pair")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    rows = {row["id"]: row for row in bo["store"].list_records(bo["project"], include_history=True)}
    assert (rows[new["id"]]["status"], rows[old["id"]]["status"]) == ("confirmed", "superseded")


def confirmed_claims(store, pid, record_id):
    from cairnmind.confirmation import archive_claims
    record = next(row for row in store.list_records(pid) if row["id"] == record_id)
    sources = {row["id"]: row for row in store.query("SELECT FROM sources WHERE project_id=:p", {"p": pid})}
    return archive_claims(record, sources)


def test_replacing_an_external_sessions_proposal_publishes_the_persons_text(ada):  # noqa: F811
    store, pid, root = ada["store"], ada["project"], ada["root"]
    with store.write_origin(EXTERNAL):
        source = store.snapshot(pid, "transcript://proposal", "Sessions last a week.", "transcript")
        proposal = store.remember(pid, {"kind": "decision", "key": "ttl", "content": "Sessions last a week."},
                                  [source["id"]])
    result = record_replace.replace_record(store, pid, proposal["id"], "Sessions last an hour.", proposal["version"])
    new = result["record"]
    stored = {row["id"]: row for row in store.list_records(pid, include_history=True)}
    assert stored[proposal["id"]]["status"] == "superseded"
    assert stored[proposal["id"]]["metadata"]["superseded_by"] == new["id"]
    assert new["metadata"]["superseded_ids"] == [proposal["id"]] and new["supersedes"] is None
    audit = json.loads(store.query("SELECT content FROM sources WHERE id=:id", {"id": result["audit_source_id"]})[0]["content"])
    assert audit["identity_sha256"] == identity_hash("decision", "ttl", None)
    published = Publisher(store).publish(pid)
    reasons = {item["id"]: item["reason"] for item in published["held"]["items"]}
    assert set(reasons) == {proposal["id"]} and reasons[proposal["id"]].endswith("(degraded origin)"), reasons
    folder = folder_of(root)
    assert (folder / f"decision/{new['id']}.md").exists() and not (folder / f"decision/{proposal['id']}.md").exists()
    assert head(folder, "decision", new["id"])["supersedes"] is None
    assert check_directory(folder)[1] == []
    assert Inspector(store).record(pid, proposal["id"])["replaced_by"] == new["id"]


def test_a_replaced_decision_makes_an_acknowledged_packet_stale(fixture):  # noqa: F811
    old = fixture.proposed("Use rollback journal mode")
    bound = fixture.session("codex")
    fixture.manager.require_ack(bound["session_id"])
    assert replace(fixture, old).status_code == 200
    with pytest.raises(ValueError, match="changed since the acknowledged packet"):
        fixture.manager.require_ack(bound["session_id"])


def test_conflict_marks_follow_the_replacement(fixture):  # noqa: F811
    store, pid = fixture.store, fixture.pid
    other = fixture.proposed("Sessions last a day.", key="ttl")
    marked = store.remember(pid, {"kind": "decision", "key": "ttl~a", "content": "Sessions last an hour.",
                                  "metadata": {"possible_conflicts": [other["id"]], "suggested_key": "ttl"}},
                            [fixture.note["id"]])
    marker = store.remember(pid, {"kind": "decision", "key": "ttl~b", "content": "Sessions last a week.",
                                  "metadata": {"possible_conflicts": [marked["id"]]}}, [fixture.note["id"]])
    response = replace(fixture, marked, "Sessions last two hours.")
    assert response.status_code == 200, response.text
    new = response.json()["record"]
    assert new["metadata"]["possible_conflicts"] == [other["id"]]
    pairs = {item["id"] for item in conflicts.inbox(Inspector(store), pid)["items"]}
    assert {f"{new['id']}:{other['id']}", f"{marker['id']}:{new['id']}"} <= pairs


@pytest.mark.unproven  # a request without the workspace's key: the function itself refuses
def test_the_function_refuses_without_the_proof(fixture):  # noqa: F811
    old = fixture.proposed("Use rollback journal mode")
    before = state(fixture)
    with pytest.raises(ConfirmationRefused) as refused:
        record_replace.replace_record(fixture.store, fixture.pid, old["id"], NEW, old["version"])
    assert str(refused.value) == person_proof.PROOF_MISSING
    assert state(fixture) == before
