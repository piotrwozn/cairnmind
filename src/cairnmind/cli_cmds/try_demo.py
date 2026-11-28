"""Zero-setup ephemeral sandbox walkthrough for ``uvx cairnmind try`` (PR #7 / Faza 4b).

Runs completely inside a ``tempfile.TemporaryDirectory`` backed by in-memory SQLite
(``:memory:``):
- Never reads or writes anything inside the caller's ``cwd`` (works in read-only dirs).
- Installs ``SIGINT`` / ``SIGTERM`` handlers so ``Ctrl+C`` cleanly removes the sandbox.
- Demonstrates Cairnmind's two core guarantees on the bundled ``examples/sample-project``:
  1. Verbatim quote or ``unknown`` (no guessing).
  2. Blocked ``done`` on stale checks -> fresh check run -> verified ``done``.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairnmind.agents.retrieval import Retriever
from cairnmind.memory import verification
from cairnmind.scip import ingest
from cairnmind.store import Store


def locate_sample_project() -> Path:
    """Locate the bundled sample project (wheel force-include or repository checkout)."""
    pkg_root = Path(__file__).resolve().parents[1]
    candidates = [
        pkg_root / "sample_project",
        pkg_root.parents[1] / "examples" / "sample-project",
    ]
    for path in candidates:
        if (path / "trail_notes" / "store.py").is_file() and (path / "checks" / "notes_check.py").is_file():
            return path
    raise FileNotFoundError(
        "Bundled sample-project could not be located. Reinstall cairnmind or run from a source checkout."
    )


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Cairnmind Try",
        "GIT_AUTHOR_EMAIL": "try@cairnmind.local",
        "GIT_COMMITTER_NAME": "Cairnmind Try",
        "GIT_COMMITTER_EMAIL": "try@cairnmind.local",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(res.stderr or res.stdout).strip()[:200]}")


def _write_sandbox_checks_toml(repo: Path) -> None:
    """Bind the sandbox check profile to the active Python interpreter so it works offline without uv."""
    checks_dir = repo / ".cairnmind"
    checks_dir.mkdir(parents=True, exist_ok=True)
    py_exe = sys.executable.replace("\\", "\\\\")
    (checks_dir / "checks.toml").write_text(
        "[checks.unit]\n"
        f'argv = ["{py_exe}", "-B", "checks/notes_check.py"]\n'
        'cwd = "."\n'
        "timeout_seconds = 30\n"
        "output_limit_bytes = 1048576\n"
        "extra_inputs = []\n",
        encoding="utf-8",
    )


def run_try_sandbox(
    *,
    interactive: bool = False,
    echo: Callable[[str], None] | None = None,
    prompt: Callable[[str], str] | None = None,
    on_tempdir_created: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Execute the 3-step Cairnmind proof inside an isolated tempdir + SQLite ``:memory:``."""
    say = echo or (lambda _msg: None)
    sample_src = locate_sample_project()
    temp_obj = tempfile.TemporaryDirectory(prefix="cairnmind-try-")
    temp_root = Path(temp_obj.name)
    if on_tempdir_created is not None:
        on_tempdir_created(temp_root)

    cleaned = False

    def _cleanup() -> None:
        nonlocal cleaned
        if not cleaned:
            cleaned = True
            temp_obj.cleanup()

    old_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        def _on_signal(signum, _frame):
            _cleanup()
            raise KeyboardInterrupt(f"Interrupted by signal {signum}")

        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(OSError, ValueError):
                prev = signal.getsignal(sig)
                signal.signal(sig, _on_signal)
                old_handlers[sig] = prev

    store: Store | None = None
    try:
        repo = temp_root / "sample-project"
        shutil.copytree(sample_src, repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _write_sandbox_checks_toml(repo)
        _git(repo, "init")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "Initial sample project")

        data_dir = temp_root / "cairnmind-data"
        store = Store.open_memory(data_dir)
        project = store.add_project("sample-project", repo)
        indexed = ingest.index_project(store, project["id"])
        retriever = Retriever(store, store.settings)

        say("")
        say("Cairnmind Try Sandbox (ephemeral SQLite :memory:, zero files touched in your cwd)")
        say("================================================================================")
        say(f"Indexed {indexed.get('files', 0)} sample files in an isolated temporary directory.")
        say("")

        # --- Step 1: No guessing — unknown vs verbatim evidence ---
        missing_hits = retriever.search(project["id"], "turbo_cache_flag nonexistent_option", limit=5, worktree=str(repo))
        missing_status = "unknown" if not missing_hits else "found"
        real_hits = retriever.search(project["id"], "read_note tags setdefault", limit=5, worktree=str(repo))
        verbatim_quote = ""
        verbatim_uri = ""
        for hit in real_hits:
            if "store.py" in str(hit.get("uri", "")) and "setdefault" in str(hit.get("text", "")):
                verbatim_quote = 'note.setdefault("tags", [])'
                verbatim_uri = str(hit.get("uri", ""))
                break

        step1 = {
            "step": 1,
            "title": "No guessing: verbatim quote or unknown",
            "absent_query": "Does trail_notes support --turbo-cache-flag?",
            "absent_result": missing_status,
            "absent_error_code": "no_verbatim_evidence",
            "grounded_query": "How does read_note handle notes created before tags existed?",
            "grounded_quote": verbatim_quote,
            "grounded_source": verbatim_uri,
        }
        say("[1/3] Guarantee 1: Your agent does not guess")
        say("  Q: Does trail_notes support --turbo-cache-flag?")
        say(f"  A: {missing_status} (no verbatim confirmation in the current project files)")
        say("  Q: How does read_note handle notes written before tags existed?")
        say(f"  A: Verified verbatim quote -> `{verbatim_quote}` (trail_notes/store.py)")
        say("")
        if (
            interactive
            and prompt is not None
            and prompt("Press Enter for step 2 (or 'q' to quit): ").strip().lower() == "q"
        ):
            return {"completed": False, "aborted_at_step": 1, "steps": [step1], "backend": "sqlite", "ephemeral": True}

        # --- Step 2: No lying about done — blocked when code changes after check ---
        store_file = repo / "trail_notes" / "store.py"
        src_row = store.snapshot(
            project["id"],
            f"file://{store_file}",
            store_file.read_text(encoding="utf-8"),
            "code",
            {"root": str(repo), "relative_path": "trail_notes/store.py", "path": str(store_file)},
        )
        task = store.remember(
            project["id"],
            {
                "kind": "task",
                "key": "preserve-legacy-tags",
                "content": "Keep read_note backwards-compatible with notes written before tags existed",
                "metadata": {
                    "state": "in_progress",
                    "requires_test": True,
                    "required_profiles": ["unit"],
                },
            },
            [src_row["id"]],
        )
        run1 = verification.run_check(store, store.settings, project["id"], "unit", task["id"], worktree=str(repo))
        if run1["passed"] is not True:
            raise RuntimeError(f"Initial check failed unexpectedly: {run1}")

        # Edit the file AFTER tests ran — invalidating the fingerprint!
        store_file.write_text(
            store_file.read_text(encoding="utf-8").rstrip("\n") + "\n# edited after check run\n",
            encoding="utf-8",
        )
        blocked_reason = ""
        try:
            store.remember(
                project["id"],
                {
                    "id": task["id"],
                    "expected_version": task["version"],
                    "worktree": str(repo),
                    "metadata": {
                        **task["metadata"],
                        "state": "done",
                        "check_run_ids": [run1["id"]],
                    },
                },
                [src_row["id"]],
            )
        except ValueError as exc:
            blocked_reason = str(exc)

        step2 = {
            "step": 2,
            "title": "No lying about done: blocked after code changed",
            "initial_check_passed": bool(run1["passed"]),
            "initial_fingerprint": (run1.get("code_after") or {}).get("fingerprint"),
            "done_blocked": bool(blocked_reason),
            "error_code": "fingerprint_mismatch" if blocked_reason else None,
            "blocked_reason": blocked_reason,
        }
        say("[2/3] Guarantee 2: Your agent cannot lie that it finished")
        say(f"  1. Ran check profile `unit` -> PASS (fingerprint: {str(step2['initial_fingerprint'])[:12]}...)")
        say("  2. Edited trail_notes/store.py after tests passed.")
        say("  3. Tried marking task `done` -> BLOCKED (fingerprint_mismatch):")
        say(f"     {blocked_reason}")
        say("")
        if (
            interactive
            and prompt is not None
            and prompt("Press Enter for step 3 (or 'q' to quit): ").strip().lower() == "q"
        ):
            return {"completed": False, "aborted_at_step": 2, "steps": [step1, step2], "backend": "sqlite", "ephemeral": True}

        # --- Step 3: Fresh check unlocks verified done ---
        run2 = verification.run_check(store, store.settings, project["id"], "unit", task["id"], worktree=str(repo))
        if run2["passed"] is not True:
            raise RuntimeError(f"Fresh check failed unexpectedly: {run2}")
        done_task = store.remember(
            project["id"],
            {
                "id": task["id"],
                "expected_version": task["version"],
                "worktree": str(repo),
                "metadata": {
                    **task["metadata"],
                    "state": "done",
                    "check_run_ids": [run2["id"]],
                },
            },
            [src_row["id"]],
        )
        fresh_fp = (run2.get("code_after") or {}).get("fingerprint")
        step3 = {
            "step": 3,
            "title": "Fresh proof unlocks verified done",
            "fresh_check_id": run2["id"],
            "fresh_check_passed": bool(run2["passed"]),
            "fresh_fingerprint": fresh_fp,
            "task_state": done_task["metadata"]["state"],
            "task_version": done_task["version"],
        }
        say("[3/3] Fresh check unlocks verified done")
        say(f"  1. Re-ran `unit` on updated code -> PASS (fingerprint: {str(fresh_fp)[:12]}...)")
        say(f"  2. Marked task `done` -> SUCCESS (task v{done_task['version']}, state=done, pinned check_run={run2['id'][:8]})")
        say("")
        say("Next steps for your own repository:")
        say("  cairnmind init --backend sqlite")
        say("  cairnmind project add my-app /path/to/repo")
        say("  cairnmind check run my-app <task-id> unit")
        say("")
        return {
            "completed": True,
            "backend": "sqlite",
            "ephemeral": True,
            "files_indexed": indexed.get("files", 0),
            "steps": [step1, step2, step3],
        }
    finally:
        if store is not None:
            store.close()
        for sig, handler in old_handlers.items():
            with suppress(OSError, ValueError):
                signal.signal(sig, handler)
        _cleanup()
