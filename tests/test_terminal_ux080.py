"""Keyboard journeys for 0.8.0. No database, model, broker daemon or shell."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from textual import work
from textual.binding import Binding
from textual.widgets import Button, DataTable, Input, Static

from cairnmind.config import Settings
from cairnmind.terminal_i18n import language_for, tr
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import BasePanel, ChoiceModal, Field, FieldError, FolderPicker, FormModal, MessageModal
from test_terminal_panels import FakeBroker, FakeWorkspace, settle
from test_client_runtime import native as native


def test_binding_localization_preserves_routing_and_roundtrips():
    from textual.app import App
    from cairnmind.terminal_i18n import localize_bindings

    class BindingApp(App):
        BINDINGS = [Binding("ctrl+s", "save", "Save", priority=True, id="save", tooltip="Save",
                            group=Binding.Group("Next", compact=True))]

    app = BindingApp()
    before = app._bindings.key_to_bindings["ctrl+s"][0]
    localize_bindings(app, "pl")
    polish = app._bindings.key_to_bindings["ctrl+s"][0]
    assert (polish.description, polish.tooltip, polish.group.description) == ("Zapisz", "Zapisz", "Dalej")
    assert (polish.key, polish.action, polish.priority, polish.id, polish.group.compact) == ("ctrl+s", "save", True, "save", True)
    localize_bindings(app, "en")
    assert app._bindings.key_to_bindings["ctrl+s"] == [before]
    assert BasePanel.ENABLE_COMMAND_PALETTE is False


@pytest.mark.asyncio
async def test_footer_follows_language_change_without_builtin_palette(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, "pl")
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app._bindings.key_to_bindings["w"][0].description == "Rozpocznij pracę"
        assert "Palette" not in screen_text(app)
        monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: "en")
        await app._poll_workspace()
        await pilot.pause()
        assert app._bindings.key_to_bindings["w"][0].description == "Start work"
        app.push_screen(FormModal("New project", [Field("name", "Project name")]))
        await pilot.pause()
        assert app.screen._bindings.key_to_bindings["ctrl+s"][0].description == "Save"


@pytest.mark.asyncio
async def test_delayed_palette_start_after_project_change_only_prepares(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    second = {"id": "project-b", "name": "Second project", "root": str(tmp_path / "second")}
    broker.projects.append(second)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        # Simulate delivery of an old request after another surface selects B.
        workspace.select_project(second, second["root"], intent="inspect", requested_slot_ids=[])
        workspace.update({"command_request": {"id": "queued-under-a", "action": "start_work", "target_role": "sidebar"}})
        await app._poll_workspace()
        await settle(app, pilot)
        assert app.page == "project" and app.workspace_data["project_id"] == "project-b"
        assert all(selection[2] == "inspect" for selection in workspace.selections)
        assert workspace.value["requested_slot_ids"] == []
        assert app.focused.id == "project-work"


@pytest.mark.asyncio
async def test_import_summary_keeps_destination_visible_and_details_never_approve(tmp_path, monkeypatch):
    from cairnmind.terminal_panels import ConfirmModal
    from cairnmind.terminal_ui import summary_text

    value = {"status": "dry_run", "project": {"name": "Example", "root": "/synthetic/target"},
             "counts": {"records": 4, "sources": 7}, "verification": "historical"}
    for language in ("en", "pl"):
        summary = summary_text(value, language)
        assert "Example" in summary and "/synthetic/target" in summary
        assert f"{tr('Records', language)}: 4" in summary
        assert tr("Imported checks are historical.", language) in summary
    command = '{"command": "synthetic exact command", "session_id": "s", "generation": 3}'
    assert summary_text(command, "en") == command
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        modal = ConfirmModal("Import memory", value)
        app.push_screen(modal, results.append)
        await pilot.pause()
        assert "/synthetic/target" in screen_text(app)
        modal.query_one("#message-details", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is modal and not results
        await pilot.press("escape")
        await pilot.pause()
        assert results == [False]


class UIBroker(FakeBroker):
    def __init__(self, project, *, empty=False):
        super().__init__(project)
        self.projects = [] if empty else [project]
        self.profiles = [{"id": "native-private-1", "label": "Codex", "client": "codex"},
                         {"id": "copilot-private-2", "label": "GitHub Copilot", "client": "copilot", "model": "auto"}]
        self.preferences = {"default_slots": [{"id": "slot-native", "profile_id": "native-private-1"},
                                              {"id": "slot-github", "profile_id": "copilot-private-2"}],
                            "project_slots": {}, "scan_roots": []}

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/state":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            return deepcopy({"projects": self.projects, "profiles": self.profiles, "settings": self.preferences, "runs": []})
        if path in {"/api/profiles", "/api/settings"}:
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            if path == "/api/profiles":
                self.profiles = deepcopy(json["profiles"])
            else:
                self.preferences.update(deepcopy(json))
            return {"status": "saved"}
        if path == "/api/projects/inspect" and not Path(json["root"]).is_dir():
            raise ValueError("Folder does not exist")
        if path == "/api/projects" and method == "POST":
            self.calls.append((method, path, deepcopy(params), deepcopy(json)))
            project = {"id": "added", "name": json["name"], "root": json["root"]}
            self.projects.append(project)
            return {"project": project}
        return super().request(method, path, params=params, json=json, timeout=timeout)


def setup_ui(tmp_path, monkeypatch, language="en", empty=False):
    project_path = tmp_path / "Keyboard project"
    project_path.mkdir()
    project = {"id": "project-keyboard", "name": "Keyboard project", "root": str(project_path)}
    broker = UIBroker(project, empty=empty)
    workspace = FakeWorkspace(None if empty else project)
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", lambda *args, **kwargs: workspace.read())
    return settings, broker, workspace


async def focus_with_keyboard(app, pilot, identifier):
    for _ in range(40):
        if app.focused and app.focused.id == identifier:
            return
        await pilot.press("tab")
    raise AssertionError(f"Unreachable by Tab: {identifier}; focused {app.focused}")


def screen_text(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("size", [(80, 24), (120, 35)])
async def test_keyboard_project_open_then_one_explicit_assistant(tmp_path, monkeypatch, language, size):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        assert workspace.selections[-1][2] == "inspect"
        assert workspace.read()["requested_slot_ids"] == []
        assert app.page == "project"
        await focus_with_keyboard(app, pilot, "assistant-select")
        await pilot.press("enter")
        await pilot.pause()
        assert "GitHub Copilot" in screen_text(app)
        assert "private-2" not in screen_text(app)
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "project-work")
        await pilot.press("enter")
        await settle(app, pilot)
        assert workspace.selections[-1][2] == "work"
        assert workspace.read()["requested_slot_ids"] == ["slot-github"]
        assert workspace.read()["focus"] == "client1"
        assert len(broker.preferences["default_slots"]) == 2


@pytest.mark.asyncio
async def test_empty_search_does_not_start_previously_active_project(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "project-search")
        await pilot.press(*"wprm-no-match")
        await pilot.pause()
        assert app.query_one("#project-rows", DataTable).row_count == 0
        assert app.query_one("#project-inspect", Button).disabled
        assert workspace.selections == []  # shortcut letters are ordinary input
        app.action_work()
        await settle(app, pilot)
        assert workspace.selections == []
        assert "Select a visible project" in str(app.query_one("#panel-notice", Static).render())


@pytest.mark.asyncio
async def test_first_project_added_entirely_by_keyboard_without_start(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, empty=True)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert "No projects yet" in screen_text(app)
        await focus_with_keyboard(app, pilot, "project-add")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*str(tmp_path), "ctrl+s")
        await settle(app, pilot)
        assert workspace.selections[-1][2] == "inspect"
        assert broker.projects[-1]["name"] == tmp_path.name
        assert not any(path == "/api/client-runs" for _, path, _, _ in broker.calls)
        assert app.page == "project"


@pytest.mark.asyncio
async def test_connections_copilot_account_has_no_api_key_action(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "auth-open")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        labels = [label for label, _ in app.screen.choices]
        assert "Connect GitHub account" in labels
        assert "Set API key" not in labels
        assert "Remove saved key" not in labels
        assert "copilot-private-2" not in screen_text(app)
        await pilot.press("escape")


class ModalHost(BasePanel):
    def __init__(self, settings, screen, **kwargs):
        super().__init__(settings, **kwargs)
        self.test_screen, self.result = screen, "not returned"

    def compose(self):
        yield Static("Host", id="panel-notice")

    def on_mount(self):
        self.open_test()

    @work
    async def open_test(self):
        self.result = await self.push_screen_wait(self.test_screen)


@pytest.mark.asyncio
async def test_form_retains_entries_after_async_validation_and_retries_only_explicitly(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    attempts = []
    async def save(values):
        attempts.append(dict(values))
        await asyncio.sleep(.01)
        if values["name"] == "bad":
            raise FieldError("name", "Choose a different name")
    form = FormModal("Connection name", [Field("name", "Connection name", required=True)], on_submit=save)
    app = ModalHost(settings, form, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press(*"bad", "ctrl+s")
        await pilot.pause(.1)
        assert app.screen is form
        assert form.query_one("#field-name", Input).value == "bad"
        assert app.focused.id == "field-name"
        assert len(attempts) == 1
        await pilot.press("ctrl+a", "ctrl+k", "g", "o", "o", "d", "ctrl+s")
        await settle(app, pilot)
        assert app.result == {"name": "good"}
        assert len(attempts) == 2


@pytest.mark.asyncio
async def test_folder_picker_keyboard_navigation_and_selection(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    target = tmp_path / "Only folder"
    target.mkdir()
    picker = FolderPicker(target)
    app = ModalHost(settings, picker, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(.15)
        await pilot.press("alt+up")
        await pilot.pause(.15)
        assert picker.path == tmp_path
        await pilot.press("ctrl+s")
        await settle(app, pilot)
        assert app.result == str(tmp_path)


@pytest.mark.asyncio
async def test_details_are_opt_in_and_escape_returns_to_parent(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    dialog = MessageModal("Status", {"status": "ready", "opaque_internal": "diagnostic-only"})
    app = ModalHost(settings, dialog, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert "diagnostic-only" not in screen_text(app)
        await focus_with_keyboard(app, pilot, "message-details")
        await pilot.press("enter")
        await pilot.pause()
        assert "diagnostic-only" in screen_text(app)
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.result is None


@pytest.mark.asyncio
async def test_old_command_is_not_replayed_and_new_command_dispatched_once(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    workspace.value["command_request"] = {"id": "old", "action": "start_work", "target_role": "sidebar"}
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert workspace.selections == []
        workspace.update({"command_request": {"id": "new", "action": "search", "target_role": "sidebar"}})
        await app._poll_workspace()
        await pilot.pause()
        assert app.focused.id == "project-search"
        await pilot.press("tab")
        await app._poll_workspace()
        await pilot.pause()
        assert app.focused.id != "project-search"
        assert not app.open_command("__import__('os').system('anything')")


@pytest.mark.asyncio
async def test_inspector_primary_detail_shows_evidence_and_supersede_warning(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    broker.record["metadata"].update({
        "evidence_earliest": "2026-03-04T10:00:00+00:00",
        "evidence_latest": "2026-03-04T10:00:00+00:00",
        "evidence_basis": "stated",
        "supersede_warning": "anachronistic_supersede",
        "supersede_replaced_evidence_latest": "2026-06-01T09:00:00+00:00",
    })
    app = InspectorApp(settings, broker=broker, workspace=workspace)

    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await app.open_entity("record-a", "records")

        body = str(app.query_one("#entity-body", Static).render())
        assert "Validate token safely." in body
        assert "Stated" in body
        assert "2026-03-04" in body
        assert "2026-06-01" in body
        assert "anachronistic_supersede" not in body
        assert '"record"' not in body

        app.toggle_technical()
        assert '"record"' in str(app.query_one("#entity-body", Static).render())


@pytest.mark.asyncio
async def test_inspector_primary_detail_shows_observed_evidence_time(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    broker.record["metadata"].update({
        "evidence_earliest": "2026-11-09T10:00:00+00:00",
        "evidence_latest": "2026-11-09T10:00:00+00:00",
        "evidence_basis": "observed",
    })
    app = InspectorApp(settings, broker=broker, workspace=workspace)

    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await app.open_entity("record-a", "records")

        body = str(app.query_one("#entity-body", Static).render())
        assert "Observed no later than" in body
        assert "2026-11-09" in body


@pytest.mark.asyncio
async def test_inspector_primary_detail_shows_mixed_evidence_range(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    broker.record["metadata"].update({
        "evidence_earliest": "2026-03-04T10:00:00+00:00",
        "evidence_latest": "2026-11-09T10:00:00+00:00",
        "evidence_basis": "mixed",
    })
    app = InspectorApp(settings, broker=broker, workspace=workspace)

    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await app.open_entity("record-a", "records")

        body = str(app.query_one("#entity-body", Static).render())
        assert "Mixed evidence" in body
        assert "2026-03-04" in body
        assert "2026-11-09" in body


@pytest.mark.parametrize("language, evidence, expected", [
    ("en", {"evidence_state": "orphaned", "quote_survives": None},
     "Evidence: orphaned: no supporting source has a current version"),
    ("en", {"evidence_state": "evidence_replaced", "quote_survives": True},
     "Evidence: the source changed; the quoted passage is still there"),
    ("en", {"evidence_state": "evidence_replaced", "quote_survives": False},
     "Evidence: the source changed; the quoted passage is gone"),
    ("en", {"evidence_state": "unverified", "quote_survives": None},
     "Evidence: imported file evidence has not been compared with local files yet"),
    ("pl", {"evidence_state": "evidence_replaced", "quote_survives": None},
     "Dowody: \u017ar\u00f3d\u0142o si\u0119 zmieni\u0142o; brak zweryfikowanego cytatu do por\u00f3wnania"),
    ("pl", {"evidence_state": "anchored", "quote_survives": None},
     "Dowody: co najmniej jedno \u017ar\u00f3d\u0142o nadal obowi\u0105zuje"),
])
async def test_inspector_detail_names_the_evidence_state(tmp_path, monkeypatch, language, evidence, expected):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language=language)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    original = broker.request

    def request(method, path, *, params=None, json=None, timeout=20):
        result = original(method, path, params=params, json=json, timeout=timeout)
        if path == "/api/inspector/records/record-a":
            result["evidence"] = dict(evidence)
        return result

    broker.request = request
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await app.open_entity("record-a", "records")
        assert expected in app._detail_text


CONFLICT = {
    "id": "new-1:old-1", "kind": "conflict", "status": "pending",
    "content": "Sessions expire after one hour.  <>  Sessions last 24 hours.",
    "newer_evidence": "new-1", "newer_evidence_basis": "stated", "can_supersede": True,
    "candidate": {"id": "new-1", "kind": "decision", "status": "proposed", "version": 3,
                  "content": "Sessions expire after one hour.\x1b[31m", "evidence_latest": "2026-06-01T10:00:00+00:00",
                  "evidence_basis": "stated", "scope": "/repo/api", "sources": ["transcript://new"], "sources_total": 3,
                  "evidence_state": "anchored"},
    "existing": {"id": "old-1", "kind": "decision", "status": "confirmed", "version": 5,
                 "content": "Sessions last 24 hours.", "evidence_latest": "2026-03-01T10:00:00+00:00",
                 "evidence_basis": "observed", "scope": None, "sources": ["file:///repo/auth.py"], "sources_total": 1,
                 "evidence_state": "orphaned"},
}


def test_conflict_text_shows_both_findings_with_dates_scope_sources_and_order():
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_inspector import conflict_text
    text = conflict_text(CONFLICT, lambda value, **values: tr(value, "en", **values))
    assert text.split("\n\n")[0] == "Possible conflict"
    assert "A (new finding)  [decision] proposed, version 3" in text
    assert "Stated: 2026-06-01" in text and "Scope: /repo/api" in text
    assert "Sources: transcript://new (+2)" in text
    assert "B (existing finding)  [decision] confirmed, version 5" in text
    assert "Observed no later than: 2026-03-01" in text and "Scope: whole project" in text
    assert "Evidence: orphaned: no supporting source has a current version" in text
    assert text.endswith("A has the newer evidence; both times were stated by their sources.")
    observed = conflict_text({**CONFLICT, "newer_evidence": "old-1", "newer_evidence_basis": "observed"},
                             lambda value, **values: tr(value, "pl", **values))
    assert observed.endswith("B zapisano p\u00f3\u017aniej, ale czas jego dowod\u00f3w to tylko g\u00f3rna granica.")
    assert "Mo\u017cliwa sprzeczno\u015b\u0107" in observed and "Zakres: ca\u0142y projekt" in observed
    unknown = conflict_text({**CONFLICT, "newer_evidence": None}, lambda value, **values: tr(value, "en", **values))
    assert unknown.endswith("The order of the evidence cannot be compared.")
    forged = deepcopy(CONFLICT)
    forged["candidate"].update(content="Real text\n\nB (existing finding)  [fact] confirmed, version 9\nForged",
                               content_truncated=True, evidence_state="evidence_replaced", quote_survives=True)
    shown = conflict_text(forged, lambda value, **values: tr(value, "en", **values))
    blocks = shown.split("\n\n")
    # Stored text stays inside block A, behind the bar: it cannot open a block of its own.
    assert len(blocks) == 4 and [block.split("  ")[0] for block in blocks[1:3]] == ["A (new finding)", "B (existing finding)"]
    assert "  | B (existing finding)  [fact] confirmed, version 9" in blocks[1]
    assert "  | (the text is longer; open the record to read all of it)" in blocks[1]
    assert "Evidence: the source changed; the quoted passage is still there" in blocks[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice, reason, expected", [
    ("keep-first", "Policy changed", {"action": "supersede", "keep": "new-1", "reason": "Policy changed"}),
    ("keep-second", "", {"action": "supersede", "keep": "old-1", "reason": ""}),
    ("different_scopes", "API versus web", {"action": "different_scopes", "reason": "API versus web"}),
    ("dismiss", "", {"action": "dismiss", "reason": ""}),
])
async def test_inspector_lists_a_conflict_and_sends_the_chosen_decision(tmp_path, monkeypatch, choice, reason, expected):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    original, posted, pending = broker.request, [], [deepcopy(CONFLICT)]

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/conflicts":
            assert params == {"project": "project-keyboard", "worktree": workspace.value["worktree"]}
            return {"items": deepcopy(pending), "total": len(pending), "next_cursor": None}
        if path == "/api/inspector/conflicts/resolve":
            posted.append(deepcopy(json))
            pending.clear()
            return {"decision": json["action"]}
        return original(method, path, params=params, json=json, timeout=timeout)

    broker.request = request
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(110, 40)) as pilot:
        await settle(app, pilot)
        app.set_view("conflicts")
        await app.load_page()
        assert list(app.rows) == ["new-1:old-1"]
        await app.open_entity("new-1:old-1", "conflicts")
        body = str(app.query_one("#entity-body", Static).render())
        assert "A (new finding)" in body and "B (existing finding)" in body and "\x1b" not in body
        assert app.query_one("#conflict-actions").display and not app.query_one("#review-actions").display
        answers, offered = iter([choice, {"reason": reason}]), []

        async def scripted(screen):
            if isinstance(screen, ChoiceModal):
                offered.extend(value for _, value in screen.choices)
            return next(answers)

        monkeypatch.setattr(app, "push_screen_wait", scripted)
        app.query_one("#conflict-resolve", Button).press()
        for _ in range(100):
            await pilot.pause(.02)
            if posted:
                break
        await settle(app, pilot)
    assert posted == [{"project": "project-keyboard", "record_id": "new-1", "other_id": "old-1",
                       "expected_version": 3, "other_expected_version": 5, **expected}]
    assert offered == ["keep-first", "keep-second", "different_scopes", "dismiss"]
    assert app.rows == {}


@pytest.mark.asyncio
async def test_a_refused_decision_reloads_the_list_and_a_mixed_pair_is_not_offered_keep(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    original, loads, offered = broker.request, [], []

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/conflicts":
            loads.append(1)
            return {"items": [{**deepcopy(CONFLICT), "can_supersede": False}], "total": 1, "next_cursor": None}
        if path == "/api/inspector/conflicts/resolve":
            raise ValueError("Broker 409: Conflict version conflict; read the current findings")
        return original(method, path, params=params, json=json, timeout=timeout)

    broker.request = request
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(110, 40)) as pilot:
        await settle(app, pilot)
        app.set_view("conflicts")
        await app.load_page()
        await app.open_entity("new-1:old-1", "conflicts")
        answers = iter(["dismiss", {"reason": ""}])

        async def scripted(screen):
            if isinstance(screen, ChoiceModal):
                offered.extend(value for _, value in screen.choices)
            return next(answers)

        monkeypatch.setattr(app, "push_screen_wait", scripted)
        app.query_one("#conflict-resolve", Button).press()
        for _ in range(100):
            await pilot.pause(.02)
            if len(loads) >= 2:
                break
        await settle(app, pilot)
        notice = str(app.query_one("#panel-notice", Static).render())
        assert "The decision was not saved. The current list was reloaded." in notice and "version conflict" in notice
        assert offered == ["different_scopes", "dismiss"] and len(loads) == 2
        await app.open_entity("new-1:old-1", "conflicts")
        app.action_refresh()
        for _ in range(100):
            await pilot.pause(.02)
            if len(loads) >= 3:
                break
        await settle(app, pilot)
        assert len(loads) == 3 and app.entity_kind == "conflicts"


def test_every_evidence_label_has_a_polish_translation():
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_inspector import EVIDENCE_LABELS
    assert {state for state, _ in EVIDENCE_LABELS} == {"anchored", "evidence_replaced", "orphaned", "unverified",
                                                       "degraded"}
    for text in ["Evidence", *EVIDENCE_LABELS.values()]:
        assert tr(text, "en") == text and tr(text, "pl") != text


def test_default_locale_is_english_without_creating_preferences(tmp_path):
    settings = Settings(data_dir=tmp_path / "absent")
    assert language_for(settings) == "en"
    assert not settings.data_dir.exists()
    assert tr("Connections") == "Connections"
    assert tr("Connections", "pl") == "Połączenia"
    assert tr("Ustalenia") == "Records"


@pytest.mark.asyncio
async def test_named_add_assistant_preserves_assignments_without_starting(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    broker.profiles.append({"id": "opaque-third", "label": "Claude", "client": "claude"})
    original = deepcopy(broker.preferences["default_slots"])
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "assistant-add")
        await pilot.press("enter")
        await pilot.pause()
        assert "opaque-third" not in screen_text(app)
        await pilot.press("down", "down", "enter")
        await settle(app, pilot)
        assigned = broker.preferences["project_slots"][workspace.read()["project_id"]]
        assert assigned[:2] == original
        assert assigned[-1]["profile_id"] == "opaque-third"
        assert broker.preferences["default_slots"] == original
        assert all(intent == "inspect" for _, _, intent in workspace.selections)
        assert app.selected_slot_id == assigned[-1]["id"]


@pytest.mark.asyncio
async def test_language_changes_without_workspace_revision_or_restarting_panel(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    language = ["en"]
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language[0])
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        revision = workspace.read()["revision"]
        language[0] = "pl"
        await app._poll_workspace()
        await pilot.pause()
        assert str(app.query_one("#auth-open", Button).label) == "Połączenia"
        assert workspace.read()["revision"] == revision
        assert workspace.selections == []


@pytest.mark.asyncio
async def test_advanced_invalid_limit_keeps_form_and_does_not_write(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        app.edit_profiles("copilot-private-2", advanced=True)
        await pilot.pause()
        await focus_with_keyboard(app, pilot, "field-max_tool_calls")
        await pilot.press("ctrl+a", "ctrl+k", "0", "ctrl+s")
        await pilot.pause(.15)
        assert isinstance(app.screen, FormModal)
        assert app.focused.id == "field-max_tool_calls"
        assert app.screen.query_one("#field-max_tool_calls", Input).value == "0"
        assert not any(method == "PUT" for method, _, _, _ in broker.calls)
        await pilot.press("escape")


@pytest.mark.parametrize("change,matched", [
    ({}, True), ({"lifecycle": "starting"}, True), ({"lifecycle": "stopping"}, True),
    ({"profile_id": "edited-profile", "model": "different-model"}, True),
    ({"project_id": "other"}, False), ({"worktree": "/different"}, False),
    ({"client": "claude"}, False), ({"slot_id": "different"}, False),
    ({"purpose": "auth"}, False), ({"lifecycle": "exited"}, False),
])
def test_primary_resume_requires_exact_native_live_identity(tmp_path, monkeypatch, change, matched):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    app.data = {"profiles": broker.profiles, "settings": broker.preferences, "runs": [{
        "id": "old", "generation": 2, "purpose": "coding", "lifecycle": "running",
        "project_id": broker.project["id"], "worktree": broker.project["root"],
        "slot_id": "slot-native", "profile_id": "native-private-1", "client": "codex", **change}]}
    app.workspace_data = workspace.read()
    app.selected_slot_id = "slot-native"
    assert (app.reusable_run() is not None) is matched


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("exit_stage", ["fresh_state", "reconcile", "final_state"])
async def test_keyboard_resume_returns_to_actual_native_pid_and_never_launches_after_exit(native, monkeypatch, language, exit_stage, record_property):
    """Real Textual + private native tmux, synthetic CLI; no accounts/models/DB."""
    runtime, project, events = native
    original = runtime.start(project, "codex", slot_id="slot-native", profile_id="native-private-1", model="before-edit")
    identity_keys = ("id", "generation", "pid", "process_start", "session_id")
    original_identity = tuple(original[k] for k in identity_keys)
    assert original["pid"] > 0

    class RuntimeBroker(UIBroker):
        def request(self, method, path, **kwargs):
            assert method == "GET", "Resume must not create runs, turn on processing or send model requests"
            value = super().request(method, path, **kwargs)
            if path == "/api/state":
                value["runs"] = runtime.list_runs()
            return value

    broker = RuntimeBroker(project)
    broker.profiles[0]["model"] = "after-edit"
    workspace = FakeWorkspace(project)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    def reconcile(*args):
        assert workspace.value["intent"] == "inspect", "Resume is attach/focus only"
        row = runtime.get_run(original["id"])
        return workspace.update({"slots": [{"id": "slot-native", "run_id": row["id"], "generation": row["generation"],
                                            "client": row["client"], "lifecycle": row["lifecycle"]}]})
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", reconcile)
    app = SidebarApp(runtime.settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        assert str(app.query_one("#project-work", Button).label) == tr("Resume session", language)
        assert app._bindings.key_to_bindings["w"][0].description == tr("Resume session", language)
        before_events = list(events)
        await focus_with_keyboard(app, pilot, "project-work")
        await pilot.press("enter")
        await settle(app, pilot)
        after = runtime.get_run(original["id"])
        assert tuple(after[k] for k in identity_keys) == original_identity
        assert after["model"] == "before-edit" and events == before_events
        assert workspace.value["focus"] == "client1" and workspace.value["visible_slot_ids"] == ["slot-native"]
        assert all(intent == "inspect" for _, _, intent in workspace.selections)
        assert tr("Returned to the running session. No process was restarted.", language) in screen_text(app)

        # Hold the fresh GET during an actual keyboard click, then end the
        # process. The already-displayed Resume must never become a launch.
        started, release = asyncio.Event(), asyncio.Event()
        reconciled = False
        if exit_stage == "final_state":
            actual_reconcile = app.reconcile
            async def record_reconcile():
                nonlocal reconciled
                await actual_reconcile()
                reconciled = True
            monkeypatch.setattr(app, "reconcile", record_reconcile)
        method = "reconcile" if exit_stage == "reconcile" else "load_state"
        actual = getattr(app, method)
        async def delayed():
            if exit_stage == "final_state" and not reconciled:
                await actual()
                return
            started.set()
            await release.wait()
            await actual()
        monkeypatch.setattr(app, method, delayed)
        await focus_with_keyboard(app, pilot, "project-work")
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), 3)
        try:
            await asyncio.to_thread(runtime.stop, original["id"], original["generation"])
        finally:
            release.set()
        await settle(app, pilot)
        assert str(app.query_one("#project-work", Button).label) == tr("Start work", language)
        assert len(runtime.list_runs()) == 1 and runtime.get_run(original["id"])["generation"] == original["generation"]
        assert events == before_events
        assert all(method == "GET" for method, _, _, _ in broker.calls)
        rendered = screen_text(app)
        expected = tr("Session ended or changed. Review the project before choosing Start work.", language)
        record_property("resume_notice_render", json.dumps({
            "language": language, "exit_stage": exit_stage, "size": [80, 24],
            "expected": expected,
            "rendered_lines": rendered[:8192].splitlines(), "rendered_truncated": len(rendered) > 8192,
        }, ensure_ascii=False))
        assert " ".join(expected.split()) in " ".join(rendered.split())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("start", [False, True])
async def test_keyboard_remembers_claude_per_project_before_start_and_live_resume(native, monkeypatch, language, start):
    from cairnmind.workspace_state import WorkspaceState

    runtime, project, events = native
    second_root = Path(project["root"]).parent / "project-b"
    second_root.mkdir()
    second = {"id": "project-b", "name": "Second project", "root": str(second_root)}
    workspace = WorkspaceState(runtime.settings)
    workspace.select_project(project)
    starts = []

    class RuntimeBroker(UIBroker):
        def request(self, method, path, **kwargs):
            assert (method, path) == ("GET", "/api/state"), "Selection must not query providers or create runs"
            value = super().request(method, path, **kwargs)
            value["runs"] = runtime.list_runs()
            return value

    broker = RuntimeBroker(project)
    broker.projects.append(second)
    broker.profiles[1] = {"id": "claude-private-2", "label": "Claude", "client": "claude"}
    broker.preferences["default_slots"][1] = {"id": "slot-claude", "profile_id": "claude-private-2"}
    preferences = deepcopy(broker.preferences)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)

    def reconcile(*args):
        current = workspace.read()
        if workspace.claim_work_request(current):
            assert current["project_id"] == project["id"] and current["requested_slot_ids"] == ["slot-claude"]
            starts.append(runtime.start(project, "claude", slot_id="slot-claude", profile_id="claude-private-2"))
        slots = []
        for slot in broker.preferences["default_slots"]:
            row = dict(slot)
            run = next((r for r in runtime.list_runs() if r["project_id"] == current["project_id"]
                        and r["worktree"] == current["worktree"] and r["slot_id"] == slot["id"]), None)
            if run:
                row.update(run_id=run["id"], generation=run["generation"], client=run["client"], lifecycle=run["lifecycle"])
            slots.append(row)
        return workspace.update({"slots": slots})

    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", reconcile)
    app = SidebarApp(runtime.settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "assistant-select")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert app.selected_slot_id == "slot-claude"
        assert workspace.read()["assistant_selections"] == [
            {"project_id": project["id"], "worktree": project["root"], "slot_id": "slot-claude"}]
        assert runtime.list_runs() == [] and starts == [] and events == []
        if start:
            await focus_with_keyboard(app, pilot, "project-work")
            await pilot.press("enter")
            await settle(app, pilot)
            await app.load_state()
        identity = tuple(starts[0][key] for key in ("id", "generation", "pid", "process_start", "session_id")) if start else None
        before_events = list(events)
        await pilot.press("escape")
        await settle(app, pilot)
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert workspace.read()["project_id"] == second["id"] and app.selected_slot_id == "slot-native"
        assert str(app.query_one("#project-work", Button).label) == tr("Start work", language)
        await pilot.press("escape")
        await settle(app, pilot)
        await pilot.press("up", "enter")
        await settle(app, pilot)
        assert workspace.read()["project_id"] == project["id"] and app.selected_slot_id == "slot-claude"
        assert str(app.query_one("#project-work", Button).label) == tr("Resume session" if start else "Start work", language)
        if start:
            await focus_with_keyboard(app, pilot, "project-work")
            await pilot.press("enter")
            await settle(app, pilot)
            row = runtime.get_run(starts[0]["id"])
            assert tuple(row[key] for key in ("id", "generation", "pid", "process_start", "session_id")) == identity
            assert workspace.read()["focus"] == "client1" and workspace.read()["visible_slot_ids"] == ["slot-claude"]
        assert len(starts) == int(start) and events == before_events
        assert len(runtime.list_runs()) == int(start) and broker.preferences == preferences
        assert all(method == "GET" and path == "/api/state" for method, path, _, _ in broker.calls)

    # A new Sidebar process projection must restore the preference without a write/start.
    before = workspace.read()
    restored = SidebarApp(runtime.settings, broker=broker, workspace=WorkspaceState(runtime.settings))
    async with restored.run_test(size=(80, 24)) as pilot:
        await settle(restored, pilot)
        assert restored.selected_slot_id == "slot-claude"
        assert workspace.read() == before
        assert len(starts) == int(start) and events == before_events


@pytest.mark.asyncio
async def test_assistant_choice_is_separate_for_worktrees_and_not_written_by_render(tmp_path, monkeypatch):
    from cairnmind.workspace_state import WorkspaceState

    settings, broker, _ = setup_ui(tmp_path, monkeypatch)
    workspace = WorkspaceState(settings)
    workspace.select_project(broker.project)
    other = tmp_path / "other-worktree"
    other.mkdir()
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", lambda *args: workspace.read())
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "assistant-select")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await settle(app, pilot)
        workspace.select_project(broker.project, str(other))
        await app._poll_workspace()
        assert app.selected_slot_id == "slot-native"
        await app.remember_assistant("slot-native", app.assistant_scope())
        workspace.select_project(broker.project)
        await app._poll_workspace()
        assert app.selected_slot_id == "slot-github"
        before = workspace.read()
        await app.load_state()
        await app._poll_workspace()
        app.render_project()
        assert workspace.read() == before
        assert len(before["assistant_selections"]) == 2
        assert before["intent"] == "inspect" and before["work_request"] is None
        assert all(method == "GET" for method, _, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("return_to_a", [False, True])
async def test_delayed_assistant_dialog_cannot_write_after_scope_change(tmp_path, monkeypatch, return_to_a):
    from cairnmind.workspace_state import WorkspaceState

    settings, broker, _ = setup_ui(tmp_path, monkeypatch)
    workspace = WorkspaceState(settings)
    workspace.select_project(broker.project)
    second = {"id": "b", "name": "B", "root": broker.project["root"]}
    broker.projects.append(second)
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", lambda *args: workspace.read())
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "assistant-select")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        workspace.select_project(second)
        await app._poll_workspace()
        if return_to_a:
            workspace.select_project(broker.project)
            await app._poll_workspace()
        before = workspace.read()
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert workspace.read() == before
        assert before["assistant_selections"] == [] and app.selected_slot_id == "slot-native"
        assert all(method == "GET" for method, _, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed-slot", "removed-profile", "reordered"])
async def test_remembered_assistant_uses_only_current_valid_assignments(tmp_path, monkeypatch, change):
    from cairnmind.workspace_state import WorkspaceState

    settings, broker, _ = setup_ui(tmp_path, monkeypatch)
    workspace = WorkspaceState(settings)
    workspace.select_project(broker.project)
    workspace.update({"assistant_selections": [{"project_id": broker.project["id"],
        "worktree": broker.project["root"], "slot_id": "slot-github"}]})
    if change == "removed-slot":
        broker.preferences["default_slots"].pop()
    elif change == "removed-profile":
        broker.profiles.pop()
        broker.preferences["default_slots"].reverse()  # An invalid first slot must not hide the valid one.
    else:
        broker.preferences["default_slots"].reverse()
    before = workspace.read()
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        assert app.selected_slot_id == ("slot-github" if change == "reordered" else "slot-native")
        assert not app.query_one("#project-work", Button).disabled
        assert workspace.read() == before
        assert all(method == "GET" for method, _, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_keyboard_assistant_cas_conflict_keeps_choice_and_localizes_notice(tmp_path, monkeypatch, language):
    from cairnmind.workspace_state import WorkspaceState

    settings, broker, _ = setup_ui(tmp_path, monkeypatch, language)
    workspace = WorkspaceState(settings)
    workspace.select_project(broker.project)
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", lambda *args: workspace.read())
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        await app.remember_assistant("slot-native", app.assistant_scope())
        original_update = workspace.update
        concurrent = []

        def conflicting_update(changes, expected_revision=None):
            assert set(changes) == {"assistant_selections"}
            assert not concurrent
            concurrent.append(original_update({"zoom": True}))
            return original_update(changes, expected_revision=expected_revision)

        monkeypatch.setattr(workspace, "update", conflicting_update)
        await focus_with_keyboard(app, pilot, "assistant-select")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert len(concurrent) == 1 and workspace.read() == concurrent[0]
        assert app.selected_slot_id == "slot-native"
        assert workspace.read()["assistant_selections"][0]["slot_id"] == "slot-native"
        assert workspace.read()["work_request"] is None and workspace.read()["intent"] == "inspect"
        expected = ("Workspace changed. Refresh its current state and try again." if language == "en" else
                    "stale_workspace_revision: odczytaj bieżący stan i ponów operację.")
        assert " ".join(expected.split()) in " ".join(screen_text(app).split())
        assert "Operacja nie została ukończona:" not in screen_text(app)
        assert all(method == "GET" for method, _, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("kind,english,polish", [
    ("list", "Invalid assistant selections", "Niepoprawna lista wyborów asystenta"),
    ("record", "Invalid assistant selection", "Niepoprawny wybór asystenta"),
    ("worktree", "Invalid assistant worktree", "Niepoprawny katalog pracy asystenta"),
    ("duplicate", "Duplicate assistant selection scope", "Powtórzony wybór asystenta dla tego samego projektu i katalogu"),
])
async def test_worker_state_validation_errors_are_localized_without_mutation(tmp_path, monkeypatch, language, kind, english, polish):
    from cairnmind.workspace_state import WorkspaceState

    settings, broker, _ = setup_ui(tmp_path, monkeypatch, language)
    workspace = WorkspaceState(settings)
    workspace.select_project(broker.project)
    row = {"project_id": broker.project["id"], "worktree": broker.project["root"], "slot_id": "slot-native"}
    invalid = {"list": {}, "record": [{}], "worktree": [dict(row, worktree="relative")],
               "duplicate": [row, dict(row)]}[kind]
    observed = asyncio.Event()

    class ErrorSidebar(SidebarApp):
        @work(exit_on_error=False)
        async def invalid_selection(self):
            await asyncio.to_thread(self.workspace.update, {"assistant_selections": invalid})

        def notice(self, value):
            super().notice(value)
            if isinstance(value, ValueError):
                observed.set()

    app = ErrorSidebar(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        before = workspace.read()
        app.invalid_selection()
        await asyncio.wait_for(observed.wait(), 3)
        await pilot.pause()
        expected = english if language == "en" else polish
        rendered = screen_text(app)
        assert " ".join(expected.split()) in " ".join(rendered.split())
        assert "Operacja nie została ukończona:" not in rendered
        assert "Operation did not complete:" not in rendered
        assert workspace.read() == before and app.selected_slot_id == "slot-native"
        assert all(method == "GET" for method, _, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("later_action", ["failure", "saved", "scope_change"])
async def test_keyboard_start_feedback_tracks_fresh_selected_slot_without_overwriting_other_action(
        tmp_path, monkeypatch, language, later_action):
    from cairnmind.terminal_i18n import native_failure_notice
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    slot = {"id": "slot-native", "run_id": "run-keyboard", "generation": 1,
            "client": "codex", "lifecycle": "starting", "exit_code": None}
    def reconcile(*args, **kwargs):
        if workspace.value.get("intent") == "work" and not workspace.value.get("slots"):
            workspace.update({"slots": [slot]})
        return workspace.read()
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", reconcile)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        await focus_with_keyboard(app, pilot, "project-work")
        await pilot.press("enter")
        await settle(app, pilot)
        assert tr("Starting the selected assistant…", language) in str(app.query_one("#panel-notice", Static).render())
        assert workspace.selections[-1][2] == "work"
        if later_action == "saved":
            app.notice("Connection saved. You can connect it or choose it for your project.")
        app.data["runs"] = [{"id": slot["run_id"], "generation": 1, "purpose": "coding",
            "project_id": workspace.value["project_id"], "worktree": workspace.value["worktree"],
            "client": "codex", "profile_id": "native-private-1", "slot_id": "slot-native", "lifecycle": "running"}]
        failed = {**slot, "lifecycle": "failed", "exit_code": 1}
        if later_action == "scope_change":
            workspace.update({"project_id": "another-project", "worktree": "/other/worktree", "slots": []})
        else:
            workspace.update({"slots": [failed]})
        # The cached GET snapshot still says running: feedback and the button
        # must follow this fresh workspace revision before the next 3-second GET.
        await app._poll_workspace()
        await pilot.pause()
        notice = str(app.query_one("#panel-notice", Static).render())
        assert tr("Starting the selected assistant…", language) not in notice
        if later_action == "scope_change":
            assert not notice
        else:
            expected = native_failure_notice({"client": "codex", "lifecycle": "failed", "exit_code": 1}, language)
            assert expected in str(app.query_one("#project-state", Static).render())
            assert str(app.query_one("#project-work", Button).label) == tr("Start work", language)
            assert (expected in notice) if later_action == "failure" else (
                tr("Connection saved. You can connect it or choose it for your project.", language) in notice)
        assert len(workspace.selections) == 2
        assert not any(method == "POST" and path == "/api/client-runs" for method, path, _, _ in broker.calls)


@pytest.mark.asyncio
async def test_brief_from_sidebar_shows_marked_lines_for_the_open_worktree(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    asked = []
    original = broker.request

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/brief":
            assert timeout == 120
            asked.append((method, deepcopy(params)))
            return {"text": "Brief for Keyboard project in /repo\n[machine]  Latest checkpoint saved 2 hours ago.\n"
                            "[reported] Checkpoint summary: half done", "lines": []}
        return original(method, path, params=params, json=json, timeout=timeout)

    broker.request = request
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        assert app.open_command("brief") is True
        for _ in range(100):
            await pilot.pause(.02)
            if isinstance(app.screen, MessageModal):
                break
        assert isinstance(app.screen, MessageModal)
        await pilot.pause()
        text = screen_text(app)
        assert "[machine]  Latest checkpoint saved 2 hours ago." in text
        assert "[reported] Checkpoint summary: half done" in text
        await pilot.press("escape")
        await settle(app, pilot)
    assert asked == [("GET", {"project": "project-keyboard", "worktree": workspace.value["worktree"]})]


@pytest.mark.asyncio
async def test_ask_memory_from_sidebar_shows_cited_answer(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    asked = []
    original = broker.request

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/ask":
            asked.append((method, deepcopy(json), timeout))
            quote = "ArcadeDB keeps documents, graph and vectors in one engine."
            return {"status": "complete", "answer": "ArcadeDB keeps graph and vectors together.",
                    "sources": [{"uri": "file://README.md", "line_start": 1, "line_end": 1, "quote": quote}],
                    "unknowns": [], "coverage": {"answer_terms": 5, "terms_in_quotes": 4, "ratio": .8}}
        return original(method, path, params=params, json=json, timeout=timeout)

    broker.request = request
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        assert app.open_command("ask") is True
        # The ask worker stays busy while its form is open, so wait for the screen.
        for _ in range(100):
            await pilot.pause(.02)
            if isinstance(app.screen, FormModal):
                break
        assert isinstance(app.screen, FormModal)
        await pilot.press(*"WhyArcadeDB", "ctrl+s")
        for _ in range(100):
            await pilot.pause(.02)
            if isinstance(app.screen, MessageModal):
                break
        assert isinstance(app.screen, MessageModal)
        await pilot.pause()
        text = screen_text(app)
        assert "ArcadeDB keeps graph and vectors together." in text
        assert "file://README.md" in text
        await pilot.press("escape")
        await settle(app, pilot)
    assert len(asked) == 1
    method, payload, timeout = asked[0]
    assert method == "POST" and payload["project"] == "project-keyboard"
    assert payload["question"] == "WhyArcadeDB" and timeout == settings.ask_timeout + 150



@pytest.mark.asyncio
async def test_workspace_poll_after_shutdown_does_not_touch_removed_widgets(tmp_path, monkeypatch):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
    # A poll tick that fires while the app shuts down sees a newer workspace revision.
    workspace.update({"launch_errors": []})
    assert app.is_running is False
    seen = app._workspace_revision
    await app._poll_workspace()
    assert app._workspace_revision == seen


def menu_row(action):
    from cairnmind.terminal_commands import COMMANDS
    return next(row for row in COMMANDS if row[3] == action)


@pytest.mark.asyncio
async def test_brief_chosen_in_the_menu_reaches_what_the_project_page_reaches(tmp_path, monkeypatch):
    from test_workspace_state import menu_request
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    asked = []
    original = broker.request

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/brief":
            asked.append((method, deepcopy(params)))
            return {"text": "Brief for Keyboard project\n[machine]  Latest checkpoint saved.", "lines": []}
        return original(method, path, params=params, json=json, timeout=timeout)

    broker.request = request
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        workspace.update({"command_request": menu_request(tmp_path, menu_row("brief"))})
        await app._poll_workspace()
        for _ in range(100):
            await pilot.pause(.02)
            if isinstance(app.screen, MessageModal):
                break
        assert isinstance(app.screen, MessageModal)
        await pilot.pause()
        assert "[machine]  Latest checkpoint saved." in screen_text(app)
        await pilot.press("escape")
        await settle(app, pilot)
    assert asked == [("GET", {"project": "project-keyboard", "worktree": workspace.value["worktree"]})]


@pytest.mark.asyncio
async def test_ask_memory_chosen_in_the_menu_reaches_what_the_project_page_reaches(tmp_path, monkeypatch):
    from test_workspace_state import menu_request
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    asked = []
    original = broker.request

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/ask":
            asked.append((method, deepcopy(json)))
            return {"status": "complete", "answer": "Graph and vectors together.", "sources": [],
                    "unknowns": [], "coverage": {}}
        return original(method, path, params=params, json=json, timeout=timeout)

    broker.request = request
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        workspace.update({"command_request": menu_request(tmp_path, menu_row("ask"))})
        await app._poll_workspace()
        for _ in range(100):
            await pilot.pause(.02)
            if isinstance(app.screen, FormModal):
                break
        assert isinstance(app.screen, FormModal)
        await pilot.press(*"Why", "ctrl+s")
        for _ in range(100):
            await pilot.pause(.02)
            if isinstance(app.screen, MessageModal):
                break
        assert isinstance(app.screen, MessageModal)
        await pilot.pause()
        assert "Graph and vectors together." in screen_text(app)
        await pilot.press("escape")
        await settle(app, pilot)
    assert [(method, payload["project"], payload["question"]) for method, payload in asked] \
        == [("POST", "project-keyboard", "Why")]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,language", [("ask", "en"), ("brief", "en"), ("ask", "pl"), ("brief", "pl")])
async def test_menu_ask_and_brief_say_so_when_no_project_is_shown(tmp_path, monkeypatch, action, language):
    from test_workspace_state import menu_request
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language=language, empty=True)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        workspace.update({"command_request": menu_request(tmp_path, menu_row(action))})
        await app._poll_workspace()
        await settle(app, pilot)
        assert not isinstance(app.screen, (FormModal, MessageModal))
        assert str(app.query_one("#panel-notice", Static).render()) == tr("Select a visible project first.", language)
    assert not any(path in {"/api/ask", "/api/brief"} for _, path, _, _ in broker.calls)
