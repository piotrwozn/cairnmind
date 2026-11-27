#!/usr/bin/env python3
"""Time `memory snapshot` of a synthetic project of N records. For maintainers; needs the database engine.

    uv run --locked python scripts/measure_snapshot.py 10000

Prints the seconds of a first snapshot, of a repeat with nothing changed, and of a repeat after one record
was added (#161: a repeat reads the previous package and compares every object; the cost was never measured
at size). The records are made up for this run. The project, its worktree and its own database engine live
in a temporary directory with ports of their own, so no installation is touched, and all of it is removed
at the end, after the engine process has exited. Time grows with N: 100,000 records take many minutes to create, before the first measurement.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def git(worktree: Path, *args):
    subprocess.run(["git", "-C", str(worktree), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                   check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def timed(label, action):
    started = time.perf_counter()
    result = action()
    print(f"{label}: {time.perf_counter() - started:.2f} s", flush=True)
    return result


def measure(store, worktree: Path, records: int):
    from cairnmind.memory.memory import MemoryManager

    project = store.add_project("synthetic", worktree)["id"]
    lines = [f"Synthetic fact {number}: the value of setting {number} is {number * 7}." for number in range(records + 1)]
    source = store.snapshot(project, "note://synthetic/facts", "\n".join(lines), "note")
    started = time.perf_counter()
    for number in range(records):
        store.remember(project, {"kind": "fact", "key": f"fact-{number}", "content": lines[number]}, [source["id"]])
    print(f"made {records} records: {time.perf_counter() - started:.2f} s", flush=True)
    manager = MemoryManager(store)
    timed("first snapshot", lambda: manager.snapshot(project, worktree=str(worktree)))
    timed("repeat, nothing changed", lambda: manager.snapshot(project, worktree=str(worktree)))
    store.remember(project, {"kind": "fact", "key": "fact-new", "content": lines[records]}, [source["id"]])
    timed("repeat after one record was added", lambda: manager.snapshot(project, worktree=str(worktree)))


def main() -> int:
    if not sys.platform.startswith("linux"):
        raise SystemExit("Measure on Linux or WSL2.")
    records = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    if records < 1:
        raise SystemExit("Give a number of records of at least 1.")
    from cairnmind.config import Settings

    user = Settings.load()
    for name in list(os.environ):
        if name.startswith(("CAIRNMIND_", "DEVBRAIN_")) and not name.endswith(("_HOME", "_ARCADE_DIST")):
            os.environ.pop(name)
    from cairnmind.pal import process_identity
    from cairnmind.storage.engine import install_engine, runtime_dir, start_engine, stop_engine
    from cairnmind.store import Store

    distribution = Path(os.environ.get("CAIRNMIND_ARCADE_DIST") or os.environ.get("DEVBRAIN_ARCADE_DIST") or runtime_dir(user))
    if not (distribution / "lib").is_dir():
        raise SystemExit(f"No database engine distribution at {distribution}.")
    temporary = Path(tempfile.mkdtemp(prefix="cairnmind-measure-"))
    worktree = temporary / "project"
    settings = replace(user, data_dir=temporary / "data", arcade_url=f"http://127.0.0.1:{free_port()}", arcade_user="root",
                       arcade_password=secrets.token_urlsafe(32), arcade_database="cairnmind_measure_" + uuid4().hex,
                       token=secrets.token_urlsafe(32), port=free_port(), cockpit_port=free_port(),
                       engine_mode="", arcade_connect_url="", arcade_password_file="")  # a local engine, whatever the user's config says
    try:
        worktree.mkdir()
        (worktree / "README.md").write_text("A synthetic project for measuring a snapshot.\n")
        git(worktree, "init", "-q", "-b", "main")
        git(worktree, "add", "-A")
        git(worktree, "-c", "user.name=Measure", "-c", "user.email=measure@example.invalid", "commit", "-qm", "Synthetic")
        settings.save()
        install_engine(settings, distribution)
        start_engine(settings)
        measure(Store(settings.data_dir, settings=settings), worktree, records)
    finally:
        try:
            pid = int(json.loads((settings.data_dir / "arcade" / "process.json").read_text())["pid"])
        except (OSError, ValueError, KeyError):
            pid = None  # the engine never started
        try:
            stop_engine(settings)  # harmless when the engine never started
        except Exception as exc:  # noqa: BLE001 - the engine may have gone by itself
            print(f"The measuring engine could not be stopped cleanly: {exc}", file=sys.stderr)
        # stop_engine returns when the engine stops answering; the process still writes while it shuts down.
        deadline = time.monotonic() + 60
        while pid and process_identity.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
