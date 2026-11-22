"""The workspace's dialogs for a change of where storage and models run (#339).

Headless Textual with the profiles' World of test_setup_profiles standing in for the stores and models, and a fake
helper: no engine, no tmux, no Docker, no service. Every test runs unchanged on macOS.
"""
from __future__ import annotations

import asyncio
import json
import string
import types

import pytest
from textual import work
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Static

from cairnmind import database_credentials, setup_change, setup_mark, setup_profiles, terminal_setup_change
from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_panels import ConfirmModal, SidebarApp
from cairnmind.terminal_setup_change import MESSAGES, change_setup, consent_text
from cairnmind.terminal_ui import ChoiceModal, FormModal, MessageModal
from consent_steps import consent_drawn, early_enter_sends_nothing, form_ready
from test_setup_change import Rig
from test_setup_profiles import (DB, MODELS, RELEASE_CHAT, RELEASE_EMBEDDING, config_bytes, connect_only_installation,
                                 make, private)
from test_terminal_panels import settle
from test_terminal_ux080 import UIBroker, screen_text, setup_ui


class Broker(UIBroker):
    reconnects = 0

    def reconnect(self):
        self.reconnects += 1


class Panel(SidebarApp):
    """The panel as the screen of Storage and models uses it: the flow runs from a worker and shows what it raises."""

    @work(group="test-change", exclusive=True, exit_on_error=False)
    async def change(self, profile=None):
        try:
            await change_setup(self, profile)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)


class Helper:
    """The process `begin` would have started: its progress file is written by the test."""
    pid = 4242

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


@pytest.fixture
def ui(tmp_path, monkeypatch):
    def build(language="en", **installation):
        settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
        rig = Rig(tmp_path, monkeypatch)
        broker = Broker(next(iter(broker.projects)))
        if installation.pop("connect_only", False):
            connect_only_installation(tmp_path, **installation)
        else:
            make(tmp_path, **installation)
        helper, begun = Helper(), []
        monkeypatch.setattr(setup_change, "begin", lambda *args: begun.append(args) or helper)
        app = Panel(settings, broker=broker, workspace=workspace)
        return types.SimpleNamespace(app=app, rig=rig, broker=broker, helper=helper, begun=begun, tmp=tmp_path,
                                     reopen=lambda: Panel(settings, broker=broker, workspace=workspace))
    return build


async def until(pilot, condition, seconds=8.0, what="the condition"):
    """Wait, bounded, for ``condition``; the timeout says what the screen showed instead."""
    for _ in range(int(seconds / .02)):
        await pilot.pause(.02)
        if condition():
            return
    app = pilot.app
    raise AssertionError(f"{what} was not reached within {seconds} s; the top screen is {type(app.screen).__name__} "
                         f"and shows: {' '.join(screen_text(app).split())[:400]}")


def laid_out(app, kind):
    """The top screen is a ``kind`` that is mounted and laid out: a key or a click before that goes to the screen below,
    or (a click on a widget whose region is empty) to the corner, and nothing says so."""
    screen = app.screen
    if not isinstance(screen, kind) or not screen.is_attached:
        return False
    try:
        buttons = list(screen.query(Button))
        if not buttons or any(button.region.width <= 0 for button in buttons):
            return False
        if isinstance(screen, ChoiceModal):  # rows added and the table focused, as the dialog's mount does
            table = screen.query_one("#choices")
            return table.row_count == len(screen.choices) and app.focused is table
        if isinstance(screen, FormModal):
            return isinstance(app.focused, Input) and all(
                screen.query_one("#field-" + field.name).region.width > 0 for field in screen.fields)
    except NoMatches:
        return False
    return True


def on_screen(app, kind):
    return lambda: laid_out(app, kind)


async def click(pilot, selector):
    """Click a button of the top screen once it has a region and can be pressed."""
    def ready():
        try:
            button = pilot.app.screen.query_one(selector)
        except NoMatches:
            return False
        return button.region.width > 0 and button.region.height > 0 and not button.disabled
    await until(pilot, ready, what=f"{selector} laid out and enabled")
    await pilot.click(selector)


