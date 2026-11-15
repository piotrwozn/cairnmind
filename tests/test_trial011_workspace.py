"""Findings of the 0.11.0 release trial in the workspace panels (#243), driven as a person would."""
from __future__ import annotations

from copy import deepcopy

import pytest
from textual.widgets import Button, DataTable, Static, TextArea

from cairnmind.config import Settings
from cairnmind.terminal_commands import JoinWorkspaceApp
from cairnmind.terminal_detail import joined, record_parts
from cairnmind import terminal_i18n
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import FormModal, MessageModal
from test_api_panels import ProfileBroker
from test_terminal_panels import settle, setup

SIZES = [(80, 24), (120, 40)]


async def wait_until(pilot, predicate, seconds=4):
    for _ in range(int(seconds / .05)):
        if predicate():
            return
        await pilot.pause(.05)
    assert predicate()


def notice_of(app):
    return str(app.query_one("#panel-notice", Static).render())


def body_of(app):
    return str(app.query_one("#entity-body", Static).render())


def inspector(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    languages = [language]
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: languages[0])
    return InspectorApp(settings, broker=broker, workspace=workspace), broker, languages


# Notice after Confirm or Dispute is hard-coded Polish ('Ocena zapisana z audytem.') in the English interface

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(("language", "saved"), [("en", "Review saved with an audit note."),
                                                 ("pl", "Ocena zapisana z audytem.")])
async def test_the_notice_after_a_review_reads_in_the_panel_language(tmp_path, monkeypatch, size, language, saved):
    app, broker, _ = inspector(tmp_path, monkeypatch, language)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        app.review("confirm")
        await wait_until(pilot, lambda: isinstance(app.screen, FormModal) and app.screen.is_mounted)
        app.screen.query_one("#field-reason", TextArea).text = "Read the source."
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert broker.record["status"] == "confirmed"
        assert notice_of(app) == saved
        assert [entry.text for entry in app.notice_history.entries][-1] == saved  # an event: kept in Notices


# Dispute dialog's submit button and Ctrl+S hint read 'Confirm review'

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(("language", "action", "label"), [
    ("en", "dispute", "Save dispute"), ("pl", "dispute", "Zapisz zakwestionowanie"),
    ("en", "confirm", "Confirm review"), ("pl", "confirm", "Potwierdź ocenę")])
async def test_the_review_dialog_names_its_own_action(tmp_path, monkeypatch, size, language, action, label):
    app, _, _ = inspector(tmp_path, monkeypatch, language)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        app.review(action)
        await wait_until(pilot, lambda: isinstance(app.screen, FormModal) and app.screen.is_mounted)
        assert str(app.screen.query_one("#form-submit", Button).label) == label
        hint = str(app.screen.query_one(".cm-hint", Static).render())
        assert hint == tr("Ctrl+S: {action} · Esc: cancel", language, action=label)
        await pilot.press("escape")


# Switching language does not re-render an inspector view that is already open

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_a_language_switch_redraws_an_open_record(tmp_path, monkeypatch, size):
    app, broker, languages = inspector(tmp_path, monkeypatch, "en")
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        assert "Status: needs_review" in body_of(app) and "Version: 3" in body_of(app)
        reads = len(broker.calls)
        languages[0] = "pl"  # More → Settings → Language → Polski, while the record is open
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        assert "Stan: do przeglądu" in body_of(app) and "Wersja: 3" in body_of(app), body_of(app)
        assert "Status:" not in body_of(app)
        assert not app.query_one("#entity-confirm", Button).disabled  # still the record, reviewable
        assert not any(path.startswith("/api/inspector/") for _, path, _, _ in broker.calls[reads:])  # not read again
        languages[0] = "en"
        await wait_until(pilot, lambda: app.language == "en")
        await settle(app, pilot)
        assert "Status: needs_review" in body_of(app)


