"""Questions with known answers about a real repository, asked with the local models.

``cairnmind eval repository ROOT`` copies the files Git tracks in ROOT into a temporary
directory, indexes the copy into a throwaway data directory and database on the running engine
(as ``cairnmind eval run`` and ``eval memory`` do), embeds it, asks every question of a question
file with the configured models and writes a report. The user's own projects and database are
not read or written; the temporary database is dropped afterwards. It needs Ollama with the
configured models and a running engine, so it is not part of the automated tests.

What is indexed: only files Git tracks in ROOT (``git ls-files``), never a link, never a file
outside ROOT, and never the paths of NOT_INDEXED or the question file: the evaluation must not
read its own questions, accepted answers or reported results.

A question file is JSON: ``{"format_version": 1, "questions": [{"id", "question", "file",
"answer"}]}``. ``answer`` is ``{"accepted": [...]}`` for an answer that does not move, or
``{"pattern": REGEX}`` for a value read from ``file`` at run time (the first group of the first
match, multi-line), so that a changed default does not make the question wrong. ``"forms"`` adds
written forms of the value that also count for that question: ``"{}m"`` (the value with a unit),
``"{word}"`` (a number as its English word).

Scoring is mechanical. A question is *complete* when the answer's status is complete (its
citations verified and the support check agreed). It is *right* when an accepted form occurs in
the answer as a whole token, ignoring case: not inside a longer word or number (``7`` is not in
``17``, ``7.5``, ``7,168`` or ``7m``); ``45.0`` also counts as ``45``. A complete answer without
an accepted form counts as *complete and wrong*. The acceptance figure is complete and right.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from . import __version__
from .arcade import ArcadeClient, ArcadeError
from .config import Settings
from .evaluation import MeasuredOllama, _fixture_settings, _git
from .ollama import Ollama, OllamaUnavailable

PASS_SHARE = 0.8  # complete and right, of all questions
# Never indexed: the questions and the document that reports the runs (the question file given
# on the command line is left out too, wherever it lies), and the tests of this evaluation and of
# the definition lookup, whose fixtures imitate the evaluated questions (a settings class with a
# port and a cockpit port, a default timeout, answer tokens) with other values.
NOT_INDEXED = ("docs/evaluation/", "tests/test_definitions.py", "tests/test_repository_evaluation.py")
_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                 "fifteen sixteen seventeen eighteen nineteen twenty").split()


def _forms(value: str, forms: list[str]) -> list[str]:
    values = [value, *([value.split(".")[0]] if re.fullmatch(r"\d+\.0+", value) else [])]
    # The same number written with thousands separators (16,384) is the same answer.
    found = [*values, *(f"{int(item):,}" for item in values if item.isdigit() and int(item) >= 1000)]
    for form in forms:
        for item in values:
            word = _NUMBER_WORDS[int(item)] if item.isdigit() and int(item) < len(_NUMBER_WORDS) else None
            if "{word}" in form and word is None:
                continue
            found.append(form.replace("{}", item).replace("{word}", word or ""))
    return list(dict.fromkeys(found))


def load_questions(path: Path, root: Path) -> list[dict]:
    """Questions of ``path`` with their accepted forms resolved against ``root``.

    Raises ValueError, naming the question, when the file is malformed or a pattern finds nothing:
    a question whose answer cannot be stated is not asked.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read the question file {path}: {exc}") from None
    questions = data.get("questions") if isinstance(data, dict) else None
    if data.get("format_version") != 1 or not isinstance(questions, list) or not questions:
        raise ValueError("A question file needs format_version 1 and a nonempty questions list")
    resolved, seen = [], set()
    for item in questions:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key]
                                                  for key in ("id", "question", "file")):
            raise ValueError("Every question needs id, question and file")
        if item["id"] in seen:
            raise ValueError(f"Question id {item['id']!r} is used twice")
        seen.add(item["id"])
        answer = item.get("answer") if isinstance(item.get("answer"), dict) else {}
        forms = answer.get("forms", [])
        if not isinstance(forms, list) or not all(isinstance(form, str) and form for form in forms):
            raise ValueError(f"{item['id']}: forms must be a list of strings")
        if isinstance(answer.get("pattern"), str):
            try:
                text = (root / item["file"]).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"{item['id']}: cannot read {item['file']}: {exc}") from None
            match = re.search(answer["pattern"], text, re.MULTILINE)
            if not match or not match.groups() or not match.group(1):
                raise ValueError(f"{item['id']}: the pattern finds no value in {item['file']}")
            values = [match.group(1)]
        elif isinstance(answer.get("accepted"), list) and answer["accepted"] and all(
                isinstance(value, str) and value for value in answer["accepted"]):
            values = list(answer["accepted"])
        else:
            raise ValueError(f"{item['id']}: answer needs a pattern or a nonempty accepted list")
        accepted = list(dict.fromkeys(form for value in values for form in _forms(value, forms)))
        resolved.append({"id": item["id"], "question": item["question"], "file": item["file"],
                         "accepted": accepted})
    return resolved


