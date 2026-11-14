"""The activity column of the workspace and its key (#144), on a real private tmux."""
from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from cairnmind import terminal_notice_feed as feed
from cairnmind.cockpit_state import CockpitState
from cairnmind.terminal_notices import NoticeHistory
from cairnmind.workspace import ACTIVITY_WIDTH, COCKPIT_MIN_WIDTH, SESSION, WorkspaceManager
from cairnmind.workspace_activity import RESPAWN_PAUSE
from test_workspace import manager as manager, rows

ROLES = {"sidebar", "client1", "client2", "inspector", "graph"}


def terminal(workspace, columns, lines):
    """The window of a terminal of this size: tmux keeps two rows for the status lines."""
    workspace.tmux("resize-window", "-t", "cairnmind-ui:workspace", "-x", str(columns), "-y", str(lines - 2))
    workspace._layout()


def shown(workspace):
    """Role -> (left, width) of the panes in the workspace window, the one a terminal shows."""
    result = workspace.tmux("list-panes", "-t", SESSION + ":workspace", "-F",
                            "#{@cairnmind_role}\t#{pane_left}\t#{pane_width}").stdout
    return {parts[0]: (int(parts[1]), int(parts[2])) for line in result.splitlines() if (parts := line.split("\t"))}


def view(workspace, name):
    CockpitState(workspace.settings).update({"view": name})


def dead(workspace, pane):
    return workspace.tmux("display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip()


def zoomed(workspace):
    return workspace.tmux("display-message", "-p", "-t", SESSION + ":workspace", "#{window_zoomed_flag}").stdout.strip()


@pytest.mark.parametrize("size", [(160, 45), (180, 55), (COCKPIT_MIN_WIDTH + 20, 60)])
def test_focus_zooms_as_before_at_every_width_and_moves_no_pane(manager, size):
    workspace, broker, _ = manager
    view(workspace, "focus")  # the default layout
    workspace.create(start_supervisor=False)
    terminal(workspace, *size)
    before = {role: value[-1] for role, value in rows(workspace).items()}
    for role in ("client1", "graph", "sidebar", "client2"):
        workspace.focus(role)
        assert workspace.activity_pane() is None and zoomed(workspace) == "1"
        assert set(shown(workspace)) == ROLES  # every view stays in the one workspace window
        assert len(workspace.tmux("list-windows", "-t", SESSION, "-F", "#{window_id}").stdout.split()) == 1
    assert {role: value[-1] for role, value in rows(workspace).items()} == before
    assert not broker.calls


def test_a_column_whose_process_ended_is_started_again_in_place(manager, monkeypatch):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, COCKPIT_MIN_WIDTH, 55)
    pane = workspace.activity_pane()

    def end_it():
        pid = int(workspace.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while dead(workspace, pane) != "1" and time.monotonic() < deadline:
            time.sleep(.05)
        assert dead(workspace, pane) == "1"
    end_it()
    sent, real = [], workspace.tmux

    def tmux(*args, **kwargs):
        sent.append(args)
        return real(*args, **kwargs) if args[0] != "display-popup" else None
    monkeypatch.setattr(workspace, "_owner", lambda pid, tty: {"tty": "/dev/pts/7"})
    monkeypatch.setattr(workspace, "tmux", tmux)
    workspace.open_activity(client_pid=1, client_tty="/dev/pts/7")  # a dead column is never selected
    assert any(args[0] == "display-popup" for args in sent) and not any(args[0] == "select-pane" for args in sent)
    workspace._layout()
    assert workspace.activity_pane() == pane and dead(workspace, pane) == "0"
    assert workspace.tmux("display-message", "-p", "-t", pane, "#{@cairnmind_role}").stdout.strip() == "activity"
    assert shown(workspace)["activity"] == (COCKPIT_MIN_WIDTH - ACTIVITY_WIDTH, ACTIVITY_WIDTH)
    assert not any(args[0] in {"join-pane", "break-pane"} for args in sent)  # restarted in place, never moved
    end_it()
    workspace._layout()  # ended again at once: no restart loop, it waits for the pause
    assert dead(workspace, pane) == "1"
    workspace._activity_restarted -= RESPAWN_PAUSE
    workspace._layout()
    assert workspace.activity_pane() == pane and dead(workspace, pane) == "0"
    assert workspace.exists()


@pytest.mark.parametrize("size", [(160, 45), (180, 55), (COCKPIT_MIN_WIDTH - 1, 55), (80, 24), (120, 40)])
def test_cockpit_has_no_column_where_it_would_narrow_a_coding_pane(manager, size):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, *size)
    assert workspace.activity_pane() is None
    assert set(rows(workspace)) == ROLES


