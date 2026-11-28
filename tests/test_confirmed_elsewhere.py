"""What a memory package brings as confirmed is kept as confirmed elsewhere, never as this machine's (#177).

Real ArcadeDB and Git; no model is asked.
"""
from __future__ import annotations

import subprocess

import pytest
from conftest import as_person, person_edit
from test_published_memory import commit, folder_of, published_bytes, repository, reviewed, teammate

from cairnmind.memory.confirmation import confirmation_of, confirmation_text
from cairnmind.memory.evidence import evidence_states
from cairnmind.memory.memory import MemoryManager
from cairnmind.memory.memory_acts import covered_by_local_act
from cairnmind.memory.memory_published import Publisher
from cairnmind.memory.memory_published_import import PublishedImporter
from cairnmind.memory.memory_view import show_snapshot
from cairnmind.memory.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.terminal.inspector import Inspector
from cairnmind.terminal.terminal_detail import joined, record_parts
from cairnmind.terminal.terminal_i18n import tr


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def rows(store, project_id):
    return {row["id"]: row for row in store.list_records(project_id)}


@pytest.fixture
def ada(tmp_path):
    root = repository(tmp_path / "ada-repository")
    store = Store(tmp_path / "ada-database")
    return {"store": store, "project": store.add_project("team", root)["id"], "root": root, "tmp": tmp_path}


@pytest.fixture
def two_computers(tmp_path):
    first, second = tmp_path / "computer-a", tmp_path / "computer-b"
    first.mkdir()
    git(first, "init", "--quiet", "-b", "main")
    git(first, "config", "user.name", "Ada Nowak")
    git(first, "config", "user.email", "ada@example.invalid")
    (first / "app.py").write_text("print('portable')\n")
    git(first, "add", "app.py")
    git(first, "commit", "--quiet", "-m", "Fixture")
    subprocess.run(["git", "clone", "--quiet", str(first), str(second)], check=True)
    git(second, "config", "user.name", "Bo Lee")
    git(second, "config", "user.email", "bo@example.invalid")
    source, destination = Store(tmp_path / "data-a"), Store(tmp_path / "data-b")
    project = source.add_project("Portable", first)
    return source, destination, project["id"], first, second


def test_an_archive_brings_a_confirmation_as_confirmed_elsewhere_with_who_and_where(two_computers):
    source, destination, pid, first, second = two_computers
    note = source.snapshot(pid, "manual://note", "Keep the decision across computers", "manual")
    proposed = source.remember(pid, {"kind": "decision", "content": "Use the portable decision"}, [note["id"]])
    reviewed_here = Inspector(source).review(pid, proposed["id"], "confirm", 1)["record"]
    typed = as_person(source, pid, {"kind": "constraint", "content": "Typed on the command line"}, [note["id"]])
    waiting = source.remember(pid, {"kind": "fact", "content": "Still proposed"}, [note["id"]])
    assert reviewed_here["metadata"]["confirmation"]["by"] == "person"
    snapshot = MemoryManager(source).snapshot(pid)

    # The file itself says who confirmed what, as a claim, and carries no store's mark.
    shown = show_snapshot(snapshot["path"])
    assert "confirmed: by Ada Nowak on main at " in shown and "(claimed by the package)" in shown
    assert "confirmed: who confirmed it is not recorded in the package" in shown

    preview = MemoryManager(destination).import_snapshot(snapshot["path"], second, dry_run=True)
    assert preview["status"] == "dry_run"
    assert preview["confirmed_records"]["total"] == 2
    assert preview["confirmed_records"]["confirmed_by"] == ["Ada Nowak"]
    assert preview["confirmed_records"]["without_a_name"] == 1
    assert not destination.query("SELECT id FROM records")
    imported = MemoryManager(destination).import_snapshot(snapshot["path"], second)
    assert imported["confirmed_records"]["total"] == 2

    found = rows(destination, pid)
    record, command_line = found[proposed["id"]], found[typed["id"]]
    assert record["status"] == "confirmed" and command_line["status"] == "confirmed"
    assert found[waiting["id"]]["status"] == "proposed" and confirmation_of(found[waiting["id"]]) is None
    mark = record["metadata"]["confirmation"]
    assert mark["by"] == "import" and mark["origin"] == "import" and mark["package"] == "archive"
    assert mark["where"] == str(first) and mark["snapshot_id"] == snapshot["snapshot_id"]
    assert mark["confirmed_by"][-1]["name"] == "Ada Nowak" and mark["confirmed_by"][-1]["branch"] == "main"
    assert mark["confirmed_by"][-1]["action"] == "confirm" and mark["version"] == record["version"]
    assert command_line["metadata"]["confirmation"]["confirmed_by"] == []
    line = confirmation_text(record)
    assert line.startswith("confirmed elsewhere by Ada Nowak on main at ") and line.endswith("not verified here")
    assert "who confirmed it is not recorded" in confirmation_text(command_line)

    # Never this machine's own: the audit that travelled does not cover it here, and a new session sees
    # the difference in its packet.
    assert not covered_by_local_act(destination, record)
    packet = SessionManager(destination, destination.settings).prepare(pid, "codex", second, timeout=0)["packet"]
    assert {item["id"]: item.get("confirmed_by") for item in packet["records"]} == {
        proposed["id"]: "import", typed["id"]: "import"}

    # The inspector shows it in both languages, and the person here can confirm it: then it is theirs.
    detail = Inspector(destination).record(pid, proposed["id"])
    english = joined(record_parts(detail, lambda text: tr(text, "en")))
    polish = joined(record_parts(detail, lambda text: tr(text, "pl")))
    assert "Confirmation: confirmed elsewhere by Ada Nowak on main at " in english and "not verified here" in english
    assert "Potwierdzenie: potwierdzone gdzie indziej przez Ada Nowak" in polish and "niezweryfikowane tutaj" in polish
    theirs = Inspector(destination).review(pid, proposed["id"], "confirm", record["version"])["record"]
    assert theirs["metadata"]["confirmation"]["by"] == "person"
    assert theirs["metadata"]["confirmation"]["actor_name"] == "Bo Lee"
    assert covered_by_local_act(destination, theirs)


