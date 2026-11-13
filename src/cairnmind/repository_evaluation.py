"""Questions with known answers about a real repository, asked with the local models.

``cairnmind eval repository ROOT`` copies ROOT into a temporary directory, indexes the copy into
a throwaway data directory and database on the configured engine (as ``cairnmind eval run`` and
``eval memory`` do), embeds it, asks every question of a question file with the configured
models and writes a report. The user's own projects and database are not read or written; the
temporary database is dropped afterwards. It needs Ollama with the configured models and a
running engine, so it is not part of the automated tests.

A question file is JSON: ``{"format_version": 1, "questions": [{"id", "question", "file",
"answer"}]}``. ``answer`` is ``{"accepted": [...]}`` for an answer that does not move, or
``{"pattern": REGEX}`` for a value read from ``file`` at run time (the first group of the first
match, multi-line), so that a changed default does not make the question wrong.

Scoring is mechanical. A question is *complete* when the answer's status is complete (its
citations verified and the support check agreed). It is *right* when an accepted value occurs in
the answer as a whole token, ignoring case (a number also as its English word up to twenty, and
``60.0`` also as ``60``). A complete answer without an accepted value counts as *complete and
wrong*. The acceptance figure is complete and right.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from uuid import uuid4

from . import __version__
from .arcade import ArcadeClient, ArcadeError
from .config import Settings
from .evaluation import MeasuredOllama, _fixture_settings, _git
from .ollama import Ollama, OllamaUnavailable

PASS_SHARE = 0.8  # complete and right, of all questions
COPY_IGNORED = (".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".memory")
_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                 "fifteen sixteen seventeen eighteen nineteen twenty").split()


def load_questions(path: Path, root: Path) -> list[dict]:
    """Questions of ``path`` with their accepted answers resolved against ``root``.

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
        if isinstance(answer.get("pattern"), str):
            try:
                text = (root / item["file"]).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"{item['id']}: cannot read {item['file']}: {exc}") from None
            match = re.search(answer["pattern"], text, re.MULTILINE)
            if not match or not match.groups() or not match.group(1):
                raise ValueError(f"{item['id']}: the pattern finds no value in {item['file']}")
            accepted = [match.group(1)]
        elif isinstance(answer.get("accepted"), list) and answer["accepted"] and all(
                isinstance(value, str) and value for value in answer["accepted"]):
            accepted = list(answer["accepted"])
        else:
            raise ValueError(f"{item['id']}: answer needs a pattern or a nonempty accepted list")
        resolved.append({"id": item["id"], "question": item["question"], "file": item["file"],
                         "accepted": accepted})
    return resolved


def _variants(value: str) -> list[str]:
    found = [value]
    if re.fullmatch(r"\d+\.0+", value):
        found.append(value.split(".")[0])
    for item in list(found):
        if item.isdigit() and int(item) < len(_NUMBER_WORDS):
            found.append(_NUMBER_WORDS[int(item)])
    return found


def answer_is_right(answer: str, accepted: list[str]) -> bool:
    """True when an accepted value occurs in ``answer`` as a whole token, ignoring case."""
    for value in accepted:
        for variant in _variants(value):
            if re.search(r"(?<![\w.])" + re.escape(variant) + r"(?![\w]|\.\d)", answer, re.IGNORECASE):
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


async def _ask_all(settings: Settings, root: Path, cases: list[dict], measured, report: dict) -> None:
    from .agent import KnowledgeAgent
    from .ingest import index_project
    from .retrieval import Retriever
    from .store import Store

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


def run_repository_evaluation(settings: Settings, root: Path, questions_path: Path, *,
                              report_dir: Path | None = None, ollama=None) -> dict:
    """Ask the questions of ``questions_path`` about ``root``; see the module docstring."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    cases = load_questions(questions_path, root)
    started = time.monotonic()
    measured = MeasuredOllama(ollama or Ollama(settings))
    report = {"format_version": 1, "cairnmind_version": __version__, "evaluation_mode": "repository_questions",
              "repository": root.name, "questions_file": str(questions_path), "status": "not_run",
              "model": settings.chat_model, "embedding_model": settings.embedding_model, "model_identities": {},
              "questions": [], "isolation": {"copied_repository": True, "throwaway_data_directory": True,
                                             "user_projects_modified": False},
              "scoring": "complete = verified citations and support check; right = an accepted value in the "
                         "answer; a complete answer without one counts as complete and wrong"}
    isolated = None
    try:
        async def preflight():
            for field, model in (("chat", settings.chat_model), ("embedding", settings.embedding_model)):
                report["model_identities"][field] = await measured.model_identity(model)
        asyncio.run(preflight())
    except OllamaUnavailable as exc:
        # Without the local models nothing is measured; that is never a result.
        report.update(error=measured._error(exc), reason="The local model server or a configured model is not available")
    else:
        with tempfile.TemporaryDirectory(prefix="cairnmind-repo-eval-") as name:
            temporary = Path(name)
            copy = temporary / "repository"
            try:
                shutil.copytree(root, copy, symlinks=True, ignore=shutil.ignore_patterns(*COPY_IGNORED))
                # The question file names accepted answers; it must not be quotable evidence.
                listed = Path(questions_path).expanduser().resolve()
                if listed.is_relative_to(root):
                    (copy / listed.relative_to(root)).unlink(missing_ok=True)
                _git(copy, "init", "--quiet")
                _git(copy, "add", "-A")
                _git(copy, "-c", "user.name=Cairnmind Evaluation", "-c", "user.email=eval@localhost",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Evaluated copy")
                isolated = _fixture_settings(settings, temporary / "data")
                asyncio.run(_ask_all(isolated, copy, cases, measured, report))
                report.update(summarize(report["questions"]), stage="complete")
            except OllamaUnavailable as exc:
                report.update(status="failed", failure_stage=report.get("stage"), error=measured._error(exc))
            except Exception as exc:  # noqa: BLE001 - the report records the failure, never a pass
                report.update(status="failed", failure_stage=report.get("stage", "setup"), error=measured._error(exc))
            finally:
                if isolated is not None:
                    try:
                        ArcadeClient(isolated).server_command("drop database " + isolated.arcade_database)
                        report["isolation"]["database_dropped"] = True
                    except ArcadeError as exc:
                        report["isolation"]["database_dropped"] = False
                        report["cleanup_error"] = measured._error(exc)
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    output = Path(report_dir or settings.data_dir / "evaluations").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = output / ("repository-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8] + ".json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    report["report_path"] = str(path)
    return report
