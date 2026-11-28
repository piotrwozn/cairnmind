"""Edge-case tests for workspace.py (mocked tmux/broker manager flows, helpers, CLI main)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import own_time

from cairnmind.config import Settings
from cairnmind.terminal import workspace as ws
from cairnmind.terminal.terminal_client import BrokerError


class _FakeTmuxManager(ws.WorkspaceManager):
    def __init__(self, settings, workspace_id="default"):
        super().__init__(settings, workspace_id, tmux_binary=None)
        self.binary = "tmux"
        self.alive = True
        self.tmux_calls = []
        self.pane_map = {"sidebar": "%1", "slot-1": "%2"}
        self.bound_runs = {"%2": "run-1"}
        self.list_panes_rows = ["%9\tdefault\tauth\trun-auth-1"]
        self.broker_calls = []
        self.broker_responses = {}
        self._broker_instance = self

    def exists(self):
        return self.alive

    def panes(self):
        return dict(self.pane_map)

    def tmux(self, *args, check=True, timeout=10):
        self.tmux_calls.append(args)
        if args and args[0] == "show-options":
            pane = args[3]
            return SimpleNamespace(stdout=self.bound_runs.get(pane, ""), returncode=0)
        if args and args[0] == "new-window":
            return SimpleNamespace(stdout="%9\n", returncode=0)
        if args and args[0] == "list-panes":
            return SimpleNamespace(stdout="\n".join(self.list_panes_rows), returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    def request(self, method, path, *, params=None, json=None, timeout=20):
        self.broker_calls.append((method, path, json))
        val = self.broker_responses.get((method, path))
        if isinstance(val, Exception):
            raise val
        return val if val is not None else {}

    def ensure(self):
        return {"available": True}

    def close(self):
        pass


def test_workspace_manager_take_control_auth_and_supervise(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    mgr = _FakeTmuxManager(settings)

    assert mgr.take_control("run-1") == {"requested": True, "run_id": "run-1"}
    assert mgr.take_control("missing") == {"requested": False, "run_id": "missing"}

    # open_auth validation & copilot/codex branches
    with pytest.raises(ValueError, match="supported client"):
        mgr.open_auth("unknown")
    mgr.broker_responses[("POST", "/api/copilot/login")] = {"run": {"id": "run-auth-1", "client": "copilot", "purpose": "auth", "generation": 1}}
    out = mgr.open_auth("copilot")
    assert out["pane_id"] == "%9"
    with pytest.raises(ValueError, match="Invalid sign-in"):
        mgr.show_auth_run({"id": "x", "client": "codex", "purpose": "coding", "generation": 1})

    # auth_finished kills matching pane; absent workspace returns False
    assert mgr.auth_finished("run-auth-1") == {"closed": True}
    mgr.alive = False
    assert mgr.auth_finished("run-auth-1") == {"closed": False}

    # supervise executes one iteration (swallowing OSError from refresh) then stops
    ticks = [True, False]
    monkeypatch.setattr(mgr, "exists", lambda: ticks.pop(0) if ticks else False)
    monkeypatch.setattr(mgr, "refresh", lambda: (_ for _ in ()).throw(OSError("temp")))
    own_time(monkeypatch, ws, sleep=lambda s: None)
    mgr.supervise()


def test_workspace_launch_detach_stop_and_run_helpers(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")

    # Non-tty launch_workspace raises ValueError
    monkeypatch.setattr(ws.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(ws.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    with pytest.raises(ValueError, match="interactive terminal"):
        ws.launch_workspace(settings)

    # Windows launch_workspace delegates to run_standalone
    monkeypatch.setattr(ws.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(ws.sys, "platform", "win32")
    monkeypatch.setattr("cairnmind.terminal.terminal_standalone.run_standalone", lambda s: 42)
    assert ws.launch_workspace(settings) == 42

    # POSIX launch_workspace paths: inside own tmux pane & join workspace cancel/takeover
    monkeypatch.setattr(ws.sys, "platform", "linux")
    mgr = _FakeTmuxManager(settings)
    monkeypatch.setattr(mgr, "diagnostics", lambda: {"available": True})
    monkeypatch.setattr(mgr, "create", lambda: None)
    monkeypatch.setattr(mgr, "attach", lambda takeover=False: 7 if takeover else 0)
    monkeypatch.setattr(ws, "WorkspaceManager", lambda s, wid="default", **k: mgr)

    monkeypatch.setenv("TMUX", f"{mgr.socket},123,0")
    monkeypatch.setenv("TMUX_PANE", "%1")
    assert ws.launch_workspace(settings, takeover=False) == 0
    with pytest.raises(ValueError, match="Detach this view"):
        ws.launch_workspace(settings, takeover=True)

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(mgr, "clients", lambda: [{"readonly": False}])
    monkeypatch.setattr("cairnmind.terminal.terminal_commands.JoinWorkspaceApp", lambda s: SimpleNamespace(run=lambda: None))
    assert ws.launch_workspace(settings, takeover=False) == 0
    monkeypatch.setattr("cairnmind.terminal.terminal_commands.JoinWorkspaceApp", lambda s: SimpleNamespace(run=lambda: "takeover"))
    assert ws.launch_workspace(settings, takeover=False) == 7

    # detach_workspace absent vs running
    mgr.alive = False
    assert ws.detach_workspace(settings) == {"detached": False, "available": False}
    mgr.alive = True
    assert ws.detach_workspace(settings) == {"detached": True, "available": True}

    # _forget_finished handles stale_workspace_revision retry & re-raises other errors
    attempts = {"n": 0}
    orig_update = mgr.state.update
    def flaky_update(changes, expected_revision=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("stale_workspace_revision")
        return orig_update(changes, expected_revision=expected_revision)
    monkeypatch.setattr(mgr.state, "update", flaky_update)
    ws._forget_finished(mgr, {"r1": "exited"})
    assert attempts["n"] == 2

    # stop_workspace with stop_clients covering unknown, ended, running, error, and supervisor kill
    mgr.state.update({"run_ids": ["r-unk", "r-end", "r-live", "r-err"], "slots": [{"id": "s1", "profile_id": "codex", "run_id": "r-live", "lifecycle": "running"}]})
    mgr.broker_responses[("GET", "/api/client-runs/r-unk")] = BrokerError(404, "Unknown native run")
    mgr.broker_responses[("GET", "/api/client-runs/r-end")] = {"id": "r-end", "lifecycle": "exited", "generation": 1}
    mgr.broker_responses[("GET", "/api/client-runs/r-live")] = {"id": "r-live", "lifecycle": "running", "generation": 2}
    mgr.broker_responses[("POST", "/api/client-runs/r-live/stop")] = {"lifecycle": "exited"}
    mgr.broker_responses[("GET", "/api/client-runs/r-err")] = RuntimeError("broker down")
    monkeypatch.setattr(mgr, "_supervisor_info", lambda: {"pid": 999999})
    monkeypatch.setattr(ws.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(ws.os, "O_NOFOLLOW", getattr(ws.os, "O_NOFOLLOW", 0), raising=False)
    stop_res = ws.stop_workspace(settings, stop_clients=True)
    assert stop_res["clients_unknown"] == ["r-unk"]
    assert stop_res["clients_already_ended"] == ["r-end"]
    assert stop_res["clients_stopped"] == ["r-live"]
    assert len(stop_res["errors"]) == 1

    # show_auth, take_control, select_run (auth + coding), resume_run, stop_run
    mgr.alive = True
    mgr.broker_responses[("POST", "/api/auth/login")] = {"run": {"id": "a1", "client": "codex", "purpose": "auth", "generation": 1}}
    assert ws.show_auth(settings, "codex")["pane_id"] == "%9"
    assert ws.take_control(settings, "run-1")["requested"] is True
    mgr.broker_responses[("GET", "/api/client-runs/a1")] = {"id": "a1", "client": "codex", "purpose": "auth", "generation": 1}
    assert ws.select_run(settings, "a1")["pane_id"] == "%9"
    mgr.broker_responses[("GET", "/api/client-runs/c1")] = {"id": "c1", "client": "codex", "purpose": "coding", "project_id": "p1", "worktree": "/w", "slot_id": "s1"}
    monkeypatch.setattr(mgr, "_fetch", lambda force=False: {"projects": [{"id": "p1", "name": "P1", "root": "/w"}]})
    monkeypatch.setattr(mgr, "refresh", lambda: {"refreshed": True})
    assert ws.select_run(settings, "c1") == {"refreshed": True}
    assert ws.resume_run(settings, "c1") == {}
    assert ws.stop_run(settings, "c1", 1) == {}


def test_workspace_main_flag_dispatch(tmp_path, monkeypatch):
    called = []
    fake_mgr = SimpleNamespace(
        open_palette=lambda **k: called.append(("palette", k)),
        open_finding=lambda **k: called.append(("finding", k)),
        open_activity=lambda **k: called.append(("activity", k)),
        next_view=lambda: called.append(("next_view",)),
        request_command=lambda cmd: called.append(("cmd", cmd)),
        supervise=lambda: called.append(("supervise",)),
        focus=lambda role: called.append(("focus", role)),
        toggle_zoom=lambda: called.append(("zoom",)),
    )
    monkeypatch.setattr(ws, "WorkspaceManager", lambda *a, **k: fake_mgr)
    monkeypatch.setattr("cairnmind.terminal.terminal_commands.CommandPaletteApp", lambda m, help_only=False: SimpleNamespace(run=lambda: "help"))
    monkeypatch.setattr("cairnmind.terminal.terminal_commands.dispatch_command", lambda m, c: called.append(("dispatch", c)))
    monkeypatch.setattr("cairnmind.terminal.workspace_finding.run_finding_form", lambda m, tty: called.append(("finding_ui", tty)))

    for flags in (
        ["--palette-ui"],
        ["--palette"],
        ["--finding-ui", "--client-tty", "/dev/pts/1"],
        ["--finding"],
        ["--activity"],
        ["--next-view"],
        ["--ui-command", "search"],
        ["--supervise"],
        ["--focus", "sidebar"],
        ["--toggle-zoom"],
    ):
        monkeypatch.setattr(ws.sys, "argv", ["ws", "--data-dir", str(tmp_path), *flags])
        ws.main()
    assert len(called) == 10
