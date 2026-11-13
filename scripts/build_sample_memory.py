#!/usr/bin/env python3
"""Build the memory snapshot of examples/sample-project. For maintainers; needs the database engine.

    uv run --locked python scripts/build_sample_memory.py

The snapshot is made by the same code as any other: the store, the plan manager, the check runner
and `memory snapshot`. Nothing is written by hand, so the package is one that an import accepts.
It is built under a neutral path, because a snapshot carries the path of its worktree, and with its
own database engine in a temporary directory, so no installation is touched.

Identifiers and times differ on every run. The committed sample is therefore checked by
tests/test_sample_memory.py, which reads it with the loader the commands use, and not by its diff.
All texts below are written for the sample: they are no real project history and no real conversation.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample-project"
NEUTRAL = Path("/tmp/cairnmind-sample/trail-notes")

NOTES = {
    "note://trail-notes/2026-03-02-storage": (
        "Meeting notes, 2 March. We compared one SQLite file with one JSON file per note. A hiker edits notes on a "
        "phone and syncs the folder with whatever tool is at hand; a single database file would conflict on every "
        "sync, while separate files merge by themselves. Decision: one JSON file per note. Mira asked that the "
        "format stays readable in a text editor."),
    "note://trail-notes/2026-03-05-identifiers": (
        "First idea for identifiers, 5 March: number the notes 1, 2, 3 in the order they are written, because short "
        "numbers are easy to say aloud on the trail."),
    "note://trail-notes/2026-03-19-identifiers-revisited": (
        "19 March. Two phones wrote note 14 on the same day and the sync kept only one of them. Sequential numbers "
        "cannot work without a shared counter, and there is no network on the trail. Decision: every note gets a "
        "random UUID; a short number, if wanted, is only a display matter."),
    "note://trail-notes/2026-03-20-offline": (
        "Constraint agreed on 20 March: the tool never opens a network connection. Everything must work in a valley "
        "without signal; syncing is the job of another tool."),
    "note://trail-notes/2026-04-02-tags": (
        "2 April. Plan for tags: a note carries a list of tags; searching by tag comes after storing them. A note "
        "written before tags existed must still load, with no tags. Accepted for the first step once the unit check "
        "passes and an old note file was opened by hand."),
}


def git(*args):
    # Without the builder's own Git configuration: no signing, no hooks, no templates.
    subprocess.run(["git", "-C", str(NEUTRAL), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                   check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build(store, settings) -> Path:
    from cairnmind.memory import MemoryManager
    from cairnmind.plans import PlanManager
    from cairnmind.verification import run_check

    project = store.add_project("trail-notes", NEUTRAL)["id"]
    notes = {uri: store.snapshot(project, uri, text, "note") for uri, text in NOTES.items()}
    code_path = NEUTRAL / "trail_notes" / "store.py"
    code = store.snapshot(project, str(code_path), code_path.read_text(), "code", {"path": str(code_path), "root": str(NEUTRAL)})

    def note(name):
        return notes[f"note://trail-notes/{name}"]["id"]

    store.remember(project, {"kind": "decision", "key": "storage-format", "status": "confirmed",
                             "content": "Each note is stored as one JSON file. A single database file would conflict "
                                        "whenever two devices sync the folder; separate files merge by themselves."},
                   [note("2026-03-02-storage"), code["id"]])
    numbered = store.remember(project, {"kind": "decision", "key": "note-identifiers", "status": "confirmed",
                                        "content": "Notes are numbered in the order they are written."},
                              [note("2026-03-05-identifiers")])
    store.remember(project, {"kind": "decision", "key": "note-identifiers", "status": "confirmed", "supersedes": numbered["id"],
                             "content": "Every note gets a random UUID as its identifier. Sequential numbers need a "
                                        "shared counter, and two phones wrote the same number on one day."},
                   [note("2026-03-19-identifiers-revisited"), code["id"]])
    store.remember(project, {"kind": "constraint", "key": "offline", "status": "confirmed",
                             "content": "The tool never opens a network connection. Syncing is left to other tools."},
                   [note("2026-03-20-offline")])

    plans = PlanManager(store)
    saved = plans.save(project, {"title": "Find notes by tag", "goal": "A hiker can list every note that carries a tag.",
                                 "constraints": ["Works without a network.", "Old note files keep loading."]},
                       [{"key": "store-tags", "content": "Store a list of tags with each note",
                         "metadata": {"requires_test": True, "required_profiles": ["unit"], "acceptance_criteria": [
                             {"text": "The unit check passes.", "check": "unit"},
                             "A note written before tags existed still loads."]}},
                        {"key": "search", "content": "List the notes that carry a given tag", "depends_on": ["store-tags"]},
                        {"key": "document", "content": "Describe tags in the README", "depends_on": ["search"]}],
                       "sample-plan", worktree=str(NEUTRAL), activate=True)
    tasks = {task["metadata"]["step_key"]: task for task in saved["tasks"]}

    def move(task, evidence=(), **metadata):
        plan = plans.get(project, saved["plan"]["id"])["plan"]
        return store.remember(project, {"id": task["id"], "expected_version": task["version"],
                                        "expected_plan_version": plan["version"], "metadata": metadata,
                                        "worktree": str(NEUTRAL)}, [*task["source_ids"], *evidence])

    first = move(tasks["store-tags"], state="in_progress")
    run = run_check(store, settings, project, "unit", first["id"], worktree=str(NEUTRAL))
    if run.get("passed") is not True:
        raise SystemExit(f"The sample check did not pass: {run}")
    accepted = store.snapshot(project, "note://trail-notes/2026-04-02-tags", NOTES["note://trail-notes/2026-04-02-tags"], "note")
    move(first, evidence=[accepted["id"]], state="done", acceptance_passed=True, acceptance_source_ids=[accepted["id"]])
    move(tasks["search"], state="in_progress")

    MemoryManager(store).snapshot(project, worktree=str(NEUTRAL))
    return NEUTRAL / ".memory" / "cairnmind"


def main() -> int:
    if not sys.platform.startswith("linux"):
        raise SystemExit("Build the sample on Linux or WSL2.")
    from cairnmind.config import Settings

    user = Settings.load()
    for name in list(os.environ):
        if name.startswith(("CAIRNMIND_", "DEVBRAIN_")) and not name.endswith(("_HOME", "_ARCADE_DIST")):
            os.environ.pop(name)
    from cairnmind.engine import install_engine, runtime_dir, start_engine, stop_engine
    from cairnmind.store import Store

    distribution = Path(os.environ.get("CAIRNMIND_ARCADE_DIST") or os.environ.get("DEVBRAIN_ARCADE_DIST") or runtime_dir(user))
    if not (distribution / "lib").is_dir():
        raise SystemExit(f"No database engine distribution at {distribution}.")
    try:
        # The path is fixed, so that it can be neutral, and lies where every user may write. It is taken
        # only when nothing is there, and only what this run created is removed afterwards.
        os.mkdir(NEUTRAL.parent, 0o700)
    except FileExistsError:
        raise SystemExit(f"{NEUTRAL.parent} exists: another build is running, or one was interrupted. "
                         "Look at it, remove it and run again.") from None
    temporary = Path(tempfile.mkdtemp(prefix="cairnmind-sample-build-"))
    settings = replace(user, data_dir=temporary / "data", arcade_url=f"http://127.0.0.1:{free_port()}", arcade_user="root",
                       arcade_password=secrets.token_urlsafe(32), arcade_database="cairnmind_sample_" + uuid4().hex,
                       token=secrets.token_urlsafe(32), port=free_port(), cockpit_port=free_port())
    failed = True
    try:
        shutil.copytree(SAMPLE, NEUTRAL, ignore=shutil.ignore_patterns(".memory", "__pycache__", "README.md"))
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("-c", "user.name=Sample", "-c", "user.email=sample@example.invalid", "commit", "-qm", "Trail Notes")
        settings.save()
        install_engine(settings, distribution)
        start_engine(settings)
        package = build(Store(settings.data_dir, settings=settings), settings)
        target = SAMPLE / ".memory" / "cairnmind"
        # Built whole beside the committed package and put in its place at the end.
        staged = target.with_name("cairnmind.new")
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(package, staged, ignore=shutil.ignore_patterns(".publish.lock", "*.lock"))
        shutil.rmtree(target, ignore_errors=True)
        staged.rename(target)
        print(f"written {target}: {sum(1 for item in target.rglob('*') if item.is_file())} files")
        failed = False
    finally:
        try:
            stop_engine(settings)  # harmless when the engine never started
        except Exception as exc:  # noqa: BLE001 - the engine may have gone by itself
            print(f"The build engine could not be stopped cleanly: {exc}", file=sys.stderr)
        if failed:
            print(f"The build failed. Kept for a look: {temporary} (engine log under data/arcade) and {NEUTRAL.parent}. "
                  "Remove both before the next run.", file=sys.stderr)
        else:
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(NEUTRAL.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
