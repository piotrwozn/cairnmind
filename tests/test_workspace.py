"""Actual private outer tmux; no native models or browser are started."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from types import SimpleNamespace
import shlex
import shutil
import signal
import subprocess
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
    monkeypatch.setattr(manager, "_activity_command", lambda *args: idle)  # the activity column (#144)
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
    assert set(original) == {"sidebar", "client1", "client2", "inspector", "graph"}
    assert original["sidebar"][2] == 24
    assert original["sidebar"][0] == 0 and original["client1"][0] == 25
    assert original["client1"][1] < original["inspector"][1]
    assert original["inspector"][2] > original["graph"][2]
    assert workspace.socket_directory.stat().st_mode & 0o777 == 0o700
    assert workspace.config.stat().st_mode & 0o777 == 0o600
    workspace.create(start_supervisor=False)
    assert rows(workspace) == original and not broker.calls
    bindings = workspace.tmux("list-keys", "-T", "prefix").stdout
    for key in ("p", "1", "2", "i", "g", "z", "d", "C-b"):
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
    assert rows(workspace)["client1"][2] > 140
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
    before = {key: value[-1] for key, value in rows(workspace).items()}
    workspace.tmux("resize-window", "-t", "cairnmind-ui:workspace", "-x", "100", "-y", "30")
    workspace._layout()
    assert workspace.state.read()["layout"] == "compact"
    assert workspace.tmux("display-message", "-p", "-t", workspace.panes()["sidebar"], "#{window_zoomed_flag}").stdout.strip() == "1"
    workspace.focus("graph")
    assert workspace.tmux("display-message", "-p", "-t", workspace.panes()["graph"], "#{pane_active}").stdout.strip() == "1"
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
    # #243: the line was Polish ("potem") in an English workspace.
    assert calls[-1][-1] == "READ ONLY: C-b d, then cairnmind workspace takeover"
    calls.clear()
    assert workspace._claim_attached(observer["pid"], observer["tty"], takeover=True) is True
    assert calls[0] == ("refresh-client", "-t", writer["tty"], "-f", "read-only,ignore-size")
    assert calls[1] == ("switch-client", "-r", "-c", observer["tty"], "-t", "cairnmind-ui")
    assert not (tmp_path / "missing").exists()


def test_the_read_only_line_of_an_observer_reads_in_the_workspace_language(tmp_path, monkeypatch):
    from cairnmind.cockpit_state import CockpitState
    settings = Settings.load(tmp_path / "data")
    CockpitState(settings).update({"language": "pl"})
    workspace = WorkspaceManager(settings)
    calls = []
    writer = {"pid": 10, "tty": "/dev/pts/10", "readonly": False, "ignore_size": False}
    observer = {"pid": 20, "tty": "/dev/pts/20", "readonly": True, "ignore_size": True}
    monkeypatch.setattr(workspace, "clients", lambda: [writer, observer])
    monkeypatch.setattr(workspace, "tmux", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(stdout="", returncode=0))
    assert workspace._claim_attached(observer["pid"], observer["tty"]) is False
    assert calls[-1] == ("display-message", "-c", observer["tty"], "-d", "8000",
                         "TYLKO ODCZYT: C-b d, potem cairnmind workspace takeover")


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


def test_stop_with_clients_skips_ended_runs_and_saves_what_it_stopped(manager, monkeypatch):
    # #154: every ended run was reported as client_stop_unavailable, and the saved slots still said running.
    from cairnmind import workspace as module
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    base = {"generation": 1, "client": "codex", "profile_id": "codex", "project_id": project["id"],
            "worktree": project["root"], "purpose": "coding"}
    broker.value["runs"] = [{**base, "id": "live", "slot_id": "first", "lifecycle": "running"},
                            {**base, "id": "ended", "slot_id": "second", "lifecycle": "exited"}]
    workspace._adopt(["ended", "live"])
    workspace.state.update({"slots": [{"id": "first", "profile_id": "codex", "run_id": "live", "lifecycle": "running"},
                                      {"id": "second", "profile_id": "claude", "run_id": "ended", "lifecycle": "running"}]})
    monkeypatch.setattr(module, "WorkspaceManager", lambda *args, **kwargs: workspace)
    result = module.stop_workspace(workspace.settings, stop_clients=True)
    assert result["errors"] == [] and result["clients_stopped"] == ["live"]
    assert result["clients_already_ended"] == ["ended"]
    # #243: the ended run is sent the stop too, which clears the dead pane tmux kept for it; its end stays.
    assert sorted(path for method, path, _ in broker.calls if method == "POST") == [
        "/api/client-runs/ended/stop", "/api/client-runs/live/stop"]
    saved = workspace.state.read()
    assert [slot["lifecycle"] for slot in saved["slots"]] == ["exited", "exited"]
    assert saved["run_ids"] == []


def test_stop_with_clients_still_reports_an_ended_run_whose_pane_cannot_be_cleared(manager, monkeypatch):
    # #243: clearing the dead pane of an ended run is best effort; a refused stop is not a client error.
    from cairnmind import workspace as module
    from cairnmind.terminal_client import BrokerError
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    broker.value["runs"] = [{"id": "ended", "generation": 3, "project_id": project["id"], "lifecycle": "failed"}]
    workspace._adopt(["ended"])
    known = broker.request

    def request(method, path, **kwargs):
        if method == "POST":
            broker.calls.append((method, path, kwargs))
            raise BrokerError(409, "Native run generation changed")
        return known(method, path, **kwargs)
    monkeypatch.setattr(broker, "request", request)
    monkeypatch.setattr(module, "WorkspaceManager", lambda *args, **kwargs: workspace)
    result = module.stop_workspace(workspace.settings, stop_clients=True)
    assert result["errors"] == [] and result["clients_already_ended"] == ["ended"] and result["clients_stopped"] == []
    assert [(method, path, kwargs["json"]) for method, path, kwargs in broker.calls if method == "POST"] == [
        ("POST", "/api/client-runs/ended/stop", {"generation": 3})]
    assert workspace.state.read()["run_ids"] == []


@pytest.mark.parametrize("answer", [(404, "Unknown native run"), (400, "Unknown native run")])
def test_stop_with_clients_forgets_a_run_the_broker_no_longer_knows(manager, monkeypatch, answer):
    # #161: an adopted run missing from the broker's records was reported as client_stop_unavailable.
    # 400 is how a broker before this change answers; 404 is the answer now.
    from cairnmind import workspace as module
    from cairnmind.terminal_client import BrokerError
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    broker.value["runs"] = [{"id": "live", "generation": 1, "project_id": project["id"], "lifecycle": "running"}]
    workspace._adopt(["gone", "live"])
    known = broker.request

    def request(method, path, **kwargs):
        if "/gone" in path:
            broker.calls.append((method, path, kwargs))
            raise BrokerError(*answer)
        return known(method, path, **kwargs)
    monkeypatch.setattr(broker, "request", request)
    monkeypatch.setattr(module, "WorkspaceManager", lambda *args, **kwargs: workspace)
    result = module.stop_workspace(workspace.settings, stop_clients=True)
    assert result["errors"] == [] and result["clients_unknown"] == ["gone"] and result["clients_stopped"] == ["live"]
    assert ("POST", "/api/client-runs/gone/stop") not in [(method, path) for method, path, _ in broker.calls]
    assert workspace.state.read()["run_ids"] == []


@pytest.mark.parametrize("answer", [(503, "The local broker does not answer."), (404, "Not Found")])
def test_stop_with_clients_still_reports_a_broker_that_does_not_answer(manager, monkeypatch, answer):
    # A 404 that does not name the run (a route the service lacks) says nothing about the run (#161 review).
    from cairnmind import workspace as module
    from cairnmind.terminal_client import BrokerError
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace._adopt(["kept"])

    def request(method, path, **kwargs):
        raise BrokerError(*answer)
    monkeypatch.setattr(broker, "request", request)
    monkeypatch.setattr(module, "WorkspaceManager", lambda *args, **kwargs: workspace)
    result = module.stop_workspace(workspace.settings, stop_clients=True)
    assert result["errors"] == [{"run_id": "kept", "code": "client_stop_unavailable"}] and result["clients_unknown"] == []
    assert workspace.state.read()["run_ids"] == ["kept"]  # still adopted: it may be running

def test_a_real_tmux_server_holds_and_answers_the_key(manager, tmp_path):
    from cairnmind import person_proof
    from cairnmind.workspace import SESSION
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    kind, key = person_proof.read_key(workspace.settings, workspace.socket, workspace.binary)
    assert kind == "key" and len(key) == 64 and set(key) <= set("0123456789abcdef")
    assert person_proof.workspace_socket(workspace.settings, str(workspace.socket)) == workspace.socket
    assert workspace.status()["person_key"] == "present" and key not in json.dumps(workspace.status())
    # The key is in no command line of the server's processes and never changes while it lives.
    workspace.create(start_supervisor=False)
    assert person_proof.read_key(workspace.settings, workspace.socket, workspace.binary) == ("key", key)
    # A panel started by the server sees TMUX, with the socket that holds the key.
    seen = tmp_path / "tmux_env"
    workspace.tmux("new-window", "-d", "-t", SESSION, "sh", "-c", f'echo "$TMUX" > {seen}')
    for _ in range(50):
        if seen.exists() and seen.read_text().strip():
            break
        time.sleep(.1)
    assert seen.read_text().split(",")[0] == str(workspace.socket)
    assert person_proof.workspace_headers(workspace.settings, {"TMUX": seen.read_text().strip()}) in (
        {}, {person_proof.KEY_HEADER: key, person_proof.SOCKET_HEADER: str(workspace.socket)})


def test_a_server_without_the_option_and_a_missing_server_answer_no_key_and_unavailable(manager, short_tmp_path):
    from cairnmind import person_proof
    workspace, _, _ = manager
    socket = short_tmp_path / "bare.sock"
    # No server there: unavailable, and asking starts none.
    assert person_proof.read_key(workspace.settings, socket, workspace.binary) == ("unavailable", None)
    assert not socket.exists()
    subprocess.run([workspace.binary, "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "sleep", "60"],
                   check=True, env={k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")})
    try:
        assert person_proof.read_key(workspace.settings, socket, workspace.binary) == ("no_key", None)
    finally:
        subprocess.run([workspace.binary, "-S", str(socket), "kill-server"], check=False)
