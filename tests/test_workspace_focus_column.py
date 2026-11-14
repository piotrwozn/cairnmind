"""The activity column beside the focused view in the Focus layout (#149), on a real private tmux 3.4.

The other views stay in the one workspace window, off screen to the right of the terminal, each in a
cell as large as the focused view: switching views moves no pane between windows and resizes none.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import shlex
import signal
import struct
import sys
import termios
import threading
import time
import warnings

import pytest

from cairnmind.cockpit_state import CockpitState
from cairnmind.workspace import ACTIVITY_WIDTH, SESSION, WorkspaceManager
from cairnmind.workspace_activity import FOCUS_MIN_WIDTH
from test_workspace import manager as manager  # noqa: F401 (the fixture)

pytestmark = pytest.mark.tmux
VIEWS = ("sidebar", "client1", "client2", "inspector", "graph")
# A stand-in for a pane process: logs its size at start and on every SIGWINCH, and shows its mark.
PROGRAM = """import os, signal, sys, time
log, mark = sys.argv[1], sys.argv[2]
def record(*_):
    size = os.get_terminal_size(0)
    with open(log, "a") as handle:
        handle.write(f"{size.columns} {size.lines}\\n")
    sys.stdout.write("\\033[2J\\033[H" + mark + "\\n")
    sys.stdout.flush()
signal.signal(signal.SIGWINCH, record)
record()
while True:
    time.sleep(3600)
"""


@pytest.fixture
def focus_workspace(manager, tmp_path, monkeypatch):  # noqa: F811
    workspace, _, _ = manager
    program = tmp_path / "pane.py"
    program.write_text(PROGRAM)

    def command(role):
        return shlex.join([sys.executable, str(program), str(tmp_path / f"{role}.log"), f"MARK-{role}"])
    monkeypatch.setattr(workspace, "_panel", command)
    monkeypatch.setattr(workspace, "_activity_command", lambda *args: command("activity"))
    CockpitState(workspace.settings).update({"view": "focus"})  # the default layout
    workspace.create(start_supervisor=False)
    workspace.logs = tmp_path
    return workspace


def terminal(workspace, columns, lines):
    """The window of a terminal of this size, without an attached terminal: two rows are the status lines."""
    workspace.tmux("resize-window", "-t", SESSION + ":workspace", "-x", str(columns), "-y", str(lines - 2))
    workspace._layout()


def geometry(workspace):
    """Role -> (left, width, height, pid, dead) of every pane of the workspace window."""
    result = workspace.tmux("list-panes", "-t", SESSION + ":workspace", "-F",
                            "#{@cairnmind_role}\t#{pane_left}\t#{pane_width}\t#{pane_height}\t#{pane_pid}\t#{pane_dead}")
    return {parts[0]: (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), parts[5] == "1")
            for line in result.stdout.splitlines() if (parts := line.split("\t"))}


def window(workspace, value):
    return workspace.tmux("display-message", "-p", "-t", SESSION + ":workspace", value).stdout.strip()


def sizes_logged(workspace, role):
    path = workspace.logs / f"{role}.log"
    return path.read_text().split() if path.exists() else []


def assert_beside(workspace, focused, columns):
    found = geometry(workspace)
    inner = columns - ACTIVITY_WIDTH - 1
    assert found[focused][:2] == (0, inner)
    assert found["activity"][:2] == (inner + 1, ACTIVITY_WIDTH)  # ends at the terminal's last column
    assert all(found[role][0] > columns for role in found if role not in {focused, "activity"})  # off screen
    assert all(found[role][1] == inner for role in VIEWS if role in found)  # every view as large as the shown one
    assert window(workspace, "#{window_zoomed_flag}") == "0"
    assert len(workspace.tmux("list-windows", "-t", SESSION, "-F", "#{window_id}").stdout.split()) == 1


@contextlib.contextmanager
def attached(workspace, columns, lines):
    """A real terminal of this size attached to the workspace (a pseudo-terminal running tmux attach)."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}
    environment["TERM"] = "xterm-256color"
    with warnings.catch_warnings():  # the child only replaces itself with tmux at once
        warnings.simplefilter("ignore", DeprecationWarning)
        pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child process
        os.execve(workspace.binary, [workspace.binary, "-S", str(workspace.socket), "attach-session", "-t", SESSION],
                  environment)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
    output = bytearray()

    def drain():
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if not data:
                return
            output.extend(data)
    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + 10
    while not workspace.tmux("list-clients", "-t", SESSION, "-F", "#{client_tty}").stdout.strip():
        assert time.monotonic() < deadline, "the terminal did not attach"
        time.sleep(.05)
    try:
        yield output, fd
    finally:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        os.close(fd)


