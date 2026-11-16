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


def test_publishing_writes_again_a_published_file_that_went_missing_and_refuses_a_conflict_with_a_way_out(ada):  # noqa: F811
    from cairnmind.inspector import Inspector
    from cairnmind.memory import MemoryConflict
    import pytest

    from conftest import person_edit
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    # A replacement the person writes in their terminal and then reviews: the replaced record is superseded
    # mechanically, which a reader accepts only beside its replacement.
    source = store.snapshot(pid, "note://replacement", "Sessions last an hour.", "note")
    new = person_edit(store, pid, {"kind": "decision", "key": "ttl-2", "content": "Sessions last an hour.",
                                   "supersedes": old["id"]}, [source["id"]])
    Inspector(store).review(pid, new["id"], "confirm", new["version"])
    Publisher(store).publish(pid)
    assert head(folder_of(root), "decision", old["id"])["covered"]["by"] == "superseded"
    commit(root, "Replace the day-long sessions")
    path = folder_of(root) / f"decision/{new['id']}.md"
    original = path.read_bytes()
    path.unlink()  # Only the replacement's file: the replaced one now names nothing in the tree.
    result = Publisher(store).publish(pid)
    assert result["written"] == [f"decision/{new['id']}.md"] and path.read_bytes() == original
    assert check_directory(folder_of(root))[1] == []
    path.write_bytes(b"<<<<<<< ours\n" + original + b"=======\n>>>>>>> theirs\n")
    with pytest.raises(MemoryConflict, match=r"resolve the conflict, or restore the file with `git checkout -- <path>`"):
        Publisher(store).publish(pid)


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
    assert set(reasons) == {new["id"]} and "(degraded origin)" in reasons[new["id"]]
    # ... and the preview says why the kept one stays, and that the rejected one goes out without it.
    assert "a person kept it in a conflict decision" in reasons[new["id"]]
    assert "in your own words" in reasons[new["id"]]
    assert result["superseded_without_replacement"]["records"] == [
        {"id": old["id"], "kind": "decision", "replaced_by": new["id"]}]
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


def test_a_record_that_names_an_external_proposal_is_held_with_that_reason(ada):  # noqa: F811
    from cairnmind.inspector import Inspector

    from conftest import person_edit
    store, pid = ada["store"], ada["project"]
    with store.write_origin(EXTERNAL):
        note = store.snapshot(pid, "transcript://proposal", "Sessions last a week.", "transcript")
        proposal = store.remember(pid, {"kind": "decision", "key": "ttl", "content": "Sessions last a week."},
                                  [note["id"]])
    source = store.snapshot(pid, "note://replacement", "Sessions last an hour.", "note")
    mine = person_edit(store, pid, {"kind": "decision", "key": "ttl-2", "content": "Sessions last an hour.",
                                    "supersedes": proposal["id"]}, [source["id"]])
    Inspector(store).review(pid, mine["id"], "confirm", mine["version"])
    result = Publisher(store).publish(pid, preview=True)
    reasons = {item["id"]: item["reason"] for item in result["held"]["items"]}
    assert reasons[mine["id"]] == (f"it names record {proposal['id']}, which an external or third-party session "
                                   "wrote and which is never published")


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
    result = Publisher(store).publish(pid)
    # The person's decision is published; only the external proposal is held, for its origin (#239).
    reasons = {item["id"]: item["reason"] for item in result["held"]["items"]}
    assert set(reasons) == {new["id"]} and reasons[new["id"]].endswith("(degraded origin)"), reasons
    assert "superseded_without_replacement" not in result
    folder = folder_of(root)
    assert not (folder / f"decision/{new['id']}.md").exists()
    kept = head(folder, "decision", old["id"])
    assert (kept["status"], kept["supersedes"]) == ("confirmed", None)
    assert check_directory(folder)[1] == []
    stored = {row["id"]: row for row in store.list_records(pid, include_history=True)}
    assert stored[new["id"]]["status"] == "superseded"
    assert stored[new["id"]]["metadata"]["superseded_by"] == old["id"]
    assert stored[old["id"]]["metadata"]["superseded_ids"] == [new["id"]]
    commit(root, "Keep the day-long sessions")
    cy = teammate(ada, "cy", "Cy Park")
    PublishedImporter(cy["store"]).import_published(folder_of(cy["root"]), cy["project"])
    rows = {row["id"]: row for row in cy["store"].list_records(cy["project"], include_history=False)}
    assert rows[old["id"]]["status"] == "confirmed" and new["id"] not in rows


def test_keeping_the_confirmed_decision_against_a_proposal_citing_an_external_source_publishes_it(ada):  # noqa: F811
    # A proposal of native origin that cites a source an external session wrote is never published either,
    # so the decision a person kept must not name it (#239).
    store, pid, root = ada["store"], ada["project"], ada["root"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    with store.write_origin(EXTERNAL):
        source = store.snapshot(pid, "transcript://external", "Sessions last an hour.", "transcript")
    new = store.remember(pid, {"kind": "decision", "key": "ttl~a", "content": "Sessions last an hour.",
                               "metadata": {"possible_conflicts": [old["id"]], "suggested_key": "ttl"}},
                         [source["id"]])
    conflicts.resolve_conflict(store, pid, new["id"], old["id"], "supersede", new["version"], old["version"],
                               keep=old["id"])
    result = Publisher(store).publish(pid)
    reasons = {item["id"]: item["reason"] for item in result["held"]["items"]}
    assert set(reasons) == {new["id"]} and reasons[new["id"]] == "it cites a source of degraded origin", reasons
    kept = head(folder_of(root), "decision", old["id"])
    assert (kept["status"], kept["supersedes"]) == ("confirmed", None)
    assert check_directory(folder_of(root))[1] == []
    stored = {row["id"]: row for row in store.list_records(pid, include_history=True)}
    assert stored[new["id"]]["metadata"]["superseded_by"] == old["id"]
    assert stored[old["id"]]["metadata"]["superseded_ids"] == [new["id"]]


def test_keeping_the_confirmed_decision_against_a_native_proposal_still_links_it(ada):  # noqa: F811
    store, pid = ada["store"], ada["project"]
    old = reviewed(store, pid, "decision", "Sessions last a day.", "ttl")
    new = proposed_against(store, pid, old, "Sessions last an hour.")
    conflicts.resolve_conflict(store, pid, new["id"], old["id"], "supersede", new["version"], old["version"],
                               keep=old["id"])
    stored = {row["id"]: row for row in store.list_records(pid, include_history=True)}
    assert stored[old["id"]]["supersedes"] == new["id"]
    assert "superseded_ids" not in (stored[old["id"]].get("metadata") or {})