def test_the_import_dialog_says_how_many_confirmed_records_a_package_brings():
    from cairnmind.terminal.terminal_ui import summary_text
    preview = {"status": "dry_run", "counts": {"records": 3}, "confirmed_records": {
        "total": 2, "confirmed_by": ["Ada Nowak", "Eve\x1b[31m\u202e"], "without_a_name": 1}}
    english, polish = summary_text(preview, "en"), summary_text(preview, "pl")
    assert ("Confirmed elsewhere: 2. They stay confirmed, are shown as imported and are not verified here."
            in english)
    assert "Confirmed by, as the package says: Ada Nowak, Eve" in english
    assert "Potwierdzone gdzie indziej: 2." in polish and "Potwierdzone przez, według paczki: Ada Nowak" in polish
    assert "\x1b" not in english and "\u202e" not in english
    assert "Confirmed elsewhere" not in summary_text({"status": "dry_run", "confirmed_records": {"total": 0}}, "en")


def test_a_later_archive_never_replaces_a_local_persons_confirmation(two_computers):
    source, destination, pid, first, second = two_computers
    note = source.snapshot(pid, "manual://note", "Keep the decision across computers", "manual")
    proposed = source.remember(pid, {"kind": "decision", "content": "Use the portable decision"}, [note["id"]])
    mine = Inspector(source).review(pid, proposed["id"], "confirm", 1)["record"]
    before = MemoryManager(source).snapshot(pid)
    MemoryManager(destination).import_snapshot(before["path"], second)
    other = destination.snapshot(pid, "manual://other", "A note written on the second computer", "manual")
    destination.remember(pid, {"kind": "fact", "content": "Added on the second computer"}, [other["id"]])
    later = MemoryManager(destination).snapshot(pid, second)
    # The marks are local and never part of the state a package compares.
    assert MemoryManager(source).snapshot(pid)["state_hash"] == before["state_hash"]
    back = MemoryManager(source).import_snapshot(later["path"], first)
    assert back["status"] == "imported" and back["confirmed_records"]["total"] == 0
    kept = rows(source, pid)[proposed["id"]]
    assert kept["metadata"]["confirmation"] == mine["metadata"]["confirmation"]
    assert kept["metadata"]["confirmation"]["by"] == "person" and covered_by_local_act(source, kept)


