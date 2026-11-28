"""Edge-case and modal tests for terminal_panels (SidebarApp, modals, profile forms)."""
from __future__ import annotations

from copy import deepcopy

import pytest
from test_terminal_panels import FakeBroker, FakeWorkspace, settle
from textual.app import App
from textual.widgets import Button, DataTable, Input, Static, Switch

from cairnmind.config import Settings
from cairnmind.terminal.terminal_client import BrokerError
from cairnmind.terminal.terminal_panels import (
    CredentialModal,
    DiscoveryModal,
    SidebarApp,
    profile_fields,
    profile_from_form,
)
from cairnmind.terminal.terminal_panels import (
    main as panels_main,
)


class _ModalHost(App):
    def __init__(self, modal):
        super().__init__()
        self.modal = modal
        self.result = "UNSET"
        self.language = "en"

    def on_mount(self):
        self.push_screen(self.modal, self._capture)

    def _capture(self, value):
        self.result = value


@pytest.mark.asyncio
async def test_credential_modal_validation_save_and_cancel():
    modal = CredentialModal("OpenAI")
    host = _ModalHost(modal)
    async with host.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        key_input = modal.query_one("#credential-key", Input)
        modal.query_one("#credential-save", Button).press()
        await pilot.pause()
        assert "Enter a new key" in str(modal.query_one("#credential-error", Static).render())
        key_input.value = "sk-secret-123"
        modal.query_one("#credential-persist", Switch).value = True
        modal.query_one("#credential-save", Button).press()
        await pilot.pause()
        assert host.result == {"key": "sk-secret-123", "persist": True}
        assert key_input.value == ""

    modal2 = CredentialModal("OpenAI")
    host2 = _ModalHost(modal2)
    async with host2.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        key_input2 = modal2.query_one("#credential-key", Input)
        key_input2.value = "temp-secret"
        modal2.query_one("#credential-cancel", Button).press()
        await pilot.pause()
        assert host2.result is None
        assert key_input2.value == ""


def test_profile_fields_and_profile_from_form_variants_and_errors():
    codex_fields = profile_fields({"label": "C", "model": "o3"}, client="codex")
    assert [f.name for f in codex_fields] == ["label", "client", "model"]
    assert profile_from_form({"label": "C", "client": "codex", "model": "o3"}, identifier="p1", client="codex") == {
        "id": "p1", "label": "C", "client": "codex", "model": "o3"
    }

    ollama_fields = profile_fields({}, client="api", provider="ollama")
    assert any(f.name == "context_window_tokens" for f in ollama_fields)
    assert not any(f.name == "input_per_million" for f in ollama_fields)
    ollama_row = profile_from_form(
        {
            "label": "Local",
            "model": "qwen2.5",
            "base_url": "http://127.0.0.1:11434",
            "credential_ref": "",
            "max_model_calls": "10",
            "max_output_tokens": "2048",
            "command_timeout_seconds": "60",
            "context_window_tokens": "16384",
        },
        identifier="ol",
        client="api",
        provider="ollama",
    )
    assert ollama_row["limits"]["context_window_tokens"] == 16384

    openai_fields = profile_fields({"pricing": {"input_per_million": 2.5, "output_per_million": 10.0}}, client="api", provider="openai")
    assert any(f.name == "input_per_million" for f in openai_fields)
    openai_row = profile_from_form(
        {
            "label": "OpenAI",
            "model": "gpt-4.1",
            "base_url": "https://api.openai.com/v1",
            "credential_ref": "env:OPENAI_API_KEY",
            "max_model_calls": "15",
            "max_output_tokens": "4096",
            "command_timeout_seconds": "90",
            "input_per_million": "2.5",
            "output_per_million": "10.0",
        },
        identifier="oa",
        client="api",
        provider="openai",
    )
    assert openai_row["pricing"] == {"input_per_million": 2.5, "output_per_million": 10.0}
    assert openai_row["credential_ref"] == "env:OPENAI_API_KEY"

    copilot_fields = profile_fields({}, client="copilot")
    assert any(f.name == "max_tool_calls" for f in copilot_fields)
    copilot_row = profile_from_form(
        {
            "label": "Copilot",
            "model": "gpt-4o",
            "credential_ref": "",
            "max_tool_calls": "12",
            "max_turn_seconds": "300",
            "command_timeout_seconds": "60",
        },
        identifier="cp",
        client="copilot",
    )
    assert copilot_row["limits"]["max_turn_seconds"] == 300

    with pytest.raises(ValueError, match="whole-number"):
        profile_from_form(
            {
                "label": "OpenAI",
                "model": "gpt-4.1",
                "base_url": "https://api.openai.com/v1",
                "max_model_calls": "10",
                "max_output_tokens": "1000",
                "command_timeout_seconds": "60",
                "input_per_million": "2.5",
                "output_per_million": "",
            },
            identifier="oa",
            client="api",
            provider="openai",
        )

    with pytest.raises(ValueError, match="whole-number"):
        profile_from_form(
            {"label": "Bad", "max_tool_calls": "abc", "max_turn_seconds": "10", "command_timeout_seconds": "10"},
            identifier="bad",
            client="copilot",
        )


