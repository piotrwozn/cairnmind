"""Language findings of the 0.10.0 trial (#153): English source text, Polish translation, nothing mixed."""
import ast
import re
from pathlib import Path

import pytest

from cairnmind.terminal_commands import ask_summary
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import ChoiceModal
from test_terminal_panels import setup, settle

SOURCE = Path(__file__).resolve().parents[1] / "src" / "cairnmind"
POLISH = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")


def polish_literals(name):
    tree = ast.parse((SOURCE / name).read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and POLISH.search(node.value)]


@pytest.mark.parametrize("name", [
    "agent.py", "service.py", "cockpit.py", "terminal_client.py", "terminal_graph.py", "api_profiles.py",
    "api_credentials.py", "api_runtime.py", "cockpit_state.py", "workspace_state.py", "workspace.py"])
def test_backend_messages_are_written_in_english(name):
    # The one sentence kept as written: the catalog translates it to English, as a pinned test expects.
    kept = {"stale_workspace_revision: odczytaj bieżący stan i ponów operację."}
    assert [text for text in polish_literals(name) if text not in kept] == []
    assert all(tr(text, "en") != text for text in kept)


@pytest.mark.parametrize(("language", "line"), [
    ("en", "Broker 503: The memory service is unavailable. Restart cairnmind."),
    ("pl", "Broker 503: Usługa pamięci jest niedostępna. Uruchom ponownie cairnmind.")])
def test_a_broker_error_reads_in_the_panel_language(language, line):
    from cairnmind.terminal_client import BrokerError
    from cairnmind.terminal_i18n_messages import broker_error_text
    translate = lambda text: tr(text, language)  # noqa: E731
    assert broker_error_text(BrokerError(503, "The memory service is unavailable. Restart cairnmind."),
                             translate) == line
    # A broker still running the earlier version answers in Polish: an English panel reads English.
    assert broker_error_text(BrokerError(503, "Usługa pamięci jest niedostępna. Uruchom ponownie cairnmind."),
                             translate) == line
    # Any other detail is shown as it came.
    assert broker_error_text(BrokerError(409, "Open [bold]"), translate) == "Broker 409: Open [bold]"


@pytest.mark.asyncio
async def test_a_panel_notice_of_a_broker_error_is_translated(tmp_path, monkeypatch):
    from textual.widgets import Static

    from cairnmind.terminal_client import BrokerError
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: "en")
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.notice(BrokerError(503, "Usługa pamięci jest niedostępna. Uruchom ponownie cairnmind."))
        await pilot.pause()
        assert str(app.query_one("#panel-notice", Static).render()) == (
            "Broker 503: The memory service is unavailable. Restart cairnmind.")


# What `cairnmind ask` returned on the trial installation for an English question, now in English.
UNVERIFIED = {
    "status": "partial", "answer": "The model's answer could not be verified in the sources.",
    "sources": [], "diagnostics": {"code": "answer_unsupported"},
    "unknowns": ["The quoted passage does not occur in the checked version of the source.",
                 "The quotes do not support the answer: the quote names another key [bold]",
                 "Nothing in memory says when the trial ran."],
}


def test_ask_answer_and_gaps_read_in_english_in_an_english_panel():
    text = ask_summary(UNVERIFIED, lambda value: tr(value, "en"))
    assert POLISH.search(text) is None
    assert "Status: Partial" in text and "could not be verified in the sources." in text


def test_ask_answer_and_gaps_are_translated_in_a_polish_panel_but_model_text_is_kept():
    text = ask_summary(UNVERIFIED, lambda value: tr(value, "pl"))
    assert "Stan: Częściowa" in text
    assert "Nie udało się zweryfikować odpowiedzi modelu w źródłach." in text
    assert "- Cytowany fragment nie występuje w sprawdzonej wersji źródła." in text
    # The fixed prefix is translated; the judging model's reason after it stays exactly as it came.
    assert "- Cytaty nie potwierdzają odpowiedzi: the quote names another key [bold]" in text
    assert "- Nothing in memory says when the trial ran." in text


