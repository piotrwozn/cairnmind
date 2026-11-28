"""Published team memory (#170) on real ArcadeDB, with real Git: merges, conflicts, round trips."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import as_person, person_edit, person_unproven

from cairnmind.memory.confirmation import ConfirmationRefused
from cairnmind.memory.evidence import evidence_states
from cairnmind.memory.findings import save_finding
from cairnmind.memory.memory import MemoryConflict, MemoryManager
from cairnmind.memory.memory_acts import confirm_plan
from cairnmind.memory.memory_published import PUBLICATION_SWITCH, Publisher
from cairnmind.memory.memory_published_check import check_directory
from cairnmind.memory.memory_published_format import render
from cairnmind.memory.memory_published_import import PublishedImporter
from cairnmind.memory.memory_published_parse import parse_file
from cairnmind.memory.plans import PlanManager
from cairnmind.store import Store
from cairnmind.terminal.inspector import Inspector

GUIDE = "# Guide\n\nThe active backend is ArcadeDB.\nNothing else stores memory.\n"


def git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise AssertionError(result.stderr + result.stdout)
    return result


def identify(root, name):
    git(root, "config", "user.name", name)
    git(root, "config", "user.email", "team@example.invalid")


def repository(root):
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    identify(root, "Ada Nowak")
    (root / "docs").mkdir()
    (root / "docs" / "GUIDE.md").write_text(GUIDE)
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def commit(root, message):
    git(root, "add", "-A", ".memory")
    git(root, "commit", "-qm", message)


def reviewed(store, project_id, kind, text, key, *, quote=None):
    source = store.snapshot(project_id, f"note://{key}/{len(text)}", text, "note")
    sources = [source["id"]]
    metadata = {}
    if quote:
        root = Path(store.query("SELECT root FROM projects WHERE id=:id", {"id": project_id})[0]["root"])
        guide = store.snapshot(project_id, str(root / "docs/GUIDE.md"), GUIDE, "document",
                               {"path": str(root / "docs/GUIDE.md"), "root": str(root), "relative_path": "docs/GUIDE.md",
                                "snapshot": "working_tree"})
        sources.append(guide["id"])
        metadata["evidence"] = quote
    record = store.remember(project_id, {"kind": kind, "key": key, "content": text, "metadata": metadata}, sources)
    return Inspector(store).review(project_id, record["id"], "confirm", record["version"])["record"]


def edited(store, project_id, record, text):
    """An edit and then a person's review of it, as on any machine."""
    current = next(r for r in store.list_records(project_id) if r["id"] == record["id"])
    source = store.snapshot(project_id, f"note://edit/{len(text)}", text, "note")
    # Only a person changes a record a person confirmed (#177): the edit is the person's own act.
    changed = person_edit(store, project_id, {"id": current["id"], "expected_version": current["version"],
                                              "content": text}, [*current["source_ids"], source["id"]])
    return Inspector(store).review(project_id, changed["id"], "confirm", changed["version"])["record"]


def folder_of(root):
    return root / ".memory" / "cairnmind-published"


def published_bytes(root):
    folder = folder_of(root)
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


@pytest.fixture
def ada(tmp_path, monkeypatch):
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    root = repository(tmp_path / "ada-repository")
    store = Store(tmp_path / "ada-database")
    project = store.add_project("team", root)
    return {"store": store, "project": project["id"], "root": root, "tmp": tmp_path}


def teammate(ada, name, person):
    root = ada["tmp"] / f"{name}-repository"
    git(ada["tmp"], "clone", "-q", str(ada["root"]), str(root))
    identify(root, person)
    store = Store(ada["tmp"] / f"{name}-database")
    project = store.add_project("team", root)
    return {"store": store, "project": project["id"], "root": root}


