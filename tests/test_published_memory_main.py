"""The main-branch import (#172): published memory of a commit, re-checked against that commit's code."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

from cairnmind.memory.evidence import evidence_states
from cairnmind.terminal.inspector import Inspector
from cairnmind.memory.memory import MemoryConflict, MemoryManager
from cairnmind.memory.memory_published import PUBLICATION_SWITCH, Publisher
from cairnmind.memory.memory_published_main import import_commit
from cairnmind.store import Store
from cairnmind.terminal.terminal_detail import evidence_parts, joined

GUIDE = "# Guide\n\nThe active backend is ArcadeDB.\nNothing else stores memory.\n"


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def reviewed(store, project_id, kind, text, key, *, quote=None, cite_guide=False):
    source = store.snapshot(project_id, f"note://{key}", text, "note")
    sources, metadata = [source["id"]], {}
    if quote or cite_guide:
        root = Path(store.query("SELECT root FROM projects WHERE id=:id", {"id": project_id})[0]["root"])
        guide = store.snapshot(project_id, str(root / "docs/GUIDE.md"), GUIDE, "document",
                               {"path": str(root / "docs/GUIDE.md"), "root": str(root),
                                "relative_path": "docs/GUIDE.md", "snapshot": "working_tree"})
        sources.append(guide["id"])
    if quote:
        metadata["evidence"] = quote
    record = store.remember(project_id, {"kind": kind, "key": key, "content": text, "metadata": metadata}, sources)
    return Inspector(store).review(project_id, record["id"], "confirm", record["version"])["record"]


def every_row(store, project_id):
    """Everything the import could write, to show that a repeat import writes nothing."""
    return {table: sorted(map(repr, store.query(f"SELECT FROM {table} WHERE project_id=:p", {"p": project_id})))
            for table in ("records", "record_revisions", "sources", "plans", "plan_revisions")}


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setenv(PUBLICATION_SWITCH, "1")
    root = tmp_path / "ada"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Ada Nowak")
    git(root, "config", "user.email", "ada@example.invalid")
    (root / "docs").mkdir()
    (root / "docs" / "GUIDE.md").write_text(GUIDE)
    git(root, "add", ".")
    git(root, "commit", "-qm", "Guide")
    store = Store(tmp_path / "ada-data")
    project = store.add_project("team", root)
    kept = reviewed(store, project["id"], "decision", "ArcadeDB is the only storage engine.", "storage",
                    quote="The active backend is ArcadeDB.")
    lost = reviewed(store, project["id"], "constraint", "Nothing else may store memory.", "only",
                    quote="Nothing else stores memory.")
    plain = reviewed(store, project["id"], "fact", "The guide names the backend.", "guide", cite_guide=True)
    talk = reviewed(store, project["id"], "fact", "We agreed to keep one engine.", "agreed")
    Publisher(store).publish(project["id"])
    git(root, "add", "-A", ".memory")
    git(root, "commit", "-qm", "Publish team memory")
    server = tmp_path / "server"
    git(tmp_path, "clone", "-q", str(root), str(server))
    main_store = Store(tmp_path / "server-data")
    main_project = main_store.add_project("team", server)
    return {"root": root, "server": server, "store": main_store, "project": main_project["id"],
            "records": {"kept": kept, "lost": lost, "plain": plain, "talk": talk}}


def state_of(store, project_id, record_id):
    row = next(r for r in store.list_records(project_id) if r["id"] == record_id)
    return evidence_states(store, project_id, [row])[record_id]


def test_a_passage_gone_from_the_main_branch_leaves_its_record_orphaned_and_one_still_there_anchors(team):
    store, pid, records, server = team["store"], team["project"], team["records"], team["server"]
    # The merge on the main branch also removed the line one constraint quotes.
    (team["root"] / "docs" / "GUIDE.md").write_text(GUIDE.replace("Nothing else stores memory.\n", ""))
    git(team["root"], "commit", "-qam", "Drop a sentence")
    git(server, "pull", "-q", "--ff-only")
    result = import_commit(store, pid, "HEAD")
    assert result["commit"] == git(server, "rev-parse", "HEAD") and len(result["inserted"]) == 4
    assert result["model_calls"] == 0 and "does not verify" in result["verification"]
    paths = {name: f"{record['kind']}/{record['id']}.md" for name, record in records.items()}
    assert [item["path"] for item in result["evidence"]["orphaned"]] == [paths["lost"]]
    assert result["evidence"]["orphaned"][0]["title"] == "Nothing else may store memory."
    assert result["evidence"]["orphaned"][0]["reviewed"] == \
        "confirm by Ada Nowak on main (claimed by the file; not verified here)"
    assert sorted(item["path"] for item in result["evidence"]["anchored"]) == sorted([paths["kept"]])
    # The whole guide changed, so a record citing it without a quote cannot say more than that.
    assert [item["path"] for item in result["evidence"]["evidence_replaced"]] == [paths["plain"]]
    # A conversation seen on another machine is never compared with anything: still unverified.
    assert [item["path"] for item in result["evidence"]["unverified"]] == [paths["talk"]]
    detail = Inspector(store).record("team", records["lost"]["id"])
    assert detail["evidence"]["evidence_state"] == "orphaned"
    assert joined(evidence_parts(detail["evidence"], lambda text: text)[0]) == \
        "Evidence: orphaned: no supporting source has a current version"
    assert Inspector(store).record("team", records["kept"]["id"])["evidence"]["evidence_state"] == "anchored"
    # Anchored is a fact about this commit's code, never verification; the reviewer stays a claim.
    row = next(r for r in store.list_records(pid) if r["id"] == records["kept"]["id"])
    assert row["metadata"]["publication"]["reviewed_by"][0]["name"] == "Ada Nowak"
    assert not row["metadata"].get("verified") and "verification_status" not in row["metadata"]
    # A repeat import of the same commit changes nothing at all.
    before = every_row(store, pid)
    again = import_commit(store, pid, "HEAD")
    assert again["inserted"] == again["advanced"] == again["rechecked"] == [] and again["stubs"] == 0
    assert every_row(store, pid) == before
    # A later commit that only changes code, putting the sentence back, anchors the constraint again.
    (team["root"] / "docs" / "GUIDE.md").write_text(GUIDE)
    git(team["root"], "commit", "-qam", "Restore the sentence")
    git(server, "pull", "-q", "--ff-only")
    restored = import_commit(store, pid, "HEAD")
    assert restored["inserted"] == restored["advanced"] == [] and len(restored["rechecked"]) == 1
    assert state_of(store, pid, records["lost"]["id"])["evidence_state"] == "anchored"
    assert state_of(store, pid, records["plain"]["id"])["evidence_state"] == "anchored"
    assert MemoryManager(store).snapshot(pid)["status"] == "created"


def test_an_earlier_commit_can_be_imported_and_a_withdrawn_file_is_reported_but_never_removed(team):
    store, pid, records, server = team["store"], team["project"], team["records"], team["server"]
    published = git(server, "rev-parse", "HEAD")
    dry = import_commit(store, pid, published, dry_run=True)
    assert dry["status"] == "dry_run" and len(dry["inserted"]) == 4 and not store.list_records(pid)
    import_commit(store, pid, published)
    assert state_of(store, pid, records["lost"]["id"])["evidence_state"] == "anchored"
    folder = team["root"] / ".memory" / "cairnmind-published"
    (folder / f"fact/{records['talk']['id']}.md").unlink()
    git(team["root"], "commit", "-qam", "Withdraw a fact")
    git(server, "pull", "-q", "--ff-only")
    result = import_commit(store, pid, "HEAD")
    assert [item["id"] for item in result["withdrawn"]] == [records["talk"]["id"]]
    assert records["talk"]["id"] in {row["id"] for row in store.list_records(pid)}
    with pytest.raises(ValueError, match="commit name"):
        import_commit(store, pid, "--all")
    nothing = Store(team["server"].parent / "empty-data")
    empty_root = team["server"].parent / "empty"
    empty_root.mkdir()
    git(empty_root, "init", "-q", "-b", "main")
    git(empty_root, "-c", "user.name=A", "-c", "user.email=a@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
    other = nothing.add_project("empty", empty_root)
    assert import_commit(nothing, other["id"], "HEAD")["status"] == "nothing_published"


class NoInference:
    async def models(self):
        pytest.fail("The main-branch import must not invoke a model")

    async def chat(self, *args, **kwargs):
        pytest.fail("The main-branch import must not invoke a model")

    async def embed(self, *args, **kwargs):
        pytest.fail("The main-branch import must not invoke a model")


def test_the_service_route_imports_a_commit_without_a_model(team):
    from fastapi.testclient import TestClient
    from cairnmind.service import create_app
    store = team["store"]
    headers = {"Authorization": f"Bearer {store.settings.token}"}
    with TestClient(create_app(store.settings, start_worker=False, ollama=NoInference())) as client:
        body = {"project": "team", "revision": "HEAD"}
        assert client.post("/memory/import-commit", headers=headers, json={**body, "colour": 1}).status_code == 422
        dry = client.post("/memory/import-commit", headers=headers, json={**body, "dry_run": True}).json()
        assert dry["status"] == "dry_run" and not store.list_records(team["project"])
        done = client.post("/memory/import-commit", headers=headers, json=body).json()
        assert len(done["inserted"]) == 4 and done["commit"] == git(team["server"], "rev-parse", "HEAD")
        assert client.post("/memory/import-commit", headers=headers, json=body).json()["inserted"] == []


def drop_the_quoted_sentence(team, message):
    (team["root"] / "docs" / "GUIDE.md").write_text(GUIDE.replace("Nothing else stores memory.\n", ""))
    git(team["root"], "commit", "-qam", message)
    git(team["server"], "pull", "-q", "--ff-only")


def test_a_quote_edited_in_the_file_is_refused_and_an_unhashed_quote_never_anchors(team):
    store, pid, records = team["store"], team["project"], team["records"]
    folder = team["root"] / ".memory" / "cairnmind-published"
    path = folder / f"constraint/{records['lost']['id']}.md"
    original = path.read_text()
    assert '"evidence_sha256":"' in original  # The review hashed the passage it saw.
    # A short, common string put in place of the reviewed passage: the review no longer covers the revision.
    path.write_text(original.replace("    Nothing else stores memory.", "    ArcadeDB.", 1))
    drop_the_quoted_sentence(team, "Edit a quote by hand")
    with pytest.raises(MemoryConflict, match="not covered by a person's act"):
        import_commit(store, pid, "HEAD")
    assert store.list_records(pid) == []
    # An audit recorded before reviews hashed the passage covers the text only. Its quote can then be edited
    # without a refusal, so it never decides anchoring: the record is judged by its file, which changed.
    path.write_text(re.sub(r'"evidence_sha256":"[0-9a-f]{64}",', "", original)
                    .replace("    Nothing else stores memory.", "    ArcadeDB.", 1))
    git(team["root"], "commit", "-qam", "An old review, and an edited quote")
    git(team["server"], "pull", "-q", "--ff-only")
    result = import_commit(store, pid, "HEAD")
    assert state_of(store, pid, records["lost"]["id"])["evidence_state"] == "evidence_replaced"
    assert records["lost"]["id"] not in {item["id"] for item in result["evidence"].get("anchored", [])}
    # The decision's quote is still hashed by its review and still in the guide: it anchors as before.
    assert state_of(store, pid, records["kept"]["id"])["evidence_state"] == "anchored"


def test_a_file_source_pointing_into_the_published_directory_never_anchors_itself(team):
    store, pid, records = team["store"], team["project"], team["records"]
    folder = team["root"] / ".memory" / "cairnmind-published"
    own = f".memory/cairnmind-published/constraint/{records['lost']['id']}.md"
    for path in folder.glob("*/*.md"):
        path.write_text(path.read_text().replace('"path":"docs/GUIDE.md"', f'"path":"{own}"'))
    drop_the_quoted_sentence(team, "Point the guide at a published file")
    result = import_commit(store, pid, "HEAD")
    # The published file holds its own quote, but a passage is compared with the project's code only.
    assert state_of(store, pid, records["lost"]["id"])["evidence_state"] == "orphaned"
    assert {item["id"] for item in result["evidence"]["orphaned"]} >= {records["lost"]["id"], records["kept"]["id"]}


def test_a_broken_commit_is_refused_with_its_path_and_imports_nothing(team):
    store, pid, records = team["store"], team["project"], team["records"]
    path = f"decision/{records['kept']['id']}.md"
    broken = team["root"] / ".memory" / "cairnmind-published" / path
    broken.write_text(broken.read_text().replace("    ArcadeDB is", "=======\n    ArcadeDB is", 1))
    git(team["root"], "commit", "-qam", "Conflict committed")
    git(team["server"], "pull", "-q", "--ff-only")
    with pytest.raises(MemoryConflict, match=f"{path}: unresolved conflict"):
        import_commit(store, pid, "HEAD")
    assert store.list_records(pid) == []