def test_published_memory_keeps_who_confirmed_and_is_published_again_only_as_theirs(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    record = reviewed(store, pid, "decision", "Use secure HttpOnly cookies.", "auth")
    Publisher(store).publish(pid)
    commit(root, "Publish")
    bo = teammate(ada, "bo", "Bo Lee")
    importer = PublishedImporter(bo["store"])
    preview = importer.import_published(folder_of(bo["root"]), bo["project"], dry_run=True)
    assert preview["status"] == "dry_run" and preview["confirmed_records"]["total"] == 1
    assert preview["confirmed_records"]["confirmed_by"] == ["Ada Nowak"]
    assert not bo["store"].query("SELECT id FROM records")
    assert importer.import_published(folder_of(bo["root"]), bo["project"])["confirmed_records"]["total"] == 1
    row = rows(bo["store"], bo["project"])[record["id"]]
    mark = row["metadata"]["confirmation"]
    assert row["status"] == "confirmed" and mark["by"] == "import" and mark["package"] == "published"
    assert mark["where"] == f"decision/{record['id']}.md" and mark["confirmed_by"][-1]["name"] == "Ada Nowak"
    assert confirmation_text(row).startswith("confirmed elsewhere by Ada Nowak on main at ")
    assert not covered_by_local_act(bo["store"], row)
    assert evidence_states(bo["store"], bo["project"], [row])[row["id"]]["evidence_state"] == "unverified"
    # Published again from the importing store, the file is byte for byte Ada's: never Bo's confirmation.
    before = published_bytes(bo["root"])
    again = Publisher(bo["store"]).publish(bo["project"])
    assert again["status"] == "unchanged" and published_bytes(bo["root"]) == before
    assert b"Bo Lee" not in before[f"decision/{record['id']}.md"]
    # A second import of the same tree changes nothing and brings nothing.
    assert importer.import_published(folder_of(bo["root"]), bo["project"])["confirmed_records"]["total"] == 0
    # Progress by a session keeps the imported confirmation; a changed text loses it.
    note = bo["store"].snapshot(bo["project"], "note://bo", "Bo's note", "note")
    changed = bo["store"].remember(bo["project"], {"id": row["id"], "expected_version": row["version"],
                                                   "content": "Use any cookies."}, [*row["source_ids"], note["id"]])
    assert changed["status"] == "proposed" and changed["metadata"]["confirmation"]["by"] == "lapsed"


def test_a_local_persons_verdict_is_not_overwritten_by_an_import(ada):
    store, pid, root = ada["store"], ada["project"], ada["root"]
    record = reviewed(store, pid, "decision", "Use secure HttpOnly cookies.", "auth")
    Publisher(store).publish(pid)
    commit(root, "Publish")
    bo = teammate(ada, "bo", "Bo Lee")
    importer = PublishedImporter(bo["store"])
    importer.import_published(folder_of(bo["root"]), bo["project"])
    row = rows(bo["store"], bo["project"])[record["id"]]
    disputed = Inspector(bo["store"]).review(bo["project"], row["id"], "dispute", row["version"],
                                             reason="Cookies are not allowed here")["record"]
    # The same tree again, and a newer one from Ada: Bo's dispute stands, and the newer file is refused as
    # diverged instead of being written over it.
    assert importer.import_published(folder_of(bo["root"]), bo["project"])["unchanged"] == 1
    assert rows(bo["store"], bo["project"])[record["id"]]["status"] == "disputed"
    current = rows(store, pid)[record["id"]]
    source = store.snapshot(pid, "note://edit", "Use signed cookies.", "note")
    changed = person_edit(store, pid, {"id": current["id"], "expected_version": current["version"],
                                       "content": "Use signed cookies."}, [*current["source_ids"], source["id"]])
    Inspector(store).review(pid, changed["id"], "confirm", changed["version"])
    Publisher(store).publish(pid)
    from cairnmind.memory.memory import MemoryConflict
    with pytest.raises(MemoryConflict, match="diverged"):
        importer.import_published(folder_of(root), bo["project"])
    kept = rows(bo["store"], bo["project"])[record["id"]]
    assert kept["status"] == "disputed" and kept["version"] == disputed["version"]