def test_a_complete_answer_is_never_swapped_for_a_catalog_sentence():
    # A model may answer with any words, even a word the catalog knows; a verified answer is shown as it is.
    result = {"status": "complete", "answer": "Open", "sources": [], "unknowns": ["Open"]}
    text = ask_summary(result, lambda value: tr(value, "pl"))
    assert "Stan: Pełna" in text and "\nOpen" in text and "- Open" in text and "Otwórz" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "title"), [
    ("language", "Language"), ("view", "Layout"), ("reduced_motion", "Reduce motion")])
async def test_the_value_dialog_is_titled_after_the_setting_it_changes(tmp_path, monkeypatch, choice, title):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    seen = []

    async def choose(screen):
        seen.append(screen)
        return choice if len(seen) == 1 else None
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.action_settings()
        await settle(app, pilot)
    assert [screen.heading for screen in seen if isinstance(screen, ChoiceModal)] == ["Settings", title]


async def wait_until(pilot, predicate, seconds=4):
    for _ in range(int(seconds / .05)):
        if predicate():
            return
        await pilot.pause(.05)
    assert predicate()


@pytest.mark.asyncio
async def test_a_running_inspector_switches_every_label_to_the_new_language(tmp_path, monkeypatch):
    from textual.widgets import DataTable, Select, Static
    from textual.widgets._select import SelectCurrent

    from cairnmind.terminal_inspector import InspectorApp
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    language = ["en"]
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language[0])
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        table = app.query_one("#inspector-rows", DataTable)
        assert str(app.query_one("#panel-notice", Static).render()) == "1 items · read only"
        assert app.title == "Cairnmind · Memory"
        language[0] = "pl"  # Settings → Language → Polski, while the panel runs
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        shown = app.query_one("#inspector-view", Select).query_one(SelectCurrent).label
        assert "Ustalenia" in str(shown) and app.query_one("#inspector-view", Select).value == "records"
        assert app.view == "records"
        assert [str(column.label) for column in table.ordered_columns] == [
            "Typ / stan", "Treść / źródło", "Wersja / próba"]
        assert str(app.query_one("#panel-notice", Static).render()) == "1 wpisów · tylko odczyt"
        assert app.title == "Cairnmind · Pamięć"
        assert str(table.get_row("record-a")[0]) == "zadanie / do przeglądu"


@pytest.mark.asyncio
async def test_a_running_graph_lays_out_its_buttons_again_for_longer_labels(tmp_path):
    from textual.widgets import Button

    from cairnmind.cockpit_state import CockpitState
    from cairnmind.terminal_graph import GraphApp
    from test_terminal_graph import ControlledBroker
    from cairnmind.config import Settings
    from cairnmind.workspace_state import WorkspaceState
    settings = Settings(tmp_path / "data")
    project = {"id": "alpha", "name": "Alpha", "root": str(tmp_path / "alpha")}
    (tmp_path / "alpha").mkdir()
    workspace = WorkspaceState(settings, "graph-tests")
    workspace.select_project(project, intent="inspect")
    workspace.update({"focus": "graph"})
    app = GraphApp(settings, broker=ControlledBroker([project]), workspace=workspace, motion=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: bool(app.model.nodes))
        await pilot.press("]")
        await wait_until(pilot, lambda: "Enter: " in str(app.query_one("#graph-selection").render()))
        CockpitState(settings).update({"language": "pl"})
        button = app.query_one("#graph-open", Button)
        await wait_until(pilot, lambda: str(button.label) == "Otwórz")
        await pilot.pause(.2)
        # The whole label is on screen, not the part that fitted the width laid out for "Open".
        row = app.screen._compositor.render_strips()[button.region.y].text
        assert "Otwórz" in row and "Grupy" in row, row  # the trial showed "Otwó    Grupy" here
        app.invalidate_frame()
        await wait_until(pilot, lambda: str(app.query_one("#graph-selection").render()).endswith("· źródło"))


@pytest.mark.parametrize("name", ["api_providers.py", "copilot_adapter.py"])
def test_provider_errors_are_written_in_english(name):
    assert polish_literals(name) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("language", "shown", "hidden"), [
    ("en", "The request exceeds the context limit.", "Żądanie"),
    ("pl", "Żądanie przekracza limit kontekstu.", "The request exceeds")])