@pytest.mark.asyncio
async def test_a_language_switch_keeps_a_saved_version_a_saved_version(tmp_path, monkeypatch):
    app, _, languages = inspector(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        app.action_history()
        await settle(app, pilot)
        app.open_selected("record-a:1")
        await settle(app, pilot)
        assert "Version: 1" in body_of(app)
        languages[0] = "pl"
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        assert "Wersja: 1" in body_of(app)
        # A saved version is never reviewed from here, in either language.
        assert app.query_one("#entity-confirm", Button).disabled and app.query_one("#entity-dispute", Button).disabled


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_a_language_switch_redraws_the_snapshot_view(tmp_path, monkeypatch, size):
    app, _, languages = inspector(tmp_path, monkeypatch, "en")
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.action_snapshot()
        await settle(app, pilot)
        assert body_of(app).startswith("Snapshots contain complete conversations and sources.")
        languages[0] = "pl"
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        assert body_of(app).startswith("Snapshoty zawierają pełne rozmowy i źródła."), body_of(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("technical", [False, True])
async def test_a_language_switch_keeps_an_import_check_on_screen_with_apply(tmp_path, monkeypatch, size, technical):
    # Review: the switch used to read the snapshot status again over the check meant to be read before Apply.
    from textual.widgets import Input
    settings, broker, workspace = setup(tmp_path, monkeypatch, project=False)
    languages = ["en"]
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: languages[0])
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.action_snapshot()
        await settle(app, pilot)
        app.memory_operation("import")
        await wait_until(pilot, lambda: isinstance(app.screen, FormModal) and app.screen.is_mounted)
        app.screen.query_one("#field-path", Input).value = "/projects/a/.memory/devbrain"
        app.screen.query_one("#field-root", Input).value = "/projects/a"
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert body_of(app).startswith("Import checked. Review the details, then choose Apply.")
        if technical:
            app.toggle_technical()
        reads = len(broker.calls)
        languages[0] = "pl"
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        if technical:
            assert '"status": "dry_run"' in body_of(app)  # still the stored structure the person chose to see
            app.toggle_technical()
        assert body_of(app).startswith(tr("Import checked. Review the details, then choose Apply.", "pl")), body_of(app)
        assert app.pending_import and not app.query_one("#memory-apply", Button).disabled
        assert broker.calls[reads:] == []  # nothing read again, nothing imported
        languages[0] = "en"
        await wait_until(pilot, lambda: app.language == "en")
        await settle(app, pilot)
        assert body_of(app).startswith("Import checked. Review the details, then choose Apply.")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_a_language_switch_says_the_open_entrys_hint_again(tmp_path, monkeypatch, size):
    hint = "History and Sources show saved evidence. Review changes confidence only."
    app, _, languages = inspector(tmp_path, monkeypatch, "en")
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        assert notice_of(app) == hint
        languages[0] = "pl"
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        assert notice_of(app) == tr(hint, "pl") != hint
        # A real notice that took the line is left alone by the next switch.
        app.notice("Something happened.")
        languages[0] = "en"
        await wait_until(pilot, lambda: app.language == "en")
        await settle(app, pilot)
        assert notice_of(app) != hint


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["checks", "jobs"])
async def test_a_language_switch_redraws_an_open_check_or_queue_entry(tmp_path, monkeypatch, kind):
    app, _, languages = inspector(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.set_view(kind)
        entry = {"id": "entry-1", "status": "failed", "version": 1}
        app.entity_kind, app.entity = kind, entry
        app.show_detail(entry, kind=kind)
        assert "Status: failed" in body_of(app) and "Version: 1" in body_of(app)
        languages[0] = "pl"
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        assert "Status:" not in body_of(app) and "Wersja: 1" in body_of(app), body_of(app)


# Record view shows the raw status ('Stan: confirmed') in Polish

@pytest.mark.parametrize(("language", "line"), [("pl", "Stan: potwierdzone"), ("en", "Status: confirmed")])
def test_the_record_view_says_its_status_in_the_panel_language(language, line):
    record = {"record": {"id": "r", "kind": "decision", "content": "Use WAL.", "status": "confirmed", "version": 2}}
    assert line in joined(record_parts(record, lambda text: tr(text, language))).splitlines()
    # A value that is not a stored word is shown as it is, never swapped for a catalog word.
    odd = {"record": {"id": "r", "kind": "decision", "content": "x", "status": "Open", "version": 1}}
    assert "Stan: Open" in joined(record_parts(odd, lambda text: tr(text, "pl"))).splitlines()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_an_open_confirmed_record_reads_potwierdzone_like_the_list(tmp_path, monkeypatch, size):
    app, broker, _ = inspector(tmp_path, monkeypatch, "pl")
    broker.record.update(kind="decision", status="confirmed", metadata={})
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        assert str(app.query_one("#inspector-rows", DataTable).get_row("record-a")[0]) == "decyzja / potwierdzone"
        app.open_selected("record-a")
        await settle(app, pilot)
        assert "Stan: potwierdzone" in body_of(app).splitlines()


# Inspector status lines flood Notices and the Activity feed; '1 items' / '1 wpisów'

@pytest.mark.parametrize(("count", "en", "pl"), [
    (0, "0 items · read only", "0 wpisów · tylko odczyt"), (1, "1 item · read only", "1 wpis · tylko odczyt"),
    (2, "2 items · read only", "2 wpisy · tylko odczyt"), (4, "4 items · read only", "4 wpisy · tylko odczyt"),
    (5, "5 items · read only", "5 wpisów · tylko odczyt"), (12, "12 items · read only", "12 wpisów · tylko odczyt"),
    (22, "22 items · read only", "22 wpisy · tylko odczyt"), (114, "114 items · read only", "114 wpisów · tylko odczyt")])
def test_a_count_of_items_takes_the_form_its_number_needs(count, en, pl):
    assert terminal_i18n.tr_count("1 item · read only", "{count} items · read only", count, "en") == en
    assert terminal_i18n.tr_count("1 item · read only", "{count} items · read only", count, "pl") == pl


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_count_and_the_hints_of_the_inspector_stay_out_of_notices(tmp_path, monkeypatch, size, language):
    app, _, _ = inspector(tmp_path, monkeypatch, language)
    published = []
    monkeypatch.setattr("cairnmind.terminal_notice_feed.publish",
                        lambda settings, workspace_id, role, rows: published.append(deepcopy(rows)))
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.notice("A real event")
        for _ in range(4):  # r, or a journal event that reloads the list
            app.action_refresh()
            await settle(app, pilot)
        assert notice_of(app) == {"en": "1 item · read only", "pl": "1 wpis · tylko odczyt"}[language]
        app.open_selected("record-a")
        await settle(app, pilot)
        assert notice_of(app) == tr("History and Sources show saved evidence. Review changes confidence only.", language)
        app.action_history()
        await settle(app, pilot)
        app.action_back()
        await settle(app, pilot)
        await pilot.pause(.7)  # the feed is copied half a second after a notice
        await settle(app, pilot)
        assert [entry.text for entry in app.notice_history.entries] == ["A real event"]
        assert all(entry.count == 1 for entry in app.notice_history.entries)
        assert published and all("read only" not in str(rows) and "tylko odczyt" not in str(rows) for rows in published)
        assert all("History and Sources" not in str(rows) and "Historia" not in str(rows) for rows in published)
        assert app.notice_history.on_line() is None  # the real event left the line: Notices counts it as unseen


# A fact extracted with metadata.state shows 'fact / todo' in the inspector list

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(("language", "fact", "task"), [("en", "fact / proposed", "task / needs_review"),
                                                         ("pl", "fakt / proponowane", "zadanie / do przeglądu")])
async def test_only_a_task_shows_its_metadata_state_in_the_list(tmp_path, monkeypatch, size, language, fact, task):
    app, broker, _ = inspector(tmp_path, monkeypatch, language)
    extracted = {**deepcopy(broker.record), "kind": "fact", "status": "proposed", "metadata": {"state": "todo"}}
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        table = app.query_one("#inspector-rows", DataTable)
        assert str(table.get_row("record-a")[0]) == task  # a task keeps its progress
        broker.record = extracted
        app.action_refresh()
        await settle(app, pilot)
        assert str(table.get_row("record-a")[0]) == fact
        app.open_selected("record-a")
        await settle(app, pilot)
        assert tr("Status", language) + ": " + fact.split(" / ")[1] in body_of(app).splitlines()


# 'Open in another terminal' dialog ignores Up/Down and shows no key hint

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(("language", "hint"), [("en", "Enter: select · Esc: back"), ("pl", "Enter: wybierz · Esc: wstecz")])
async def test_the_join_dialog_moves_with_the_arrows_and_names_its_keys(tmp_path, monkeypatch, size, language, hint):
    monkeypatch.setattr("cairnmind.terminal_i18n.language_for", lambda settings: language)
    settings = Settings(data_dir=tmp_path / "data")
    app = JoinWorkspaceApp(settings)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        shown = app.query_one(".cm-hint", Static)
        assert str(shown.render()) == hint and shown.region.height >= 1
        assert shown.region.bottom <= size[1] and shown.region.right <= size[0]  # on screen
        await pilot.press("down", "enter")
    assert app.return_value == "takeover"
    app = JoinWorkspaceApp(settings)
    async with app.run_test(size=size) as pilot:
        await pilot.press("down", "down", "up", "up", "enter")
    assert app.return_value == "observe"
    assert not settings.data_dir.exists()


# Start work with a hosted profile that has no key says only 'Operation needs attention. Connections'; Check
# connection shows the bare code 'credential_missing'

MISSING_KEY = {
    "en": "The assistant was not started. No API key is set for this connection. Open Connections, choose the "
          "connection, then Set API key.",
    "pl": "Asystent nie został uruchomiony. To połączenie nie ma klucza API. Otwórz Połączenia, wybierz "
          "połączenie, a potem Ustaw klucz API.",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_start_work_without_a_key_says_what_to_do_once(tmp_path, monkeypatch, size, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        workspace.update({"launch_errors": [{"slot_id": "codex", "client": "api", "code": "credential_missing"}]})
        await wait_until(pilot, lambda: notice_of(app) == MISSING_KEY[language])
        for _ in range(5):  # the supervisor writes the state again while nothing changes for the person
            workspace.update({"focus": "sidebar"})
            await pilot.pause(.4)
        entries = [entry for entry in app.notice_history.entries if entry.text == MISSING_KEY[language]]
        assert len(entries) == 1 and entries[0].count == 1  # not "(×23)" as on the trial
        # A code without a sentence keeps the general line.
        workspace.update({"launch_errors": [{"slot_id": "codex", "client": "codex", "code": "client_missing"}]})
        general = tr("Operation needs attention.", language) + " " + tr("Connections", language)
        await wait_until(pilot, lambda: notice_of(app) == general)


@pytest.mark.asyncio
@pytest.mark.parametrize(("language", "code", "sentence"), [
    ("en", "credential_missing", "No API key is set for this connection."),
    ("pl", "credential_missing", "To połączenie nie ma klucza API."),
    ("en", "credential_invalid", "The API key of this connection is not a valid key."),
    ("pl", "credential_invalid", "Klucz API tego połączenia nie jest poprawnym kluczem.")])
async def test_check_connection_explains_a_missing_or_invalid_key(tmp_path, monkeypatch, language, code, sentence):
    settings, inner, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    profile = {"id": "api-p", "label": "OpenAI", "client": "api", "provider": "openai", "credential_ref": "profile:api-p"}

    class Status(ProfileBroker):
        def request(self, method, path, *, params=None, json=None, timeout=20):
            if path == "/api/profiles/api-p/status":
                return {"profile_id": "api-p", "client": "api", "configured": False, "storage": "none", "code": code}
            return super().request(method, path, params=params, json=json, timeout=timeout)
    app = SidebarApp(settings, broker=Status(inner, [profile]), workspace=workspace)
    shown = []

    async def choose(screen):
        shown.append(screen)
        return "status" if not isinstance(screen, MessageModal) else None
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await app.profile_connection(profile)
    result = shown[-1].value
    from cairnmind.terminal_ui import summary_text
    text = summary_text(result, language)
    assert text.splitlines()[0].startswith(sentence) and code in text  # the code stays, after the words


# Out-of-range profile limit says only 'A profile limit is outside the allowed range.', attached to Connection name

@pytest.mark.asyncio
@pytest.mark.parametrize(("language", "field", "value", "message"), [
    ("en", "context_window_tokens", "1", "Ollama context tokens (uses RAM/VRAM): Enter a whole number from 4096 to 262144."),
    ("pl", "context_window_tokens", "1",
     "Okno kontekstu Ollamy (tokeny; zużywa RAM/VRAM): Wpisz liczbę całkowitą od 4096 do 262144."),
    ("en", "max_model_calls", "5000", "Maximum model calls per turn: Enter a whole number from 1 to 1000."),
    ("en", "command_timeout_seconds", "zero", "Command timeout (seconds): Enter a whole number from 1 to 3600.")])
async def test_an_out_of_range_limit_names_its_field_and_range(tmp_path, monkeypatch, language, field, value, message):
    settings, inner, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    profile = {"id": "ollama-p", "label": "ollama", "client": "api", "provider": "ollama", "model": "coder",
               "base_url": "http://127.0.0.1:11434"}
    broker = ProfileBroker(inner, [profile])
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.edit_profiles("ollama-p", advanced=True)
        await wait_until(pilot, lambda: isinstance(app.screen, FormModal) and app.screen.is_mounted)
        app.screen.query_one("#field-" + field).value = value
        await pilot.press("ctrl+s")
        await wait_until(pilot, lambda: str(app.screen.query_one("#form-error", Static).render()) != "")
        assert str(app.screen.query_one("#form-error", Static).render()) == message
        assert app.screen.focused.id == "field-" + field  # the field it is about, not Connection name
        await pilot.press("escape")
        await settle(app, pilot)
    assert not any(method == "PUT" for method, *_ in broker.calls)  # nothing was saved


# Test stage mapped to a check profile that does not exist is offered as runnable and fails as 'Check did not
# pass. Unknown check profile'

def test_an_unknown_check_profile_is_named_with_the_declared_ones():
    from cairnmind.verification import unknown_profile_message
    assert unknown_profile_message("nope", {"unit": {}, "lint": {}}) == (
        "Unknown check profile: nope. This worktree declares: lint, unit. See cairnmind check profiles PROJECT.")
    assert unknown_profile_message("x\x1b[2J", {}) == (
        "Unknown check profile: ?. This worktree declares no check profiles. See cairnmind check profiles PROJECT.")


@pytest.mark.parametrize(("declared", "polish"), [
    ({"unit": {}, "lint": {}}, "Nieznany profil testu: nope. Ten katalog roboczy deklaruje: lint, unit. "
                               "Zobacz cairnmind check profiles PROJECT."),
    ({}, "Nieznany profil testu: nope. Ten katalog roboczy nie deklaruje profili testów. "
         "Zobacz cairnmind check profiles PROJECT.")])
def test_the_unknown_check_profile_sentence_reads_in_the_panel_language(declared, polish):
    # Review: the sentence the check gives existed in English only.
    from types import SimpleNamespace

    from cairnmind.check_profile_messages import check_error_text
    from cairnmind.verification import unknown_profile_message
    from cairnmind.workspace_stage import _outcome, check_summary
    english = unknown_profile_message("nope", declared)
    error = "Error: Cairnmind: " + english  # as `cairnmind check run` writes it on stderr
    translate = lambda text: tr(text, "pl")  # noqa: E731
    assert check_error_text(error, translate) == polish
    assert check_error_text(error, lambda text: tr(text, "en")) == english
    never = {"passed": False, "outcome": None, "error": error}
    assert polish in check_summary(never, translate) and english not in check_summary(never, translate)
    assert _outcome(SimpleNamespace(t=translate), "nope", never).endswith(polish)
    # Any other error, or a forged name, is shown as it came and never turned into the fixed sentence.
    forged = english.replace("nope", "x{0}y")
    assert check_error_text(forged, translate) == forged
    assert check_error_text("Runner did not start.", translate) == "Runner did not start."


@pytest.mark.parametrize("language", ["en", "pl"])
def test_a_check_that_never_ran_is_not_reported_as_failed(language):
    from types import SimpleNamespace

    from cairnmind.workspace_stage import _outcome, check_summary
    translate = lambda text: tr(text, language)  # noqa: E731
    never = {"passed": False, "outcome": None, "error": "Error: Cairnmind: Unknown check profile: nope."}
    text = check_summary(never, translate)
    assert text.splitlines()[0] == translate("The check could not run") + "."
    assert translate("Check did not pass.") not in text and "Unknown check profile: nope." in text
    assert _outcome(SimpleNamespace(t=translate), "nope", never).startswith(translate("The check could not run") + ": nope")
    ran = {"id": "run-1", "passed": False, "outcome": "failed", "error": None}
    assert check_summary(ran, translate).splitlines()[0] == translate("Check did not pass.")


# Stage messages on the notice line are cut at five rows, so the Assistant (and at 80 columns the Stage) line is lost

def notice_rows(app):
    line = app.query_one("#panel-notice", Static)
    strips = app.screen._compositor.render_strips()
    region = line.region
    return "\n".join(strips[y].text[region.x:region.right] for y in range(region.y, region.bottom))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 40), (45, 40)])
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_started_stage_names_its_stage_and_assistant_on_the_notice_line(tmp_path, monkeypatch, size, language):
    from cairnmind import workspace_stage
    from test_terminal_ux080 import setup_ui
    from test_workspace_stage import answer_stage, decision
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    project = broker.projects[0]
    answer_stage(broker, decision("start_slot", project, stage="review", slot_id="slot-api", client="api",
                                  profile_id="api-profile", label="Agent", model="tool-model",
                                  credential="configured_unchecked"))
    monkeypatch.setattr(workspace_stage, "run_stage_check", lambda *a: pytest.fail("an agent stage ran a check"))
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        rows = " ".join(notice_rows(app).split())
        full = notice_of(app)
    head = tr("Stage", language) + ": " + tr("review", language) + " · " + tr("Assistant", language) + ": Agent"
    assert full.splitlines()[0] == head and head in rows, rows
    assert tr(workspace_stage.AGENT_STARTED, language) in full  # what happened, in full in Notices
    assert full.count(tr("Assistant", language) + ":") == 1 and "Build \\x1b[2J it" in full