def test_only_acts_of_a_person_are_published_and_publishing_twice_changes_no_file(ada, monkeypatch):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    decision = reviewed(store, pid, "decision", "ArcadeDB is the only storage engine.", "storage",
                        quote="The active backend is ArcadeDB.")
    source = store.snapshot(pid, "note://cli", "Agents may write facts.", "note")
    # Confirmed on the command line by a person (#177): confirmed here, but not an act that travels.
    unreviewed = as_person(store, pid, {"kind": "fact", "content": "Agents may write facts."}, [source["id"]])
    with store.transaction():
        typed = save_finding(store, pid, "8d2e4f60-1a3b-4c5d-9e7f-0a1b2c3d4e5f",
                             "Cairnmind never stages, commits or pushes.", "constraint", "confirmed")["record"]
    saved = PlanManager(store).save(pid, {"title": "Publication", "goal": "Publish confirmed knowledge"},
                                    [{"key": "format", "content": "Write the format"},
                                     {"key": "import", "content": "Read it back", "depends_on": ["format"]}],
                                    "publication-plan", worktree=str(root))
    confirmed = confirm_plan(store, pid, saved["plan"]["id"], saved["plan"]["version"])
    publisher = Publisher(store)
    preview = publisher.publish(pid, preview=True)
    assert preview["status"] == "preview" and not folder_of(root).exists()
    first = publisher.publish(pid)
    files = published_bytes(root)
    steps = [task["id"] for task in saved["tasks"]]
    assert set(files) == {"FORMAT.json", ".gitattributes", ".gitignore", f"decision/{decision['id']}.md",
                          f"constraint/{typed['id']}.md", f"plan/{saved['plan']['id']}.md",
                          *(f"task/{step}.md" for step in steps)}
    assert first["status"] == "written" and first["files"] == len(files) == preview["files"]
    assert first["held"]["items"] == [{"id": unreviewed["id"], "kind": "fact", "key": unreviewed["key"],
                                       "excerpt": "Agents may write facts.",
                                       "reason": "confirmed in a terminal, which writes no audit that travels; "
                                                 "confirm it again in the inspector to publish it"}]
    text = files[f"decision/{decision['id']}.md"].decode()
    assert '"name":"Ada Nowak"' in text and '"branch":"main"' in text
    assert '"lines":[3,3]' in text and "\n    The active backend is ArcadeDB.\n" in text
    assert '"path":"docs/GUIDE.md"' in text and str(root) not in text
    assert "workspace_form" in files[f"constraint/{typed['id']}.md"].decode()
    assert check_directory(folder_of(root))[1] == []
    times = {path: path.stat().st_mtime_ns for path in folder_of(root).rglob("*") if path.is_file()}
    again = publisher.publish(pid)
    assert again["status"] == "unchanged" and again["written"] == [] and published_bytes(root) == files
    assert {path: path.stat().st_mtime_ns for path in folder_of(root).rglob("*") if path.is_file()} == times
    # Progress on one step changes that step's file only: no new act, nothing in the plan file.
    step = confirmed["tasks"][0]
    plan_version = PlanManager(store).get(pid, saved["plan"]["id"])["plan"]["version"]
    store.remember(pid, {"id": step["id"], "expected_version": step["version"], "metadata": {"state": "in_progress"},
                         "expected_plan_version": plan_version}, step["source_ids"])
    progressed = publisher.publish(pid)
    assert progressed["written"] == [f"task/{step['id']}.md"]
    assert '\ncovered: {"by":"progress"}\nstate: "in_progress"\n' in (folder_of(root) / f"task/{step['id']}.md").read_text()