async def fill(app, pilot, **values):
    for name, value in values.items():
        app.screen.query_one("#field-" + name, Input).value = value
    await pilot.press("ctrl+s")


def remote_db(tmp_path):
    return {"url": DB, "user": "cairnmind", "database": "cairnmind",
            "password_file": private(tmp_path, "db-password", "PRIVATE_DB_MARKER")}


def remote(tmp_path):
    return {**remote_db(tmp_path), "models_url": MODELS, "provider": "openai_compatible", "chat_model": "gpt-x",
            "embedding_model": "emb-y"}


def unchanged(tmp_path, before):
    assert config_bytes(tmp_path) == before
    assert not (tmp_path / "data" / setup_mark.MARK).exists()


# The consent dialog ---------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_consent_dialog_shows_the_plan_and_where_content_goes(ui, language):
    rig = ui(language, knowledge_agent="disabled")
    app, tmp = rig.app, rig.tmp
    before = config_bytes(tmp)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote(tmp))
        modal = await consent_drawn(app, pilot)
        shown = modal.value

        def say(text, **values):
            return tr(text, language, **values)
        assert say(terminal_setup_change.WHERE) in shown
        assert say(terminal_setup_change.STORE_AT, url=DB) in shown
        host = "models.example"
        assert setup_profiles.render_one((setup_profiles.SENDS, {"host": host, "classes": "project"}), language) in shown
        assert say(terminal_setup_change.DOWNTIME) in shown and say(terminal_setup_change.GOES_ON) in shown
        assert say(terminal_setup_change.PLAN) in shown
        assert terminal_setup_change.KEEP_OPEN not in shown  # only a way back with a backup
        result = await asyncio.to_thread(setup_profiles.run, Settings.load(tmp / "data"), setup_profiles.Request(
            "remote", **{**remote(tmp), "allow_remote": True}), yes=False, say=lambda text: None)
        for item in (*result["tests"], *[i for i in result["changes"] if i[0] != setup_profiles.SENDS], *result["writer"],
                     *result["rebuild"], *result["left_behind"]):
            assert setup_profiles.render_one(item, language) in shown, item
        assert rig.begun == []
        await early_enter_sends_nothing(app, pilot, modal)
        assert rig.begun == []
        await pilot.click("#confirm-yes")
        await until(pilot, lambda: rig.begun)
        assert len(rig.begun) == 1
        settings, request, expected, spoken = rig.begun[0]
        assert request.profile == "remote" and request.allow_remote is True and request.password_file.endswith("db-password")
        assert expected == setup_change.agreed_plan(result) and spoken == language
        assert settings.data_dir == tmp / "data"
        assert isinstance(app.screen, terminal_setup_change._progress_class())
        assert config_bytes(tmp) == before  # the dialogs write nothing; the helper does


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["choice", "form", "back", "consent"])
@pytest.mark.parametrize("how", ["escape", "cancel"])
async def test_cancel_and_esc_change_nothing(ui, stage, how):
    rig = ui(connect_only=stage == "back")
    app, tmp = rig.app, rig.tmp
    before = config_bytes(tmp)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        if stage == "choice":
            app.change(None)
            kind, cancel = ChoiceModal, "#choice-cancel"
            await until(pilot, on_screen(app, kind))
        elif stage == "form":
            app.change("remote-db")
            kind, cancel = FormModal, "#form-cancel"
            await until(pilot, on_screen(app, kind))
        elif stage == "back":
            app.change("local")
            kind, cancel = ChoiceModal, "#choice-cancel"
            await until(pilot, on_screen(app, kind))
        else:
            app.change("remote-db")
            await form_ready(app, pilot)
            await fill(app, pilot, **remote_db(tmp))
            await consent_drawn(app, pilot)
            kind, cancel = ConfirmModal, "#confirm-no"
        if how == "escape":
            await pilot.press("escape")
        else:
            await click(pilot, cancel)
        await until(pilot, lambda: not isinstance(app.screen, kind))
        await settle(app, pilot)
    assert rig.begun == []
    unchanged(tmp, before)
    assert rig.rig.shutdowns == [] and rig.rig.alive