def test_cockpit_column_from_its_width_keeps_the_coding_panes_as_wide_as_at_180(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, COCKPIT_MIN_WIDTH, 55)
    window = shown(workspace)
    assert window["activity"] == (COCKPIT_MIN_WIDTH - ACTIVITY_WIDTH, ACTIVITY_WIDTH)
    assert window["client1"][1] >= 77 and window["client2"][1] >= 77
    assert set(workspace.panes()) == ROLES and workspace.state.read()["layout"] == "wide"
    terminal(workspace, 180, 55)
    assert workspace.activity_pane() is None and shown(workspace)["client1"] == (25, 77)


def test_two_processes_arranging_at_once_make_one_column(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, 180, 55)
    workspace.tmux("resize-window", "-t", "cairnmind-ui:workspace", "-x", str(COCKPIT_MIN_WIDTH), "-y", "53")
    others = [WorkspaceManager(workspace.settings, tmux_binary=workspace.binary) for _ in range(3)]
    for other in others:
        other._activity_command = workspace._activity_command
    errors = []

    def arrange(other):
        try:
            other._layout()
        except Exception as error:  # reported below
            errors.append(error)
    threads = [threading.Thread(target=arrange, args=(other,)) for other in others]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not errors
    assert workspace.tmux("list-panes", "-a", "-F", "#{@cairnmind_role}").stdout.split().count("activity") == 1
    assert set(shown(workspace)) == ROLES | {"activity"}


def test_a_left_over_second_column_is_removed_and_the_layout_still_applies(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, COCKPIT_MIN_WIDTH, 55)
    first = workspace.activity_pane()
    extra = workspace.tmux("split-window", "-d", "-h", "-f", "-l", "10", "-t", first, "-P", "-F", "#{pane_id}",
                           workspace._activity_command()).stdout.strip()
    workspace._tag(extra, "activity")
    workspace._last_layout = None
    workspace._layout()
    assert workspace.tmux("list-panes", "-a", "-F", "#{@cairnmind_role}").stdout.split().count("activity") == 1
    assert shown(workspace)["activity"][1] == ACTIVITY_WIDTH
    # Killing a column another process already removed is not an error.
    gone = workspace.activity_pane()
    workspace.tmux("kill-pane", "-t", gone)
    assert workspace._reconcile_activity(SESSION + ":workspace", gone, False, False) is None


def test_zoom_and_next_view_keep_their_five_stops(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, COCKPIT_MIN_WIDTH, 55)
    seen = []
    for _ in range(6):
        workspace.next_view()
        seen.append(workspace.state.read()["focus"])
    assert set(seen) == ROLES and "activity" not in seen
    workspace.state.update({"zoom": True})
    workspace._layout()
    assert zoomed(workspace) == "1"
    assert workspace.activity_pane() is not None


