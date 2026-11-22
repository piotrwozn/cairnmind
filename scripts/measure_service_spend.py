"""What the laptop spends on Cairnmind's own processes: one Markdown row for docs/DEPLOYMENT.md.

    CAIRNMIND_HOME=/tmp/cm-measure CAIRNMIND_PORT=18765 CAIRNMIND_ARCADE_URL=http://127.0.0.1:12480 \\
        python scripts/measure_service_spend.py --service-pid 12345 -- python -m cairnmind index .

Runs the command after `--` as a child and reports its wall time, CPU seconds and peak memory. With
--service-pid it also reads the CPU seconds and the peak memory (VmHWM) of that process from /proc before
and after the command, and reports the CPU seconds spent in between. It starts nothing else and sends
nothing anywhere. To keep it away from a real installation it refuses to run unless CAIRNMIND_HOME and
CAIRNMIND_PORT are set to something other than the default 8765, and CAIRNMIND_ARCADE_URL to something
other than the default engine port 2480. Linux and WSL; on a system without /proc (macOS) it says it
cannot read the service's figures and reports the command only.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

NO_PROC = "cannot read the service's figures here: this system has no /proc (macOS)"


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


def child_figures(usage) -> tuple[float, float]:
    """CPU seconds and peak resident MB of the finished children (ru_maxrss is KB on Linux, bytes on macOS)."""
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return usage.ru_utime + usage.ru_stime, usage.ru_maxrss / divisor


def row(command: list[str], wall: float, child: tuple[float, float], service: tuple[float, float] | None) -> str:
    cells = [f"`{' '.join(command)}`", f"{wall:.1f} s", f"{child[0]:.1f} s", f"{child[1]:.0f} MB"]
    cells += [f"{service[0]:.1f} s", f"{service[1]:.0f} MB"] if service else ["not read", "not read"]
    return "| " + " | ".join(cells) + " |"


def refuse_an_installation(env) -> str | None:
    if not env.get("CAIRNMIND_HOME") or not env.get("CAIRNMIND_PORT") or not env.get("CAIRNMIND_ARCADE_URL"):
        return "set CAIRNMIND_HOME, CAIRNMIND_PORT and CAIRNMIND_ARCADE_URL to a throwaway directory and free ports first"
    if env["CAIRNMIND_PORT"] == "8765" or env["CAIRNMIND_ARCADE_URL"].rstrip("/").endswith(":2480"):
        return "CAIRNMIND_PORT is 8765 or the engine port is 2480: those are the installation's, not a measurement's"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--service-pid", type=int, help="process id of the running knowledge service")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND...")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the command to measure after --")
    if (why := refuse_an_installation(os.environ)):
        print(f"refused: {why}", file=sys.stderr)
        return 2
    have_proc = Path("/proc/self/stat").exists()
    if args.service_pid and not have_proc:
        print(NO_PROC, file=sys.stderr)
    before = read_service(args.service_pid) if args.service_pid and have_proc else None
    started = time.monotonic()
    code = subprocess.run(command).returncode
    wall = time.monotonic() - started
    after = read_service(args.service_pid) if before else None
    service = (after[0] - before[0], after[1]) if before and after else None
    print(row(command, wall, child_figures(resource.getrusage(resource.RUSAGE_CHILDREN)), service))
    return code


if __name__ == "__main__":
    sys.exit(main())
