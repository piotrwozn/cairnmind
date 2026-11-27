"""An opt-in log of `ask` answers with the evidence they cited, and a replay that asks them again (#15).

What is kept. With ``ask --log`` (one question) or ``"answer_log": true`` in config.json (every question
the service answers), each answer is appended to ``DATA_DIR/answer-log/PROJECT_ID/answers.jsonl``: the
question, the worktree it was asked for, the answer contract if any, the code it was asked about (commit,
branch and the code fingerprint of the cache), the status with its diagnostic code and support check, the
answer, the unknowns, every cited source (id, address, sha256, kind, quote and lines), the chat model with
the digest Ollama reports, the embedding model, and the timings the answer already carries. The service's
own token and database password are replaced wherever they occur.

Where. The question and the answer are memory content, so they stay in the data directory, private to its
owner (directories 0700, files 0600), and the workspace's Delete project memory (project_forget.delete)
deletes them with the project. They are not in the store: a logged answer has no source row, no chunk and no embedding, so it is
never searched, never quoted, never cited and never exported in a memory snapshot. A stored answer never
becomes evidence. (The issue reserved an ``agent_answer`` source kind for this; a source row would be
readable by the agent's source tools and travel with memory packages, and sources are never pruned.)

Bounds. One entry is at most MAX_ENTRY_BYTES (a larger one is not logged). When a project's log grows past
MAX_LOG_BYTES the oldest entries are dropped until it fits in half of that. ``cairnmind answers prune``
keeps only the newest N entries, or none.

Replay. ``cairnmind eval replay PROJECT`` asks the newest logged answer of each distinct question again,
through the running service, and compares the status and the set of cited source addresses, never the
wording: an answer that was complete and is no longer complete has regressed, one that no longer cites a
source it cited before has lost sources.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import time
from uuid import uuid4

from .platform_fs import O_CLOEXEC, O_NOFOLLOW, owner_matches, pread_bytes, private_mode_ok
from .platform_locks import lock_file

FORMAT_VERSION = 1
DIRECTORY = "answer-log"
MAX_ENTRY_BYTES = 256 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_REPLAY = 200
_PROJECT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
log = logging.getLogger(__name__)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or not owner_matches(info) or not private_mode_ok(info, 0o700):
        raise ValueError(f"{path} must be a private directory owned by this user")


def _project_dir(settings, project_id: str) -> Path:
    if not isinstance(project_id, str) or not _PROJECT.fullmatch(project_id):
        raise ValueError("Invalid project id for the answer log")
    return Path(settings.data_dir) / DIRECTORY / project_id


def log_path(settings, project_id: str) -> Path:
    return _project_dir(settings, project_id) / "answers.jsonl"


@contextmanager
def _locked(settings, project_id: str, create: bool):
    """The project's log directory under an exclusive lock; None when it does not exist and is not created."""
    folder = _project_dir(settings, project_id)
    root = Path(settings.data_dir)
    if root.is_symlink():
        raise ValueError("The data directory cannot be a symbolic link")
    if not create and not folder.exists():
        yield None
        return
    for path in (root / DIRECTORY, folder):
        _private_dir(path)
    fd = os.open(folder / "answers.lock", os.O_CREAT | os.O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600)
    try:
        lock_file(fd, exclusive=True)
        yield folder
    finally:
        os.close(fd)


def _counters(calls) -> list[dict]:
    keep = ("phase", "done_reason", "prompt_eval_count", "eval_count", "total_duration")
    return [{key: call[key] for key in keep if key in call} for call in (calls or [])[:8] if isinstance(call, dict)]


def make_entry(settings, project_id: str, question: str, result: dict, *, worktree=None, answer_schema=None,
               code: dict | None = None, model_digest: str | None = None) -> dict:
    """The log entry of one answer: only what the answer, the request and the service already hold."""
    from cairnmind.sql import utc_now
    diagnostics = result.get("diagnostics") or {}
    code = code or {}
    sources = [{key: source.get(key) for key in ("id", "uri", "sha256", "kind", "quote", "line_start", "line_end")}
               for source in (result.get("sources") or [])[:8] if isinstance(source, dict)]
    entry = {
        "format_version": FORMAT_VERSION, "id": uuid4().hex, "at": utc_now(), "project_id": project_id,
        "question": question, "worktree": worktree, "answer_schema": answer_schema,
        "code": {"head": code.get("head"), "branch": code.get("branch"), "fingerprint": code.get("fingerprint"),
                 "status": code.get("status")},
        "status": result.get("status"), "diagnostic_code": diagnostics.get("code"),
        "support_check": diagnostics.get("support_check"), "repair_outcome": diagnostics.get("repair_outcome"),
        "cached": bool(result.get("cached")),
        "answer": str(result.get("answer", "")), "unknowns": [str(item) for item in result.get("unknowns") or []][:8],
        "sources": sources,
        "model": {"chat": settings.chat_model, "digest": model_digest, "embedding": settings.embedding_model},
        "timings": {"elapsed_seconds": result.get("elapsed_seconds"),
                    "model_calls": _counters(diagnostics.get("model_calls"))},
    }
    # The agent journal's rule: any non-empty credential, a truncated one at the end of a string, and
    # sensitive field names. A value that is not JSON raises ValueError, and the answer is not logged.
    from cairnmind.agent_journal import redact_secrets
    return redact_secrets(entry, (getattr(settings, "token", None), getattr(settings, "arcade_password", None)))