@pytest.mark.asyncio
async def test_discovery_modal_select_cancel_scan_and_errors():
    calls = []

    class DiscoveryBroker:
        def __init__(self):
            self.fail_get = False
            self.fail_delete = False
            self.rows = [{"name": "Alpha", "root": "/tmp/alpha", "memory_snapshot": True}]

        def request(self, method, path, **kwargs):
            calls.append((method, path))
            if method == "GET" and path == "/api/discovery":
                if self.fail_get:
                    raise BrokerError(500, "scan broken")
                return {"state": "done", "candidates": self.rows, "errors": ["warn"]}
            if method == "DELETE" and path == "/api/discovery":
                if self.fail_delete:
                    raise BrokerError(500, "cancel failed")
                return {"state": "cancelled"}
            return {}

    broker = DiscoveryBroker()
    modal = DiscoveryModal(broker)
    host = _ModalHost(modal)
    async with host.run_test(size=(90, 26)) as pilot:
        await settle(host, pilot)
        assert modal.query_one("#discovery-rows", DataTable).row_count == 1
        await pilot.click("#discovery-cancel")
        await settle(host, pilot)
        assert ("DELETE", "/api/discovery") in calls
        broker.fail_delete = True
        broker.fail_get = True
        modal.query_one("#discovery-cancel", Button).press()
        await settle(host, pilot)
        notice_txt = str(modal.query_one("#discovery-notice", Static).render())
        assert "cancel failed" in notice_txt or "scan broken" in notice_txt
        modal.refresh_results()
        await settle(host, pilot)
        assert "scan broken" in str(modal.query_one("#discovery-notice", Static).render())
        modal.query_one("#discovery-select", Button).press()
        await pilot.pause()
        assert host.result == {"name": "Alpha", "root": "/tmp/alpha", "memory_snapshot": True}

    modal_close = DiscoveryModal(DiscoveryBroker())
    host_close = _ModalHost(modal_close)
    async with host_close.run_test(size=(100, 40)) as pilot:
        await settle(host_close, pilot)
        modal_close.query_one("#discovery-close", Button).press()
        await pilot.pause()
        assert host_close.result is None

    modal_esc = DiscoveryModal(DiscoveryBroker())
    host_esc = _ModalHost(modal_esc)
    async with host_esc.run_test(size=(90, 26)) as pilot:
        await settle(host_esc, pilot)
        modal_esc.action_close()
        await pilot.pause()
        assert host_esc.result is None