def answer_is_right(answer: str, accepted: list[str]) -> bool:
    """True when an accepted form occurs in ``answer`` as a whole token, ignoring case.

    Not as part of a longer word or number: no letter, digit or underscore next to it, no decimal
    part after it, and no thousands group around it (``1,300`` does not contain ``300``).
    """
    for value in accepted:
        pattern = (r"(?<![\w.])(?<!\d,)" + re.escape(value) + r"(?![\w]|[.,]\d)")
        if re.search(pattern, answer, re.IGNORECASE):
            return True
    return False


def case_result(case: dict, result: dict, root: Path, calls: int, seconds: float) -> dict:
    """The report row of one question, from the agent's result."""
    citations = []
    for source in result.get("sources") or []:
        uri = str(source.get("uri", ""))
        try:
            uri = Path(uri).relative_to(root).as_posix()
        except ValueError:
            pass
        citations.append({"path": uri, "line_start": source.get("line_start"),
                          "quote": str(source.get("quote", ""))[:300]})
    status = result.get("status")
    answer = str(result.get("answer", ""))
    diagnostics = result.get("diagnostics") or {}
    return {"id": case["id"], "question": case["question"], "file": case["file"], "accepted": case["accepted"],
            "status": status, "code": diagnostics.get("code"), "support_check": diagnostics.get("support_check"),
            "right": status == "complete" and answer_is_right(answer, case["accepted"]),
            "answer": answer[:600], "citations": citations, "model_calls": calls, "seconds": round(seconds, 1)}


def summarize(rows: list[dict]) -> dict:
    """Counts over the report rows; status passed when complete-and-right reaches PASS_SHARE."""
    complete = sum(row["status"] == "complete" for row in rows)
    right = sum(row["status"] == "complete" and row["right"] for row in rows)
    required = math.ceil(PASS_SHARE * len(rows))
    return {"asked": len(rows), "complete": complete, "complete_and_right": right,
            "complete_and_wrong": complete - right, "not_complete": len(rows) - complete,
            "required_complete_and_right": required,
            "status": "passed" if rows and right >= required else "failed"}


def tracked_files(root: Path, excluded: list[str]) -> list[str]:
    """Paths (relative, POSIX) of the regular files Git tracks in ``root``, minus ``excluded``.

    A link, a path that resolves outside ``root`` and a tracked path missing on disk are left out.
    Raises ValueError when ``root`` is not a Git worktree.
    """
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--full-name"],
                            capture_output=True, timeout=60, check=False)
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=30, check=False)
    if listed.returncode or top.returncode or Path(top.stdout.strip()).resolve() != root:
        raise ValueError(f"{root} is not the top of a Git worktree; the evaluation indexes only the files Git tracks")
    files = []
    for name in sorted({part.decode("utf-8", "surrogateescape") for part in listed.stdout.split(b"\0") if part}):
        if any(name == item or (item.endswith("/") and name.startswith(item)) for item in excluded):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        if any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root) and parent != root):
            continue
        files.append(name)
    return files


