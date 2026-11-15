"""A conflict decision travels with published memory (#239): the finding a person kept, and the one they rejected."""
from __future__ import annotations

from cairnmind import conflicts
from cairnmind.memory_published import Publisher
from cairnmind.memory_published_check import check_directory
from cairnmind.memory_published_import import PublishedImporter
from cairnmind.memory_published_parse import parse_file

from test_published_memory import ada, commit, folder_of, git, reviewed, teammate  # noqa: F401 (ada is a fixture)

EXTERNAL = {"origin_client": "claude", "origin_session_id": "s", "origin_history": "unarchived",
            "origin_evidence": "degraded"}


def proposed_against(store, pid, existing, text, origin=None):
    """A session stores a finding with the same key beside a confirmed one and marks the pair."""
    def write():
        source = store.snapshot(pid, f"transcript://{len(text)}", text, "transcript")
        return store.remember(pid, {"kind": "decision", "key": "ttl~a", "content": text,
                                    "metadata": {"possible_conflicts": [existing["id"]], "suggested_key": "ttl"}},
                              [source["id"]])
    if origin is None:
        return write()
    with store.write_origin(origin):
        return write()


def head(folder, kind, record_id):
    model = parse_file((folder / f"{kind}/{record_id}.md").read_bytes())
    return model["revisions"][-1]


def test_keeping_the_replacement_publishes_it_and_the_rejected_decision_as_superseded(ada):  # noqa: F811
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    Publisher(store).publish(pid)
    commit(root, "Publish the first decision")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    new = proposed_against(store, pid, old, "Sessions last an hour.")
    conflicts.resolve_conflict(store, pid, new["id"], old["id"], "supersede", new["version"], old["version"],
                               keep=new["id"])
    result = Publisher(store).publish(pid)
    assert result["held"]["total"] == 0, result["held"]
    folder = folder_of(root)
    assert head(folder, "decision", old["id"])["status"] == "superseded"
    kept = head(folder, "decision", new["id"])
    assert (kept["status"], kept["supersedes"], kept["key"]) == ("confirmed", old["id"], "ttl")
    assert check_directory(folder)[1] == []
    commit(root, "Keep the shorter sessions")
    bo_root = bo["root"]
    git(bo_root, "pull", "-q", "--ff-only")
    PublishedImporter(bo["store"]).import_published(folder_of(bo_root), bo["project"])
    rows = {row["id"]: row for row in bo["store"].list_records(bo["project"], include_history=False)}
    history = {row["id"]: row for row in bo["store"].list_records(bo["project"], include_history=True)}
    assert rows[new["id"]]["status"] == "confirmed"
    assert rows[new["id"]]["metadata"]["confirmation"]["by"] == "import"  # Confirmed elsewhere, not here.
    assert history[old["id"]]["status"] == "superseded"
    assert old["id"] not in rows or rows[old["id"]]["status"] == "superseded"


def test_keeping_a_replacement_an_external_session_wrote_still_withdraws_the_rejected_decision(ada):  # noqa: F811
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    Publisher(store).publish(pid)
    new = proposed_against(store, pid, old, "Sessions last an hour.", EXTERNAL)
    conflicts.resolve_conflict(store, pid, new["id"], old["id"], "supersede", new["version"], old["version"],
                               keep=new["id"])
    result = Publisher(store).publish(pid)
    reasons = {item["id"]: item["reason"] for item in result["held"]["items"]}
    # Degraded records are never published (docs/MEMORY_CONTRACTS.md, "Trust"), even once a person kept one ...
    assert set(reasons) == {new["id"]} and reasons[new["id"]].endswith("(degraded origin)")
    # ... but the decision the person rejected is no longer published as confirmed: their act covers it.
    folder = folder_of(root)
    rejected = head(folder, "decision", old["id"])
    assert rejected["status"] == "superseded" and rejected["covered"]["by"] == "review"
    assert check_directory(folder)[1] == []
    commit(root, "Withdraw the day-long sessions")
    cy = teammate(ada, "cy", "Cy Park")
    PublishedImporter(cy["store"]).import_published(folder_of(cy["root"]), cy["project"])
    history = {row["id"]: row for row in cy["store"].list_records(cy["project"], include_history=True)}
    assert history[old["id"]]["status"] == "superseded"


def test_keeping_the_confirmed_decision_withdraws_the_proposal_it_was_marked_against(ada):  # noqa: F811
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    new = proposed_against(store, pid, old, "Sessions last an hour.")
    conflicts.resolve_conflict(store, pid, new["id"], old["id"], "supersede", new["version"], old["version"],
                               keep=old["id"])
    result = Publisher(store).publish(pid)
    assert result["held"]["total"] == 0, result["held"]
    folder = folder_of(root)
    kept = head(folder, "decision", old["id"])
    assert (kept["status"], kept["supersedes"]) == ("confirmed", new["id"])
    # The rejected proposal travels as superseded by the person's decision, never as anything else.
    rejected = parse_file((folder / f"decision/{new['id']}.md").read_bytes())["revisions"]
    assert [(r["status"], r["covered"]["by"]) for r in rejected] == [("superseded", "review")]
    assert check_directory(folder)[1] == []


def test_keeping_the_confirmed_decision_against_an_external_proposal_never_publishes_the_proposal(ada):  # noqa: F811
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    Publisher(store).publish(pid)
    new = proposed_against(store, pid, old, "Sessions last an hour.", EXTERNAL)
    conflicts.resolve_conflict(store, pid, new["id"], old["id"], "supersede", new["version"], old["version"],
                               keep=old["id"])
    Publisher(store).publish(pid)
    folder = folder_of(root)
    assert not (folder / f"decision/{new['id']}.md").exists()
    assert head(folder, "decision", old["id"])["status"] == "confirmed"
    assert check_directory(folder)[1] == []
