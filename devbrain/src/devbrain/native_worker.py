"""Private exec helpers for native CLI panes and browser terminal attachments.

No source text, terminal output or credentials are logged by this process.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import termios
import tempfile

from .config import Settings
from .runtime_selection import resolve_tmux


def process_start(pid: int) -> str | None:
    try:
        # comm may contain spaces and closing parentheses.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--attach", action="store_true")
    parser.add_argument("--tmux-bin")
    parser.add_argument("--socket")
    parser.add_argument("--session")
    args = parser.parse_args()
    if args.attach:
        if not all((args.tmux_bin, args.socket, args.session)):
            raise SystemExit(2)
        binary = resolve_tmux(Settings(Path("/")), override=args.tmux_bin)
        # Popen(start_new_session=True) has already created our session. Claim
        # its slave PTY here, avoiding preexec_fn in the threaded web server.
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        environment = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        os.execve(binary, [binary, "-S", args.socket, "attach-session", "-t", args.session], environment)
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
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
