"""The activity column of the workspace window (#144).

The column is a pane of its own (role "activity"), never one of the five views: it is no focus
stop and has no key of its own. It exists only where it does not narrow a coding pane, in the
Cockpit layout from COCKPIT_MIN_WIDTH columns, where every pane is still as wide as it is at the
recommended 180 columns without the column. The Focus layout zooms the focused view as before and
moves no pane. Where the column does not exist no process polls, and Ctrl+B a opens the panel as a
popup. A column whose process ended is started again in place, at most once per RESPAWN_PAUSE.
Every change of panes runs under the workspace's layout lock (WorkspaceManager._layout).
"""
from __future__ import annotations

ACTIVITY_WIDTH = 32
COCKPIT_MIN_WIDTH = 180 + ACTIVITY_WIDTH + 1   # the recommended width plus the column and its border
RESPAWN_PAUSE = 5.0                            # seconds between two restarts of a column that keeps ending
ORDER = ("sidebar", "client1", "client2", "inspector", "graph", "activity")


def wanted(compact: bool, width: int) -> bool:
    """Whether the column belongs on screen: only in the full Cockpit layout, from its width."""
    return not compact and width >= COCKPIT_MIN_WIDTH


def rows(manager):
    """(pane, role, dead) of every UI pane of this workspace, from one query; none without a server."""
    if not manager.binary or not manager.socket.exists():
        return []
    result = manager.tmux("list-panes", "-a", "-F",
                          "#{pane_id}\t#{@cairnmind_workspace}\t#{@cairnmind_role}\t#{pane_dead}", check=False)
    found = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[1] == manager.workspace_id and parts[2] in ORDER:
            found.append((parts[0], parts[2], parts[3] == "1"))
    return found


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