def _line(entry: dict) -> bytes:
    return (json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _parse(raw: bytes) -> tuple[list[dict], int]:
    entries, unreadable = [], 0
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            unreadable += 1
            continue
        if (isinstance(entry, dict) and entry.get("format_version") == FORMAT_VERSION
                and isinstance(entry.get("id"), str) and _ID.fullmatch(entry["id"])
                and isinstance(entry.get("question"), str) and isinstance(entry.get("sources"), list)):
            entries.append(entry)
        else:
            unreadable += 1
    return entries, unreadable


def _read_file(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    except FileNotFoundError:
        return b""
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not owner_matches(info):
            raise ValueError(f"{path} is not a regular file owned by this user")
        return handle.read(MAX_LOG_BYTES * 2)


def _rewrite(folder: Path, lines: list[bytes]) -> None:
    temporary = folder / f".answers-{uuid4().hex}.tmp"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW, 0o600), "wb") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, folder / "answers.jsonl")


def append(settings, entry: dict) -> bool:
    """Append one entry; False when it is larger than MAX_ENTRY_BYTES. Compacts past MAX_LOG_BYTES."""
    line = _line(entry)
    if len(line) > MAX_ENTRY_BYTES:
        return False
    with _locked(settings, entry["project_id"], create=True) as folder:
        path = folder / "answers.jsonl"
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        with os.fdopen(fd, "ab") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{path} is not a regular file")
            if info.st_size and pread_bytes(handle.fileno(), 1, info.st_size - 1) != b"\n":
                # An earlier write was cut short: end its fragment so it does not swallow this entry.
                line = b"\n" + line
            handle.write(line)
            size = handle.tell()
        if size > MAX_LOG_BYTES:
            # Keep the newest entries that fit in half the budget, so compaction is rare.
            kept, total = [], 0
            for item in reversed(_read_file(path).splitlines(keepends=True)):
                if total + len(item) > MAX_LOG_BYTES // 2:
                    break
                kept.append(item)
                total += len(item)
            _rewrite(folder, list(reversed(kept)))
    return True


def read_entries(settings, project_id: str) -> tuple[list[dict], int]:
    """``(entries oldest first, number of unreadable lines)``; empty when nothing was logged."""
    with _locked(settings, project_id, create=False) as folder:
        if folder is None:
            return [], 0
        return _parse(_read_file(folder / "answers.jsonl"))


def summary(entry: dict) -> dict:
    """One row of ``cairnmind answers list``."""
    return {"id": entry["id"], "at": entry.get("at"), "status": entry.get("status"),
            "diagnostic_code": entry.get("diagnostic_code"), "question": entry["question"][:200],
            "sources": len(entry["sources"]), "commit": (entry.get("code") or {}).get("head"),
            "model": (entry.get("model") or {}).get("chat")}


def listing(settings, project_id: str, limit: int = 20) -> dict:
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("--limit must be between 1 and 1000")
    entries, unreadable = read_entries(settings, project_id)
    return {"project_id": project_id, "logged": len(entries), "unreadable_lines": unreadable,
            "answers": [summary(entry) for entry in reversed(entries[-limit:])]}


def find(settings, project_id: str, entry_id: str) -> dict:
    if not isinstance(entry_id, str) or not _ID.fullmatch(entry_id):
        raise ValueError("An answer id is 32 hexadecimal characters, as `cairnmind answers list` shows it")
    entry = next((item for item in read_entries(settings, project_id)[0] if item["id"] == entry_id), None)
    if entry is None:
        raise LookupError("No logged answer with this id in this project")
    return entry