class RichBroker(FakeBroker):
    def __init__(self, project):
        super().__init__(project)
        self.profiles = [
            {"id": "codex", "client": "codex", "label": "Codex"},
            {"id": "claude", "client": "claude", "label": "Claude"},
            {"id": "copilot", "client": "copilot", "label": "Copilot"},
            {"id": "openai", "client": "api", "provider": "openai", "label": "OpenAI", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"},
        ]
        self.settings_data = {
            "default_slots": [
                {"id": "slot-1", "profile_id": "codex"},
                {"id": "slot-2", "profile_id": "claude"},
            ],
            "project_slots": {},
            "scan_roots": ["/work"],
        }
        self.runs = []
        self.status_payload = {"ok": True}
        self.bg_enable_payload = {"accepted": True, "project_id": project["id"], "worker_running": True}
        self.copilot_status = {"runtime": {"available": True}, "code": "ready"}
        self.copilot_prepare_payload = {"available": True}
        self.copilot_login_payload = {"run": {"id": "run-login-1"}}
        self.models_payload = {"models": [{"id": "gpt-4.1", "enabled": True}]}
        self.new_run_payload = {"runs": [{"id": "run-new-1"}]}
        self.fail_paths = {}

    def request(self, method, path, *, params=None, json=None, timeout=20):
        key = (method, path)
        if key in self.fail_paths:
            raise self.fail_paths[key]
        if path == "/api/state":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return {
                "projects": [self.project],
                "profiles": deepcopy(self.profiles),
                "settings": deepcopy(self.settings_data),
                "runs": deepcopy(self.runs),
            }
        if path == "/api/discovery":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return {"state": "done", "candidates": []}
        if path == "/api/status":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return deepcopy(self.status_payload)
        if path == "/api/background-inference/enable":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return deepcopy(self.bg_enable_payload)
        if path == "/api/ask":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return {"answer": "42", "citations": []}
        if path == "/api/brief":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return {"text": "Stopped at step 2."}
        if path == "/api/settings" and method == "PUT":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            self.settings_data.update(deepcopy(json or {}))
            return {"ok": True}
        if path == "/api/profiles" and method == "PUT":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            self.profiles = deepcopy(json["profiles"])
            return {"profiles": deepcopy(self.profiles)}
        if path.endswith("/credential"):
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return {"ok": True}
        if path.endswith("/probe"):
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return {"ok": True, "latency_ms": 12}
        if path == "/api/copilot/prepare":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return deepcopy(self.copilot_prepare_payload)
        if path == "/api/copilot/login":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return deepcopy(self.copilot_login_payload)
        if path.endswith("/models"):
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return deepcopy(self.models_payload)
        if path.endswith("/status") or path == "/api/auth":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return deepcopy(self.copilot_status)
        if path.endswith("/interrupt"):
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return {"ok": True}
        if path == "/api/client-runs":
            self.calls.append((method, path, deepcopy(params or {}), deepcopy(json)))
            return deepcopy(self.new_run_payload)
        return super().request(method, path, params=params, json=json, timeout=timeout)


def _setup_rich(tmp_path, monkeypatch, *, project=True):
    proj = {"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")}
    broker = RichBroker(proj)
    workspace = FakeWorkspace(proj if project else None)
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("cairnmind.terminal.workspace.refresh_workspace", lambda *args, **kwargs: workspace.read())
    return settings, broker, workspace


