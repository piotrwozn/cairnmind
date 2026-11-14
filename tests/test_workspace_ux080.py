"""User intent, keyboard navigation and additive UI settings for 0.8."""
import pytest

from cairnmind.cockpit_state import CockpitState
from cairnmind.config import Settings
from cairnmind.workspace_state import WorkspaceState
from test_workspace import manager as manager


def test_single_assistant_start_never_starts_other_configured_slots(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project, intent="work", requested_slot_ids=["first"])
    workspace.refresh()
    workspace.refresh()
    starts = [kwargs["json"] for method, path, kwargs in broker.calls if method == "POST"]
    assert len(starts) == 1
    assert starts[0]["slots"] == [{"id": "first", "profile_id": "codex"}]
    assert workspace.state.read()["run_ids"] == ["run-first"]


def test_unknown_assistant_request_is_not_executed_or_retried(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project, intent="work", requested_slot_ids=["unconfigured"])
    workspace.refresh()
    workspace.refresh()
    assert not any(method == "POST" for method, _, _ in broker.calls)
    state = workspace.state.read()
    assert state["work_request"] == state["handled_work_request"]


@pytest.mark.parametrize("same_project", [False, True])
def test_navigation_during_fetch_cancels_unclaimed_start(manager, same_project):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project, intent="work", requested_slot_ids=["first"])
    original = broker.request
    def request(method, path, **kwargs):
        if path == "/api/state":
            target = project if same_project else {"id": "other", "name": "Other", "root": str(workspace.settings.data_dir)}
            workspace.state.select_project(target, intent="inspect")
        return original(method, path, **kwargs)
    broker.request = request
    workspace.refresh()
    assert not any(method == "POST" for method, _, _ in broker.calls)
    assert workspace.state.read()["intent"] == "inspect"


def test_focus_view_and_next_key_preserve_panes(manager):
    workspace, _, _ = manager
    CockpitState(workspace.settings).update({"view": "focus"})
    workspace.create(start_supervisor=False)
    before = workspace.panes()
    assert workspace.state.read()["layout"] == "compact"
    # At the created 180 columns the activity column is beside the focused view (#149), not zoomed.
    assert workspace.tmux("display-message", "-p", "#{window_zoomed_flag}").stdout.strip() == "0"
    assert workspace.activity_pane() is not None
    workspace.next_view()
    assert workspace.state.read()["focus"] == "client1"
    assert workspace.panes() == before
    keys = workspace.tmux("list-keys", "-T", "prefix").stdout
    assert "--palette" in keys and "--help-popup" in keys and "--next-view" in keys and "--finding" in keys
    assert "copy-mode" in keys and "send-prefix" in keys