def on_screen(workspace, output):
    """The marks the attached terminal shows after a full redraw."""
    tty = workspace.tmux("list-clients", "-t", SESSION, "-F", "#{client_tty}").stdout.split()[0]
    output.clear()
    workspace.tmux("refresh-client", "-t", tty)
    time.sleep(.5)
    text = output.decode("latin-1")
    return {role for role in (*VIEWS, "activity") if f"MARK-{role}" in text}


def test_at_160x45_the_column_is_beside_the_focused_view_in_a_real_terminal(focus_workspace):
    workspace = focus_workspace
    with attached(workspace, 160, 45) as (output, _):
        workspace._layout()
        for role in ("client1", "sidebar", "graph"):
            workspace.focus(role)
            assert_beside(workspace, role, 160)
            assert on_screen(workspace, output) == {role, "activity"}
    assert workspace.state.read()["layout"] == "compact"


def test_the_layout_follows_the_terminal_when_it_is_resized(focus_workspace):
    workspace = focus_workspace
    with attached(workspace, 160, 45) as (output, fd):
        workspace.focus("client1")
        assert_beside(workspace, "client1", 160)
        for columns, lines in ((200, 50), (150, 45), (170, 45)):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
            deadline = time.monotonic() + 5
            while workspace.tmux("list-clients", "-t", SESSION, "-F", "#{client_width}").stdout.strip() != str(columns):
                assert time.monotonic() < deadline
                time.sleep(.05)
            workspace._layout()  # what the supervisor does every half second
            if columns >= FOCUS_MIN_WIDTH:
                assert_beside(workspace, "client1", columns)
                assert on_screen(workspace, output) == {"client1", "activity"}
            else:  # tmux sizes the window by the terminal again, and Focus zooms as before
                assert workspace.activity_pane() is None and window(workspace, "#{window_zoomed_flag}") == "1"
                assert window(workspace, "#{window_width} #{window_height}") == f"{columns} {lines - 2}"


def test_switching_views_keeps_the_column_every_process_and_every_coding_pane_size(focus_workspace):
    workspace = focus_workspace
    with attached(workspace, 160, 45):
        workspace._layout()
        workspace.focus("client1")
        before = geometry(workspace)
        logged = settled(workspace)
        column = workspace.activity_pane()
        for _ in range(3):
            for role in VIEWS:
                workspace.focus(role)
                assert_beside(workspace, role, 160)
                assert workspace.activity_pane() == column
        after = geometry(workspace)
        # No coding pane got a SIGWINCH while views were switched: none was resized, shown or not.
        assert settled(workspace) == logged
    assert {role: value[1:] for role, value in after.items()} == {role: value[1:] for role, value in before.items()}
    assert logged["client1"][-1] == logged["client2"][-1] == f"{160 - ACTIVITY_WIDTH - 1} {before['client1'][2]}"


def settled(workspace):
    """The sizes each coding pane logged, once no resize has arrived for a second (a SIGWINCH comes late)."""
    seen, quiet = None, time.monotonic()
    while time.monotonic() - quiet < 1:
        now = {role: sizes_logged(workspace, role) for role in ("client1", "client2")}
        if now != seen:
            seen, quiet = now, time.monotonic()
        time.sleep(.1)
    return {role: [f"{a} {b}" for a, b in zip(value[::2], value[1::2])] for role, value in seen.items()}


@pytest.mark.parametrize("with_terminal", [False, True], ids=["detached", "attached"])
@pytest.mark.parametrize("role", [*VIEWS, "activity"])
def test_a_view_or_the_column_whose_process_ended_never_brings_the_server_down(focus_workspace, role, with_terminal):
    workspace = focus_workspace
    with attached(workspace, 160, 45) if with_terminal else contextlib.nullcontext():
        if not with_terminal:
            terminal(workspace, 160, 45)
        workspace._layout()
        kill_and_focus_ten_times(workspace, role)


