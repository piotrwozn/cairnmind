"""Team memory in the memory view of the inspector (#304): check, consent, then the import with the list shown.

No engine: the broker is a fake that records every request. The key travels in the broker client (tests for it:
test_person_key_workspace); here the requests are what the panel decides to send.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from textual import _time as textual_time, events
from textual.widgets import Button, Input, Static, TextArea

from cairnmind.terminal import person_proof
from cairnmind.memory.confirmation import REPLACED_CHANGED
from cairnmind.terminal.terminal_client import BrokerError
from cairnmind.terminal.terminal_i18n import CATALOG, tr
from cairnmind.terminal.terminal_i18n_messages import BROKER_MESSAGES
from cairnmind.terminal.terminal_inspector import InspectorApp
from cairnmind.terminal import terminal_published_import as published
from cairnmind.terminal.terminal_published_import import MESSAGES, folder_error, taken_ids, take_error
from consent_steps import form_ready, form_state
from test_terminal_panels import FakeBroker, setup, settle
from test_trial011_workspace import wait_until

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def ids(count):
    return [str(uuid4()) for _ in range(count)]


class PublishedBroker(FakeBroker):
    def __init__(self, project, destination="/projects/a/.memory/cairnmind-published"):
        super().__init__(project)
        self.destination = destination
        self.check = {"status": "dry_run", "inserted": [], "advanced": [], "unchanged": 0, "taken": [], "plans": [],
                      "replaces_confirmed_here": {"total": 0, "records": []}}
        self.imported = {"status": "imported", "inserted": [], "advanced": [], "taken": [],
                         "replaces_confirmed_here": {"total": 0, "records": []}}
        self.refuse = None

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/memory/status":
            return {**super().request(method, path, params=params, json=json, timeout=timeout),
                    "published": {"destination": self.destination}}
        if path in ("/api/memory/import-published", "/api/memory/import-commit"):
            self.calls.append((method, path, params or {}, json))
            if json["dry_run"]:
                return self.check
            if self.refuse:
                raise self.refuse
            return self.imported
        return super().request(method, path, params=params, json=json, timeout=timeout)

    def writes(self):
        return [(path, body) for method, path, _, body in self.calls
                if method == "POST" and path.startswith("/api/memory/import-")]


def published_app(tmp_path, monkeypatch, language="pl", project=True, **broker):
    settings, _, workspace = setup(tmp_path, monkeypatch, project=project)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: language)
    value = {"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")}
    fake = PublishedBroker(value, **broker)
    return InspectorApp(settings, broker=fake, workspace=workspace), fake, workspace


async def open_choice(app, pilot, row=0):
    app.action_snapshot()
    await settle(app, pilot)
    await pilot.click("#memory-published")
    await pilot.pause()
    for _ in range(row):
        await pilot.press("down")
    await pilot.press("enter")
    await pilot.pause()


async def checked(app, pilot):
    """Submit the form and wait for the consent dialog: the flow is parked on it, so no worker is complete."""
    await form_ready(app, pilot)
    await pilot.click("#form-submit")
    for _ in range(300):
        await pilot.pause(.02)
        if app.screen.query("#message-body"):
            modal = app.screen  # a consent dialog takes only input made after its draw and its quiet moment (#328)
            await wait_until(pilot, lambda: modal.drawn_at + modal.QUIET <= textual_time.get_time())
            return
    raise AssertionError("no consent dialog; the top screen is " + type(app.screen).__name__ + form_state(app.screen))


async def refused_form(app, pilot):
    """Submit and wait until the form has answered (it stays open on a refusal)."""
    await form_ready(app, pilot)
    await pilot.click("#form-submit")
    await pilot.pause(.3)
    for _ in range(300):
        if not app.screen.saving:
            return
        await pilot.pause(.02)
    raise AssertionError("the form did not answer")

def body_text(app):
    return str(app.screen.query_one("#message-body", Static).render())


def notice(app):
    return str(app.query_one("#panel-notice", Static).render())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_folder_check_names_every_replaced_verdict_and_the_import_follows_consent(
        tmp_path, monkeypatch, language):
    app, broker, _ = published_app(tmp_path, monkeypatch, language)
    one, two, three = ids(3)
    replaced = [f"constraint/{one}.md", f"fact/{two}.md"]
    taken = f"decision/{three}.md"
    broker.check.update(inserted=["fact/new.md"], advanced=replaced, unchanged=4, taken=[taken],
                        replaces_confirmed_here={"total": 2, "records": replaced})
    broker.imported.update(inserted=["fact/new.md"], advanced=replaced, taken=[taken],
                           replaces_confirmed_here={"total": 2, "records": replaced})
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot)
        assert app.screen.query_one("#field-path", Input).value == broker.destination
        app.screen.query_one("#field-take", TextArea).text = f"decision/{three}.md, {three}\n{two}"
        await checked(app, pilot)
        text = body_text(app)
        assert tr(published.REPLACED_HEADING, language, count=2) in text
        for path in (*replaced, taken):
            assert text.count(path) >= 1, path
        assert tr("Nothing imported is verified here.", language) in text
        assert broker.writes() == [("/api/memory/import-published", {
            "path": broker.destination, "project": "project-a", "take_published": [three, two], "dry_run": True})]
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        check, final = (body for _, body in broker.writes())
        assert final == {**check, "dry_run": False, "replace_confirmed": True, "expected_replaced": replaced}
        assert final["take_published"] == [three, two]
        assert tr("Team memory imported: {written} records written, {replaced} of them in place of a verdict given "
                  "on this computer. Nothing imported is verified here.", language, written=4, replaced=2) in notice(app)


@pytest.mark.asyncio
async def test_cancel_writes_nothing(tmp_path, monkeypatch):
    app, broker, _ = published_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot)
        await checked(app, pilot)
        await pilot.click("#confirm-no")
        await settle(app, pilot)
        assert [body["dry_run"] for _, body in broker.writes()] == [True]
        await open_choice(app, pilot)
        await pilot.press("escape")
        await settle(app, pilot)
        assert len(broker.writes()) == 1
        app.action_snapshot()
        await pilot.click("#memory-published")
        await pilot.pause()
        await pilot.press("escape")
        await settle(app, pilot)
        assert len(broker.writes()) == 1 and app.screen is app.screen_stack[0]


@pytest.mark.asyncio
async def test_the_consent_dialog_is_a_consent_dialog_and_the_app_drops_a_key_queued_below_it(tmp_path, monkeypatch):
    app, broker, _ = published_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot)
        await checked(app, pilot)
        modal = app.screen
        assert modal.consent
        early = events.Key("shift+tab", None)
        early.time = modal.drawn_at - 1  # made before the draw, handled late
        app.on_key(early)
        assert early._no_default_action and early._stop_propagation
        await pilot.click("#confirm-no")
        await settle(app, pilot)
        assert [body["dry_run"] for _, body in broker.writes()] == [True]

@pytest.mark.asyncio
async def test_a_commit_is_imported_at_the_commit_the_check_read(tmp_path, monkeypatch):
    app, broker, workspace = published_app(tmp_path, monkeypatch, "en")
    broker.check.update(commit=COMMIT)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot, row=1)
        app.screen.query_one("#field-revision", Input).value = "main"
        await checked(app, pilot)
        assert COMMIT[:12] in body_text(app) and "No record confirmed or disputed" in body_text(app)
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        worktree = workspace.value["worktree"]
        assert broker.writes() == [
            ("/api/memory/import-commit", {"revision": "main", "project": "project-a", "worktree": worktree,
                                           "dry_run": True}),
            ("/api/memory/import-commit", {"revision": COMMIT, "project": "project-a", "worktree": worktree,
                                           "dry_run": False, "replace_confirmed": False, "expected_replaced": []})]


@pytest.mark.asyncio
async def test_a_commit_without_published_memory_says_so_and_imports_nothing(tmp_path, monkeypatch):
    app, broker, _ = published_app(tmp_path, monkeypatch, "en")
    broker.check = {"status": "nothing_published", "commit": COMMIT}
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot, row=1)
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await settle(app, pilot)
        assert f"Commit {COMMIT[:12]} holds no published memory of this project. Nothing was imported." in notice(app)
        assert len(broker.writes()) == 1 and app.screen is app.screen_stack[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_commit_form_says_a_diverged_record_is_taken_from_a_folder(tmp_path, monkeypatch, language):
    # The importer's diverged refusal names --take-published, which a commit has nowhere (#304 review).
    app, broker, _ = published_app(tmp_path, monkeypatch, language)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot, row=1)
        note = " ".join(str(app.screen.query_one(".cm-note", Static).render()).split())
        assert tr(published.COMMIT_NOTE, language) == note
        assert tr("From a published memory folder", language) in note
        await pilot.press("escape")
        await settle(app, pilot)
    assert broker.writes() == []


def test_taken_ids_accepts_ids_and_file_names_and_refuses_the_rest():
    one, two = ids(2)
    assert taken_ids("") == [] and taken_ids("  \n ") == []
    assert taken_ids(f"fact/{one}.md, {two}\n{one}") == [one, two]
    assert taken_ids(f"a/b/{one}.md") == [one]
    assert taken_ids(one.upper()) is None  # not canonical: refused, not changed
    assert taken_ids(f"{one} ../x") is None and taken_ids("fact/.md") is None
    assert taken_ids(" ".join(ids(1000))) is not None and taken_ids(" ".join(ids(1001))) is None
    assert take_error(one) is None and take_error("nonsense") == published.TAKE_ENTRY
    assert folder_error("relative/folder") == published.FULL_PATH and folder_error("/projects/a") is None
    assert folder_error("~/published") is None


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="WSL wslpath conversion is not used on native Windows")
def test_a_typed_windows_drive_path_is_a_full_path_and_is_sent_translated(monkeypatch):
    from cairnmind.terminal import cockpit_state
    monkeypatch.setattr(cockpit_state.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no wslpath")))
    typed = "C:\\Users\\me\\repo\\.memory\\cairnmind-published"
    assert folder_error(typed) is None
    assert published.full_path(typed) == "/mnt/c/Users/me/repo/.memory/cairnmind-published"
    assert folder_error("C:relative") == published.FULL_PATH


@pytest.mark.asyncio
async def test_typed_input_is_checked_before_anything_is_sent(tmp_path, monkeypatch):
    app, broker, _ = published_app(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot)
        app.screen.query_one("#field-path", Input).value = "relative/path"
        await refused_form(app, pilot)
        assert published.FULL_PATH in str(app.screen.query_one("#form-error", Static).render())
        app.screen.query_one("#field-path", Input).value = "/projects/a/published"
        for value in ("../x", " ".join(ids(1001))):
            app.screen.query_one("#field-take", TextArea).text = value
            await refused_form(app, pilot)
            assert published.TAKE_ENTRY in str(app.screen.query_one("#form-error", Static).render())
        assert broker.writes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [person_proof.IMPORT_REPLACE, REPLACED_CHANGED])
async def test_a_refused_import_shows_the_services_sentence_and_is_not_retried(tmp_path, monkeypatch, refusal):
    app, broker, _ = published_app(tmp_path, monkeypatch, "pl")
    path = f"fact/{uuid4()}.md"
    broker.check.update(replaces_confirmed_here={"total": 1, "records": [path]})
    broker.refuse = BrokerError(403 if refusal == person_proof.IMPORT_REPLACE else 409, refusal)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await open_choice(app, pilot)
        await checked(app, pilot)
        await pilot.click("#confirm-yes")
        await settle(app, pilot)
        assert tr(refusal, "pl") in notice(app) and tr(refusal, "pl") != refusal
        assert [body["dry_run"] for _, body in broker.writes()] == [True, False]


@pytest.mark.asyncio
async def test_no_project_no_request(tmp_path, monkeypatch):
    app, broker, _ = published_app(tmp_path, monkeypatch, "en", project=False)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.action_snapshot()
        await settle(app, pilot)
        await pilot.click("#memory-published")
        await settle(app, pilot)
        assert "Choose a project to import its team memory." in notice(app)
        assert broker.writes() == [] and app.screen is app.screen_stack[0]


def inside(app, widget):
    region = widget.region
    return region.width > 0 and region.height > 0 and region.x >= 0 and region.y >= 0 \
        and region.right <= app.size.width and region.bottom <= app.size.height


@pytest.mark.asyncio
async def test_the_dialog_works_at_80_by_24(tmp_path, monkeypatch):
    app, broker, _ = published_app(tmp_path, monkeypatch, "pl")
    paths = [f"fact/{uuid4()}.md" for _ in range(40)]
    broker.check.update(replaces_confirmed_here={"total": 40, "records": paths})
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.action_snapshot()
        await settle(app, pilot)
        assert inside(app, app.query_one("#memory-published", Button))
        await pilot.click("#memory-published")
        await pilot.pause()
        await pilot.press("enter")
        await form_ready(app, pilot)
        assert inside(app, app.screen.query_one("#form-submit", Button))
        assert inside(app, app.screen.query_one("#form-cancel", Button))
        await checked(app, pilot)
        assert inside(app, app.screen.query_one("#confirm-yes", Button))
        assert inside(app, app.screen.query_one("#confirm-no", Button))
        body = app.screen.query_one(".cm-dialog-body")
        assert body.max_scroll_y > 0
        body.scroll_end(animate=False)
        await pilot.pause()
        assert body.scroll_y == body.max_scroll_y and paths[-1] in body_text(app)


def test_every_new_text_has_polish():
    for english, polish in MESSAGES.items():
        assert CATALOG[english] == polish
        assert polish != english or english == "Commit {commit}", english
    for sentence in (REPLACED_CHANGED, "The published memory folder must be given as a full path. "
                     "Nothing was imported.", person_proof.IMPORT_REPLACE):
        assert BROKER_MESSAGES[sentence] != sentence and CATALOG[sentence] == BROKER_MESSAGES[sentence]
