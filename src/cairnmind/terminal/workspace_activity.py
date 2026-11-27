"""The activity column of the workspace window (#144, #149).

The column is a pane of its own (role "activity"), never one of the five views: it is no focus
stop and has no key of its own. It is on screen in two places:

- In the Cockpit layout from COCKPIT_MIN_WIDTH columns, where every pane is still as wide as it is
  at the recommended 180 columns without the column.
- In the Focus layout (the default) from FOCUS_MIN_WIDTH columns, beside the focused view, unless
  the person switched it off (the "activity_column" preference, on by default). Below that width,
  or switched off, Focus zooms the focused view as before.

How Focus shows two panes (#149): no pane ever moves to another window (on tmux 3.4 a parked view
whose process had ended crashed the server when it was joined back, #148). The window is made wider
than the terminal (window-size manual): the focused view and the column fill the terminal, and the
other views wait off screen to the right, each in a cell exactly as large as the focused one. tmux
shows a client the part of a larger window around the active pane's cursor, which is always inside
the terminal here. Switching views swaps two equal cells within the window: no pane is resized, so
no coding client gets a SIGWINCH or reflows its screen. Leaving that mode gives the window back the
terminal's size and tmux's own sizing.

Where the column does not exist no process polls, and Ctrl+B a opens the panel as a popup. A column
whose process ended is started again in place, at most once per RESPAWN_PAUSE. Every change of panes
runs under the workspace's layout lock (WorkspaceManager._layout).
"""
from __future__ import annotations

ACTIVITY_WIDTH = 32
COCKPIT_MIN_WIDTH = 180 + ACTIVITY_WIDTH + 1   # the recommended width plus the column and its border
FOCUS_MIN_WIDTH = 160                          # the focused view keeps 127 columns beside the column
RESPAWN_PAUSE = 5.0                            # seconds between two restarts of a column that keeps ending
ORDER = ("sidebar", "client1", "client2", "inspector", "graph", "activity")
WIDE = "@cairnmind_wide"                       # window option: "<terminal width> <height> <window width>"


def wanted(compact: bool, width: int, beside: bool = False) -> bool:
    """Whether the column belongs on screen: beside the focused view, or in the full Cockpit from its width."""
    return beside or (not compact and width >= COCKPIT_MIN_WIDTH)


def beside(preferences: dict, width: int) -> bool:
    """Whether Focus shows the column beside the focused view."""
    return (preferences.get("view", "focus") == "focus" and preferences.get("activity_column", True) is True
            and width >= FOCUS_MIN_WIDTH)


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


def _wide(manager, window):
    stored = manager.tmux("show-options", "-wqv", "-t", window, WIDE, check=False).stdout.split()
    return tuple(map(int, stored)) if len(stored) == 3 and all(part.isdigit() for part in stored) else None


def terminal_size(manager, session, window, width, height):
    """The terminal's size: the window's, except while the window is made wider than the terminal.

    Then it is the smallest attached terminal that sizes the window (a read-only viewer does not),
    or, with none attached, the size recorded when the window was widened.
    """
    wide = _wide(manager, window)
    if wide is None:
        return width, height
    status = manager.tmux("show-options", "-gqv", "status", check=False).stdout.strip()
    lines = {"off": 0, "on": 1}.get(status, int(status) if status.isdigit() else 1)
    sizes = []
    for line in manager.tmux("list-clients", "-t", session, "-F", "#{client_width}\t#{client_height}\t#{client_flags}",
                             check=False).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            flags = parts[2].split(",")
            if "ignore-size" not in flags and "read-only" not in flags:
                sizes.append((int(parts[0]), int(parts[1]) - lines))
    if sizes:
        return min(size[0] for size in sizes), min(size[1] for size in sizes)
    if (width, height) == (wide[2], wide[1]):
        return wide[0], wide[1]
    return width, height  # the window was resized from outside: that is now the terminal's size


def spread_width(width, views):
    """The window width that holds the focused view and the column on screen and the other views off it."""
    return width + (views - 1) * (width - ACTIVITY_WIDTH)


def spread(manager, window, focus, activity, views, width, height):
    """Focus with the column: [focused view | column] fill the terminal, the other views wait off screen.

    Every view gets a cell of the same size, so the swap that brings another view on screen resizes
    nothing. False when the window holds a pane this code does not know (the layout is then not applied).
    """
    inner = width - ACTIVITY_WIDTH - 1
    total = spread_width(width, len(views))
    actual = manager.tmux("list-panes", "-t", window, "-F", "#{pane_id}").stdout.split()
    if sorted(actual) != sorted([*views, activity]):
        return False
    others = [pane for pane in actual if pane not in (focus, activity)]
    if not order(manager, window, [focus, activity, *others]):
        return False
    size = manager.tmux("display-message", "-p", "-t", window, "#{window_width} #{window_height}").stdout.split()
    if size != [str(total), str(height)]:
        manager.tmux("set-option", "-w", "-t", window, "window-size", "manual")
        manager.tmux("resize-window", "-t", window, "-x", str(total), "-y", str(height))
    cells = [f"{inner}x{height},0,0,{focus[1:]}", f"{ACTIVITY_WIDTH}x{height},{inner + 1},0,{activity[1:]}"]
    cells += [f"{inner}x{height},{width + 1 + index * (inner + 1)},0,{pane[1:]}" for index, pane in enumerate(others)]
    manager.tmux("select-layout", "-t", window, with_checksum(f"{total}x{height},0,0{{" + ",".join(cells) + "}"))
    manager.tmux("set-option", "-w", "-t", window, WIDE, f"{width} {height} {total}")
    return True


def narrow(manager, window, width, height):
    """Give a widened window the terminal's size and tmux's own sizing back; nothing for any other window."""
    if _wide(manager, window) is None:
        return False
    manager.tmux("resize-window", "-t", window, "-x", str(width), "-y", str(height))
    manager.tmux("set-option", "-wu", "-t", window, "window-size")
    manager.tmux("set-option", "-wu", "-t", window, WIDE)
    return True
