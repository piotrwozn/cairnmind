"""What the knowledge service spends on indexing and on its queue: one Markdown row for docs/DEPLOYMENT.md.

    CAIRNMIND_HOME=/tmp/cm-measure CAIRNMIND_PORT=18765 CAIRNMIND_ARCADE_URL=http://127.0.0.1:12480 \\
        python scripts/measure_service_spend.py --service-pid 12345 --queue-seconds 120 -- python -m cairnmind index .

`cairnmind index` only asks the service to index: the work runs in the service process, so the figures
that count are the service's, read from /proc/PID. Start the service on the throwaway directory just
before the run and pass its pid (`python -m cairnmind --data-dir DIR serve`; any other process is
refused). The row then holds: the CPU seconds the service spent while the command ran, and the service's
peak memory (VmHWM, since its start, so start-up is in it) when the command ended; then the same for
--queue-seconds after the command returned, when the service works off what is left in its queue (the
peak is again since its start). The command must be the one that indexes: the service's own scan indexes a
registered folder within poll_seconds (10 s) and then the command finds it unchanged, but the row still holds
the CPU of the scan. Register the folder after the service has settled and set a large poll_seconds in the
throwaway config.json. It starts nothing and sends nothing anywhere. To keep it away from a real
installation it refuses to run unless CAIRNMIND_HOME, CAIRNMIND_PORT and CAIRNMIND_ARCADE_URL are set,
none of them points at the installation's data directory or at port 8765 (the service) or 2480 (the
engine), and the command has no --data-dir. The engine address must be a throwaway engine: a database name exported
as CAIRNMIND_ARCADE_DATABASE (or DEVBRAIN_) must start with cairnmind_measure_, and an engine that is not on
this machine is accepted only with such a name. Linux and WSL; without /proc (macOS) it cannot read the
service's figures and says so.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

NO_PROC = "cannot read the service's figures here: this system has no /proc (macOS)"
INSTALLATION_DIRS = (".local/share/cairnmind", ".local/share/devbrain")
THROWAWAY_DATABASE = "cairnmind_measure_"


def service_figures(stat_text: str, status_text: str, ticks: int) -> tuple[float, float]:
    """CPU seconds and peak resident MB from the text of /proc/PID/stat and /proc/PID/status."""
    # The command name sits in parentheses and may hold spaces: fields count from the last ")".
    fields = stat_text.rsplit(")", 1)[1].split()
    cpu = (int(fields[11]) + int(fields[12])) / ticks  # utime, stime: fields 14 and 15 of the line
    peak_kb = next(int(line.split()[1]) for line in status_text.splitlines() if line.startswith("VmHWM:"))
    return cpu, peak_kb / 1024


def read_service(pid: int, proc: Path = Path("/proc")) -> tuple[float, float]:
    return service_figures((proc / str(pid) / "stat").read_text(), (proc / str(pid) / "status").read_text(),
                           os.sysconf("SC_CLK_TCK"))


def serves(cmdline: bytes, home: Path) -> bool:
    """True when a /proc/PID/cmdline is `... serve` with --data-dir equal to this measurement's directory."""
    args = cmdline.decode(errors="replace").split("\0")
    given = [a.split("=", 1)[1] for a in args if a.startswith("--data-dir=")]
    given += [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--data-dir"]
    return "serve" in args and len(given) == 1 and Path(given[0]).expanduser().resolve() == home


def figures(pid: int) -> tuple[float, float] | None:
    try:
        return read_service(pid)
    except (OSError, ValueError, StopIteration, IndexError):  # the process ended, or /proc is hidden
        return None


def cell(spent: tuple[float, float] | None) -> str:
    return f"{spent[0]:.1f} s, {spent[1]:.0f} MB" if spent else "not read"


def row(command: list[str], wall: float, index, queue) -> str:
    return f"| `{' '.join(command)}` ({wall:.1f} s) | {cell(index)} | {cell(queue)} |"


def port_of(url: str) -> int | None:
    try:
        return urlsplit(url).port
    except ValueError:
        return None


def refuse_an_installation(env, command: list[str]) -> str | None:
    if not env.get("CAIRNMIND_HOME") or not env.get("CAIRNMIND_PORT") or not env.get("CAIRNMIND_ARCADE_URL"):
        return "set CAIRNMIND_HOME, CAIRNMIND_PORT and CAIRNMIND_ARCADE_URL to a throwaway directory and free ports first"
    if any(a == "--data-dir" or a.startswith("--data-dir=") for a in command):
        return "the command names its own --data-dir, which would replace CAIRNMIND_HOME: leave it out"
    home = Path(env["CAIRNMIND_HOME"]).expanduser().resolve()
    if home in {(Path.home() / d).resolve() for d in INSTALLATION_DIRS}:
        return "CAIRNMIND_HOME is the installation's data directory"
    try:
        service_port = int(env["CAIRNMIND_PORT"])
    except ValueError:
        return "CAIRNMIND_PORT is not a number"
    # The earlier DEVBRAIN_ spelling counts when the current one is absent; the three required names shadow theirs.
    engine_ports = [port_of(env[k]) for k in ("CAIRNMIND_ARCADE_URL", "CAIRNMIND_ARCADE_CONNECT_URL",
                                              "DEVBRAIN_ARCADE_CONNECT_URL") if env.get(k)]
    if service_port == 8765 or 2480 in engine_ports or None in engine_ports:
        return "the service port is 8765, or an engine port is 2480 or not readable: those are the installation's, not a measurement's"
    named = [env[k] for k in ("CAIRNMIND_ARCADE_DATABASE", "DEVBRAIN_ARCADE_DATABASE") if env.get(k)]
    if any(not name.startswith(THROWAWAY_DATABASE) for name in named):
        return f"an exported database name that does not start with {THROWAWAY_DATABASE} could be the installation's database"
    hosts = {urlsplit(env[k]).hostname for k in ("CAIRNMIND_ARCADE_URL", "CAIRNMIND_ARCADE_CONNECT_URL",
                                                 "DEVBRAIN_ARCADE_CONNECT_URL") if env.get(k)}
    if not named and not hosts <= {"127.0.0.1", "localhost", "::1"}:
        return f"an engine that is not on this machine needs a database named {THROWAWAY_DATABASE}... in CAIRNMIND_ARCADE_DATABASE"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--service-pid", type=int, help="process id of the running knowledge service")
    parser.add_argument("--queue-seconds", type=float, default=0, help="seconds to keep watching the service after the command")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND...")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the command to measure after --")
    if (why := refuse_an_installation(os.environ, command)):
        print(f"refused: {why}", file=sys.stderr)
        return 2
    pid = args.service_pid
    if pid and not Path("/proc/self/stat").exists():
        print(NO_PROC, file=sys.stderr)
        pid = None
    if pid:
        try:
            ours = serves(Path(f"/proc/{pid}/cmdline").read_bytes(), Path(os.environ["CAIRNMIND_HOME"]).expanduser().resolve())
        except OSError:
            ours = False
        if not ours:
            print(f"refused: process {pid} is not `serve --data-dir {os.environ['CAIRNMIND_HOME']}`", file=sys.stderr)
            return 2
    before = figures(pid) if pid else None
    started = time.monotonic()
    code = subprocess.run(command).returncode
    wall = time.monotonic() - started
    ended = figures(pid) if before else None
    if ended and args.queue_seconds > 0:
        time.sleep(args.queue_seconds)
    settled = figures(pid) if ended else None
    index = (ended[0] - before[0], ended[1]) if before and ended else None
    queue = (settled[0] - ended[0], settled[1]) if ended and settled else None
    print(row(command, wall, index, queue))
    return code


if __name__ == "__main__":
    sys.exit(main())
