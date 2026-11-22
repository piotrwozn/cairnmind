"""An archive import replaces exactly the verdicts the person was shown (#333).

The importer tests use two real stores (no engine beyond the one the store tests already use); the panel tests
use the fake broker of the other terminal tests and answer a consent dialog only after its draw and quiet moment,
with bounded waits. Nothing here depends on the platform: macOS runs all of it as it is.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from consent_steps import consent_drawn, form_ready
from textual.widgets import Input, Static

from cairnmind import person_proof
from cairnmind.confirmation import REPLACED_CHANGED
from cairnmind.memory import MemoryConflict, MemoryManager
from cairnmind.store import Store
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_panels import SidebarApp
from cairnmind import terminal_published_import as published

from conftest import as_person
from test_backup_restore_stream import comparable
from test_confirmed_by_person import fixture  # noqa: F401 (the fixture)
from test_confirmed_elsewhere import rows, two_computers  # noqa: F401 (a fixture and a helper)
from test_terminal_panels import FakeBroker, setup, settle
from test_trial011_workspace import wait_until


def verdicts_to_replace(two, count):
    """`count` records the person confirmed on the first computer, then changed on the second: the second's
    archive replaces them. Returns (record ids, a function that changes more records there and archives again)."""
    source, destination, pid, first, second = two
    note = source.snapshot(pid, "manual://note", "Keep the verdicts", "manual")
    made = [as_person(source, pid, {"kind": "fact", "content": f"Fact {number}"}, [note["id"]])["id"]
            for number in range(count)]
    MemoryManager(destination).import_snapshot(MemoryManager(source).snapshot(pid)["path"], second)
    there = rows(destination, pid)

    def change(identifiers):
        for identifier in identifiers:
            changed = destination.remember(pid, {"id": identifier, "expected_version": there[identifier]["version"],
                                                 "content": f"Changed {identifier}"}, there[identifier]["source_ids"])
            there[identifier] = changed
        return MemoryManager(destination).snapshot(pid, second)["path"]

    return made, change


def test_an_archive_that_changes_between_the_check_and_the_import_is_refused_and_nothing_is_written(two_computers):  # noqa: F811
    source, _, pid, first, _ = two_computers
    made, change = verdicts_to_replace(two_computers, 3)
    checked_archive = change(made[:2])
    shown = MemoryManager(source).import_snapshot(checked_archive, first, dry_run=True)["replaces_confirmed_here"]["records"]
    assert shown == sorted(made[:2])
    changed_archive = change(made[2:])  # the archive file is replaced after the person's check
    before = rows(source, pid)
    with pytest.raises(MemoryConflict) as refused:
        MemoryManager(source).import_snapshot(changed_archive, first, replace_confirmed=True, expected_replaced=shown)
    assert str(refused.value) == REPLACED_CHANGED
    assert rows(source, pid) == before  # nothing was written
    now = MemoryManager(source).import_snapshot(changed_archive, first, dry_run=True)["replaces_confirmed_here"]["records"]
    assert now == sorted(made)
    done = MemoryManager(source).import_snapshot(changed_archive, first, replace_confirmed=True, expected_replaced=now)
    assert done["status"] == "imported" and rows(source, pid)[made[0]]["content"].startswith("Changed")


def test_the_check_lists_every_replaced_verdict_and_an_import_with_a_shorter_list_is_refused(two_computers):  # noqa: F811
    source, _, pid, first, _ = two_computers
    made, change = verdicts_to_replace(two_computers, 205)  # more than the 200 the check used to list
    archive = change(made)
    here = MemoryManager(source).import_snapshot(archive, first, dry_run=True)["replaces_confirmed_here"]
    assert here["total"] == 205 and here["records"] == sorted(made)
    before = rows(source, pid)
    with pytest.raises(MemoryConflict) as refused:
        MemoryManager(source).import_snapshot(archive, first, replace_confirmed=True, expected_replaced=here["records"][:200])
    assert str(refused.value) == REPLACED_CHANGED and rows(source, pid) == before
    done = MemoryManager(source).import_snapshot(archive, first, replace_confirmed=True, expected_replaced=here["records"])
    assert done["status"] == "imported"


def test_a_verdict_given_here_after_the_check_refuses_the_import_and_nothing_is_written(two_computers):  # noqa: F811
    source, _, pid, first, _ = two_computers
    made, change = verdicts_to_replace(two_computers, 2)
    archive = change(made[:1])
    shown = MemoryManager(source).import_snapshot(archive, first, dry_run=True)["replaces_confirmed_here"]["records"]
    note = source.snapshot(pid, "manual://later", "Given after the check", "manual")
    as_person(source, pid, {"kind": "fact", "content": "Confirmed here after the check"}, [note["id"]])
    before = comparable(source.export())
    with pytest.raises(MemoryConflict):  # the store changed after the check: refused, whichever rule says so first
        MemoryManager(source).import_snapshot(archive, first, replace_confirmed=True, expected_replaced=shown)
    assert comparable(source.export()) == before


def test_after_an_accepted_import_a_snapshot_a_preview_and_a_backup_and_restore_still_work(two_computers, tmp_path):  # noqa: F811
    source, _, pid, first, _ = two_computers
    made, change = verdicts_to_replace(two_computers, 2)
    archive = change(made)
    shown = MemoryManager(source).import_snapshot(archive, first, dry_run=True)["replaces_confirmed_here"]["records"]
    assert MemoryManager(source).import_snapshot(archive, first, replace_confirmed=True,
                                                 expected_replaced=shown)["status"] == "imported"
    manager = MemoryManager(source)
    assert manager.preview(pid)["status"] == "preview"
    assert Path(manager.snapshot(pid)["path"]).is_file()
    restored = Store(tmp_path / "restored")
    restored.restore(source.backup(tmp_path / "after-import.cairnmind.json.gz"))
    contents = {identifier: row["content"] for identifier, row in rows(source, pid).items()}
    assert {identifier: row["content"] for identifier, row in rows(restored, pid).items()} == contents
    assert all(contents[identifier].startswith("Changed") for identifier in made)


def test_the_route_needs_the_list_the_person_was_shown(fixture, workspace_person):  # noqa: F811
    body = {"path": "/nowhere", "root": str(fixture.root), "replace_confirmed": True}
    response = fixture.client.post("/memory/import", json=body, headers=workspace_person)
    assert response.status_code >= 400 and response.status_code != 403, response.text
    assert "expected_replaced" in response.text
    sent = fixture.client.post("/memory/import", json={**body, "expected_replaced": []}, headers=workspace_person)
    assert "expected_replaced" not in sent.text  # past that check: this archive does not exist, and the route says so


# -- the workspace ----------------------------------------------------------------------------------------------

class ArchiveBroker(FakeBroker):
    def __init__(self, project, records):
        super().__init__(project)
        self.records = records

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/memory/import":
            self.calls.append((method, path, params or {}, json))
            if json["dry_run"]:
                return {"status": "dry_run", "project": self.project, "verification": "historical",
                        "replaces_confirmed_here": {"total": len(self.records), "records": list(self.records)}}
            return {"status": "imported", "project": self.project, "verification": "historical"}
        return super().request(method, path, params=params, json=json, timeout=timeout)

    def imports(self):
        return [body for method, path, _, body in self.calls if method == "POST" and path == "/api/memory/import"]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_memory_view_shows_every_replaced_verdict_and_sends_that_list(tmp_path, monkeypatch, language):
    settings, _, workspace = setup(tmp_path, monkeypatch, project=False)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    listed = sorted(str(uuid4()) for _ in range(250))
    broker = ArchiveBroker({"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")}, listed)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.action_snapshot()
        await settle(app, pilot)
        await pilot.click("#memory-import")
        await pilot.pause()
        app.screen.query_one("#field-path", Input).value = "/projects/a/.memory/archive"
        app.screen.query_one("#field-root", Input).value = "/projects/a"
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await settle(app, pilot)
        text = str(app.query_one("#entity-body", Static).render())
        assert tr(published.ARCHIVE_HEADING, language, count=250) in text
        assert all(identifier in text for identifier in listed)  # none cut
        assert app.pending_import["expected_replaced"] == listed and app.pending_import["replace_confirmed"]
        await pilot.click("#memory-apply")
        modal = await consent_drawn(app, pilot)  # Apply is a person's act: its consent dialog lists them too (#332)
        assert broker.imports()[1:] == []
        shown = str(modal.query_one("#message-body", Static).render())
        assert tr(published.ARCHIVE_HEADING, language, count=250) in shown
        assert all(identifier in shown for identifier in listed)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: len(broker.imports()) == 2)
    check, final = broker.imports()
    assert final == {**check, "dry_run": False, "replace_confirmed": True, "expected_replaced": listed}


@pytest.mark.asyncio
async def test_the_sidebar_dialog_shows_every_replaced_verdict_and_sends_that_list(tmp_path, monkeypatch):
    settings, _, workspace = setup(tmp_path, monkeypatch)
    listed = sorted(str(uuid4()) for _ in range(250))
    broker = ArchiveBroker({"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")}, listed)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 30)) as pilot:
        await settle(app, pilot)
        app.import_memory()
        await pilot.pause(.2)
        app.screen.query_one("#field-path", Input).value = "/projects/a/.memory/archive"
        app.screen.query_one("#field-root", Input).value = "/projects/a"
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await consent_drawn(app, pilot)
        text = str(app.screen.query_one("#message-body", Static).render())
        assert tr(published.ARCHIVE_HEADING, "pl", count=250) in text
        assert all(identifier in text for identifier in listed)
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
    check, final = broker.imports()
    assert final == {**check, "dry_run": False, "replace_confirmed": True, "expected_replaced": listed}


def test_the_new_sentence_has_polish_and_the_refusal_is_the_existing_one():
    assert tr(published.ARCHIVE_HEADING, "pl", count=1) != tr(published.ARCHIVE_HEADING, "en", count=1)
    assert tr(REPLACED_CHANGED, "pl") != REPLACED_CHANGED and person_proof.IMPORT_REPLACE