def prune(settings, project_id: str, keep: int) -> dict:
    """Keep only the newest ``keep`` entries (0 deletes the project's log)."""
    if not isinstance(keep, int) or keep < 0:
        raise ValueError("--keep must be zero or more")
    with _locked(settings, project_id, create=False) as folder:
        if folder is None:
            return {"project_id": project_id, "kept": 0, "removed": 0}
        lines = _read_file(folder / "answers.jsonl").splitlines(keepends=True)
        kept = lines[-keep:] if keep else []
        _rewrite(folder, kept)
    return {"project_id": project_id, "kept": len(kept), "removed": len(lines) - len(kept)}


def forget(settings, project_id: str) -> bool:
    """Delete the project's whole answer log, as the project's memory is deleted."""
    folder = _project_dir(settings, project_id)
    if folder.is_symlink():
        folder.unlink()
        return True
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    return True


def _evidence(result: dict) -> dict:
    return {source.get("uri"): source.get("sha256") for source in result.get("sources") or []
            if isinstance(source, dict) and isinstance(source.get("uri"), str)}


def compare(before: dict, after: dict) -> dict:
    """How a new answer differs from a logged one, by status and cited addresses; the wording is not compared."""
    old, new = _evidence(before), _evidence(after)
    lost = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    changed = sorted(uri for uri in set(old) & set(new) if old[uri] != new[uri])
    was, now = before.get("status"), after.get("status")
    verdict = ("regressed" if was == "complete" and now != "complete"
               else "lost_sources" if lost
               else "improved" if was != "complete" and now == "complete"
               else "status_changed" if was != now
               else "sources_added" if added else "unchanged")
    return {"verdict": verdict, "lost_sources": lost, "new_sources": added, "changed_sources": changed}


def replay_cases(entries: list[dict], limit: int) -> list[dict]:
    """The newest entry of each distinct question (with its worktree and contract), newest first."""
    if not isinstance(limit, int) or not 1 <= limit <= MAX_REPLAY:
        raise ValueError(f"--limit must be between 1 and {MAX_REPLAY}")
    seen, cases = set(), []
    for entry in reversed(entries):
        key = json.dumps([entry["question"], entry.get("worktree"), entry.get("answer_schema")], sort_keys=True)
        if key not in seen:
            seen.add(key)
            cases.append(entry)
    return cases[:limit]


def _side(result: dict) -> dict:
    diagnostics = result.get("diagnostics") or {}
    return {"status": result.get("status"), "diagnostic_code": result.get("diagnostic_code", diagnostics.get("code")),
            "sources": sorted(_evidence(result))}


def replay(settings, project_id: str, ask, *, limit: int = 20, report_dir: Path | None = None) -> dict:
    """Ask the logged questions again with ``ask(payload) -> result`` and write a report; see the module docstring."""
    from . import __version__
    entries, unreadable = read_entries(settings, project_id)
    cases = replay_cases(entries, limit)
    started = time.monotonic()
    rows = []
    for entry in cases:
        payload = {"project": project_id, "question": entry["question"], "worktree": entry.get("worktree"),
                   "log": False}
        if entry.get("answer_schema") is not None:
            payload["answer_schema"] = entry["answer_schema"]
        row = {"log_id": entry["id"], "asked_at": entry.get("at"), "question": entry["question"][:300],
               "model_before": entry.get("model"), "commit_before": (entry.get("code") or {}).get("head"),
               "before": _side(entry)}
        try:
            result = ask(payload)
        except Exception as exc:  # noqa: BLE001 - one refused question does not end the replay
            row.update(verdict="not_asked", reason=str(exc)[:300])
        else:
            row.update(after=_side(result), **compare(entry, result))
        rows.append(row)
    counts = {name: sum(row["verdict"] == name for row in rows) for name in (
        "unchanged", "sources_added", "improved", "status_changed", "lost_sources", "regressed", "not_asked")}
    failed = counts["regressed"] + counts["lost_sources"] + counts["not_asked"]
    report = {"format_version": 1, "cairnmind_version": __version__, "evaluation_mode": "answer_replay",
              "project_id": project_id, "logged": len(entries), "unreadable_lines": unreadable,
              "replayed": len(rows), "summary": counts,
              "status": "not_run" if not rows else "failed" if failed else "passed",
              "compared": "the status and the set of cited source addresses; the wording is not compared",
              "answers": rows, "elapsed_seconds": round(time.monotonic() - started, 1)}
    output = Path(report_dir or Path(settings.data_dir) / "evaluations").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = output / ("replay-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8] + ".json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    report["report_path"] = str(path)
    return report
