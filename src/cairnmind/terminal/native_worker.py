"""Private exec helpers for native CLI panes and browser terminal attachments.

No source text, terminal output or credentials are logged by this process.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

if sys.platform != "win32":
    import fcntl
    import termios
else:
    fcntl = None
    termios = None

from cairnmind.config import Settings
from cairnmind.pal.process_identity import start as process_start
from cairnmind.terminal.runtime_selection import resolve_tmux


def main():
    if sys.platform == "win32":
        raise SystemExit("native_worker is only supported on POSIX platforms")
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--attach", action="store_true")
    parser.add_argument("--tmux-bin")
    parser.add_argument("--socket")
    parser.add_argument("--session")
    args = parser.parse_args()
    if args.attach:
        if not (isinstance(args.tmux_bin, str) and args.tmux_bin
                and isinstance(args.socket, str) and args.socket
                and isinstance(args.session, str) and args.session):
            raise SystemExit(2)
        binary = resolve_tmux(Settings(Path("/")), override=args.tmux_bin)
        if not binary:
            raise SystemExit(2)
        # Popen(start_new_session=True) has already created our session. Claim
        # its slave PTY here, avoiding preexec_fn in the threaded web server.
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        environment = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        os.execve(binary, [binary, "-S", args.socket, "attach-session", "-t", args.session], environment)  # noqa: S606 - validated argv without shell
    if args.spec is None:
        raise SystemExit(2)
    fd = os.open(args.spec, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2 * 1024 * 1024:
            raise SystemExit("Invalid private native launch specification")
        spec = json.load(handle)
    args.spec.unlink()
    command, environment = spec["command"], spec["environment"]
    if (not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command)
            or not Path(command[0]).is_absolute() or not isinstance(environment, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items())):
        raise SystemExit("Invalid native exec specification")
    environment = dict(environment)
    for key in ("TERM", "TMUX", "TMUX_PANE"):
        # Capabilities and identity belong to this actual inner native pane,
        # never the terminal which happened to prepare the broker launch.
        if key in os.environ:
            environment[key] = os.environ[key]
        else:
            environment.pop(key, None)
    marker = args.spec.with_suffix(".started.json")
    fd, temporary = tempfile.mkstemp(prefix=".started-", dir=marker.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump({"pid": os.getpid(), "pgid": os.getpgrp(), "start": process_start(os.getpid()),
                   "run_id": spec["run_id"], "generation": spec["generation"]}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, marker)
    os.chdir(spec["cwd"])
    os.execve(command[0], command, environment)  # noqa: S606 - validated absolute binary and argv without shell


if __name__ == "__main__":
    main()
