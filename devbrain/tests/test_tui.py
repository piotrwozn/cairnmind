"""Headless tests: the launcher must keep project identity and show evidence."""
from __future__ import annotations

import contextlib
from pathlib import Path
import threading

import pytest
from textual.widgets import DataTable, Input, Markdown, Select

from devbrain.config import Settings
from devbrain.store import Store
from devbrain.tui import DevBrainApp


async def settle(app, pilot):
    """Network-backed refreshes may schedule a subsequent source-detail worker."""
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def seed(tmp_path: Path) -> tuple[Settings, Store, dict, dict, dict]:
    settings = Settings.load(tmp_path / "data", create=True)
    store = Store(settings.data_dir)
    root = tmp_path / "app"
    root.mkdir()
    project = store.add_project("Aplikacja", root)
    source = store.snapshot(project["id"], "conversation://user/1", "Przetestuj logowanie.", kind="transcript")
    task = store.remember(project["id"], {
        "kind": "task", "content": "Sprawdzić logowanie", "status": "proposed",
        "metadata": {"state": "needs_test", "plan_id": "login-plan", "step_index": 2},
    }, [source["id"]])
    return settings, store, project, source, task


@pytest.mark.asyncio
async def test_project_selection_shows_tasks_and_sources(tmp_path):
    settings, store, project, source, task = seed(tmp_path)
    second_root = tmp_path / "other"
    second_root.mkdir()
    second = store.add_project("Drugi", second_root)
    app = DevBrainApp(settings, store=store)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        app.query_one("#project-select", Select).value = project["id"]
        await settle(app, pilot)
        assert app.selected_project == project["id"]
        table = app.query_one("#records", DataTable)
        assert table.row_count == 1
        assert table.get_row(task["id"])[1] == "needs_test"
        app.show_record(task["id"])
        await settle(app, pilot)
        detail = app.query_one("#record-detail", Markdown)
        assert source["uri"] in detail._markdown
        assert "Przetestuj logowanie" in detail._markdown
        app.query_one("#project-select", Select).value = second["id"]
        await settle(app, pilot)
        assert app.selected_project == second["id"]
        assert app.query_one("#records", DataTable).row_count == 0


@pytest.mark.asyncio
async def test_ask_uses_selected_project_and_displays_evidence(tmp_path, monkeypatch):
    settings, store, project, source, _ = seed(tmp_path)
    app = DevBrainApp(settings, store=store)
    calls = []

    async def request(method, path, payload=None):
        calls.append((method, path, payload))
        return {
            "status": "complete", "answer": "Należy przetestować logowanie.",
            "sources": [{"source_id": source["id"], "uri": source["uri"], "line_start": 1, "line_end": 1}],
            "unknowns": [], "freshness": {"current": True}, "elapsed_seconds": 0.1,
        }

    monkeypatch.setattr(app, "_request", request)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        app.query_one("#question", Input).value = "Co dalej?"
        app.ask_question()
        await app.workers.wait_for_complete()
        await settle(app, pilot)
        assert calls == [("POST", "/ask", {"project": project["id"], "question": "Co dalej?"})]
        markdown = app.query_one("#answer", Markdown)._markdown
        assert "Należy przetestować" in markdown
        assert source["uri"] in markdown


@pytest.mark.asyncio
async def test_task_update_preserves_version_and_evidence(tmp_path, monkeypatch):
    settings, store, project, source, task = seed(tmp_path)
    app = DevBrainApp(settings, store=store)
    calls = []

    async def request(method, path, payload=None):
        calls.append(payload)
        return store.remember(payload["project"], payload["record"], payload["sources"])

    monkeypatch.setattr(app, "_request", request)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        app.show_record(task["id"])
        app.query_one("#task-state", Select).value = "done"
        app.update_task()
        await app.workers.wait_for_complete()
        await settle(app, pilot)
        assert calls[0]["sources"] == [source["id"]]
        assert calls[0]["record"]["expected_version"] == task["version"]
        assert calls[0]["record"]["metadata"]["state"] == "done"
        assert app.query_one("#records", DataTable).get_row(task["id"])[1] == "done"


@pytest.mark.asyncio
async def test_native_launch_suspends_ui_without_changing_arguments(tmp_path, monkeypatch):
    settings, store, project, _, _ = seed(tmp_path)
    app = DevBrainApp(settings, store=store)
    calls = []

    async def ensure():
        pass

    monkeypatch.setattr(app, "_ensure_service", ensure)
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    import devbrain.integrations as integrations

    monkeypatch.setattr(integrations, "run_client", lambda config, selected, client: calls.append(
        (config, selected["id"], client)
    ) or 0)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        await app.action_launch_codex()
        await settle(app, pilot)
        await app.action_launch_claude()
        await settle(app, pilot)
    assert calls == [(settings, project["id"], "codex"), (settings, project["id"], "claude")]