@pytest.mark.asyncio
async def test_sidebar_scan_ask_brief_snapshot_memory_and_more_flows(tmp_path, monkeypatch):
    settings, broker, workspace = _setup_rich(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    notices = []
    async with app.run_test(size=(100, 38)) as pilot:
        await settle(app, pilot)
        monkeypatch.setattr(app, "notice", lambda value, kind="info": notices.append(str(value)))
        monkeypatch.setattr(app, "notice_for", lambda subject, value, kind="info": notices.append(str(value)))

        # scan_projects: cancel -> existing project -> new project -> error
        seq = iter([None, {"roots": "/work"}, {"root": broker.project["root"], "name": "Project A"}, {"roots": "/work"}, {"root": "/new-proj", "name": "NewP"}])
        async def fake_push(screen):
            return next(seq, None)
        monkeypatch.setattr(app, "push_screen_wait", fake_push)
        app.scan_projects()
        await settle(app, pilot)
        app.scan_projects()
        await settle(app, pilot)
        assert workspace.selections[-1][0] == "project-a"
        app.scan_projects()
        await settle(app, pilot)
        assert any(path == "/api/projects" for _, path, _, _ in broker.calls)

        broker.fail_paths[("POST", "/api/discovery")] = BrokerError(500, "disc err")
        seq = iter([{"roots": "/work"}])
        app.scan_projects()
        await settle(app, pilot)
        assert any("disc err" in n for n in notices)
        broker.fail_paths.pop(("POST", "/api/discovery"))

        # action_memory, action_snapshot, action_refresh
        app.action_refresh()
        app.action_memory()
        await settle(app, pilot)
        assert workspace.read()["command_request"]["action"] == "memory"
        app.action_snapshot()
        await settle(app, pilot)
        assert workspace.read()["selected_entity"]["entity_type"] == "memory"

        # action_ask & action_brief (cancel + success + error)
        seq = iter([None, {"question": "What?"}, None])
        app.action_ask()
        await settle(app, pilot)
        app.action_ask()
        await settle(app, pilot)
        assert any(path == "/api/ask" for _, path, _, _ in broker.calls)

        seq = iter([None])
        app.action_brief()
        await settle(app, pilot)
        assert any(path == "/api/brief" for _, path, _, _ in broker.calls)

        # action_more -> details, notices, legacy_groups/forget_project/record_kinds/stages/mcp error paths
        seq = iter(["details", None, "notices", None])
        app.action_more()
        await settle(app, pilot)
        app.action_more()
        await settle(app, pilot)

        async def boom(*args, **kwargs):
            raise RuntimeError("subview failed")
        monkeypatch.setattr("cairnmind.terminal.workspace_stage.run_next_stage", boom)
        monkeypatch.setattr("cairnmind.terminal.workspace_stage.edit_stages", boom)
        monkeypatch.setattr("cairnmind.terminal.workspace_mcp.edit_mcp_servers", boom)
        monkeypatch.setattr("cairnmind.terminal.terminal_legacy_groups.open_view", boom)
        monkeypatch.setattr("cairnmind.terminal.terminal_project_forget.open_view", boom)
        monkeypatch.setattr("cairnmind.terminal.terminal_record_kinds.open_view", boom)
        for fn in (app.action_next_stage, app.edit_stages, app.edit_mcp_servers, app.legacy_groups, app.forget_project, app.record_kinds):
            fn()
            await settle(app, pilot)
        assert sum("subview failed" in n for n in notices) >= 6


@pytest.mark.asyncio
async def test_sidebar_assistants_slots_queue_and_settings(tmp_path, monkeypatch):
    settings, broker, workspace = _setup_rich(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    notices = []
    async with app.run_test(size=(100, 38)) as pilot:
        await settle(app, pilot)
        monkeypatch.setattr(app, "notice", lambda value, kind="info": notices.append(str(value)))
        monkeypatch.setattr(app, "notice_for", lambda subject, value, kind="info": notices.append(str(value)))

        # add_assistant: none chosen, "+", add new profile_id ("openai"), then error when no project
        answers = iter([None, "+", None, "openai"])
        async def push(screen):
            return next(answers, None)
        monkeypatch.setattr(app, "push_screen_wait", push)
        app.add_assistant()
        await settle(app, pilot)
        app.add_assistant()
        await settle(app, pilot)
        app.add_assistant()
        await settle(app, pilot)
        assert any(path == "/api/settings" and method == "PUT" for method, path, _, _ in broker.calls)

        # edit_slots: choose, replace, up, remove (confirmed), remove (only 1 slot left)
        slots_after_add = app.configured_slots("project-a")
        first_id, second_id = slots_after_add[0]["id"], slots_after_add[1]["id"]
        answers = iter([
            first_id, "choose",
            first_id, "replace", "copilot",
            second_id, "up",
            second_id, "remove", True,
            "+", None,
            None,
        ])
        for _ in range(6):
            app.edit_slots()
            await settle(app, pilot)

        # action_queue: deterministic-only, cancel, accepted=False, accepted=True
        broker.status_payload = {"models": "off"}
        app.action_queue()
        await settle(app, pilot)
        broker.status_payload = {"ok": True}
        answers = iter([False, True, True])
        app.action_queue()
        await settle(app, pilot)
        broker.bg_enable_payload = {"accepted": False, "project_id": "project-a"}
        app.action_queue()
        await settle(app, pilot)
        broker.bg_enable_payload = {"accepted": True, "project_id": "project-a", "worker_running": True}
        app.action_queue()
        await settle(app, pilot)
        assert any("Memory processing enabled" in n for n in notices)

        # clear_deferred_launch_error mismatch returns False
        assert await app.clear_deferred_launch_error(("other-project", "/other")) is False

        # action_settings: cancel, topology error, language change, reduced_motion
        async def topo_boom(self_app):
            raise RuntimeError("topo err")
        monkeypatch.setattr("cairnmind.terminal.terminal_setup.open_view", topo_boom)
        answers = iter([None, "topology", "language", "pl", "reduced_motion", "on"])
        for _ in range(4):
            app.action_settings()
            await settle(app, pilot)
        assert app.language == "pl"

        # add_assistant & action_queue when no project selected
        workspace.select_project(None)
        app.workspace_data = workspace.read()
        app.add_assistant()
        await settle(app, pilot)
        app.action_queue()
        await settle(app, pilot)
        assert any("projekt" in n.lower() or "project" in n.lower() for n in notices)


@pytest.mark.asyncio
async def test_sidebar_login_profile_connection_and_sessions(tmp_path, monkeypatch):
    settings, broker, workspace = _setup_rich(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    notices = []
    auth_opened = []
    selected_runs = []
    taken_runs = []
    resumed_runs = []
    monkeypatch.setattr("cairnmind.terminal.workspace.show_auth", lambda *args: auth_opened.append(args))
    monkeypatch.setattr("cairnmind.terminal.workspace.select_run", lambda *args: selected_runs.append(args))
    monkeypatch.setattr("cairnmind.terminal.workspace.take_control", lambda *args: taken_runs.append(args))
    monkeypatch.setattr("cairnmind.terminal.workspace.resume_run", lambda *args: resumed_runs.append(args))

    async with app.run_test(size=(100, 38)) as pilot:
        await settle(app, pilot)
        monkeypatch.setattr(app, "notice", lambda value, kind="info": notices.append(str(value)))

        # login -> "+" -> None
        answers = iter(["+", None])
        async def push(screen):
            return next(answers, None)
        monkeypatch.setattr(app, "push_screen_wait", push)
        app.login()
        await settle(app, pilot)

        codex, copilot, openai = broker.profiles[0], broker.profiles[2], broker.profiles[3]

        # profile_connection actions across codex, openai, copilot
        answers = iter(["native_login"])
        await app.profile_connection(codex)
        answers = iter(["edit", None])
        await app.profile_connection(codex)
        await settle(app, pilot)
        answers = iter([
            "credential", {"key": "sk-1", "persist": False},
            "delete", True,
            "probe", True, None,
            "models", "gpt-4.1",
            "status", None,
        ])
        await app.profile_connection(openai)
        await app.profile_connection(openai)
        await app.profile_connection(openai)
        await app.profile_connection(openai)
        await app.profile_connection(openai)
        assert auth_opened
        assert any(path.endswith("/credential") and m == "POST" for m, path, _, _ in broker.calls)
        assert any(path.endswith("/credential") and m == "DELETE" for m, path, _, _ in broker.calls)
        assert any(path.endswith("/probe") for _, path, _, _ in broker.calls)

        # empty models list
        broker.models_payload = {"models": []}
        answers = iter(["models", None])
        await app.profile_connection(openai)

        # copilot: prepare, status, login (unavailable -> prepare -> login with run, and login without run)
        broker.copilot_status = {"runtime": {"available": False}, "code": "missing_runtime"}
        broker.copilot_prepare_payload = {"available": True}
        answers = iter([
            "prepare", True, None,
            "status", None,
            "login", True,
            "login", True, None,
        ])
        await app.profile_connection(copilot)
        await app.profile_connection(copilot)
        await app.profile_connection(copilot)
        assert selected_runs[-1][1] == "run-login-1"
        broker.copilot_login_payload = {"instructions": "open browser"}
        await app.profile_connection(copilot)

        # sessions: empty notice first
        broker.runs = []
        app.sessions()
        await settle(app, pilot)
        assert any("No sessions yet" in n for n in notices)

        # sessions: show ended, takeover, interrupt, resume, new (ok + error)
        run_live = {
            "id": "run-live-1", "client": "codex", "worktree": "/w", "project_id": "project-a",
            "slot_id": "slot-1", "generation": 1, "lifecycle": "running", "memory": {"acknowledged": True},
        }
        run_done = {
            "id": "run-done-1", "client": "codex", "worktree": "/w", "project_id": "project-a",
            "slot_id": "slot-1", "generation": 1, "lifecycle": "exited", "memory": {"acknowledged": False},
        }
        broker.runs = [run_live, run_done]
        answers = iter([
            "run-done-1", "show",
            "run-live-1", "takeover",
            "run-live-1", "interrupt",
            "run-done-1", "resume",
            "run-done-1", "new",
            "run-done-1", "new",
        ])
        for _ in range(5):
            app.sessions()
            await settle(app, pilot)
        assert taken_runs and resumed_runs
        assert any(path.endswith("/interrupt") for _, path, _, _ in broker.calls)
        broker.new_run_payload = {"errors": ["boom"]}
        app.sessions()
        await settle(app, pilot)
        assert any("Starting a new session" in n for n in notices)


def test_panels_main_dispatches_all_kinds(tmp_path, monkeypatch):
    ran = []

    class DummyApp:
        def __init__(self, *args, **kwargs):
            ran.append((args, kwargs))

        def run(self):
            ran.append("run")

    monkeypatch.setattr("cairnmind.terminal.terminal_panels.SidebarApp", DummyApp)
    monkeypatch.setattr("cairnmind.terminal.terminal_inspector.InspectorApp", DummyApp)
    monkeypatch.setattr("cairnmind.terminal.terminal_graph.GraphApp", DummyApp)
    monkeypatch.setattr("cairnmind.terminal.terminal_activity.activity_app", lambda *a, **k: DummyApp(*a, **k))

    for kind in ("sidebar", "inspector", "graph", "activity"):
        monkeypatch.setattr("sys.argv", ["panels", "--kind", kind, "--data-dir", str(tmp_path)])
        panels_main()
    assert ran.count("run") == 4
