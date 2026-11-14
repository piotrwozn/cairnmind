"""The activity column of the workspace window (#144), and the views it sets aside in Focus.

The column is a pane of its own (role "activity"), never one of the five views: it is no focus
stop and has no key of its own. It exists only where it does not narrow a coding pane:

- Focus layout, from FOCUS_MIN_WIDTH columns: the focused view and the column share the window,
  so the focused view keeps at least 127 columns. tmux can zoom only one pane, so the four other
  views wait in a window of their own that no terminal shows ("parked", their processes keep
  running) and come back as soon as a layout needs them.
- Cockpit layout, from COCKPIT_MIN_WIDTH columns: every pane is still as wide as it is at the
  recommended 180 columns without the column.

Below that the pane does not exist (no process polls), and Ctrl+B a opens the panel as a popup.
Every change of panes runs under the workspace's layout lock (WorkspaceManager._layout).
"""
from __future__ import annotations

ACTIVITY_WIDTH = 32
FOCUS_MIN_WIDTH = 160
COCKPIT_MIN_WIDTH = 180 + ACTIVITY_WIDTH + 1   # the recommended width plus the column and its border
ORDER = ("sidebar", "client1", "client2", "inspector", "graph", "activity")
PARKED = "@cairnmind_parked"                  # window option naming the workspace whose views wait there


def wanted(focus_view: bool, small: bool, width: int, lines: int) -> bool:
    """Whether the column belongs on screen; `lines` is the whole terminal height."""
    if focus_view:
        return width >= FOCUS_MIN_WIDTH and lines >= 12
    return not small and width >= COCKPIT_MIN_WIDTH


def rows(manager):
    """(pane, role, window id) of every UI pane of this workspace, from one query; none without a server."""
    if not manager.binary or not manager.socket.exists():
        return []
    result = manager.tmux("list-panes", "-a", "-F",
                          "#{pane_id}\t#{@cairnmind_workspace}\t#{@cairnmind_role}\t#{window_id}", check=False)
    found = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[1] == manager.workspace_id and parts[2] in ORDER:
            found.append((parts[0], parts[2], parts[3]))
    return found


def _parked_window(manager, session):
    result = manager.tmux("list-windows", "-t", session, "-F", "#{window_id}\t#{" + PARKED + "}", check=False)
    return next((parts[0] for line in result.stdout.splitlines()
                 if len(parts := line.split("\t")) == 2 and parts[1] == manager.workspace_id), None)


def _join(manager, pane, window):
    manager.tmux("join-pane", "-d", "-s", pane, "-t", window)
    manager.tmux("select-layout", "-t", window, "tiled")  # room for the next pane; the layout follows


def place(manager, session, home, found, keep):
    """Put the views where the layout needs them; True when a pane moved.

    `keep` None: every view is in the workspace window (`home`). Otherwise only the panes in `keep`
    stay there and the other views are parked. A pane that comes back moves first, so the
    workspace window never loses its last view.
    """
    moved = False
    for pane, role, window in found:
        if role != "activity" and window != home and (keep is None or pane in keep):
            _join(manager, pane, home)
            moved = True
    if keep is not None:
        for pane, role, window in found:
            if role != "activity" and window == home and pane not in keep:
                parked = _parked_window(manager, session)
                if parked is None:
                    parked = manager.tmux("break-pane", "-d", "-s", pane, "-n", "parked", "-P", "-F",
                                          "#{window_id}").stdout.strip()
                    manager.tmux("set-option", "-w", "-t", parked, PARKED, manager.workspace_id)
                else:
                    _join(manager, pane, parked)
                moved = True
    return moved


def order(manager, window, wanted_panes):
    """Put the window's panes in this order; False when the window holds other panes too.

    tmux gives the cells of a custom layout to the panes in the window's order, and refuses a
    layout whose cell count differs from the pane count: then the layout is not applied.
    """
    actual = manager.tmux("list-panes", "-t", window, "-F", "#{pane_id}").stdout.split()
    if sorted(actual) != sorted(wanted_panes):
        return False
    for index, pane in enumerate(wanted_panes):
        if actual[index] != pane:
            other, where = actual[index], actual.index(pane)
            manager.tmux("swap-pane", "-d", "-s", pane, "-t", other)
            actual[index], actual[where] = pane, other
    return True


def with_checksum(description: str) -> str:
    checksum = 0
    for byte in description.encode():
        checksum = (checksum >> 1) + ((checksum & 1) << 15)
        checksum = (checksum + byte) & 0xffff
    return f"{checksum:04x}," + description


def beside(width: int, height: int, focus: str, activity: str) -> str:
    """The Focus layout with the column: the focused view, then the column on the right."""
    first = width - ACTIVITY_WIDTH - 1
    return with_checksum(f"{width}x{height},0,0{{{first}x{height},0,0,{focus[1:]},"
                         f"{ACTIVITY_WIDTH}x{height},{first + 1},0,{activity[1:]}}}")
