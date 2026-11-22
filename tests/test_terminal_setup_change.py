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
from textual.widgets import Button, Input, Static

from cairnmind import setup_change, setup_mark, setup_profiles, terminal_setup_change
from cairnmind.config import Settings
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_panels import ConfirmModal, SidebarApp
from cairnmind.terminal_setup_change import MESSAGES, change_setup, consent_text
from cairnmind.terminal_ui import ChoiceModal, FormModal, MessageModal
from consent_steps import consent_drawn, early_enter_sends_nothing
from test_setup_change import Rig
from test_setup_profiles import DB, MODELS, config_bytes, connect_only_installation, make, private
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
        return types.SimpleNamespace(app=app, rig=rig, broker=broker, helper=helper, begun=begun, tmp=tmp_path)
    return build


async def until(pilot, condition, seconds=8.0):
    for _ in range(int(seconds / .02)):
        await pilot.pause(.02)
        if condition():
            return
    raise AssertionError("condition not reached")


def on_screen(app, kind):
    return lambda: isinstance(app.screen, kind)


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
        await until(pilot, on_screen(app, FormModal))
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
            await until(pilot, on_screen(app, FormModal))
            await fill(app, pilot, **remote_db(tmp))
            await consent_drawn(app, pilot)
            kind, cancel = ConfirmModal, "#confirm-no"
        if how == "escape":
            await pilot.press("escape")
        else:
            await pilot.pause()  # the layout of the dialog is done before the button is clicked
            await pilot.click(cancel)
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
        await until(pilot, on_screen(app, FormModal))
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
        assert "Jobs are in work now (demo (ingest))" in " ".join(screen_text(app).split())
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
        await until(pilot, on_screen(app, FormModal))
        await fill(app, pilot, **remote_db(tmp))
        await until(pilot, on_screen(app, MessageModal))
        assert "Another installation writes the store at" in " ".join(screen_text(app).split())
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
        assert "already run as `cairnmind setup local` says; nothing to change" in " ".join(screen_text(app).split())
        await pilot.press("escape")
        await settle(app, pilot)
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
        await until(pilot, on_screen(app, FormModal))
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

def report(tmp, state, lines, pid=Helper.pid):
    (tmp / "data" / setup_change.PROGRESS).write_text(json.dumps(
        {"pid": pid, "state": state, "language": "en", "lines": lines}))


@pytest.mark.asyncio
async def test_progress_and_the_result_are_shown_and_the_panel_reconnects(ui):
    rig = ui()
    app, tmp, broker = rig.app, rig.tmp, rig.broker
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.change("remote-db")
        await until(pilot, on_screen(app, FormModal))
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
        assert "cannot be stopped halfway" in " ".join(screen_text(app).split())
        report(tmp, "changed", ["Stopping the knowledge service of this workspace for the change.",
                                "The change is made and the knowledge service runs again with the new settings."])
        await until(pilot, lambda: not modal.query_one("#progress-close", Button).disabled)
        assert modal.final == "changed" and "Esc: close" in screen_text(app)
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
        await until(pilot, on_screen(app, FormModal))
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
        text = " ".join(screen_text(app).split())
        assert "ended without saying how (exit status 7)" in text and "Stopping the knowledge service" in text
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, modal_class))


# 80 x 24 ---------------------------------------------------------------------------------------------------------------

def inside(app, *identifiers, width=80, height=24):
    for identifier in identifiers:
        region = app.screen.query_one(identifier, Button).region
        assert region.width > 0 and region.x >= 0 and region.right <= width and region.bottom <= height, identifier


@pytest.mark.asyncio
async def test_the_flow_fits_at_80_by_24(ui):
    rig = ui()
    app, tmp = rig.app, rig.tmp
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.change(None)
        await until(pilot, on_screen(app, ChoiceModal))
        inside(app, "#choice-cancel")
        assert "(remote)" in screen_text(app)
        await pilot.press("down", "down", "enter")  # remote: the longest form
        await until(pilot, on_screen(app, FormModal))
        await pilot.pause()
        inside(app, "#form-submit", "#form-cancel")
        last = app.screen.query_one("#field-credential_ref", Input)
        app.screen.query_one("#form-fields").scroll_end(animate=False)
        await pilot.pause()
        assert last.region.bottom <= 24 and last.region.y >= 0  # the last field scrolls into view
        for name, value in remote(tmp).items():
            app.screen.query_one("#field-" + name, Input).value = value
        await pilot.press("ctrl+s")
        modal = await consent_drawn(app, pilot)
        inside(app, "#confirm-yes", "#confirm-no")
        body = app.screen.query_one(".cm-dialog-body")
        assert body.max_scroll_y > 0
        body.scroll_end(animate=False)
        await pilot.pause()
        last_line = [line for line in modal.value.splitlines() if line.strip()][-1]
        assert last_line[:30] in screen_text(app)
        await pilot.click("#confirm-yes")
        await until(pilot, on_screen(app, terminal_setup_change._progress_class()))
        await pilot.pause()
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
