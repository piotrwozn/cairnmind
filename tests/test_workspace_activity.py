"""The activity column of the wide layout and its key (#144), on a real private tmux."""
from __future__ import annotations

import pytest

from cairnmind.cockpit_state import CockpitState
from cairnmind.workspace import ACTIVITY_MIN_WIDTH, ACTIVITY_WIDTH, WorkspaceManager
from test_workspace import manager as manager, rows

ROLES = {"sidebar", "client1", "client2", "inspector", "graph"}


def terminal(workspace, columns, lines):
    """The window of a terminal of this size: tmux keeps two rows for the status lines."""
    workspace.tmux("resize-window", "-t", "cairnmind-ui:workspace", "-x", str(columns), "-y", str(lines - 2))
    workspace._layout()


def test_at_160_columns_the_column_is_on_the_right_and_not_a_view(manager):
    workspace, broker, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, 160, 45)
    layout = rows(workspace)
    assert workspace.activity_pane() is not None
    assert layout["activity"][2] == ACTIVITY_WIDTH and layout["activity"][0] + ACTIVITY_WIDTH == 160
    assert layout["activity"][0] == max(left + width for role, (left, _, width, *_) in layout.items() if role != "activity") + 1
    assert layout["activity"][3] == 42 and layout["client1"][2] > 45  # 43 rows less its border row
    assert set(workspace.panes()) == ROLES and len(workspace.panes()) == 5
    assert workspace.state.read()["layout"] == "wide"
    assert not broker.calls


@pytest.mark.parametrize(("size", "view"), [((159, 45), "cockpit"), ((160, 45), "focus"), ((80, 24), "cockpit"),
                                           ((150, 45), "cockpit")])
def test_no_column_below_the_width_or_in_focus_view(manager, size, view):
    workspace, _, _ = manager
    CockpitState(workspace.settings).update({"view": view})
    workspace.create(start_supervisor=False)
    terminal(workspace, *size)
    assert workspace.activity_pane() is None
    assert "activity" not in rows(workspace)
    assert set(workspace.panes()) == ROLES


def test_narrowing_closes_the_column_and_widening_opens_it_again(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, 160, 45)
    first = workspace.activity_pane()
    views = {role: value[-1] for role, value in rows(workspace).items() if role != "activity"}
    terminal(workspace, 150, 45)
    assert workspace.activity_pane() is None
    assert rows(workspace)["client1"][0] == 25 and rows(workspace)["client2"][0] + rows(workspace)["client2"][2] == 150
    terminal(workspace, 160, 45)
    assert workspace.activity_pane() not in {None, first}
    assert {role: value[-1] for role, value in rows(workspace).items() if role != "activity"} == views
    assert rows(workspace)["activity"][2] == ACTIVITY_WIDTH


def test_zoom_and_next_view_keep_their_five_stops(manager):
    workspace, _, _ = manager
    workspace.create(start_supervisor=False)
    terminal(workspace, 180, 55)
    seen = []
    for _ in range(6):
        workspace.next_view()
        seen.append(workspace.state.read()["focus"])
    assert set(seen) == ROLES and "activity" not in seen
    workspace.state.update({"zoom": True})
    workspace._layout()
    assert workspace.tmux("display-message", "-p", "#{window_zoomed_flag}").stdout.strip() == "1"
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
    terminal(workspace, 160, 45)
    workspace.open_activity(client_pid=1, client_tty="/dev/pts/7")
    assert ("select-pane", "-t", workspace.activity_pane()) in sent
    assert not any(args[0] == "display-popup" for args in sent)
    terminal(workspace, 120, 40)
    sent.clear()
    workspace.open_activity(client_pid=1, client_tty="/dev/pts/7")
    popup = next(args for args in sent if args[0] == "display-popup")
    assert popup[:3] == ("display-popup", "-c", "/dev/pts/7") and "-E" in popup
    assert not any(args[0] == "select-pane" for args in sent)


def test_the_layout_without_the_column_is_unchanged_and_with_it_valid(tmp_path):
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
    workspace._apply_layout(panes, ACTIVITY_MIN_WIDTH, 43, "%5")
    checksum, description = sent[-1][-1].split(",", 1)
    value = 0
    for byte in description.encode():
        value = ((value >> 1) + ((value & 1) << 15) + byte) & 0xffff
    assert checksum == f"{value:04x}"
    assert description.endswith(",32x43,128,0,5}") and description.startswith("160x43,0,0{24x43,0,0,0,102x43,25,0[")
