"""Second trial of the workspace findings of the 0.11.0 release (#243): what was still open after the first fixes."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from textual.widgets import Button
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.cockpit import create_cockpit_app
from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_panels import SidebarApp
from test_cockpit_server import ORIGIN, Runtime, login
from test_stage_router import move, planned  # noqa: F401  (planned is a fixture)
from test_terminal_panels import settle, setup
from test_trial011_workspace import MISSING_KEY, SIZES, inspector, notice_of, wait_until

CHECKS = '[checks.unit]\nargv = ["python", "-c", "pass"]\n'


# Start work with a keyless hosted profile records the notice twice per press. The first fix guarded the state
# poll (workspace_changed) only; select_project, the second path, then said the same text again.

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_opening_a_project_with_a_launch_problem_records_it_once(tmp_path, monkeypatch, size, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        workspace.update({"launch_errors": [{"slot_id": "codex", "client": "api", "code": "credential_missing"}]})
        await wait_until(pilot, lambda: notice_of(app) == MISSING_KEY[language])
        app.notice("Another event")  # another notice on the line, as on the trial
        app.select_project("inspect")
        await settle(app, pilot)
        await pilot.pause(.7)
        await settle(app, pilot)
        assert notice_of(app) == MISSING_KEY[language]
        last = app.notice_history.entries[-1]
        assert last.text == MISSING_KEY[language] and last.count == 1  # not "(x2)" for one press


# Check connection prints the sentence and then the bare code.

@pytest.mark.parametrize("language", ["en", "pl"])
def test_a_status_with_a_sentence_keeps_its_code_out_of_the_summary(language):
    from cairnmind.terminal_ui import summary_text
    sentence = tr("Operation completed.", language)
    text = summary_text({"message": sentence, "code": "credential_missing", "configured": False}, language)
    assert text == sentence
    # Without a sentence the code is all there is to say, and it is still said.
    assert summary_text({"code": "credential_missing"}, language) == "credential_missing"


# stage next offers a check run for a check profile the worktree does not declare.

def test_a_test_stage_with_an_undeclared_profile_offers_no_check_run():
    from cairnmind.stages import check_declared, describe, next_command
    decision = {"stage": "test", "action": "run_check", "problem": None, "check_profile": "nope",
                "task": {"id": "task-1", "version": 2, "state": "needs_test", "content": "Build it"},
                "plan": None, "required_profiles": []}
    assert check_declared(decision, ["nope", "unit"]) is decision  # declared: the run is offered
    assert check_declared(decision, None) is decision  # unreadable: the run says itself what is wrong
    unknown = check_declared(decision, ["unit", "lint"])
    assert (unknown["action"], unknown["problem"]) == ("none", "unknown_check_profile")
    assert next_command(unknown, "demo", "/repo", slots=[], profiles=[], active_plan_id=None) == (None, None)
    english = describe(unknown).splitlines()
    assert ("Unknown check profile: nope. This worktree declares: lint, unit. See cairnmind check profiles PROJECT."
            in english)
    assert english[-1] == "Map stages in More → Plan stages."
    polish = describe(unknown, lambda text: tr(text, "pl"))
    assert "Nieznany profil testu: nope. Ten katalog roboczy deklaruje: lint, unit." in polish
    assert polish.splitlines()[-1] == "Przypisz etapy w Więcej → Etapy planu."
    assert "This worktree declares no check profiles." in describe(check_declared(decision, []))


def test_cli_stage_next_names_an_undeclared_check_profile_and_prints_no_command(planned):  # noqa: F811
    settings, store, project, plans, saved = planned
    moved = move(store, plans, project, saved, "needs_test")
    CockpitState(settings).update({"project_stages": {project["id"]: {"test": "nope"}}})
    runner = CliRunner()
    shown = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "stage", "next", "demo"])
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert "Next:" not in shown.output and "cairnmind check run" not in shown.output
    assert "Unknown check profile: nope. This worktree declares: unit." in shown.output
    assert "Map stages in More → Plan stages." in shown.output
    value = json.loads(runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "stage", "next", "demo",
                                               "--json"]).output)
    assert (value["problem"], value["action"], value["command"]) == ("unknown_check_profile", "none", None)
    assert value["task"]["version"] == moved["version"]  # nothing changed
    assert store.query("SELECT FROM check_runs WHERE project_id=:p", {"p": project["id"]}) == []


def test_cockpit_stage_names_an_undeclared_check_profile(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    root = tmp_path / "repo"
    (root / ".cairnmind").mkdir(parents=True)
    (root / ".cairnmind" / "checks.toml").write_text(CHECKS)
    project = {"id": "project-one", "name": "Example", "root": str(root)}
    task = {"id": "task-1", "version": 4, "content": "Test it", "metadata": {"state": "needs_test"}}

    def upstream(request):
        if request.url.path == "/projects":
            return httpx.Response(200, json=[project])
        if request.url.path == "/project-roots":
            return httpx.Response(200, json={"roots": [str(root)]})
        if request.url.path == "/plans/next":
            return httpx.Response(200, json={"plan": {"id": "plan-1", "version": 2, "title": "API"}, "task": task,
                                             "reason": "needs_test", "blocked": []})
        return httpx.Response(404, json={"detail": "unexpected"})
    app = create_cockpit_app(settings, runtime=Runtime(), proxy_transport=httpx.MockTransport(upstream),
                             public_url=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = login(app, client)
        for target, action, problem in (("nope", "none", "unknown_check_profile"), ("unit", "run_check", None)):
            CockpitState(settings).update({"project_stages": {project["id"]: {"test": target}}})
            value = client.get("/api/stage", params={"project": project["id"]}, headers=headers).json()
            assert (value["action"], value["problem"], value["check_profile"]) == (action, problem, target)


# The snapshot view shows the previous list's item count on the notice line.

@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES)
async def test_the_snapshot_view_leaves_no_count_of_the_list_before_it(tmp_path, monkeypatch, size):
    app, _, languages = inspector(tmp_path, monkeypatch, "en")
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        assert notice_of(app) == "1 item · read only"
        app.action_snapshot()
        await settle(app, pilot)
        assert "item" not in notice_of(app)
        languages[0] = "pl"  # a language switch does not bring the count back
        await wait_until(pilot, lambda: app.language == "pl")
        await settle(app, pilot)
        assert "wpis" not in notice_of(app) and "item" not in notice_of(app)
        # A real notice on the line is not cleared by the switch to the snapshot view.
        app.set_view("records")
        app.notice("Something happened.")
        app.set_view("memory")
        assert notice_of(app) == "Something happened."


# At 80x24 a stage notice of five rows scrolls the focused Run next stage button out of view. The sizes are those
# of the sidebar pane inside such a terminal, where the project page scrolls.

@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(30, 23), (45, 24), (40, 20)])
async def test_a_long_notice_keeps_the_focused_project_button_in_view(tmp_path, monkeypatch, size):
    from test_terminal_ux080 import setup_ui
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.show_page("project")
        await settle(app, pilot)
        button = app.query_one("#stage-next", Button)
        button.focus()
        await settle(app, pilot)
        app.notice("\n".join(f"Row {row} of a stage notice" for row in range(1, 6)))
        await settle(app, pilot)
        await pilot.pause(.1)
        page = app.query_one("#project-page")
        assert page.max_scroll_y > 0  # the page does scroll at this size: the case of the trial
        assert app.focused is button and page.region.contains_region(button.region), (page.region, button.region)


# Review of the second round: Run next stage posted the launch problem after the state poll inside reconcile had
# already posted it (the same order as Open, left out of the guard), and its own stage text gave only the codes.

@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_run_next_stage_records_a_launch_problem_once_per_press(tmp_path, monkeypatch, language):
    from test_terminal_ux080 import setup_ui
    from test_workspace_stage import answer_stage, decision
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    project = broker.projects[0]
    answer_stage(broker, decision("start_slot", project, slot_id="slot-native", client="codex"))

    def refused(*args, **kwargs):
        workspace.update({"last_error": "Start work again explicitly after the workspace resumes."})
        return workspace.read()
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", refused)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        await pilot.pause(.3)
        await settle(app, pilot)
        expected = app.t(app.launch_notice(workspace.read()))
        assert notice_of(app) == expected
        same = [entry for entry in app.notice_history.entries if entry.text == expected]
        assert sum(entry.count for entry in same) == 1, [(e.text, e.count) for e in app.notice_history.entries]


@pytest.mark.parametrize("language", ["en", "pl"])
def test_a_partial_resume_is_said_as_such_not_as_a_general_problem(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    text = app.launch_notice({"launch_errors": [{"slot_id": "slot", "client": "claude", "code": "resume_partial"}]})
    assert tr("The memory of this project is partial. `cairnmind run` names what is missing.", language) in text
    assert "Connections" not in text and "Operation needs attention" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_run_next_stage_says_what_to_do_about_a_key_the_launch_refused(tmp_path, monkeypatch, language):
    from test_terminal_ux080 import setup_ui
    from test_workspace_stage import answer_stage, decision
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    project = broker.projects[0]
    answer_stage(broker, decision("start_slot", project, slot_id="slot-api", client="api", label="Cloud API"))

    def refused(*args, **kwargs):
        workspace.update({"launch_errors": [{"slot_id": "slot-api", "client": "api", "code": "credential_invalid"}]})
        return workspace.read()
    monkeypatch.setattr("cairnmind.workspace.refresh_workspace", refused)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 35)) as pilot:
        await settle(app, pilot)
        app.action_next_stage()
        await settle(app, pilot)
        text = app.notice_history.entries[-1].text
    assert tr("The assistant did not start", language) + ": credential_invalid" in text
    assert tr("The API key of this connection is not a valid key. Open Connections, choose the connection, then "
              "Set API key, or correct its environment variable.", language) in text