# The forms -------------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_form_takes_paths_and_a_reference_and_no_password(ui):
    rig = ui()
    app = rig.app
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote")
        await until(pilot, on_screen(app, FormModal))
        names = [field.name for field in app.screen.fields]
        assert names == ["url", "user", "database", "password_file", "ca_file", "admin_password_file", "models_url",
                         "provider", "chat_model", "embedding_model", "credential_ref"]
        assert not any(widget.password for widget in app.screen.query(Input))  # a secret is never typed here
        assert "no field takes a password or a key" in app.screen.note.lower() or "No field takes" in app.screen.note
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, FormModal))
        app.change("remote-db")
        await until(pilot, on_screen(app, FormModal))
        assert [field.name for field in app.screen.fields][-2:] == ["ca_file", "admin_password_file"]


def test_a_typed_form_becomes_a_request_with_the_consent_of_the_dialog_for_remote_only(tmp_path):
    values = {"url": DB, "user": "u", "database": "d", "password_file": "~/p", "ca_file": "", "admin_password_file": ""}
    request = terminal_setup_change._request("remote-db", values)
    assert request.ca_file is None and request.admin_password_file is None and request.allow_remote is False
    assert request.password_file.endswith("/p") and not request.password_file.startswith("~")
    assert terminal_setup_change._request("remote", values).allow_remote is True
    assert terminal_setup_change._request("local", {}, empty=True).empty is True


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_refusal_of_the_test_is_shown_on_the_form_and_nothing_begins(ui, language):
    rig = ui(language)
    app, tmp = rig.app, rig.tmp
    before = config_bytes(tmp)
    rig.rig.world.block = {"available": False, "error": "refused", "database_available": False}
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp))
        await until(pilot, lambda: str(app.screen.query_one("#form-error", Static).render()) != "")
        error = str(app.screen.query_one("#form-error", Static).render())
        assert error.startswith("Nie można użyć bazy" if language == "pl" else "The store at")
        assert isinstance(app.screen, FormModal)
        await pilot.press("escape")
    assert rig.begun == []
    unchanged(tmp, before)


@pytest.mark.asyncio
async def test_a_refusal_before_the_form_is_shown_by_name_and_no_form_opens(ui, monkeypatch):
    rig = ui()
    app = rig.app
    monkeypatch.setattr(setup_change, "refusal", lambda settings: (setup_change.JOBS_IN_WORK_ANY, {"jobs": "demo (ingest)"}))
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote")
        await until(pilot, on_screen(app, MessageModal))
        await until(pilot, lambda: "Jobs are in work now (demo (ingest))" in " ".join(screen_text(app).split()), what="the refusal by name")
        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, FormModal) and rig.begun == []


@pytest.mark.asyncio
async def test_a_store_written_by_another_installation_is_refused_after_the_test(ui):
    rig = ui()
    app, tmp = rig.app, rig.tmp
    rig.rig.world.writer = {"role": "reader", "reason": "Another installation writes it."}
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp))
        await until(pilot, on_screen(app, MessageModal))
        await until(pilot, lambda: "Another installation writes the store at" in " ".join(screen_text(app).split()), what="the refusal of the target")
        await pilot.press("escape")
        await settle(app, pilot)
    assert rig.begun == []


@pytest.mark.asyncio
async def test_nothing_to_change_is_said_and_nothing_begins(ui):
    rig = ui()
    app = rig.app
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("local")
        await until(pilot, on_screen(app, MessageModal))
        await until(pilot, lambda: "already run as `cairnmind setup local` says; nothing to change" in " ".join(screen_text(app).split()),
                    what="the nothing-to-change sentence")
        await pilot.press("escape")
        await settle(app, pilot)
    assert rig.begun == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["http://127.0.0.1:11434", "http://localhost:11434"])