def test_ui_preferences_are_additive_and_invalid_change_is_atomic(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    preferences = CockpitState(settings)
    default = preferences.load()
    assert (default["language"], default["view"], default["reduced_motion"]) == ("en", "focus", False)
    assert not settings.data_dir.exists()
    saved = preferences.update({"language": "pl", "view": "cockpit", "reduced_motion": True})
    before = preferences.path.read_bytes()
    assert saved["profiles"] == default["profiles"] and saved["default_slots"] == default["default_slots"]
    for change in ({"language": []}, {"view": "unknown"}, {"reduced_motion": 1}):
        with pytest.raises(ValueError):
            preferences.update(change)
        assert preferences.path.read_bytes() == before


@pytest.mark.parametrize("requested", [None, [], ["a", "a"], [{}], "a"])
def test_start_requires_explicit_typed_assistant_selection(tmp_path, requested):
    state = WorkspaceState(Settings(data_dir=tmp_path / "data"))
    with pytest.raises(ValueError):
        state.select_project({"id": "p", "root": str(tmp_path)}, intent="work", requested_slot_ids=requested)
    assert state.read()["revision"] == 0


def test_workspace_command_is_allowlisted_and_non_executable(tmp_path):
    state = WorkspaceState(Settings(data_dir=tmp_path / "data"))
    for command in ({"id": "one", "action": "shell", "target_role": "sidebar"},
                    {"id": "two", "action": [], "target_role": "sidebar"},
                    {"id": "three", "action": "settings", "target_role": "client1"}):
        with pytest.raises(ValueError):
            state.update({"command_request": command})
    state.update({"command_request": {"id": "four", "action": "settings", "target_role": "sidebar"}})
    assert state.read()["command_request"]["action"] == "settings"


def test_navigation_discards_pending_workspace_command(tmp_path):
    state = WorkspaceState(Settings(data_dir=tmp_path / "data"))
    project = {"id": "first", "root": str(tmp_path)}
    state.select_project(project)
    state.update({"command_request": {"id": "one", "action": "start_work", "target_role": "sidebar"}})
    state.select_project({**project, "id": "second"})
    assert state.read()["command_request"] is None


def test_popup_targets_exact_initiating_writer_and_is_not_timed_out(manager, monkeypatch):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    with monkeypatch.context() as patch:
        patch.setattr(workspace, "clients", lambda: [
            {"pid": 101, "tty": "/dev/pts/1", "readonly": False},
            {"pid": 202, "tty": "/dev/pts/2", "readonly": True}])
        calls = []
        patch.setattr(workspace, "exists", lambda: True)
        patch.setattr(workspace, "tmux", lambda *args, **kwargs: calls.append((args, kwargs)))
        workspace.open_palette(client_pid=101, client_tty="/dev/pts/1")
        assert calls[0][0][:3] == ("display-popup", "-c", "/dev/pts/1")
        assert calls[0][1]["timeout"] is None
        with pytest.raises(ValueError):
            workspace.open_palette(client_pid=202, client_tty="/dev/pts/2")
        with pytest.raises(ValueError):
            workspace.open_palette(client_pid=101, client_tty="/dev/pts/2")
        assert len(calls) == 1


@pytest.mark.parametrize("language", ["en", "pl"])
def test_status_literal_width_and_cached_negative_evidence(language):
    import re
    from rich.cells import cell_len
    from cairnmind.terminal_i18n import tr
    from cairnmind.workspace import workspace_status_lines

    selected = {"project_name": "#{pid}#(true)#[fg=red]\x1b\u202e日本語e\u0301" * 5,
                "broker_available": True}
    slot = {"run_id": "#{pid}#[red]", "client": "codex", "profile_id": "p", "lifecycle": "interrupted",
            "memory": {"acknowledged": True, "stale": True}}
    cached = {"profiles": [{"id": "p", "client": "codex", "secret": "DO_NOT_DISPLAY"}],
              "auth": {"codex": {"auth": "reported_signed_in", "ready_to_launch": True,
                                    "connectivity": "unavailable", "error": "DO_NOT_DISPLAY"}}}
    first, second = workspace_status_lines(selected, cached, slot, "controller", language)
    assert "＃{pid}" in first and "#{pid}" not in first and "#(true)" not in first
    assert "\x1b" not in first and "\u202e" not in first
    assert "DO_NOT_DISPLAY" not in first + second
    assert tr("Stale", language) in second and tr("Offline", language) in second
    assert tr("ACK ready", language) not in second and tr("Ready(local)", language) not in second
    assert "colour203" in second and "colour222" in second
    # Both per-client conditional branches fit; actual tmux evaluation is below.
    plain_first = re.sub(r"#\[[^]]*\]", "", first)
    prefix, choices = second.split("#{?client_readonly,")
    for choice in choices[:-1].split(","):
        assert cell_len(re.sub(r"#\[[^]]*\]", "", prefix + choice)) <= 80
    assert cell_len(plain_first) <= 80


@pytest.mark.parametrize("auth,expected", [
    ({}, "Configured"),
    ({"ready_to_launch": True, "credential_configuration": "environment_present"}, "Configured"),
    ({"auth": "reported_signed_in", "ready_to_launch": True}, "Ready(local)"),
    ({"auth": "reported_signed_in", "ready_to_launch": True, "usage": "quota_exhausted"}, "Limited"),
    ({"login_required": True}, "Login needed"),
    ({"error_code": "auth_status_timeout"}, "Unknown"),
])
def test_status_connection_never_infers_network_or_credentials_from_live_run(auth, expected):
    from cairnmind.workspace import workspace_status_lines
    _, line = workspace_status_lines({"broker_available": True},
        {"profiles": [{"id": "p", "client": "codex"}], "auth": {"codex": auth}},
        {"profile_id": "p", "client": "codex", "run_id": "running", "lifecycle": "running"}, None)
    assert "Connection:" + expected in line
    assert "Connected" not in line and "ACK ready" not in line


@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("size", [(80, 24), (120, 36), (180, 55)])
def test_real_tmux_status_visible_in_every_focus_and_each_terminal(manager, language, size):
    import os
    import re
    import time
    from rich.cells import cell_len
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_preview import PreviewTerminal

    workspace, broker, project = manager
    CockpitState(workspace.settings).update({"view": "focus", "language": language})
    workspace.create(start_supervisor=False)
    project = {**project, "name": "#{pid}#[fg=red] Zażółć 日本語 e\u0301 " * 4}
    workspace.state.select_project(project)
    broker.value["runs"] = [{"id": "native-fixture", "generation": 7, "client": "codex", "profile_id": "codex",
        "project_id": project["id"], "worktree": project["root"], "slot_id": "first", "lifecycle": "running",
        "purpose": "coding", "memory": {"status": "ready", "acknowledged": True}}]
    broker.value["auth"] = {"codex": {"auth": "reported_signed_in", "ready_to_launch": True}}
    workspace.refresh()
    pane = workspace.panes()["client1"]
    workspace.tmux("set-option", "-p", "-t", pane, "@cairnmind_control", "controller")
    workspace._update_status(workspace.state.read())
    writer = observer = None
    try:
        writer = PreviewTerminal(workspace, size, readonly=False)
        observer = PreviewTerminal(workspace, size, readonly=True)
        deadline = time.monotonic() + 5
        while len(workspace.clients()) != 2 and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(workspace.clients()) == 2
        # The layout follows the attached terminal first, as the supervisor does: the workspace was created
        # at 180 columns, where the activity column is beside the focused view (#149), and a narrower
        # terminal removes it. Views then change places in the window, so the identities are compared as a set.
        workspace._layout()
        identities = sorted(workspace.tmux("list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}").stdout.splitlines())
        for role in ("client1", "sidebar", "inspector", "graph"):
            workspace.focus(role)
            workspace._update_status(workspace.state.read())
            assert workspace.tmux("show-options", "-gv", "status").stdout.strip() == "2"
            # From 160 columns the activity column is beside the focused view instead of a zoom (#149).
            beside = size[0] >= 160
            assert workspace.tmux("display-message", "-p", "#{window_zoomed_flag}").stdout.strip() == ("0" if beside else "1")
            assert (workspace.activity_pane() is not None) == beside
            for terminal, control in ((writer, "You"), (observer, "Read only")):
                tty = os.ttyname(terminal.slave)
                rows = [workspace.tmux("display-message", "-p", "-c", tty,
                    workspace.tmux("show-options", "-gv", f"status-format[{n}]").stdout.rstrip("\n")).stdout.rstrip("\n") for n in (0, 1)]
                assert all(cell_len(re.sub(r"#\[[^]]*\]", "", row)) <= 80 for row in rows)
                assert tr("Project", language) + ":" in rows[0] and tr("Session", language) + ":" in rows[0]
                assert "native-…" in rows[0] and tr("Running", language) in rows[0]
                assert tr("Memory", language) + ":" + tr("ACK ready", language) in rows[1]
                assert tr("Connection", language) + ":" + tr("Ready(local)", language) in rows[1]
                assert tr("Control", language) + ":" + tr(control, language) in rows[1]
                assert "＃{pid}" in rows[0]  # Actual tmux did not expand hostile project text.
        # Status is emitted to actual terminals, not merely stored in an API object.
        deadline = time.monotonic() + 3
        while tr("Read only", language).encode() not in bytes(observer.output) and time.monotonic() < deadline:
            time.sleep(.02)
        assert tr("Read only", language).encode() in bytes(observer.output)
        assert tr("Memory", language).encode() in bytes(writer.output)
        for binding, control, expected in (("native-fixture:7", "readonly", "Other"),
                                            ("native-fixture:8", "controller", "Unknown")):
            workspace.tmux("set-option", "-p", "-t", pane, "@cairnmind_binding", binding)
            workspace.tmux("set-option", "-p", "-t", pane, "@cairnmind_control", control)
            workspace._update_status(workspace.state.read())
            fmt = workspace.tmux("show-options", "-gv", "status-format[1]").stdout.rstrip("\n")
            actual = workspace.tmux("display-message", "-p", "-c", os.ttyname(writer.slave), fmt).stdout
            assert tr("Control", language) + ":" + tr(expected, language) in actual
        workspace.tmux("set-option", "-p", "-t", pane, "@cairnmind_binding", "native-fixture:7")
        workspace.tmux("set-option", "-p", "-t", pane, "@cairnmind_control", "controller")
        workspace.focus("client2")
        workspace._update_status(workspace.state.read())
        second_slot = workspace.tmux("show-options", "-gv", "status-format[0]").stdout
        assert tr("Session", language) + ":" + tr("None", language) in second_slot
        workspace.focus("client1")
        broker.available = False
        workspace._last_fetch = 0
        workspace.refresh()
        line = workspace.tmux("show-options", "-gv", "status-format[1]").stdout
        assert tr("Unavailable", language) in line and tr("Offline", language) in line
        assert sorted(workspace.tmux("list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}").stdout.splitlines()) == identities
        assert all(method == "GET" and path == "/api/state" for method, path, _ in broker.calls)
    finally:
        import sys
        primary_error = sys.exception()
        cleanup_errors = []
        for terminal in (observer, writer):
            if terminal is not None:
                try:
                    workspace.tmux("detach-client", "-t", os.ttyname(terminal.slave), check=False)
                    terminal.process.wait(timeout=5)
                except Exception as error:
                    cleanup_errors.append(error)
                finally:
                    try:
                        terminal.close()
                    except Exception as error:
                        cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is not None:
                raise BaseExceptionGroup("Status trial and cleanup failed", [primary_error, *cleanup_errors]) from None
            raise ExceptionGroup("Status terminal cleanup failed", cleanup_errors)


def test_resume_adopts_live_slot_after_same_client_profile_edit_without_restarting(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project)
    run = {"id": "unchanged", "generation": 7, "client": "codex", "profile_id": "previous-profile",
           "project_id": project["id"], "worktree": project["root"], "slot_id": "first", "lifecycle": "running",
           "purpose": "coding", "model": "previous-model"}
    broker.value["runs"] = [run]
    workspace.refresh()
    pane = workspace.panes()["client1"]
    pid = workspace.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout
    for _ in range(2):
        workspace.refresh()
        slot = workspace.state.read()["slots"][0]
        assert (slot["run_id"], slot["generation"]) == ("unchanged", 7)
        assert workspace.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout == pid
    assert not any(method == "POST" for method, _, _ in broker.calls)


def test_operational_auth_run_is_never_adopted_by_coding_slot(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project)
    broker.value["runs"] = [{"id": "auth-private", "generation": 9, "client": "codex", "profile_id": "codex",
        "project_id": project["id"], "worktree": project["root"], "slot_id": "first", "lifecycle": "running",
        "purpose": "auth", "output": "PRIVATE_LOGIN_CODE"}]
    workspace.refresh()
    assert all("run_id" not in slot for slot in workspace.state.read()["slots"])
    assert "auth-private" not in workspace.state.read()["run_ids"]
    assert "PRIVATE_LOGIN_CODE" not in workspace.tmux("show-options", "-g").stdout
    assert not any(method == "POST" for method, _, _ in broker.calls)


def test_cockpit_minimum_terminal_includes_both_status_rows(manager):
    import os
    import time
    from cairnmind.terminal_preview import PreviewTerminal
    workspace, _, _ = manager
    CockpitState(workspace.settings).update({"view": "cockpit"})
    workspace.create(start_supervisor=False)
    terminal = PreviewTerminal(workspace, (140, 42), readonly=False)
    try:
        deadline = time.monotonic() + 5
        while not workspace.clients() and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(workspace.clients()) == 1
        workspace._layout()
        assert workspace.tmux("display-message", "-p", "#{window_width} #{window_height}").stdout.strip() == "140 40"
        assert workspace.tmux("show-options", "-gv", "status").stdout.strip() == "2"
        assert workspace.state.read()["layout"] == "wide"
        assert workspace.tmux("display-message", "-p", "#{window_zoomed_flag}").stdout.strip() == "0"
        assert len(workspace.panes()) == 5
    finally:
        try:
            workspace.tmux("detach-client", "-t", os.ttyname(terminal.slave), check=False)
            assert terminal.process.wait(timeout=5) == 0
        finally:
            terminal.close()


def test_failed_native_slot_projects_only_bounded_exit_metadata_without_starting(manager):
    workspace, broker, project = manager
    workspace.create(start_supervisor=False)
    workspace.state.select_project(project, intent="inspect")
    for code in (1, 255, True, -1, 256, "1", None):
        broker.value["runs"] = [{"id": "failed-first", "generation": 1, "client": "codex",
            "profile_id": "codex", "project_id": project["id"], "worktree": project["root"],
            "slot_id": "first", "purpose": "coding", "lifecycle": "failed", "exit_code": code}]
        workspace._last_fetch = 0
        workspace.refresh()
        slot = next(row for row in workspace.state.read()["slots"] if row["id"] == "first")
        assert slot["run_id"] == "failed-first" and slot["lifecycle"] == "failed"
        assert slot["exit_code"] == (code if type(code) is int and 0 <= code <= 255 else None)
    assert not any(method == "POST" for method, _, _ in broker.calls)