@pytest.mark.asyncio
async def test_empty_launcher_registers_first_project(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    store = Store(settings.data_dir)
    root = tmp_path / "new-project"
    root.mkdir()
    app = DevBrainApp(settings, store=store)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        assert app.selected_project is None
        app.query_one("#project-name", Input).value = "Nowy"
        app.query_one("#project-root", Input).value = str(root)
        await pilot.click("#add-project")
        await settle(app, pilot)
        projects = store.list_projects()
        assert len(projects) == 1
        assert app.selected_project == projects[0]["id"]
        app.refresh_state()
        await settle(app, pilot)
        assert app.selected_project == projects[0]["id"]


@pytest.mark.asyncio
async def test_unavailable_agent_displays_gaps_without_crashing(tmp_path, monkeypatch):
    settings, store, _, _, _ = seed(tmp_path)
    app = DevBrainApp(settings, store=store)

    async def request(*args, **kwargs):
        return {
            "status": "unavailable", "answer": "Model nie jest dostępny.",
            "sources": [], "unknowns": ["Brak połączenia z Ollama"],
            "freshness": {}, "elapsed_seconds": 0.0,
        }

    monkeypatch.setattr(app, "_request", request)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        app.query_one("#question", Input).value = "Co wiemy?"
        app.ask_question()
        await app.workers.wait_for_complete()
        markdown = app.query_one("#answer", Markdown)._markdown
        assert "unavailable" in markdown
        assert "Brak połączenia z Ollama" in markdown


@pytest.mark.asyncio
async def test_store_network_calls_run_outside_textual_event_loop(tmp_path, monkeypatch):
    settings, store, _, _, task = seed(tmp_path)
    loop_thread = threading.get_ident()

    def guarded(method):
        def call(*args, **kwargs):
            assert threading.get_ident() != loop_thread, "Store HTTP call blocked the Textual event loop"
            return method(*args, **kwargs)
        return call

    for name in ("list_projects", "list_records", "status", "get_source"):
        monkeypatch.setattr(store, name, guarded(getattr(store, name)))
    app = DevBrainApp(settings, store=store)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        app.show_record(task["id"])
        await settle(app, pilot)
        assert app.selected_record == task["id"]


@pytest.mark.asyncio
async def test_plans_next_step_and_packet_readiness_are_scoped_to_registered_worktree(tmp_path):
    from devbrain.plans import PlanManager
    from devbrain.sessions import SessionManager

    settings, store, project, _, _ = seed(tmp_path)
    other = tmp_path / "second-worktree"
    other.mkdir()
    store.add_project_root(project["id"], other)
    plans = PlanManager(store)
    main = plans.save(project["id"], {"title": "Main plan", "goal": "Main goal"},
        [{"key": "first", "content": "Main next step"}], "main-plan", activate=True)
    branch = plans.save(project["id"], {"title": "Branch plan", "goal": "Branch goal"},
        [{"key": "first", "content": "Branch next step"}], "branch-plan", worktree=str(other), activate=True)
    SessionManager(store, settings).prepare(project["id"], "codex", timeout=0)
    app = DevBrainApp(settings, store=store)
    async with app.run_test(size=(150, 55)) as pilot:
        await settle(app, pilot)
        assert app.query_one("#plans", DataTable).row_count == 2
        assert app.query_one("#plans", DataTable).get_row(main["plan"]["id"])[3] == "tak"
        assert "Main next step" in app.query_one("#coordination", Markdown)._markdown
        assert "unavailable" in str(app.query_one("#session-readiness").render())
        assert "Git" in str(app.query_one("#session-readiness").render())
        app.query_one("#worktree-select", Select).value = str(other)
        await settle(app, pilot)
        assert app.selected_worktree == str(other)
        assert "Branch next step" in app.query_one("#coordination", Markdown)._markdown
        assert app.query_one("#plans", DataTable).get_row(branch["plan"]["id"])[3] == "tak"
        app.selected_plan = main["plan"]["id"]
        app.activate_selected_plan()
        await settle(app, pilot)
        assert plans.next_task(project["id"], worktree=str(other))["plan"]["id"] == main["plan"]["id"]
        assert plans.next_task(project["id"])["plan"]["id"] == main["plan"]["id"]


@pytest.mark.asyncio
async def test_managed_task_update_sends_task_and_plan_versions(tmp_path, monkeypatch):
    from devbrain.plans import PlanManager

    settings, store, project, _, _ = seed(tmp_path)
    saved = PlanManager(store).save(project["id"], {"title": "Managed"},
        [{"key": "step", "content": "Managed step"}], "managed", activate=True)
    task = saved["tasks"][0]
    app = DevBrainApp(settings, store=store)
    calls = []

    async def request(method, path, payload=None):
        calls.append(payload)
        return store.remember(payload["project"], payload["record"], payload["sources"])

    monkeypatch.setattr(app, "_request", request)
    async with app.run_test(size=(140, 48)) as pilot:
        await settle(app, pilot)
        app.show_record(task["id"])
        app.query_one("#task-state", Select).value = "in_progress"
        app.update_task()
        await settle(app, pilot)
        assert calls[0]["record"]["expected_version"] == task["version"]
        assert calls[0]["record"]["expected_plan_version"] == saved["plan"]["version"]
        assert PlanManager(store).get(project["id"], saved["plan"]["id"])["plan"]["version"] == 2


@pytest.mark.asyncio
async def test_tui_reads_real_named_check_result_without_running_commands(tmp_path, monkeypatch):
    import subprocess
    import sys

    from devbrain.verification import run_check

    settings, store, project, _, task = seed(tmp_path)
    root = Path(project["root"])
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / ".devbrain").mkdir()
    (root / ".devbrain" / "checks.toml").write_text(
        '[checks.unit]\nargv = [' + repr(sys.executable).replace("'", '"') + ', "-c", "print(123)"]\n')
    subprocess.run(["git", "-C", str(root), "add", ".devbrain"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@localhost",
                    "commit", "--quiet", "-m", "Fixture"], check=True)
    run = run_check(store, settings, project["id"], "unit", task["id"])
    assert run["passed"] is True
    monkeypatch.setattr("devbrain.verification.run_check", lambda *args, **kwargs: pytest.fail("TUI executed a test"))
    app = DevBrainApp(settings, store=store)
    async with app.run_test(size=(150, 55)) as pilot:
        await settle(app, pilot)
        row = app.query_one("#checks", DataTable).get_row(run["id"])
        assert row[0] == "unit"
        assert "passed" in row[1]
        assert row[2] == "0"
        app.show_record(task["id"])
        await settle(app, pilot)
        assert "fresh" in app.query_one("#record-detail", Markdown)._markdown
