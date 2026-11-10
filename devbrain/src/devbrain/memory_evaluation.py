"""Isolated positive extraction and portable-memory recall evaluation.

This deliberately does not reuse the pre-seeded golden recall cards: each fact
must first survive the production extractor before it can be transferred.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from . import __version__
from .agent import KnowledgeAgent, parse_object
from .arcade import ArcadeClient, ArcadeError
from .config import Settings
from .evaluation import MeasuredOllama, _fixture_settings, _git
from .ingest import index_pending_sources
from .ollama import Ollama, OllamaUnavailable
from .plans import PlanManager
from .retrieval import Retriever
from .store import Store


def _cases(marker: str) -> list[dict]:
    port = 20000 + int(marker[:6], 16) % 30000
    return [
        {"id": "fact", "kind": "fact", "marker": f"memory_fact_{marker}",
         "text": f"Durable project fact: memory_fact_{marker} uses TCP port {port}.",
         "expected": {"port": port}, "field_type": "integer"},
        {"id": "decision", "kind": "decision", "marker": f"memory_decision_{marker}",
         "text": f"Explicit chosen decision: memory_decision_{marker} uses ArcadeDB as its database engine.",
         "expected": {"engine": "ArcadeDB"}, "field_type": "string"},
        {"id": "task", "kind": "task", "marker": f"memory_task_{marker}",
         "text": f"Explicit project task: memory_task_{marker} must test memory portability. "
                 "Its current task state is needs_test.",
         "expected": {"state": "needs_test"}, "field_type": "string"},
    ]


def _answer_schema(case: dict) -> dict:
    # The caller supplies field names and types, never the expected values.
    return {"type": "object", "properties": {
        field: {"type": case["field_type"]} for field in case["expected"]},
        "required": list(case["expected"]), "additionalProperties": False}


def _expected_record(case: dict, row: dict, source_id: str, source_text: str) -> bool:
    metadata = row.get("metadata") or {}
    if (row.get("kind") != case["kind"] or row.get("status") != "proposed"
            or source_id not in row.get("source_ids", [])
            or metadata.get("origin") != "automatic_extraction"
            or not isinstance(metadata.get("evidence"), str)
            or not metadata["evidence"].strip() or metadata["evidence"] not in source_text):
        return False
    if case["kind"] == "task":
        return metadata.get("state") == case["expected"]["state"]
    return all(str(value).casefold() in row.get("content", "").casefold()
               for value in case["expected"].values())


def _project_rows(store: Store, project_id: str, table: str) -> list[dict]:
    return store.query(f"SELECT FROM {table} WHERE project_id=:project ORDER BY id", {"project": project_id})


def _semantic_rows(rows: list[dict]) -> list[dict]:
    # ArcadeDB record addresses are local implementation details.
    return [{key: value for key, value in row.items() if not key.startswith("@")} for row in rows]


def _revision_value(value):
    # Active-key indexes and physical addresses are regenerated at the target.
    if isinstance(value, dict):
        return {key: _revision_value(item) for key, item in value.items()
                if not key.startswith("@") and key != "active_key"}
    if isinstance(value, list):
        return [_revision_value(item) for item in value]
    return value


def _source_hashes(store: Store, project_id: str) -> dict:
    return {row["id"]: hashlib.sha256(row["content"].encode()).hexdigest()
            for row in _project_rows(store, project_id, "sources")}


async def _evaluate(stores: list[Store], roots: list[Path], settings: list[Settings],
                    measured: MeasuredOllama, report: dict) -> None:
    source_store, destination = stores
    source_root, destination_root, neighbor_root = roots
    project = source_store.add_project("Portable positive extraction fixture", source_root)
    neighbor = destination.add_project("Unrelated local fixture", neighbor_root)
    private_marker = "other-project-" + uuid4().hex
    private_source = destination.snapshot(neighbor["id"], "fixture://private", private_marker, "manual")
    destination.remember(neighbor["id"], {"kind": "fact", "content": private_marker,
                                        "status": "confirmed"}, [private_source["id"]])
    neighbor_before = {table: _semantic_rows(_project_rows(destination, neighbor["id"], table))
                       for table in ("sources", "records", "record_revisions", "nodes", "edges")}
    cases = _cases(uuid4().hex[:12])
    extractor = KnowledgeAgent(source_store, settings[0], ollama=measured)
    report["stage"] = "positive_extraction"
    for case in cases:
        source = source_store.snapshot(project["id"], "fixture://positive/" + case["marker"],
            case["text"], "manual", {"role": "user", "root": str(source_root), "worktree": str(source_root)})
        case["source_id"] = source["id"]
        job_id = source_store.enqueue("extract", project["id"], {"source_id": source["id"]},
                                      "positive-evaluation:" + source["id"])
        job = source_store.claim_job("memory-evaluation", 120, ["extract"])
        if not job or job["id"] != job_id:
            raise ValueError("Synthetic extraction job was not claimed")
        start = len(measured.calls)
        error = None
        try:
            await extractor.extract(job)
        except Exception as exc:
            error = measured._error(exc)
        source_store.finish_job(job_id, "Synthetic extraction failed" if error else None,
                                worker=job["worker"], expected_attempts=job["attempts"])
        records = source_store.list_records(project["id"])
        matches = [row for row in records if _expected_record(case, row, source["id"], case["text"])]
        diagnostic = source_store.query("SELECT diagnostics FROM jobs WHERE id=:id", {"id": job_id})[0]["diagnostics"]
        passed = error is None and bool(matches) and diagnostic.get("counts", {}).get("written_records", 0) > 0
        report["extraction"].append({"id": case["id"], "status": "passed" if passed else "failed",
            "source_id": source["id"], "matching_record_ids": [row["id"] for row in matches],
            "diagnostics": diagnostic, "model_calls": measured.calls[start:], **({"error": error} if error else {})})
    if not all(row["status"] == "passed" for row in report["extraction"]):
        report["status"] = "failed"
        report["failure_stage"] = "positive_extraction"
        return

    plan_manager = PlanManager(source_store)
    managed = plan_manager.save(project["id"],
        {"title": "Portable fixture plan", "goal": "Preserve shared project progress", "constraints": []},
        [{"key": "first", "content": "Preserve evidence identities"},
         {"key": "second", "content": "Review restored evidence", "depends_on": ["first"]}],
        idempotency_key="portable-evaluation-plan", worktree=source_root, activate=True)
    original_plan = managed["plan"]
    original_revisions = _revision_value(_project_rows(source_store, project["id"], "record_revisions"))
    original_plan_revisions = _revision_value(_project_rows(source_store, project["id"], "plan_revisions"))
    original_records = {row["id"]: row for row in source_store.list_records(project["id"], include_history=True)}
    hashes = _source_hashes(source_store, project["id"])
    original_graph = {table: _semantic_rows(_project_rows(source_store, project["id"], table))
                      for table in ("nodes", "edges")}
    from .memory import MemoryManager

    report["stage"] = "snapshot_export"
    exported = MemoryManager(source_store).snapshot(project["id"], worktree=source_root)
    report["stage"] = "snapshot_import"
    imported = MemoryManager(destination).import_snapshot(Path(exported["path"]), destination_root)
    report["stage"] = "portable_integrity"
    imported_project = imported["project"]
    imported_records = {row["id"]: row for row in destination.list_records(imported_project["id"], include_history=True)}
    graph = {table: _semantic_rows(_project_rows(destination, imported_project["id"], table))
             for table in ("nodes", "edges")}
    checks = report["deterministic_checks"]
    checks.update({
        "separate_databases": settings[0].arcade_database != settings[1].arcade_database,
        "different_roots": source_root != destination_root,
        "project_identity": imported_project["id"] == project["id"],
        "record_identity_versions": set(imported_records) == set(original_records) and all(
            imported_records[key]["version"] == original["version"]
            and imported_records[key]["content"] == original["content"]
            and imported_records[key]["status"] == original["status"]
            and imported_records[key]["source_ids"] == original["source_ids"]
            for key, original in original_records.items()),
        "source_hashes": _source_hashes(destination, imported_project["id"]) == hashes,
        "task_states": all(imported_records[key]["metadata"].get("state") == original["metadata"].get("state")
                           for key, original in original_records.items() if original["kind"] == "task"),
        "graph_identity": all({tuple(row.get(key) for key in ("id", "kind", "source_id", "from_id", "to_id"))
                               for row in graph[table]} == {
                                   tuple(row.get(key) for key in ("id", "kind", "source_id", "from_id", "to_id"))
                                   for row in original_graph[table]} for table in original_graph),
        "record_revisions": _revision_value(_project_rows(destination, imported_project["id"], "record_revisions"))
                            == original_revisions,
        "plan_revisions": _revision_value(_project_rows(destination, imported_project["id"], "plan_revisions"))
                          == original_plan_revisions,
        "plan_identity_version": any(row["id"] == original_plan["id"] and row["version"] == original_plan["version"]
                                     and row["step_ids"] == original_plan["step_ids"] for row in
                                     _project_rows(destination, imported_project["id"], "plans")),
        "active_plan": imported.get("active_plan_id") == original_plan["id"],
        "neighbor_unchanged": neighbor_before == {
            table: _semantic_rows(_project_rows(destination, neighbor["id"], table)) for table in neighbor_before},
        "no_imported_sessions": not _project_rows(destination, imported_project["id"], "sessions"),
    })
    report["snapshot"] = {"snapshot_id": exported["snapshot_id"], "import_status": imported["status"],
                          "project_id": imported_project["id"]}
    report["stage"] = "recall"
    index_pending_sources(destination, limit=1000)
    retriever = Retriever(destination, settings[1], ollama=measured)
    for chunk in destination.query("SELECT id FROM chunks WHERE project_id=:project", {"project": imported_project["id"]}):
        await retriever.embed_pending({"project_id": imported_project["id"], "payload": {"chunk_id": chunk["id"]}})
    # This agent has no messages, model state or retriever cache from extraction.
    recall = KnowledgeAgent(destination, settings[1], ollama=measured,
                            retriever=Retriever(destination, settings[1], ollama=measured))
    for case in cases:
        question = (f"For {case['marker']}, retrieve the exact {', '.join(case['expected'])} from this project's memory. "
                    "Return the caller-specified answer object and cite its literal source.")
        start = len(measured.calls)
        answer = await recall.ask(imported_project["id"], question, worktree=destination_root,
                                  answer_schema=_answer_schema(case))
        try:
            actual = parse_object(answer.get("answer", ""))
        except (ValueError, TypeError):
            actual = None
        cited = {row.get("id") for row in answer.get("sources", []) if isinstance(row, dict)}
        passed = (answer.get("status") == "complete" and actual == case["expected"]
                  and case["source_id"] in cited and private_marker not in json.dumps(answer))
        report["recall"].append({"id": case["id"], "status": "passed" if passed else "failed",
            "expected": case["expected"], "answer": answer, "answer_schema": _answer_schema(case),
            "model_calls": measured.calls[start:]})
    start = len(measured.calls)
    unknown = await recall.ask(imported_project["id"],
        "What secret marker belongs to the OTHER unrelated project? If absent in this project, "
        "report that it is unknown and explain the missing evidence.", worktree=destination_root,
        answer_schema={"type": "object", "properties": {"answer": {"type": "string"}},
                       "required": ["answer"], "additionalProperties": False})
    try:
        unknown_value = parse_object(unknown.get("answer", ""))
    except (ValueError, TypeError):
        unknown_value = None
    unknown_citations = {row.get("id") for row in unknown.get("sources", []) if isinstance(row, dict)}
    checks["cross_project_abstention"] = (
        unknown_value == {"answer": "unknown"} and bool(unknown.get("unknowns"))
        and unknown.get("status") == "partial"
        and unknown.get("diagnostics", {}).get("code") == "insufficient_evidence"
        and bool(unknown_citations) and unknown_citations <= set(hashes)
        and private_marker not in json.dumps(unknown))
    report["isolation_answer"] = {"answer": unknown, "model_calls": measured.calls[start:]}
    report["status"] = ("passed" if all(checks.values()) and all(row["status"] == "passed" for row in report["recall"])
                        else "failed")
    if report["status"] == "failed":
        report["failure_stage"] = ("portable_integrity" if any(not value for key, value in checks.items()
                                                              if key != "cross_project_abstention")
                                   else "recall" if any(row["status"] != "passed" for row in report["recall"])
                                   else "cross_project_abstention")
    report["stage"] = "complete"


def run_memory_evaluation(settings: Settings, *, report_dir: Path | None = None, ollama=None) -> dict:
    """Run explicitly requested local-model inference in two disposable databases."""
    started = time.monotonic()
    measured = MeasuredOllama(ollama or Ollama(settings))
    report = {"format_version": 1, "devbrain_version": __version__, "evaluation_mode": "portable_positive_extraction",
        "inference": "injected_adapter" if ollama is not None else "local_ollama", "status": "not_run",
        "model": settings.chat_model, "embedding_model": settings.embedding_model,
        "model_identities": {}, "extraction": [], "recall": [], "deterministic_checks": {},
        "isolation": {"separate_databases": True, "different_roots": True, "user_projects_modified": False,
                      "native_clients_started": False},
        "limits": ["Three synthetic explicit facts are not proof of universal extraction or recall.",
                   "Extracted records remain proposals; importing a snapshot grants no native permissions."]}
    owned_settings = []
    preflight_complete = False

    async def preflight():
        for field, model in (("chat", settings.chat_model), ("embedding", settings.embedding_model)):
            report["model_identities"][field] = await measured.model_identity(model)

    try:
        asyncio.run(preflight())
        preflight_complete = True
        with tempfile.TemporaryDirectory(prefix="devbrain-memory-eval-") as name:
            temporary = Path(name)
            roots = [temporary / "computer-a" / "project", temporary / "computer-b" / "project",
                     temporary / "computer-b" / "neighbor"]
            for root in roots:
                root.mkdir(parents=True)
            for root in roots[:2]:
                _git(root, "init", "--quiet")
                (root / "README.md").write_text("Portable memory synthetic fixture.\n")
                _git(root, "add", "README.md")
                _git(root, "-c", "user.name=Cairnmind Evaluation", "-c", "user.email=eval@localhost",
                     "commit", "--quiet", "-m", "Portable memory fixture")
            stores = []
            for index in range(2):
                isolated = _fixture_settings(settings, temporary / f"data-{index}")
                owned_settings.append(isolated)
                stores.append(Store(isolated.data_dir, settings=isolated))
            asyncio.run(_evaluate(stores, roots, owned_settings, measured, report))
    except OllamaUnavailable as exc:
        # An absent runtime is not evidence that extraction or transfer passed.
        report.update(status="failed" if preflight_complete else "not_run",
                      failure_stage=report.get("stage", "model_availability"), error=measured._error(exc))
    except Exception as exc:
        report.update(status="failed", failure_stage=report.get("failure_stage", report.get("stage", "evaluation")),
                      error=measured._error(exc))
    finally:
        cleanup_errors = []
        for isolated in owned_settings:
            try:
                ArcadeClient(isolated).server_command("drop database " + isolated.arcade_database)
            except ArcadeError as exc:
                cleanup_errors.append(measured._error(exc))
        report["isolation"]["databases_cleaned"] = not cleanup_errors
        if cleanup_errors:
            report.update(status="failed", cleanup_errors=cleanup_errors)
    report["elapsed_seconds"] = round(time.monotonic() - started, 4)
    output = Path(report_dir or settings.data_dir / "evaluations").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = output / ("memory-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8] + ".json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    report["report_path"] = str(path)
    return report