def _copy(root: Path, copy: Path, files: list[str]) -> None:
    for name in files:
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    _git(copy, "init", "--quiet")
    _git(copy, "add", "-A")
    _git(copy, "-c", "user.name=Cairnmind Evaluation", "-c", "user.email=eval@localhost",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Evaluated copy")


async def _ask_all(settings: Settings, root: Path, cases: list[dict], measured, report: dict) -> None:
    from .agent import KnowledgeAgent
    from .engine import engine_status
    from .ingest import index_project
    from .retrieval import Retriever
    from .store import Store

    if not engine_status(settings)["available"]:
        # Store would otherwise start an engine from the temporary data directory.
        raise ArcadeError("The database engine stopped during the evaluation")
    store = Store(settings.data_dir, settings=settings)
    project = store.add_project("repository evaluation", root)
    report["stage"] = "index"
    started = time.monotonic()
    indexed = await asyncio.to_thread(index_project, store, project["id"])
    retriever = Retriever(store, settings, measured)
    chunks = store.query("SELECT id FROM chunks WHERE project_id=:project AND is_current=true",
                         {"project": project["id"]})
    report["stage"] = "embed"
    for row in chunks:
        await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": row["id"]}})
    report["index"] = {"files": indexed.get("files"), "chunks": len(chunks), "errors": indexed.get("errors", [])[:5],
                       "seconds": round(time.monotonic() - started, 1)}
    report["stage"] = "ask"
    agent = KnowledgeAgent(store, settings, ollama=measured, retriever=retriever)
    for case in cases:
        before, asked = len(measured.calls), time.monotonic()
        result = await agent.ask(project["id"], case["question"])
        # MeasuredOllama records chat calls only (answer, repair, support check), not embeddings.
        report["questions"].append(case_result(case, result, root, len(measured.calls) - before,
                                               time.monotonic() - asked))


def _preflight(settings: Settings, measured, report: dict) -> bool:
    """Models and engine are there; otherwise the report says why nothing was measured."""
    from .engine import engine_status

    try:
        async def models():
            for field, model in (("chat", settings.chat_model), ("embedding", settings.embedding_model)):
                report["model_identities"][field] = await measured.model_identity(model)
        asyncio.run(models())
    except OllamaUnavailable as exc:
        report.update(error=measured._error(exc), reason="The local model server or a configured model is not available")
        return False
    status = engine_status(settings)
    if not status["available"] or not status.get("version_matches", True):
        report["reason"] = ("The database engine is not running; start it with `cairnmind engine start`"
                            if not status["available"] else "The running database engine is not the pinned release")
        return False
    return True


def _drop(isolated: Settings, measured, report: dict) -> None:
    try:
        ArcadeClient(isolated).server_command("drop database " + isolated.arcade_database)
        report["isolation"]["database_dropped"] = True
    except (ArcadeError, OSError) as exc:
        report["isolation"]["database_dropped"] = False
        report.update(status="failed", cleanup_error=measured._error(exc))


def run_repository_evaluation(settings: Settings, root: Path, questions_path: Path, *,
                              report_dir: Path | None = None, ollama=None) -> dict:
    """Ask the questions of ``questions_path`` about ``root``; see the module docstring."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    cases = load_questions(questions_path, root)
    listed = Path(questions_path).expanduser().resolve()
    excluded = [*NOT_INDEXED, *([listed.relative_to(root).as_posix()] if listed.is_relative_to(root) else [])]
    files = tracked_files(root, excluded)
    started = time.monotonic()
    measured = MeasuredOllama(ollama or Ollama(settings))
    report = {"format_version": 1, "cairnmind_version": __version__, "evaluation_mode": "repository_questions",
              "repository": root.name, "questions_file": str(questions_path), "status": "not_run",
              "model": settings.chat_model, "embedding_model": settings.embedding_model, "model_identities": {},
              "questions": [], "isolation": {"copied": "files Git tracks", "copied_files": len(files),
                                             "not_indexed": excluded, "throwaway_data_directory": True,
                                             "user_projects_modified": False},
              "scoring": "complete = verified citations and support check; right = an accepted form in the "
                         "answer; a complete answer without one counts as complete and wrong"}
    isolated, interrupted = None, False
    if _preflight(settings, measured, report):
        # mkdtemp creates the directory for its owner only (0700).
        with tempfile.TemporaryDirectory(prefix="cairnmind-repo-eval-") as name:
            temporary = Path(name)
            try:
                _copy(root, temporary / "repository", files)
                isolated = _fixture_settings(settings, temporary / "data")
                report["isolation"]["database"] = isolated.arcade_database
                asyncio.run(_ask_all(isolated, temporary / "repository", cases, measured, report))
                report.update(summarize(report["questions"]), stage="complete")
            except KeyboardInterrupt:
                interrupted = True
                report.update(status="failed", failure_stage="interrupted")
            except OllamaUnavailable as exc:
                report.update(status="failed", failure_stage=report.get("stage"), error=measured._error(exc))
            except Exception as exc:  # noqa: BLE001 - the report records the failure, never a pass
                report.update(status="failed", failure_stage=report.get("stage", "setup"), error=measured._error(exc))
            finally:
                if isolated is not None:
                    _drop(isolated, measured, report)
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    output = Path(report_dir or settings.data_dir / "evaluations").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = output / ("repository-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8] + ".json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    report["report_path"] = str(path)
    if report["isolation"].get("database_dropped") is False:
        print(f"The evaluation database {report['isolation']['database']} could not be dropped; "
              "see docs/EVALUATION.md to remove it.", flush=True)
    if interrupted:
        raise KeyboardInterrupt
    return report