def test_the_key_is_bound_once_and_the_other_keys_are_unchanged(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    keys = [line.split()[3] for line in workspace.tmux("list-keys", "-T", "prefix").stdout.splitlines()]
    assert keys.count("a") == 1
    assert {"p", "1", "2", "i", "g", "z", "d", "C-b", "Space", "?", "n", "Tab", "[", "a"} <= set(keys)
    bound = next(line for line in workspace.tmux("list-keys", "-T", "prefix").stdout.splitlines() if line.split()[3] == "a")
    assert "--activity" in bound and "--client-pid" in bound


def test_the_key_focuses_the_column_or_opens_the_popup(manager, monkeypatch):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    with pytest.raises(ValueError, match="writable terminal"):
        workspace.open_activity(client_pid=1, client_tty="/dev/pts/999")  # not an attached writable client
    sent, real = [], workspace.tmux

    def tmux(*args, **kwargs):
        sent.append(args)
        return real(*args, **kwargs) if args[0] != "display-popup" else None
    monkeypatch.setattr(workspace, "_owner", lambda pid, tty: {"tty": "/dev/pts/7"})
    monkeypatch.setattr(workspace, "tmux", tmux)
    terminal(workspace, COCKPIT_MIN_WIDTH, 55)
    workspace.open_activity(client_pid=1, client_tty="/dev/pts/7")
    assert ("select-pane", "-t", workspace.activity_pane()) in sent
    assert not any(args[0] == "display-popup" for args in sent)
    terminal(workspace, 180, 55)
    sent.clear()
    workspace.open_activity(client_pid=1, client_tty="/dev/pts/7")
    popup = next(args for args in sent if args[0] == "display-popup")
    assert popup[:3] == ("display-popup", "-c", "/dev/pts/7") and "-E" in popup
    assert not any(args[0] == "select-pane" for args in sent)


def test_notices_travel_on_the_panel_pane_and_never_touch_the_disk(manager, monkeypatch):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    sidebar = workspace.panes()["sidebar"]
    monkeypatch.setattr(feed, "_channels", {})
    history = NoticeHistory()
    history.add("Check failed: #{pid} ;\t[bold]x", {"project_id": "p"}, "error")
    feed.publish(workspace.settings, "default", "sidebar", feed.snapshot(history))
    assert feed.read_all(workspace.settings, "default") == []  # not from inside the workspace's tmux
    monkeypatch.setenv("TMUX", f"{workspace.socket},1,0")
    monkeypatch.setenv("TMUX_PANE", workspace.panes()["graph"])
    feed.publish(workspace.settings, "default", "sidebar", feed.snapshot(history))
    assert feed.read_all(workspace.settings, "default") == []  # a pane of another role is refused
    monkeypatch.setenv("TMUX_PANE", sidebar)
    feed.publish(workspace.settings, "default", "sidebar", feed.snapshot(history))
    entries = feed.read_all(workspace.settings, "default")
    assert [(entry["role"], entry["kind"]) for entry in entries] == [("sidebar", "error")]
    assert "#{pid} ;" in entries[0]["text"] and "[bold]x" in entries[0]["text"]
    stored = workspace.tmux("show-options", "-pv", "-t", sidebar, feed.OPTION).stdout.strip()
    assert stored and "#" not in stored and ";" not in stored
    assert not (workspace.settings.data_dir / "workspaces" / "default" / "notices").exists()
    # Another pane tagged with the role (anyone with the socket can tag one) can neither replace nor hide the copy.
    graph = workspace.panes()["graph"]
    forged = NoticeHistory()
    forged.add("FORGED", {"project_id": "p"}, "error")
    workspace.tmux("set-option", "-p", "-t", graph, "@cairnmind_role", "sidebar")
    workspace.tmux("set-option", "-p", "-t", graph, feed.OPTION, feed.encode(feed.snapshot(forged)))
    assert [entry["text"] for entry in feed.read_all(workspace.settings, "default")] == [entries[0]["text"]]


def test_the_layout_without_the_column_is_unchanged_and_with_it_valid():
    workspace = WorkspaceManager.__new__(WorkspaceManager)
    sent = []
    workspace.tmux = lambda *args, **kwargs: sent.append(args)
    panes = {"sidebar": "%0", "client1": "%1", "client2": "%2", "inspector": "%3", "graph": "%4"}
    workspace._apply_layout(panes, 180, 53)
    # The string of the release before the activity column (#144), byte for byte.
    assert sent[-1][-1] == ("9927,180x53,0,0{24x53,0,0,0,155x53,25,0[155x30,25,0{77x30,25,0,1,77x30,103,0,2},"
                            "155x22,25,31{92x22,25,31,3,62x22,118,31,4}]}")
    workspace._apply_layout({key: value for key, value in panes.items() if key != "client2"}, 180, 53)
    assert sent[-1][-1] == "c8f7,180x53,0,0{24x53,0,0,0,155x53,25,0[155x30,25,0,1,155x22,25,31{92x22,25,31,3,62x22,118,31,4}]}"
    workspace._apply_layout(panes, COCKPIT_MIN_WIDTH, 53, "%5")
    checksum, description = sent[-1][-1].split(",", 1)
    value = 0
    for byte in description.encode():
        value = ((value >> 1) + ((value & 1) << 15) + byte) & 0xffff
    assert checksum == f"{value:04x}"
    assert description.endswith(",32x53,181,0,5}") and "{77x30,25,0,1,77x30,103,0,2}" in description