async def test_the_same_models_at_the_same_place_are_nothing_to_change_with_no_dialog_and_no_stop(ui, stored):
    rig = ui(connect_only=True, knowledge_agent="enabled", ollama_url=stored)
    app, tmp = rig.app, rig.tmp
    before = config_bytes(tmp)
    asked = "http://localhost:11434" if stored.startswith("http://127") else "http://127.0.0.1:11434"  # the other name
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp), models_url=asked, provider="ollama", chat_model=RELEASE_CHAT,
                   embedding_model=RELEASE_EMBEDDING)
        await until(pilot, on_screen(app, MessageModal))
        await until(pilot, lambda: "already run as `cairnmind setup remote` says; nothing to change" in " ".join(
            screen_text(app).split()), what="the nothing-to-change sentence")
        assert not any(isinstance(screen, ConfirmModal) for screen in app.screen_stack)  # no consent dialog
        await pilot.press("escape")
        await settle(app, pilot)
    assert rig.begun == [] and rig.rig.shutdowns == [] and rig.rig.starts == []  # no helper, no stop, no start
    unchanged(tmp, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_consent_dialog_shows_what_the_administrators_file_will_create(ui, language):
    rig = ui(language)
    app, tmp = rig.app, rig.tmp
    admin = private(tmp, "root-password", "PRIVATE_ADMIN_MARKER")
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp), admin_password_file=admin)
        modal = await consent_drawn(app, pilot)
        shown = " ".join(modal.value.split())
        for sentence in (setup_profiles.TEST_ADMIN_OK, setup_profiles.CREATE_BOTH):
            assert " ".join(tr(sentence, language, url=DB, version="26.11.1", user="cairnmind",
                               database="cairnmind").split()) in shown
        assert "PRIVATE_ADMIN_MARKER" not in modal.value
        await pilot.click("#confirm-no")
    assert rig.begun == [] and rig.rig.world.calls("create_user") == []


@pytest.mark.asyncio
async def test_a_refused_administrator_is_shown_on_the_form_before_any_dialog(ui, monkeypatch):
    rig = ui()
    app, tmp = rig.app, rig.tmp
    admin = private(tmp, "root-password", "PRIVATE_ADMIN_MARKER")

    def refused(candidate, admin_secret):
        raise ArcadeError("refused", status_code=401)
    monkeypatch.setattr(database_credentials, "plan_user", refused)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp), admin_password_file=admin)
        await until(pilot, lambda: str(app.screen.query_one("#form-error", Static).render()) != "")
        assert isinstance(app.screen, FormModal) and "The administrator was refused" in str(
            app.screen.query_one("#form-error", Static).render())
        await pilot.press("escape")
    assert rig.begun == []


@pytest.mark.asyncio
async def test_going_back_to_local_asks_backup_or_empty_and_says_to_keep_the_workspace_open(ui):
    rig = ui(connect_only=True)
    app, tmp = rig.app, rig.tmp
    before = config_bytes(tmp)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("local")
        await until(pilot, on_screen(app, ChoiceModal))
        assert [label for label, _ in app.screen.choices] == [terminal_setup_change.BACK_BACKUP,
                                                              terminal_setup_change.BACK_EMPTY]
        await pilot.press("enter")  # the backup
        await form_ready(app, pilot)
        assert [field.name for field in app.screen.fields] == ["backup"]
        target = tmp / "remote.cairnmind.json.gz"
        await fill(app, pilot, backup=str(target))
        modal = await consent_drawn(app, pilot)
        assert terminal_setup_change.KEEP_OPEN in modal.value
        assert setup_profiles.RESTORE_WILL_KEEP in modal.value or setup_profiles.RESTORE_WILL_CLAIM[:60] in modal.value
        assert terminal_setup_change.STORE_HERE.split("{")[0] in modal.value
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        app.change("local")
        await until(pilot, on_screen(app, ChoiceModal))
        await pilot.press("down", "enter")  # without the data
        modal = await consent_drawn(app, pilot)
        assert terminal_setup_change.KEEP_OPEN not in modal.value
        await pilot.click("#confirm-yes")
        await until(pilot, lambda: rig.begun)
    request = rig.begun[0][1]
    assert request.profile == "local" and request.empty is True and request.backup is None
    assert not target.exists() and config_bytes(tmp) == before  # the dry run writes no backup