async def test_a_provider_error_reads_in_the_panel_language(tmp_path, language, shown, hidden):
    from textual.widgets import RichLog, Static

    from cairnmind.api_providers import ProviderError
    from cairnmind.api_worker import AgentApp
    from cairnmind.cockpit_state import CockpitState
    from cairnmind.config import Settings
    from test_api_worker import Engine
    from test_api_worker import settle as settle_agent

    class Overflow(Engine):
        async def turn(self, text):
            self.turns.append(text)
            # What AgentEngine.turn returns for a context_limit: the provider's own fixed hint.
            hint = ProviderError("context_limit").hint
            return {"status": "context_limit", "error_code": "context_limit", "hint": hint,
                    "unknowns": [hint, "A note of the model [bold]"]}

    run = {"id": "run-a", "run_id": "run-a", "generation": 2, "project_id": "project-a",
           "worktree": str(tmp_path / "project"), "session_id": "session-a", "client": "api"}
    profile = {"id": "profile-a", "label": "Test API", "client": "api", "model": "controlled"}
    app = AgentApp(Settings(data_dir=tmp_path / "data"), run, profile, key="fixture-key", engine_factory=Overflow)
    CockpitState(app.settings).update({"language": language})
    app.language = language
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        app.execute_turn("Long request")
        await settle_agent(app, pilot)
        status = app.query_one("#agent-status", Static).render().plain
        history = "\n".join(line.text for line in app.query_one("#agent-history", RichLog).lines)
    for text in (status, history):
        assert shown in text and hidden not in text and "A note of the model [bold]" in text


@pytest.mark.asyncio
async def test_the_finding_form_offers_kinds_and_statuses_in_polish_and_saves_the_stored_word(
        tmp_path, monkeypatch):
    from textual.widgets import Select
    from textual.widgets._select import SelectCurrent

    from cairnmind.workspace_finding import FindingApp
    from test_quick_finding import FakeBroker, selected_workspace
    monkeypatch.setattr("cairnmind.terminal_i18n.language_for", lambda settings: "pl")
    settings, state = selected_workspace(tmp_path)
    broker = FakeBroker()
    app = FindingApp(settings, broker=broker, workspace=state)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        kind, status = app.query_one("#finding-kind", Select), app.query_one("#finding-status", Select)
        assert str(kind.query_one(SelectCurrent).label) == "decyzja" and kind.value == "decision"
        assert str(status.query_one(SelectCurrent).label) == "proponowane" and status.value == "proposed"
        await pilot.press(*"Use", "space", *"WAL", "ctrl+s")
        await app.workers.wait_for_complete()
    sent = broker.calls[0][2]["json"]
    assert (sent["kind"], sent["status"]) == ("decision", "proposed")


def test_a_stage_and_its_check_result_name_states_and_outcomes_in_polish():
    from cairnmind.stages import describe
    from cairnmind.workspace_stage import check_summary
    polish = lambda text: tr(text, "pl")  # noqa: E731
    step = describe({"task": {"content": "Add a comment", "state": "needs_test", "version": 7}}, polish)
    assert step == "Krok: Add a comment [do testu, v7]"
    assert "Wynik: zakończone" in check_summary({"passed": True, "outcome": "completed"}, polish)
    # Text that is not a stored word (a model's or a task's) is never translated.
    assert describe({"task": {"content": "done", "state": "odd state", "version": 1}}, polish).endswith(
        "done [odd state, v1]")


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_command_menu_descriptions_wrap_instead_of_being_cut_at_80x24(tmp_path, monkeypatch, language):
    from types import SimpleNamespace

    from textual.widgets import DataTable

    from cairnmind.config import Settings
    from cairnmind.terminal_commands import COMMANDS, CommandPaletteApp
    monkeypatch.setattr("cairnmind.terminal_i18n.language_for", lambda settings: language)
    app = CommandPaletteApp(SimpleNamespace(settings=Settings(data_dir=tmp_path / "data")))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.pause()
        table = app.query_one(DataTable)
        width = table.ordered_columns[1].width
        for index, row in enumerate(COMMANDS):
            cell = str(table.get_row(str(index))[1])
            assert " ".join(cell.split()) == tr(row[1], language)  # every word of it, none cut off
            assert all(len(line) <= width for line in cell.splitlines())
        # The cursor still moves one command at a time.
        await pilot.press("down")
        assert table.cursor_row == 1