def kill_and_focus_ten_times(workspace, role):
    server = window(workspace, "#{pid}")
    other = "graph" if role != "graph" else "client1"
    for _ in range(10):
        pane = workspace.activity_pane() if role == "activity" else workspace.panes()[role]
        os.kill(geometry(workspace)[role][3], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while not geometry(workspace)[role][4]:
            assert time.monotonic() < deadline
            time.sleep(.05)
        if role == "activity":
            workspace.focus(other)
            workspace.tmux("select-pane", "-t", pane)  # the dead column itself
            workspace._activity_restarted = float("-inf")  # no pause between the ten rounds
            workspace._layout()  # started again in place
        else:
            workspace.focus(role)  # a projects, memory or graph view starts again when asked for
            workspace.focus(other)
            workspace.focus(role)
            if geometry(workspace)[role][4]:  # a coding pane stays ended until its session is bound again
                workspace.tmux("respawn-pane", "-k", "-t", pane, workspace._panel(role))
        assert workspace.exists() and window(workspace, "#{pid}") == server
        assert not geometry(workspace)[role][4]
        assert (workspace.activity_pane() if role == "activity" else workspace.panes()[role]) == pane
    workspace.focus(other)
    assert_beside(workspace, other, 160)


@pytest.mark.parametrize("size", [(FOCUS_MIN_WIDTH - 1, 45), (140, 42), (80, 24)])
def test_below_the_width_focus_zooms_as_today(focus_workspace, size):
    workspace = focus_workspace
    terminal(workspace, 160, 45)
    terminal(workspace, *size)  # from the column back to the narrow terminal
    for role in ("client1", "graph"):
        workspace.focus(role)
        assert workspace.activity_pane() is None and window(workspace, "#{window_zoomed_flag}") == "1"
        assert window(workspace, "#{window_width}") == str(size[0])
    assert workspace.tmux("show-options", "-wv", "-t", SESSION + ":workspace", "window-size", check=False).stdout.strip() == ""


def test_the_key_of_the_view_in_focus_brings_the_keyboard_back_from_the_column(focus_workspace):
    workspace = focus_workspace
    with attached(workspace, 160, 45):
        workspace._layout()
        workspace.focus("client1")
        workspace.tmux("select-pane", "-t", workspace.activity_pane())  # Ctrl+B a, as open_activity does
        workspace.focus("client1")  # Ctrl+B 1 again, in the same process as the supervisor's refreshes
        assert window(workspace, "#{pane_id}") == workspace.panes()["client1"]
        assert_beside(workspace, "client1", 160)


@pytest.mark.parametrize("off", [False, True], ids=["narrower", "switched-off"])
def test_leaving_the_column_gives_the_views_their_cockpit_cells_not_narrow_strips(focus_workspace, off):
    workspace = focus_workspace
    terminal(workspace, 170, 45)
    assert_beside(workspace, workspace.state.read()["focus"], 170)
    if off:
        CockpitState(workspace.settings).update({"activity_column": False})
        workspace._layout()
    else:
        terminal(workspace, FOCUS_MIN_WIDTH - 1, 45)
    columns = 170 if off else FOCUS_MIN_WIDTH - 1
    workspace.focus("client1")
    workspace.tmux("resize-pane", "-Z", "-t", workspace.panes()["client1"])  # unzoomed: the cells behind the zoom
    found = geometry(workspace)
    right = columns - 25
    assert found["sidebar"][1] == 24 and found["client1"][1] == (right - 1) // 2
    assert found["inspector"][1] == round((right - 1) * .6)
    assert all(found[role][2] < 42 for role in VIEWS if role != "sidebar")  # rows, not full-height strips


def test_the_switch_hides_and_shows_the_column_and_is_remembered(focus_workspace):
    workspace = focus_workspace
    preferences = CockpitState(workspace.settings)
    assert preferences.load()["activity_column"] is True  # on by default
    terminal(workspace, 180, 55)
    assert_beside(workspace, workspace.state.read()["focus"], 180)
    preferences.update({"activity_column": False})
    workspace._layout()
    assert workspace.activity_pane() is None and window(workspace, "#{window_zoomed_flag}") == "1"
    assert window(workspace, "#{window_width}") == "180"
    # A new process (the next run) reads the same choice.
    again = WorkspaceManager(workspace.settings, tmux_binary=workspace.binary)
    assert CockpitState(again.settings).load()["activity_column"] is False
    preferences.update({"activity_column": True})
    workspace._layout()
    assert_beside(workspace, workspace.state.read()["focus"], 180)
    before = preferences.path.read_bytes()
    for value in (1, "on", None):
        with pytest.raises(ValueError):
            preferences.update({"activity_column": value})
        assert preferences.path.read_bytes() == before


def test_cockpit_and_back_restore_the_terminal_size(focus_workspace):
    workspace = focus_workspace
    terminal(workspace, 180, 55)
    workspace.toggle_zoom()  # Ctrl+B z: Focus to Cockpit
    assert window(workspace, "#{window_width}") == "180" and workspace.activity_pane() is None
    assert geometry(workspace)["client1"][:2] == (25, 77)  # the Cockpit layout as before
    CockpitState(workspace.settings).update({"view": "focus"})
    workspace._layout()
    assert_beside(workspace, workspace.state.read()["focus"], 180)