# The progress ----------------------------------------------------------------------------------------------------------

def report(tmp, state, lines, pid=Helper.pid, language="en", **extra):
    (tmp / "data" / setup_change.PROGRESS).write_text(json.dumps(
        {"pid": pid, "state": state, "language": language, "lines": lines, **extra}))


@pytest.mark.asyncio
async def test_progress_and_the_result_are_shown_and_the_panel_reconnects(ui):
    rig = ui()
    app, tmp, broker = rig.app, rig.tmp, rig.broker
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp))
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        modal_class = terminal_setup_change._progress_class()
        await until(pilot, on_screen(app, modal_class))
        modal = app.screen
        assert modal.query_one("#progress-close", Button).disabled
        report(tmp, "running", ["Stopping the knowledge service of this workspace for the change."])
        await until(pilot, lambda: "Stopping the knowledge service" in screen_text(app))
        await pilot.press("escape")  # nothing here can stop the helper
        await pilot.pause(.1)
        assert app.screen is modal and not modal.final
        await until(pilot, lambda: "cannot be stopped halfway" in " ".join(screen_text(app).split()), what="the hint")
        report(tmp, "changed", ["Stopping the knowledge service of this workspace for the change.",
                                "The change is made and the knowledge service runs again with the new settings."])
        await until(pilot, lambda: not modal.query_one("#progress-close", Button).disabled)
        await until(pilot, lambda: modal.final == "changed" and "Esc: close" in screen_text(app), what="the closing hint")
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, modal_class))
        await settle(app, pilot)
        assert broker.reconnects == 1  # the panel asks the broker again, as it does after a restart
        assert any(call[1] == "/api/state" for call in broker.calls[-3:])  # and reads the sidebar again
        notice = str(app.query_one("#panel-notice", Static).render())
    assert "the knowledge service runs again with the new settings" in notice


@pytest.mark.asyncio
async def test_a_progress_report_of_another_process_is_not_shown_and_a_silent_end_is_said(ui):
    rig = ui()
    app, tmp = rig.app, rig.tmp
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp))
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        modal_class = terminal_setup_change._progress_class()
        await until(pilot, on_screen(app, modal_class))
        modal = app.screen
        report(tmp, "changed", ["Somebody else's report."], pid=1)
        await pilot.pause(1.2)
        assert "Somebody else's report." not in screen_text(app) and not modal.final
        report(tmp, "running", ["Stopping the knowledge service of this workspace for the change."])
        rig.helper.code = 7
        await until(pilot, lambda: modal.final == "ended")
        await until(pilot, lambda: "ended without saying how (exit status 7)" in " ".join(screen_text(app).split())
                    and "Stopping the knowledge service" in screen_text(app), what="the silent end said on the dialog")
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, modal_class))


# The end of a change that nobody watched --------------------------------------------------------------------------------

ENGLISH = [setup_change.STOPPING, setup_change.CHANGED]
POLISH = [setup_change.MESSAGES[sentence] for sentence in ENGLISH]


def finished_outcome(tmp, state="changed", language="en", **extra):
    """The progress file of a helper that has ended, with its lines in both languages as the helper writes them."""
    shown, other = (ENGLISH, POLISH) if language == "en" else (POLISH, ENGLISH)
    report(tmp, state, shown, language=language, other=other, **extra)


def seen(app):
    return " ".join(screen_text(app).split())


def recorded(tmp):
    return json.loads((tmp / "data" / setup_change.PROGRESS).read_text())


