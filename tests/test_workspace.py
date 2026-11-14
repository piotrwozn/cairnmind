"""Actual private outer tmux; no native models or browser are started."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from types import SimpleNamespace
import shlex
import shutil
import signal
import sys
import time

import pytest

from cairnmind.config import Settings
from cairnmind.cockpit_state import CockpitState
from cairnmind.workspace import WorkspaceManager, workspace_status


class Broker:
    def __init__(self, project):
        self.calls = []
        self.value = {"projects": [project], "profiles": [{"id": "codex", "client": "codex"}, {"id": "claude", "client": "claude"}],
                      "settings": {"default_slots": [{"id": "first", "profile_id": "codex"}, {"id": "second", "profile_id": "claude"}], "project_slots": {}},
                      "runs": []}
        self.available = True

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if not self.available:
            raise ConnectionError
        if path == "/api/state":
            return deepcopy(self.value)
        if path == "/api/client-runs" and method == "POST":
            payload = kwargs["json"]
            profiles = {row["id"]: row for row in self.value["profiles"]}
            runs = [{"id": "run-" + slot["id"], "generation": 1, "client": profiles[slot["profile_id"]]["client"],
                     "profile_id": slot["profile_id"], "project_id": payload["project"], "worktree": payload["worktree"],
                     "slot_id": slot["id"], "lifecycle": "running", "purpose": "coding"} for slot in payload["slots"]]
            self.value["runs"] = runs
            return {"runs": runs, "errors": []}
        if path == "/api/auth/login":
            return {"run": {"id": "auth-run", "generation": 1, "client": kwargs["json"]["client"], "purpose": "auth"}}
        if path.startswith("/api/client-runs/"):
            identifier = path.split("/")[3]
            run = next(row for row in self.value["runs"] if row["id"] == identifier)
            return {**run, "lifecycle": "exited"} if path.endswith("/stop") else deepcopy(run)
        raise AssertionError((method, path))


@pytest.fixture
def manager(tmp_path, monkeypatch):
    binary = (os.environ.get("CAIRNMIND_TEST_TMUX") or os.environ.get("DEVBRAIN_TEST_TMUX")) or shutil.which("tmux")
    if not binary and Path("/tmp/devbrain-tmux-3.4/tmux").is_file():
        binary = "/tmp/devbrain-tmux-3.4/tmux"
    if not binary:
        pytest.skip("real tmux >=3.4 required")
    settings = Settings.load(tmp_path / "data", create=True)
    CockpitState(settings).update({"view": "cockpit"})
    project = {"id": "project-a", "name": "Project A", "root": str(tmp_path)}
    broker = Broker(project)
    manager = WorkspaceManager(settings, tmux_binary=binary, broker=broker)
    if not manager.diagnostics()["available"]:
        pytest.skip("real tmux >=3.4 required")
    idle = shlex.join([sys.executable, "-c", "import time; print('SAFE_UI_VIEW',flush=True); time.sleep(120)"])
    monkeypatch.setattr(manager, "_panel", lambda role: idle)
    monkeypatch.setattr(manager, "_bridge_command", lambda *args: idle)
    monkeypatch.setattr(manager, "_activity_command", lambda *args: idle)
    try:
        yield manager, broker, project
    finally:
        manager.tmux("kill-server", check=False)
        shutil.rmtree(manager.socket_directory, ignore_errors=True)


def rows(manager):
    result = manager.tmux("list-panes", "-a", "-F", "#{@cairnmind_role}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}\t#{pane_pid}")
    return {parts[0]: list(map(int, parts[1:])) for line in result.stdout.splitlines() if (parts := line.split("\t"))[0]}


def test_real_five_pane_geometry_private_socket_keys_and_reuse(manager):
    workspace, broker, project = manager
    assert workspace.create(start_supervisor=False)["available"] is True
    original = rows(workspace)
    assert set(original) == {"sidebar", "client1", "client2", "inspector", "graph", "activity"}
    assert set(workspace.panes()) == {"sidebar", "client1", "client2", "inspector", "graph"}
    assert original["activity"][2] == 32 and original["activity"][0] == 180 - 32
    assert original["sidebar"][2] == 24
    assert original["sidebar"][0] == 0 and original["client1"][0] == 25
    assert original["client1"][1] < original["inspector"][1]
    assert original["inspector"][2] > original["graph"][2]
    assert workspace.socket_directory.stat().st_mode & 0o777 == 0o700
    assert workspace.config.stat().st_mode & 0o777 == 0o600
    workspace.create(start_supervisor=False)
    assert rows(workspace) == original and not broker.calls
    bindings = workspace.tmux("list-keys", "-T", "prefix").stdout
    for key in ("p", "1", "2", "i", "g", "z", "d", "C-b", "a"):
        assert key in bindings
    assert "send-prefix" in bindings


def test_explicit_create_recovers_dead_ui_panel_without_restarting_client_views(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project, intent="work", requested_slot_ids=["first", "second"])
    before_state = workspace.state.read()
    before = rows(workspace)
    sidebar = workspace.panes()["sidebar"]
    os.kill(before["sidebar"][-1], signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if workspace.tmux("display-message", "-p", "-t", sidebar, "#{pane_dead}").stdout.strip() == "1":
            break
        time.sleep(.01)
    assert workspace.tmux("display-message", "-p", "-t", sidebar, "#{pane_dead}").stdout.strip() == "1"
    workspace.create(start_supervisor=False)
    after = rows(workspace)
    assert workspace.tmux("display-message", "-p", "-t", sidebar, "#{pane_dead}").stdout.strip() == "0"
    assert after["sidebar"][-1] != before["sidebar"][-1]
    assert after["client1"][-1] == before["client1"][-1]
    assert after["client2"][-1] == before["client2"][-1]
    assert workspace.state.read() == before_state
    assert not broker.calls


def test_restore_inspect_and_repeated_refresh_never_start_native_clients(manager):
    workspace, broker, project = manager
    workspace.state.select_project(project, intent="work", requested_slot_ids=["first", "second"])
    workspace.create(start_supervisor=False)
    workspace.refresh()
    assert not any(method == "POST" for method, _, _ in broker.calls)
    workspace.state.select_project(project, intent="inspect")
    workspace.refresh()
    assert not any(method == "POST" for method, _, _ in broker.calls)
    workspace.state.select_project(project, intent="work", requested_slot_ids=["first", "second"])
    workspace.refresh()
    workspace.refresh()
    starts = [call for call in broker.calls if call[:2] == ("POST", "/api/client-runs")]
    assert len(starts) == 1
    assert set(workspace.state.read()["run_ids"]) == {"run-first", "run-second"}
    before = rows(workspace)
    broker.available = False
    workspace._last_fetch = 0
    workspace.refresh()
    assert workspace.state.read()["broker_available"] is False
    assert rows(workspace) == before


def test_profile_client_change_never_attaches_old_higher_generation(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project)
    broker.value["settings"]["default_slots"] = [{"id": "same-slot", "profile_id": "codex"}]
    base = {"project_id": project["id"], "worktree": project["root"], "slot_id": "same-slot", "profile_id": "codex", "lifecycle": "running", "purpose": "coding"}
    broker.value["profiles"][0]["client"] = "claude"
    broker.value["runs"] = [{**base, "id": "old-codex", "client": "codex", "generation": 5},
                            {**base, "id": "current-claude", "client": "claude", "generation": 1}]
    workspace.refresh()
    assert workspace.state.read()["slots"][0]["run_id"] == "current-claude"
    assert len(workspace.panes()) == 4 and "client2" not in workspace.panes()
    assert rows(workspace)["client1"][2] > 110  # 180 columns less the activity column (#144)
    assert not any(method == "POST" for method, _, _ in broker.calls)


def test_native_border_ack_is_only_validated_broker_state(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project)
    run = {"id": "run-existing", "generation": 1, "client": "codex", "profile_id": "codex",
           "project_id": project["id"], "worktree": project["root"], "slot_id": "first", "lifecycle": "running", "purpose": "coding",
           "memory": {"status": "ready", "acknowledged": False}}
    broker.value["runs"] = [run]
    workspace.refresh()
    pane = workspace.panes()["client1"]
    def badge():
        return workspace.tmux("show-options", "-pv", "-t", pane, "@cairnmind_memory").stdout.strip()
    assert badge() == "ACK required"
    for memory, expected in (({"status": "ready", "acknowledged": True}, "ack"),
                             ({"status": "ready", "acknowledged": False, "stale": True}, "stale"),
                             ({"status": "partial", "acknowledged": False}, "partial")):
        run["memory"] = memory
        workspace._last_fetch = 0
        workspace.refresh()
        assert badge() == expected
    assert not any(method == "POST" for method, _, _ in broker.calls)


def test_compact_focus_and_zoom_preserve_all_view_processes(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    before = {key: value[-1] for key, value in rows(workspace).items() if key != "activity"}
    workspace.tmux("resize-window", "-t", "cairnmind-ui:workspace", "-x", "100", "-y", "30")
    workspace._layout()
    assert workspace.state.read()["layout"] == "compact"
    assert workspace.tmux("display-message", "-p", "-t", workspace.panes()["sidebar"], "#{window_zoomed_flag}").stdout.strip() == "1"
    workspace.focus("graph")
    assert workspace.tmux("display-message", "-p", "-t", workspace.panes()["graph"], "#{pane_active}").stdout.strip() == "1"
    # The activity column (#144) is not a view: below its width it is closed, the five views stay.
    assert {key: value[-1] for key, value in rows(workspace).items()} == before


def test_auth_window_is_owned_ephemeral_and_cannot_close_regular_view(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    before = workspace.panes()
    auth = workspace.open_auth("claude")
    assert auth["run"]["purpose"] == "auth"
    assert "auth-run" in workspace.state.read()["auth_runs"]
    assert workspace.auth_finished("not-owned") == {"closed": False}
    assert workspace.auth_finished("auth-run") == {"closed": True}
    assert workspace.panes() == before and not workspace.state.read()["auth_runs"]


def test_adopted_run_ids_merge_under_concurrent_updates_and_status_does_not_initialize(tmp_path):
    settings = Settings.load(tmp_path / "missing")
    assert workspace_status(settings)["available"] is False
    assert not settings.data_dir.exists()
    manager = WorkspaceManager(settings)
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda n: manager._adopt([f"run-{n}"]), range(12)))
    assert set(manager.state.read()["run_ids"]) == {f"run-{n}" for n in range(12)}


def test_outer_keyboard_claim_requires_exact_identity_and_explicit_takeover(tmp_path, monkeypatch):
    workspace = WorkspaceManager(Settings.load(tmp_path / "missing"))
    calls = []
    writer = {"pid": 10, "tty": "/dev/pts/10", "readonly": False, "ignore_size": False}
    observer = {"pid": 20, "tty": "/dev/pts/20", "readonly": True, "ignore_size": True}
    monkeypatch.setattr(workspace, "clients", lambda: [writer, observer])
    monkeypatch.setattr(workspace, "tmux", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(stdout="", returncode=0))
    assert workspace._claim_attached(999, observer["tty"]) is None and not calls
    assert workspace._claim_attached(observer["pid"], observer["tty"]) is False
    assert all(command[0] == "display-message" for command in calls)
    calls.clear()
    assert workspace._claim_attached(observer["pid"], observer["tty"], takeover=True) is True
    assert calls[0] == ("refresh-client", "-t", writer["tty"], "-f", "read-only,ignore-size")
    assert calls[1] == ("switch-client", "-r", "-c", observer["tty"], "-t", "cairnmind-ui")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("stop_clients", [False, True])
def test_detach_preserves_ui_and_stop_only_targets_explicitly_adopted_runs(manager, monkeypatch, stop_clients):
    from cairnmind import workspace as module
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    before = rows(workspace)
    broker.value["runs"] = [{"id": "adopted", "generation": 2, "project_id": "project-a"},
                            {"id": "unrelated", "generation": 1, "project_id": "project-b"}]
    workspace._adopt(["adopted"])
    monkeypatch.setattr(module, "WorkspaceManager", lambda *args, **kwargs: workspace)
    assert module.detach_workspace(workspace.settings)["detached"] is True
    assert rows(workspace) == before and not broker.calls
    result = module.stop_workspace(workspace.settings, stop_clients=stop_clients)
    assert result["stopped"] is True and not workspace.exists()
    assert result["clients_stopped"] == (["adopted"] if stop_clients else [])
    assert not any("unrelated" in path for _, path, _ in broker.calls)
    assert [path for method, path, _ in broker.calls if method == "POST"] == (["/api/client-runs/adopted/stop"] if stop_clients else [])