def test_an_edit_after_a_review_stays_local_until_a_person_reviews_it(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    record = reviewed(store, pid, "decision", "Use cookies.", "auth")
    publisher = Publisher(store)
    publisher.publish(pid)
    before = published_bytes(root)
    source = store.snapshot(pid, "note://agent", "Use local storage.", "note")
    update = {"id": record["id"], "expected_version": record["version"], "content": "Use local storage."}
    # An agent cannot change what a person confirmed (#177): nothing is stored, so nothing is published.
    with pytest.raises(ValueError, match="Only a person takes a confirmation away"):
        store.remember(pid, update, [*record["source_ids"], source["id"]])
    assert publisher.publish(pid)["status"] == "unchanged" and published_bytes(root) == before
    # The person's own edit is proposed until they review it.
    changed = person_edit(store, pid, update, [*record["source_ids"], source["id"]])
    assert changed["status"] == "proposed"
    assert publisher.publish(pid)["status"] == "unchanged" and published_bytes(root) == before
    Inspector(store).review(pid, record["id"], "confirm", changed["version"])
    assert publisher.publish(pid)["written"] == [f"decision/{record['id']}.md"]
    assert "## Revision 2" in (folder_of(root) / f"decision/{record['id']}.md").read_text()


def test_a_record_an_external_session_wrote_is_never_published(ada):
    store, pid = ada["store"], ada["project"]
    mark = {"origin_client": "claude", "origin_session_id": "s", "origin_history": "unarchived",
            "origin_evidence": "degraded"}
    with store.write_origin(mark):
        source = store.snapshot(pid, "note://external", "Use tabs.", "note")
        record = store.remember(pid, {"kind": "decision", "content": "Use tabs."}, [source["id"]])
    Inspector(store).review(pid, record["id"], "confirm", record["version"])
    result = Publisher(store).publish(pid, preview=True)
    assert result["held"]["items"][0]["reason"].endswith("(degraded origin)") and result["records"] == 0


def test_hidden_characters_and_dangling_references_hold_only_the_records_concerned(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    hidden = reviewed(store, pid, "decision", "Use cookies‮, then tokens.", "hidden")
    plain = reviewed(store, pid, "fact", "The service listens on localhost only.", "listen")
    # A replacement a person confirmed, of a record nobody did: it names something that is not published.
    old_source = store.snapshot(pid, "note://old", "Use sessions.", "note")
    old = store.remember(pid, {"kind": "decision", "key": "sessions", "content": "Use sessions."}, [old_source["id"]])
    new_source = store.snapshot(pid, "note://new", "Use short sessions.", "note")
    replacement = store.remember(pid, {"kind": "decision", "key": "sessions-2", "content": "Use short sessions.",
                                       "supersedes": old["id"]}, [new_source["id"]])
    Inspector(store).review(pid, replacement["id"], "confirm", replacement["version"])
    result = Publisher(store).publish(pid)
    reasons = {item["id"]: item["reason"] for item in result["held"]["items"]}
    assert reasons[hidden["id"]].startswith("its text holds control or bidi characters")
    assert reasons[replacement["id"]] == f"it names record {old['id']}, which is not published"
    assert result["records"] == 1 and (folder_of(root) / f"fact/{plain['id']}.md").is_file()


def test_two_branches_with_different_records_merge_cleanly_and_import_holds_both(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    shared = reviewed(store, pid, "decision", "ArcadeDB is the only storage engine.", "storage",
                      quote="The active backend is ArcadeDB.")
    Publisher(store).publish(pid)
    commit(root, "Publish team memory")
    bo = teammate(ada, "bo", "Bo Lee")
    imported = PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    assert imported["inserted"] == [f"decision/{shared['id']}.md"] and imported["stubs"] == 3
    git(root, "checkout", "-qb", "ada-branch")
    mine = reviewed(store, pid, "constraint", "Never commit for the user.", "commits")
    Publisher(store).publish(pid)
    commit(root, "Ada's constraint")
    git(bo["root"], "checkout", "-qb", "bo-branch")
    theirs = reviewed(bo["store"], bo["project"], "fact", "The service listens on localhost only.", "listen")
    bo_result = Publisher(bo["store"]).publish(bo["project"])
    # The shared record was imported, so Bo's store renders it byte for byte as Ada's did.
    assert bo_result["written"] == [f"fact/{theirs['id']}.md"]
    commit(bo["root"], "Bo's fact")
    git(root, "fetch", "-q", str(bo["root"]), "bo-branch:bo-branch")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--no-edit", "ada-branch")
    merged = git(root, "merge", "--no-edit", "bo-branch", check=False)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    cy = teammate(ada, "cy", "Cy Park")
    result = PublishedImporter(cy["store"]).import_published(folder_of(cy["root"]), cy["project"])
    assert sorted(result["inserted"]) == sorted([f"decision/{shared['id']}.md", f"constraint/{mine['id']}.md",
                                                 f"fact/{theirs['id']}.md"])
    records = {row["id"]: row for row in cy["store"].list_records(cy["project"])}
    assert set(records) == {shared["id"], mine["id"], theirs["id"]}
    # Nothing imported is verified: the record rests only on what another machine saw.
    states = evidence_states(cy["store"], cy["project"], records.values())
    assert {state["evidence_state"] for state in states.values()} == {"unverified"}
    assert {state.get("unverified_by") for state in states.values()} == {"published_elsewhere"}
    # A repeat import writes nothing, and the archive of the importing store is still valid.
    again = PublishedImporter(cy["store"]).import_published(folder_of(cy["root"]), cy["project"])
    assert again["inserted"] == again["advanced"] == [] and again["unchanged"] == 3 and again["stubs"] == 0
    snapshot = MemoryManager(cy["store"]).snapshot(cy["project"])
    assert snapshot["status"] == "created" and snapshot["counts"]["records"] == 3


def test_a_file_cannot_attach_the_importers_own_source_to_a_record(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    shared = reviewed(store, pid, "decision", "ArcadeDB is the only storage engine.", "storage",
                      quote="The active backend is ArcadeDB.")
    Publisher(store).publish(pid)
    commit(root, "Publish team memory")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    theirs = reviewed(bo["store"], bo["project"], "fact", "The service listens on localhost only.", "listen")
    Publisher(bo["store"]).publish(bo["project"])
    folder = folder_of(bo["root"])
    original = (folder / f"fact/{theirs['id']}.md").read_bytes()
    # Ada's indexed guide, copied by hand from her file into Bo's fact: canonical, and still covered by Bo's review.
    guide = next(s for s, source in parse_file((folder / f"decision/{shared['id']}.md").read_bytes())["sources"].items()
                 if source["ref"]["kind"] == "document")
    model = parse_file(original)
    model["sources"][guide] = parse_file((folder / f"decision/{shared['id']}.md").read_bytes())["sources"][guide]
    model["revisions"][0]["sources"] = sorted([*model["revisions"][0]["sources"], guide])
    (folder / f"fact/{theirs['id']}.md").write_bytes(render(model))
    assert check_directory(folder)[1] == []
    with pytest.raises(MemoryConflict, match=f"cites source {guide}, which this record never cited here"):
        PublishedImporter(store).import_published(folder, pid)
    assert theirs["id"] not in {row["id"] for row in store.list_records(pid)}
    (folder / f"fact/{theirs['id']}.md").write_bytes(original)
    assert PublishedImporter(store).import_published(folder, pid)["inserted"] == [f"fact/{theirs['id']}.md"]
    states = evidence_states(store, pid, [r for r in store.list_records(pid) if r["id"] == theirs["id"]])
    assert states[theirs["id"]]["evidence_state"] == "unverified"


def test_a_round_trip_keeps_identity_revisions_and_sources(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    record = reviewed(store, pid, "decision", "Use cookies.", "auth", quote="Nothing else stores memory.")
    edited(store, pid, record, "Use secure HttpOnly cookies.")
    saved = PlanManager(store).save(pid, {"title": "Ship", "goal": "Ship it"}, [{"key": "one", "content": "Step one"}],
                                    "ship-plan", worktree=str(root))
    confirm_plan(store, pid, saved["plan"]["id"], saved["plan"]["version"])
    Publisher(store).publish(pid)
    commit(root, "Publish")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    original = {model["id"]: model for model in Publisher(store).build(pid)["models"]}
    copy = {model["id"]: model for model in Publisher(bo["store"]).build(bo["project"])["models"]}
    assert copy == original and len(copy[record["id"]]["revisions"]) == 2
    rows = {row["id"]: row for row in bo["store"].list_records(bo["project"])}
    assert rows[record["id"]]["metadata"]["publication"]["reviewed_by"][0]["name"] == "Ada Nowak"
    task = rows[saved["tasks"][0]["id"]]
    assert task["metadata"]["plan_ref"] == saved["plan"]["id"] and task["metadata"]["step_index"] == 0
    from cairnmind.memory.verification import verification_status
    assert verification_status(bo["store"], task, bo["root"])["status"] != "fresh"
    assert MemoryManager(bo["store"]).snapshot(bo["project"])["status"] == "created"


def test_the_same_record_changed_on_two_branches_conflicts_in_exactly_its_file(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    record = reviewed(store, pid, "decision", "Use cookies.", "auth")
    other = reviewed(store, pid, "fact", "The service listens on localhost only.", "listen")
    Publisher(store).publish(pid)
    commit(root, "Publish")
    bo = teammate(ada, "bo", "Bo Lee")
    PublishedImporter(bo["store"]).import_published(folder_of(bo["root"]), bo["project"])
    git(root, "checkout", "-qb", "ada-change")
    edited(store, pid, record, "Use secure cookies.")
    Publisher(store).publish(pid)
    commit(root, "Ada changes the decision")
    git(bo["root"], "checkout", "-qb", "bo-change")
    edited(bo["store"], bo["project"], record, "Use signed cookies.")
    Publisher(bo["store"]).publish(bo["project"])
    commit(bo["root"], "Bo changes the decision")
    git(root, "fetch", "-q", str(bo["root"]), "bo-change:bo-change")
    merge = git(root, "merge", "--no-edit", "bo-change", check=False)
    assert merge.returncode != 0
    path = f"decision/{record['id']}.md"
    unmerged = git(root, "diff", "--name-only", "--diff-filter=U").stdout.split()
    assert unmerged == [f".memory/cairnmind-published/{path}"]
    _, errors = check_directory(folder_of(root))
    assert len(errors) == 1 and errors[0][0] == path and errors[0][1].startswith(f"unresolved conflict in {path} line ")
    with pytest.raises(MemoryConflict, match=path.replace(".", r"\.")):
        PublishedImporter(store).import_published(folder_of(root), pid)
    # A person resolves it like a conflict in code: by taking one side whole.
    git(root, "checkout", "--theirs", "--", f".memory/cairnmind-published/{path}")
    commit(root, "Take Bo's decision")
    assert check_directory(folder_of(root))[1] == []
    with pytest.raises(MemoryConflict, match="take-published"):
        PublishedImporter(store).import_published(folder_of(root), pid)
    versions = len(store.record_history(record["id"]))
    # Ada confirmed the record here: taking the published side replaces her verdict, so it needs her choice
    # and the workspace's proof like any other replacement (#214).
    with pytest.raises(MemoryConflict, match="would replace 1 record"):
        PublishedImporter(store).import_published(folder_of(root), pid, take_published=[record["id"]])
    with person_unproven(), pytest.raises(ConfirmationRefused):
        PublishedImporter(store).import_published(folder_of(root), pid, take_published=[record["id"]],
                                                  replace_confirmed=True)
    assert len(store.record_history(record["id"])) == versions
    taken = PublishedImporter(store).import_published(folder_of(root), pid, take_published=[record["id"]],
                                                      replace_confirmed=True)
    assert taken["taken"] == [path] and taken["unchanged"] == 1
    current = next(r for r in store.list_records(pid) if r["id"] == record["id"])
    assert current["content"] == "Use signed cookies." and len(store.record_history(record["id"])) == versions + 1
    # Ada's own wording stays in her history, and her next publication follows the published file.
    assert Publisher(store).publish(pid)["status"] == "unchanged"
    edited(store, pid, current, "Use secure, signed cookies.")
    assert Publisher(store).publish(pid)["written"] == [path]
    assert "## Revision 3" in (folder_of(root) / path).read_text() and other["id"]


def test_a_directory_of_another_team_or_a_mismatched_binding_is_refused(ada, tmp_path):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    reviewed(store, pid, "decision", "Use cookies.", "auth")
    Publisher(store).publish(pid)
    other_root = tmp_path / "other-repository"
    other_root.mkdir()
    other = store.add_project("other", other_root)
    folder = other_root / ".memory" / "cairnmind-published"
    folder.mkdir(parents=True)
    (folder / "FORMAT.json").write_bytes(json.dumps({"format": "cairnmind-published-memory", "format_version": 1,
        "project_id": "5a1f0c3e-8b2d-4e6f-9a7c-1d3b5e7f9a20"}, separators=(",", ":"), sort_keys=True).encode() + b"\n")
    with pytest.raises(MemoryConflict, match="belongs to 5a1f0c3e"):
        Publisher(store).publish(other["id"])
    # Another project's own directory is refused as such first (#239) ...
    with pytest.raises(MemoryConflict, match="each project imports only its own directory"):
        PublishedImporter(store).import_published(folder_of(root), other["id"], bind=True)
    # ... and the same record IDs cannot be imported into a second local project from a copy either.
    import shutil
    copy = tmp_path / "copy-of-team-memory"
    shutil.copytree(folder_of(root), copy)
    with pytest.raises(MemoryConflict, match="another local project"):
        PublishedImporter(store).import_published(copy, other["id"], bind=True)