async def opened_without_a_dialog(app):
    """An opening of the workspace in which no outcome is shown: the worker that looks for one has finished. The wait
    is bounded, and ends at a dialog on top: a dialog shown by mistake keeps its worker waiting for ever."""
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, MessageModal) or all(
            worker.is_finished for worker in app.workers if worker.group == "setup-outcome"), what="the look for an outcome")
        assert not isinstance(app.screen, MessageModal), seen(app)
        await settle(app, pilot)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_finished_outcome_nobody_saw_is_shown_once_at_the_next_opening(ui, language):
    rig = ui(language)
    app, tmp = rig.app, rig.tmp
    finished_outcome(tmp, "changed", "en")  # the helper's language was English; the workspace opens in `language`
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, on_screen(app, MessageModal), what="the outcome of the last change")
        shown = ENGLISH if language == "en" else POLISH
        await until(pilot, lambda: all(line[:28] in seen(app) for line in shown), what="the outcome's sentences")
        title = tr(terminal_setup_change.OUTCOME_TITLE, language)
        await until(pilot, lambda: title in seen(app), what="the title of the outcome dialog")
        assert all(line[:28] not in seen(app) for line in (POLISH if language == "en" else ENGLISH))
        assert recorded(tmp)["shown"] is True  # recorded in the file, at once: it appears once, not each time
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, MessageModal), what="the dialog closed")
        await settle(app, pilot)
    assert rig.begun == [] and (tmp / "data" / setup_change.PROGRESS).exists()  # nothing was removed or started
    await opened_without_a_dialog(rig.reopen())  # the workspace opened again: not shown again


@pytest.mark.asyncio
async def test_the_outcome_seen_in_the_progress_dialog_is_not_shown_again(ui):
    rig = ui()
    app, tmp = rig.app, rig.tmp
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await form_ready(app, pilot)
        await fill(app, pilot, **remote_db(tmp))
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        modal_class = terminal_setup_change._progress_class()
        await until(pilot, on_screen(app, modal_class))
        finished_outcome(tmp, "changed")
        await until(pilot, lambda: app.screen.final == "changed", what="the end in the progress dialog")
        assert recorded(tmp)["shown"] is True
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, modal_class), what="the progress dialog closed")
        await settle(app, pilot)
    await opened_without_a_dialog(rig.reopen())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["mark_running", "mark_unreadable", "not_finished"])
async def test_a_change_that_still_runs_shows_no_end(ui, monkeypatch, case):
    rig = ui()
    finished_outcome(rig.tmp, "running" if case == "not_finished" else "changed")
    if case != "not_finished":
        monkeypatch.setattr(setup_mark, "state", lambda data_dir: (case.removeprefix("mark_"), 4242))
    await opened_without_a_dialog(rig.app)
    assert "shown" not in recorded(rig.tmp)  # nobody was shown it: it is shown when the change has ended


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(setup_change.EXIT))
async def test_each_final_state_is_shown_in_polish_with_no_english_inside(ui, state):
    rig = ui("pl")
    finished_outcome(rig.tmp, state, "en")
    app = rig.app
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, on_screen(app, MessageModal), what=f"the outcome {state}")
        await until(pilot, lambda: POLISH[0][:28] in seen(app) and tr(terminal_setup_change.OUTCOME_TITLE, "pl") in seen(app),
                    what="the Polish sentences and title")
        assert ENGLISH[0][:20] not in seen(app) and ENGLISH[1][:20] not in seen(app)
        await pilot.press("escape")


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_kept_copy_is_named_and_showing_the_outcome_does_not_remove_it(ui, language):
    rig = ui(language)
    app, tmp = rig.app, rig.tmp
    finished_outcome(tmp, "service_down", language)
    copy = tmp / "data" / setup_mark.BEFORE
    copy.write_text("{}")
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, on_screen(app, MessageModal), what="the outcome with the kept copy")
        sentence = setup_mark.unchecked(tmp / "data")  # the sentence doctor and the refusal to start give
        fragment = tr(sentence.template, language)[:30]
        await until(pilot, lambda: fragment in seen(app), what="the sentence about the kept copy")
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, MessageModal), what="the dialog closed")
    assert copy.exists() and recorded(tmp)["shown"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["{", "[]", '{"pid": 7, "state": "changed", "lines": "x", "language": "en"}', ""])
async def test_an_outcome_file_that_cannot_be_read_shows_nothing_and_the_opening_goes_on(ui, raw):
    rig = ui()
    (rig.tmp / "data" / setup_change.PROGRESS).write_text(raw)
    await opened_without_a_dialog(rig.app)
    assert (rig.tmp / "data" / setup_change.PROGRESS).read_text() == raw  # left as it was for a person to look at


@pytest.mark.asyncio
async def test_the_outcome_dialog_fits_80_by_24_and_scrolls(ui):
    rig = ui()
    app, tmp = rig.app, rig.tmp
    lines = [f"Line {number} of the outcome of the change." for number in range(40)]
    report(tmp, "failed", lines, other=lines)
    async with app.run_test(size=(80, 24)) as pilot:
        await until(pilot, on_screen(app, MessageModal), what="the outcome dialog")
        inside(app, "#message-close")
        body = app.screen.query_one(".cm-dialog-body")
        await until(pilot, lambda: body.max_scroll_y > 0, what="a body that scrolls")
        body.scroll_end(animate=False)
        await until(pilot, lambda: "Line 39" in seen(app), what="the last line of the outcome")
        await click(pilot, "#message-close")
        await until(pilot, lambda: not isinstance(app.screen, MessageModal), what="the dialog closed")


# 80 x 24 ---------------------------------------------------------------------------------------------------------------

def inside(app, *identifiers, width=80, height=24):
    for identifier in identifiers:
        region = app.screen.query_one(identifier, Button).region
        assert region.width > 0 and region.x >= 0 and region.right <= width and region.bottom <= height, identifier


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_no_label_of_the_form_is_cut_at_80_by_24(ui, language):
    rig = ui(language)
    app = rig.app
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.change(None)
        await until(pilot, on_screen(app, ChoiceModal))
        await pilot.press("down", "down", "enter")
        await until(pilot, on_screen(app, FormModal))
        fields = app.screen.query_one("#form-fields")
        shown = set()
        for _ in range(12):  # scroll through the form: each label must be whole where it is drawn
            shown.update(line.strip() for line in screen_text(app).splitlines())
            fields.scroll_page_down(animate=False)
            await pilot.pause()
        label = tr(terminal_setup_change.F_ADMIN_FILE, language)
        assert any(label in line for line in shown), label


@pytest.mark.asyncio
async def test_the_flow_fits_at_80_by_24(ui):
    rig = ui()
    app, tmp = rig.app, rig.tmp
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.change(None)
        await until(pilot, on_screen(app, ChoiceModal))
        inside(app, "#choice-cancel")
        await until(pilot, lambda: "(remote)" in screen_text(app), what="the choice of profiles")
        await pilot.press("down", "down", "enter")  # remote: the longest form
        await until(pilot, on_screen(app, FormModal))
        inside(app, "#form-submit", "#form-cancel")
        last = app.screen.query_one("#field-credential_ref", Input)
        app.screen.query_one("#form-fields").scroll_end(animate=False)
        await until(pilot, lambda: 0 <= last.region.y and last.region.bottom <= 24, what="the last field scrolled into view")
        for name, value in remote(tmp).items():
            app.screen.query_one("#field-" + name, Input).value = value
        await pilot.press("ctrl+s")
        modal = await consent_drawn(app, pilot)
        inside(app, "#confirm-yes", "#confirm-no")
        body = app.screen.query_one(".cm-dialog-body")
        assert body.max_scroll_y > 0
        body.scroll_end(animate=False)
        last_line = [line for line in modal.value.splitlines() if line.strip()][-1]
        await until(pilot, lambda: last_line[:30] in screen_text(app), what="the last line of the plan")
        await pilot.click("#confirm-yes")
        await until(pilot, on_screen(app, terminal_setup_change._progress_class()))
        inside(app, "#progress-close")
        report(tmp, "failed", ["The change was made, but it failed."])
        await until(pilot, lambda: not app.screen.query_one("#progress-close", Button).disabled)
        await pilot.press("escape")
        await settle(app, pilot)


# The status row, the sentences -----------------------------------------------------------------------------------------

def test_the_status_row_rereads_config_json_when_it_changes(tmp_path):
    from cairnmind.workspace import WorkspaceManager
    make(tmp_path)
    manager = types.SimpleNamespace(settings=Settings.load(tmp_path / "data"))
    first = WorkspaceManager._place(manager)
    assert WorkspaceManager._place(manager) is first  # not read again while the file is as it was
    config = json.loads((tmp_path / "data" / "config.json").read_text())
    config.update({"arcade_connect_url": DB, "arcade_user": "u", "arcade_database": "d"})
    (tmp_path / "data" / "config.json").write_text(json.dumps(config))
    changed = WorkspaceManager._place(manager)
    assert changed is not first and changed["lines"][0] != first["lines"][0] and "db.example" in changed["lines"][0]
    (tmp_path / "data" / "config.json").unlink()
    assert WorkspaceManager._place(manager) is changed  # a file that cannot be read keeps the last answer


def test_the_status_row_keeps_its_last_answer_while_config_json_does_not_parse(tmp_path):
    """The call site too: what the supervisor draws each moment is built from _place, which never raises."""
    from cairnmind.workspace import WorkspaceManager
    make(tmp_path)
    path = tmp_path / "data" / "config.json"
    manager = types.SimpleNamespace(settings=Settings.load(tmp_path / "data"), panes=lambda: {}, _border_language="en",
                                    _broker_state=None, _last_status=None,
                                    tmux=lambda *args, **kwargs: types.SimpleNamespace(stdout=""))
    manager._place = lambda: WorkspaceManager._place(manager)

    def draw():
        WorkspaceManager._update_status(manager, {})
        return json.dumps(manager._last_status)
    first = draw()
    good = path.read_text()
    config = json.loads(good)
    config.update({"arcade_connect_url": DB, "arcade_user": "u", "arcade_database": "d"})
    path.write_text(json.dumps(config))
    moved = draw()
    assert moved != first and "db.example" in moved  # the row follows config.json
    path.write_text('{"arcade_connect_url": ')  # a hand edit in progress
    assert draw() == moved
    path.write_text(json.dumps(config) + " ")  # edited again, and valid
    assert draw() == moved


def test_every_dialog_sentence_has_polish_with_the_same_placeholders():
    names = {name for name, value in vars(terminal_setup_change).items()
             if isinstance(value, str) and name.isupper() and name != "PATHS"}
    sentences = {getattr(terminal_setup_change, name) for name in names}
    assert len(sentences) >= 35 and sentences == set(MESSAGES)
    for english in sentences:
        def holes(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}
        polish = MESSAGES[english]
        assert holes(polish) == holes(english), english
        assert CATALOG[english] == polish
    assert setup_change.__name__  # the helper's own sentences are tested with the helper


def test_the_consent_text_of_a_local_change_says_the_store_is_here(tmp_path, monkeypatch):
    make(tmp_path, knowledge_agent="disabled")
    settings = Settings.load(tmp_path / "data")
    result = {"tests": [], "changes": [(setup_profiles.NOTHING_TO_DO, {"profile": "local"})], "writer": [],
              "rebuild": [], "left_behind": []}
    text = consent_text(result, setup_profiles.Request("local", backup="/b"), settings, "en")
    assert "stored on this computer, in " in text and "stays off (deterministic-only mode)" in text
    assert terminal_setup_change.KEEP_OPEN in text
    assert "Przechowywana" not in text and "Cała pamięć jest przechowywana na tym komputerze" in consent_text(
        result, setup_profiles.Request("local"), settings, "pl")
