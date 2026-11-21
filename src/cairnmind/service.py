"""Authenticated local knowledge service and single-owner inference scheduler."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from cairnmind import __version__
from cairnmind.config import BACKUP_SUFFIXES, SERVICE_NAMES, Settings
from cairnmind.store_backup import stale_temporaries

log = logging.getLogger(__name__)
# How often background work that lost the writer role looks again whether a person took it back (#70).
WRITER_LOST_RETRY_SECONDS = 30


class InferenceQueue:
    """Serialize GPU users across all MCP clients; queued foreground work goes first."""

    def __init__(self) -> None:
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.sequence = itertools.count()

    async def submit(self, operation: Callable[[], Awaitable[Any]], priority: int = 0) -> Any:
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((priority, next(self.sequence), operation, future))
        return await future

    async def run(self) -> None:
        while True:
            _, _, operation, future = await self.queue.get()
            operation_task = None
            try:
                if future.cancelled():
                    continue
                operation_task = asyncio.create_task(operation())
                def cancel_abandoned(f, task=operation_task):
                    if f.cancelled():
                        task.cancel()
                future.add_done_callback(cancel_abandoned)
                result = await operation_task
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    if not future.done():
                        future.cancel()
                    raise
                if future.cancelled():
                    continue
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.queue.task_done()


class AskBody(BaseModel):
    project: str
    question: str = Field(min_length=1, max_length=16000)
    worktree: str | None = None
    session_id: str | None = None
    answer_schema: dict | None = None
    # Keep this answer in the project's answer log (#15); None follows the answer_log setting.
    log: bool | None = None


class RememberBody(BaseModel):
    project: str
    record: dict
    sources: list[str] = Field(default_factory=list)
    session_id: str | None = None
    packet_id: str | None = None


class FindingBody(BaseModel):
    project: str
    content: str
    kind: str
    key: str
    status: str = "proposed"
    worktree: str | None = None
    session_id: str | None = None
    packet_id: str | None = None


class CheckpointBody(BaseModel):
    project: str
    state: dict
    session_id: str | None = None
    packet_id: str | None = None


class RegisterProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    root: str = Field(min_length=1, max_length=4096)


class PlanSaveBody(BaseModel):
    project: str
    plan: dict
    steps: list[dict]
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_version: int | None = None
    task_versions: dict[str, int] | None = None
    worktree: str | None = None
    activate: bool = False
    session_id: str | None = None
    packet_id: str | None = None


class PlanNextBody(BaseModel):
    project: str
    plan_id: str | None = None
    worktree: str | None = None
    session_id: str | None = None


class PlanActivateBody(BaseModel):
    project: str
    plan_id: str
    worktree: str | None = None
    session_id: str | None = None
    packet_id: str | None = None


class SessionPrepareBody(BaseModel):
    project: str
    client: str
    worktree: str | None = None
    session_id: str | None = None
    reason: str = "startup"


class SessionContextBody(BaseModel):
    session_id: str
    packet_id: str | None = None
    # Compatibility with startup hooks from 0.3.4 and earlier. Both spellings
    # request an exact read; they must never silently initiate a refresh.
    expected_packet_id: str | None = None


class SessionAckBody(BaseModel):
    session_id: str
    packet_id: str
    packet_hash: str
    readback: dict


class AgentArchiveBody(BaseModel):
    model_config = {"extra": "forbid"}
    session_id: str = Field(min_length=1, max_length=200)


class JobRetryBody(BaseModel):
    project: str
    expected_attempts: int = Field(ge=0, strict=True)
    include_omissions: bool = Field(default=False, strict=True)
    session_id: str | None = None
    packet_id: str | None = None


class ProjectBody(BaseModel):
    project: str


class ImportBody(BaseModel):
    path: str
    project: str | None = None
    client: str = "auto"


class MemoryScopeBody(BaseModel):
    project: str
    worktree: str | None = None
    session_id: str | None = None


class MemorySnapshotBody(MemoryScopeBody):
    packet_id: str | None = None


class BackgroundInferenceBody(MemorySnapshotBody):
    model_config = {"extra": "forbid"}


class MemoryInspectBody(BaseModel):
    path: str = Field(min_length=1)
    session_id: str | None = None


class MemoryImportBody(MemoryInspectBody):
    root: str = Field(min_length=1)
    name: str | None = None
    dry_run: bool = Field(default=False, strict=True)
    replace_confirmed: bool = Field(default=False, strict=True)
    packet_id: str | None = None


class MemoryPublishBody(MemorySnapshotBody):
    model_config = {"extra": "forbid"}
    preview: bool = Field(default=False, strict=True)
    new_package: bool = Field(default=False, strict=True)


class MemoryImportCommitBody(BaseModel):
    model_config = {"extra": "forbid"}
    revision: str = Field(min_length=1, max_length=256)
    project: str = Field(min_length=1)
    worktree: str | None = Field(default=None, max_length=4096)
    dry_run: bool = Field(default=False, strict=True)
    bind: bool = Field(default=False, strict=True)
    replace_confirmed: bool = Field(default=False, strict=True)
    session_id: str | None = None
    packet_id: str | None = None


class MemoryImportPublishedBody(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(min_length=1, max_length=4096)
    project: str = Field(min_length=1)
    dry_run: bool = Field(default=False, strict=True)
    bind: bool = Field(default=False, strict=True)
    take_published: list[str] = Field(default_factory=list, max_length=1000)
    replace_confirmed: bool = Field(default=False, strict=True)
    session_id: str | None = None
    packet_id: str | None = None


class MemoryCarrierBody(BaseModel):
    model_config = {"extra": "forbid"}
    project: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=32)
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    worktree: str | None = None
    session_id: str | None = None
    packet_id: str | None = None


class MemorySettingsBody(MemorySnapshotBody):
    enabled: bool = Field(strict=True)
    interval_minutes: int = Field(default=15, ge=1, le=10080, strict=True)


def compact_job_history(store, settings) -> dict | None:
    """The daily compaction of old completed jobs (store_prune.compact_old_jobs); None when the setting turns it off."""
    from cairnmind.store_prune import compact_old_jobs, valid_days
    days = valid_days(settings.job_history_days)
    if days is None:
        return None
    report = compact_old_jobs(store, days)
    if report["compacted"]:
        log.info("Removed the attempt history of %d completed jobs older than %d days", report["compacted"], days)
    return report


def daily_backups(backup_dir) -> list[Path]:
    """The scheduled backups of a data directory, oldest first, under the current and the earlier suffix.

    Only files named by the time they were written are scheduled backups. The safety copies made
    before a restore or a schema change, and backups a user named, are never pruned with them.
    The order follows the time in the name, which a copy or a restore of the directory keeps.
    """
    found = []
    for suffix in BACKUP_SUFFIXES:
        for path in Path(backup_dir).glob(f"[0-9]*{suffix}") if Path(backup_dir).is_dir() else ():
            written = path.name[:-len(suffix)]
            if written.isdigit():
                found.append((int(written), path.name, path))
    return [path for _, _, path in sorted(found)]


def instance_id(settings: Settings) -> str:
    return hashlib.sha256(str(settings.data_dir).encode()).hexdigest()[:16]


def schedule_embeddings(store, namespace: str, *, project_ids=None, settings=None, report=None,
                        known_held=None) -> int:
    """Repair missing/digest-changed derived vectors without touching archived evidence.

    With ``settings``, chunks a remote embedding server may not receive are not queued (#181); their
    number goes to ``report["embedding_held_back"]``. They are queued by the first repair that allows them.
    ``known_held`` keeps the held chunks between repairs (memory_egress.held_chunk_ids).
    """
    rows = (store.missing_embeddings(namespace) if project_ids is None else
            [row for pid in project_ids for row in store.missing_embeddings(namespace, pid)])
    if settings is not None:
        from cairnmind.memory_egress import held_chunk_ids
        held = held_chunk_ids(store, settings, rows, known_held)
        rows = [row for row in rows if row["id"] not in held]
        if report is not None:
            report["embedding_held_back"] = len(held)
    for row in rows:
        job_id = store.enqueue("embed", row["project_id"], {"chunk_id": row["id"]},
                               f"embedding-namespace:{namespace}:{row['id']}")
        store.reset_missing_embedding_job(job_id, row["id"], namespace)
    return len(rows)


def finish_job_if_present(store, job_id: str, error: str | None = None, *, worker=None, expected_attempts=None) -> None:
    """Project deletion cancels in-flight work instead of creating a model failure."""
    try:
        store.finish_job(job_id, error, worker=worker, expected_attempts=expected_attempts)
    except ValueError:
        rows = store.query("SELECT state,worker,attempts FROM jobs WHERE id=:id", {"id": job_id})
        if not rows:
            return
        current = rows[0]
        if ((expected_attempts is not None and (current.get("attempts", 0) != expected_attempts
                                               or current["state"] != "running"))
                or (worker is not None and (current.get("worker") != worker or current["state"] != "running"))):
            return  # A superseded lease cannot complete or fail its replacement.
        raise


def create_app(settings: Settings | None = None, *, start_worker: bool = True,
               background_inference: bool = True, ollama=None) -> FastAPI:
    from cairnmind.agent import KnowledgeAgent, extraction_outer_timeout, inference_lease_seconds
    from cairnmind.memory_models import from_settings, ready_kinds
    from cairnmind.ollama import OllamaUnavailable
    from cairnmind.retrieval import Retriever
    from cairnmind.store import Store
    from cairnmind.plans import PlanManager
    from cairnmind.sessions import SessionManager, origin_mark
    from cairnmind.memory import MemoryConflict, MemoryManager, inspect_snapshot
    from cairnmind.store_writer import StoreNotWriter

    settings = settings or Settings.load(create=True)
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Cairnmind must bind to a loopback address")
    if not settings.token:
        raise ValueError("Run cairnmind init to create a local authentication token")
    from cairnmind import knowledge_mode
    models_on = knowledge_mode.enabled(settings)  # read once, here; an invalid value stops the start (#274)
    store = Store(settings.data_dir)
    # A caller sends a path as an absolute one; a relative one would be read from the service's directory (#238).
    store.relative_paths = False
    if store.not_writer:
        # The service writes sessions, jobs and the queue: it runs only for the store's writer (#70).
        raise StoreNotWriter(store.not_writer)
    model = ollama or from_settings(settings)
    from cairnmind import memory_fingerprint
    fingerprint_client = memory_fingerprint.client_of(model)
    if fingerprint_client is not None:
        # An OpenAI-compatible embedding role: no vector is made or searched before the start check (#212).
        fingerprint_client.fingerprint_checked = False
    retriever = Retriever(store, settings, ollama=model)
    agent = KnowledgeAgent(store, settings, ollama=model, retriever=retriever)
    plans = PlanManager(store)
    sessions = SessionManager(store, settings)
    memory = MemoryManager(store)
    inference = InferenceQueue()
    background_status: dict = {"scanner_error": None, "model_error": None, "memory_error": None}

    async def writer_lost(exc) -> None:
        # Another installation took over writing: the background work pauses instead of failing on every turn.
        # Each retry costs one read of the writer row (store_writer.recheck); when a person takes writing back
        # here, the next pass writes again, so the service needs no restart.
        if background_status.get("writer_lost") != str(exc):
            log.warning("Background work paused: %s", exc)
        background_status["writer_lost"] = str(exc)
        await asyncio.sleep(WRITER_LOST_RETRY_SECONDS)

    def writer_back() -> None:
        if background_status.pop("writer_lost", None):
            log.warning("Background work resumed: this installation writes the store again")
    cache: dict = {}
    inference_projects: set[str] = set()
    background_ready = asyncio.Event()
    if background_inference:
        background_ready.set()

    def inference_status():
        running = start_worker and models_on  # without models nothing processes memory, so none is claimed
        return {"worker_running": running,
                "mode": ("disabled" if not running else "all_projects" if background_inference
                         else "selected_projects" if inference_projects else "deferred"),
                "project_ids": sorted(inference_projects)}

    def enable_background(pid):
        # Called only on the event-loop thread, after scoped authorization succeeds.
        inference_projects.add(pid)
        background_ready.set()
        return {"accepted": True, "project_id": pid, **inference_status()}

    async def memory_snapshots() -> None:
        # Filesystem snapshots have their own clock and never enter the GPU queue.
        # tick() coalesces missed intervals and preserves each last good manifest.
        while True:
            try:
                result = await asyncio.to_thread(memory.tick)
                writer_back()
                background_status["memory_error"] = result.get("errors") or None
            except asyncio.CancelledError:
                raise
            except StoreNotWriter as exc:
                await writer_lost(exc)
                continue
            except Exception as exc:
                background_status["memory_error"] = {"code": type(exc).__name__}
                log.exception("Portable memory scheduler failed")
            await asyncio.sleep(min(60, max(1, settings.poll_seconds)))

    async def scanner() -> None:
        from pathlib import Path
        from cairnmind.ingest import (forget_missing_projects, import_history, index_pending_sources,
                                      index_project_if_changed, poll_imports)

        while True:
            try:
                from cairnmind.integrations import drain_hook_spool
                scan_errors = []
                drained = await asyncio.to_thread(drain_hook_spool, settings, store)
                scan_errors.extend(f"hook: {err}" for err in drained.get("errors", []))
                # CPU/archive jobs never wait for the model, including end-hook spools.
                for _ in range(20):
                    job = await asyncio.to_thread(store.claim_job, "archive", 120, ["import"])
                    if not job:
                        break
                    try:
                        payload = job["payload"]
                        if isinstance(payload, str):
                            import json
                            payload = json.loads(payload)
                        imported = await asyncio.to_thread(import_history, store, Path(payload["path"]),
                                                          job["project_id"], payload.get("client", "auto"))
                        errors = imported.get("errors", [])
                        scan_errors.extend(f"import: {err}" for err in errors)
                        await asyncio.to_thread(finish_job_if_present, store, job["id"],
                                                "; ".join(map(str, errors))[:2000] if errors else None,
                                                worker=job["worker"], expected_attempts=job["attempts"])
                    except Exception as exc:
                        await asyncio.to_thread(finish_job_if_present, store, job["id"], str(exc),
                                                worker=job["worker"], expected_attempts=job["attempts"])
                polled = await asyncio.to_thread(poll_imports, store)
                scan_errors.extend(f"history: {err}" for err in polled.get("errors", []))
                projects = await asyncio.to_thread(store.list_projects)
                forget_missing_projects(store, [project["id"] for project in projects])
                for project in projects:
                    indexed = await asyncio.to_thread(index_project_if_changed, store, project["id"])
                    scan_errors.extend(f"{project['name']}: {err}" for err in indexed.get("errors", []))
                pending = await asyncio.to_thread(index_pending_sources, store)
                scan_errors.extend(f"memory index: {err}" for err in pending.get("errors", []))
                from cairnmind.verification import recover_interrupted_runs
                await asyncio.to_thread(recover_interrupted_runs, store, settings)
                backup_dir = settings.data_dir / "backups"
                # A backup killed midway leaves its temporary file; one a day old is no longer being written.
                for stale in stale_temporaries(backup_dir, 86400):
                    stale.unlink(missing_ok=True)
                backups = daily_backups(backup_dir)
                if not backups or time.time() - backups[-1].stat().st_mtime > 86400:
                    backup_dir.mkdir(exist_ok=True)
                    await asyncio.to_thread(store.backup, backup_dir / f"{time.time_ns()}{BACKUP_SUFFIXES[0]}")
                    for old in daily_backups(backup_dir)[:-7]:
                        old.unlink()
                    # Once a day, right after the backup that still holds it: the attempt history of old
                    # completed jobs goes; every job row stays (#190, store_prune).
                    await asyncio.to_thread(compact_job_history, store, settings)
                background_status["scanner_error"] = "; ".join(scan_errors[:5]) or None
                background_status["scan_errors"] = scan_errors[:20]
                background_status["last_scan_at"] = time.time()
                writer_back()
            except asyncio.CancelledError:
                raise
            except StoreNotWriter as exc:
                await writer_lost(exc)
                continue
            except Exception as exc:
                background_status["scanner_error"] = str(exc)
                log.exception("Archive scan failed")
            await asyncio.sleep(settings.poll_seconds)

    async def fingerprint_start() -> None:
        """The fixed request to an OpenAI-compatible embedding role at start (#212), retried while it is down.

        It holds no memory content, so it needs no consent beyond the role's own: search stays text-only
        and embed jobs wait until it answers. Unlike other model calls it does not wait for background work
        to be allowed: in the deferred mode (the cockpit) search would otherwise stay text-only until a
        project allows it. Nor does it enter the inference queue: a server that hangs on it would hold every
        extraction and question behind it, often on another server, at every retry.
        """
        while True:
            try:
                result = await memory_fingerprint.check(fingerprint_client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a server that is down is checked again later
                background_status["embedding_fingerprint"] = {"status": "unchecked", "error": type(exc).__name__}
                await asyncio.sleep(30)
                continue
            background_status["embedding_fingerprint"] = result
            if result["status"] == "changed":
                log.warning("The embedding model answers the fixed check differently: run cairnmind rebuild-vectors")
            return

    async def jobs() -> None:
        last_namespace, last_repair, last_projects = None, 0.0, None
        released, known_held = False, {}
        while True:
            try:
                # Opening a UI or acknowledging context does not authorize model work.
                await background_ready.wait()
                if not released:
                    # Extractions held back for a remote chat server whose class is allowed now (#181).
                    from cairnmind.memory_egress import release_held_extractions
                    await asyncio.to_thread(release_held_extractions, store, settings)
                    released = True
                selected = None if background_inference else tuple(sorted(inference_projects))
                # Keep pending work pending when Ollama is offline, instead of failing the whole queue.
                # With split model servers, each job kind waits only for the server of its own role.
                kinds, role_error = await ready_kinds(model)
                identity = getattr(model, "model_identity", None)
                namespace = None
                if identity and "embed" in kinds:
                    try:
                        namespace = await identity(settings.embedding_model)
                    except OllamaUnavailable as exc:
                        if exc.error_code not in (memory_fingerprint.CHANGED, memory_fingerprint.UNCHECKED):
                            raise
                        # Other weights behind the same name, or not checked yet: embed jobs wait (#212).
                        kinds = [kind for kind in kinds if kind != "embed"]
                        role_error = role_error or exc
                if namespace is not None:
                    if (namespace != last_namespace or selected != last_projects
                            or time.monotonic() - last_repair > 60):
                        held: dict = {}
                        missing = await asyncio.to_thread(schedule_embeddings, store, namespace, project_ids=selected,
                                                          settings=settings, report=held, known_held=known_held)
                        background_status.update(embedding_namespace=namespace, embedding_missing=missing, **held)
                        last_namespace, last_repair, last_projects = namespace, time.monotonic(), selected
                background_status["model_error"] = str(role_error) if role_error else None
                job = await asyncio.to_thread(store.claim_job, "inference", inference_lease_seconds(), kinds,
                                              project_ids=selected)
                writer_back()
                if job:
                    try:
                        async def process():
                            if job["kind"] == "embed":
                                return await retriever.embed_pending(job)
                            return await agent.extract(job)
                        timeout = 30 if job["kind"] == "embed" else extraction_outer_timeout()
                        await inference.submit(lambda: asyncio.wait_for(process(), timeout), priority=10)
                        await asyncio.to_thread(finish_job_if_present, store, job["id"],
                                                worker=job["worker"], expected_attempts=job["attempts"])
                    except Exception as exc:
                        await asyncio.to_thread(finish_job_if_present, store, job["id"], str(exc) or type(exc).__name__,
                                                worker=job["worker"], expected_attempts=job["attempts"])
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except StoreNotWriter as exc:
                await writer_lost(exc)
                continue
            except Exception as exc:
                background_status["model_error"] = str(exc)
                await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(inference.run())]
        if fingerprint_client is not None and models_on:
            tasks.append(asyncio.create_task(fingerprint_start()))
        if start_worker:
            tasks += [asyncio.create_task(scanner()), asyncio.create_task(memory_snapshots())]
            if models_on:  # held: without models the embed and extract jobs stay pending, asked of nobody (#274)
                tasks.append(asyncio.create_task(jobs()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Cairnmind local knowledge agent", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.agent = agent
    app.state.plans = plans
    app.state.sessions = sessions
    app.state.inference = inference
    app.state.memory = memory

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Invalid local service token")

    auth = [Depends(authorize)]

    def project(identifier: str) -> dict:
        try:
            return store.resolve_project(identifier)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def write_scoped(pid: str, session_id: str | None, packet_id: str | None, operation):
        """Session validation and the resulting mutation share one writer transaction.

        The worktree's code state is hashed before that transaction begins: inside it, a slow worktree let
        ArcadeDB expire the transaction (#255).
        """
        code = None
        if session_id:
            from cairnmind.integrations import drain_hook_spool
            drain_hook_spool(settings, store)
            code = sessions.code_scope(session_id, packet_id)
        with store.transaction():
            scope, mark = None, None
            if session_id:
                sessions.require_ack(session_id, packet_id, code)
                row = store.query("SELECT id,project_id,worktree,client FROM sessions WHERE id=:id", {"id": session_id})
                if not row or row[0]["project_id"] != pid:
                    raise ValueError("resume_required: session belongs to another project")
                scope = row[0]["worktree"]
                # Derived from the session row, never from the payload: an external session's writes are
                # stamped as such, and nothing a client sends can add, change or remove that stamp.
                mark = origin_mark(row[0])
                if mark is None and row[0].get("client") in {"api", "copilot"}:
                    # An own-agent session that received a third-party MCP result writes as unverified (#126).
                    from cairnmind.agent_memory import third_party_origin
                    mark = third_party_origin(store, pid, session_id)
            # A write that names a session is never a person's act (#177), whatever the session's client.
            with store.write_origin(mark), store.session_write(session_id):
                return operation(scope)

    def read_scope(pid: str, requested: str | None, session_id: str | None = None) -> str | None:
        scope = str(Path(requested).expanduser().resolve()) if requested else None
        if session_id:
            rows = store.query("SELECT project_id,worktree FROM sessions WHERE id=:id", {"id": session_id})
            if not rows or rows[0]["project_id"] != pid:
                raise ValueError("Session read belongs to another project")
            if scope and scope != rows[0]["worktree"]:
                raise ValueError("Requested read worktree differs from session scope")
            scope = rows[0]["worktree"]
        return scope

    @app.exception_handler(ValueError)
    async def bad_input(request: Request, exc: ValueError):
        from fastapi.responses import JSONResponse
        from cairnmind.confirmation import ConfirmationRefused
        status = 409 if (isinstance(exc, MemoryConflict) or any(
            word in str(exc).lower() for word in ("version", "conflict", "stale", "resume_required"))) else 400
        if isinstance(exc, ConfirmationRefused):
            # Not something to read again and retry (#177), whatever words the refusal uses ("conflict inbox").
            # Marked, so that the built-in agent hands the model this reason and what to do instead (#240).
            return JSONResponse(status_code=400, content={"detail": str(exc), "refusal": "confirmation"})
        from cairnmind.store import RecordRefused
        if isinstance(exc, RecordRefused):
            # The store's fixed sentence about the record's shape, marked like record_error's (#240).
            return JSONResponse(status_code=400, content={"detail": str(exc), "refusal": "record"})
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    from cairnmind.arcade import ArcadeError

    @app.exception_handler(ArcadeError)
    async def backend_unavailable(request: Request, exc: ArcadeError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": str(exc), "backend": "arcadedb"})

    @app.get("/health")
    async def health():
        return {"service": SERVICE_NAMES[0], "version": __version__, "instance": instance_id(settings),
                "backend": "arcadedb"}

    @app.get("/projects", dependencies=auth)
    async def projects():
        return await asyncio.to_thread(store.list_projects)

    @app.post("/projects", dependencies=auth)
    async def register_project(body: RegisterProjectBody):
        try:
            root = Path(body.root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("Project root is unavailable") from exc
        if not root.is_dir():
            raise ValueError("Project root must be an existing directory")
        created = await asyncio.to_thread(store.add_project, body.name, root)
        cache.clear()
        return {"project": created}

    @app.get("/project-roots", dependencies=auth)
    async def project_roots(project: str):
        pid = store.resolve_project(project)["id"]
        rows = await asyncio.to_thread(store.query,
            "SELECT path FROM project_roots WHERE project_id=:project ORDER BY path", {"project": pid})
        return {"roots": [row["path"] for row in rows]}

    @app.get("/status", dependencies=auth)
    async def status(project: str | None = None):
        pid = store.resolve_project(project)["id"] if project else None
        result = await asyncio.to_thread(store.status, pid)
        error_rows = await asyncio.to_thread(store.recent_job_errors, pid)
        pipeline = await asyncio.to_thread(store.job_summary, pid)
        writer = await asyncio.to_thread(store.writer_state)
        from cairnmind.database_mode import address, describe
        report = {**result, "writer": writer, "storage": {**describe(settings), "url": address(settings)},
                  "background": {**background_status, "inference": inference_status()}, "pipeline_jobs": pipeline,
                  "inference_queue": inference.queue.qsize(), "cached_answers": len(cache),
                  "recent_job_errors": error_rows}
        if not models_on:
            report.update(knowledge_mode.status_fields(pipeline, settings))
        return report

    def selected_memory_scope(pid: str, requested: str | None, scope: str | None):
        if scope and requested and str(Path(requested).expanduser().resolve()) != scope:
            raise ValueError("resume_required: memory worktree does not match the session")
        return scope or requested

    @app.post("/background-inference/enable", dependencies=auth)
    async def enable_background_inference(body: BackgroundInferenceBody):
        selected = await asyncio.to_thread(project, body.project)
        pid = selected["id"]

        def authorize_enable(scope):
            requested = selected_memory_scope(pid, body.worktree, scope) or selected["root"]
            root = str(Path(requested).expanduser().resolve())
            if not store.query("SELECT path FROM project_roots WHERE project_id=:project AND path=:root",
                               {"project": pid, "root": root}):
                raise ValueError("Background processing requires a registered project worktree")

        await asyncio.to_thread(write_scoped, pid, body.session_id, body.packet_id, authorize_enable)
        return enable_background(pid)

    def person_imports(body):
        """A memory import writes records that count as confirmed and can move on what a person confirmed
        here, so only a person runs it (#177): never a request that names a session. A dry run writes
        nothing and stays open to a session."""
        if body.session_id and not body.dry_run:
            from cairnmind.confirmation import IMPORT_IN_SESSION
            raise ValueError(IMPORT_IN_SESSION)

    def memory_write_authorization(pid: str, requested: str | None,
                                   session_id: str | None, packet_id: str | None):
        """Keep file-lock -> DB-lock ordering, rechecking the actor in core's transaction.

        The worktree is hashed outside any transaction (#255): here for the first check, and again by
        ``require.refresh()``, which core calls right before it opens the transaction it calls ``require`` in.
        So the recheck compares the code as it was then, not as it was before core's own file work.
        """
        if not session_id:
            return requested, None
        from cairnmind.integrations import drain_hook_spool
        drain_hook_spool(settings, store)
        code = sessions.code_scope(session_id, packet_id)

        def require():
            sessions.require_ack(session_id, packet_id, code)
            rows = store.query("SELECT project_id,worktree FROM sessions WHERE id=:id", {"id": session_id})
            if not rows or rows[0]["project_id"] != pid:
                raise ValueError("resume_required: memory belongs to another project")
            return selected_memory_scope(pid, requested, rows[0]["worktree"])

        def refresh():
            nonlocal code
            if getattr(store._local, "transaction", None) is None:  # inside one, keep the earlier capture
                code = sessions.code_scope(session_id, packet_id)

        require.refresh = refresh
        # Reject an already-unacknowledged actor before creating filesystem output.
        # Core invokes require again while capturing/importing in its transaction.
        return require(), require

    @app.post("/memory/preview", dependencies=auth)
    async def memory_preview(body: MemoryScopeBody):
        pid = project(body.project)["id"]
        scope = await asyncio.to_thread(read_scope, pid, body.worktree, body.session_id)
        return await asyncio.to_thread(memory.preview, pid, scope)

    @app.post("/memory/snapshot", dependencies=auth)
    async def memory_snapshot(body: MemorySnapshotBody):
        pid = project(body.project)["id"]
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, pid, body.worktree, body.session_id, body.packet_id)
        return await asyncio.to_thread(memory.snapshot, pid, scope, authorize=authorize)

    @app.post("/memory/inspect", dependencies=auth)
    async def memory_inspect(body: MemoryInspectBody):
        result = await asyncio.to_thread(inspect_snapshot, body.path)
        if body.session_id:
            await asyncio.to_thread(read_scope, result["project"]["id"], None, body.session_id)
        return result

    @app.post("/memory/import", dependencies=auth)
    async def memory_import(body: MemoryImportBody):
        # Bind authorization to the exact fully validated package. The importer
        # rechecks this identity, so swapping a manifest cannot change scope.
        inspected = await asyncio.to_thread(inspect_snapshot, body.path)
        pid = inspected["project"]["id"]
        def restore(scope, authorize=None):
            selected_memory_scope(pid, body.root, scope)
            person_imports(body)  # After the scope checks: a session learns of another project's memory as before.
            return memory.import_snapshot(
                body.path, body.root, body.name, body.dry_run,
                expected_snapshot_id=inspected["snapshot_id"], expected_project_id=pid, authorize=authorize,
                replace_confirmed=body.replace_confirmed)
        if body.dry_run:
            scope = await asyncio.to_thread(read_scope, pid, body.root, body.session_id)
            return await asyncio.to_thread(restore, scope)
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, pid, body.root, body.session_id, body.packet_id)
        result = await asyncio.to_thread(restore, scope, authorize)
        cache.clear()
        from cairnmind.ingest import forget_index_signature
        forget_index_signature(store, pid)
        return result

    @app.post("/memory/publish", dependencies=auth)
    async def memory_publish(body: MemoryPublishBody):
        # Team memory (#170): one file per confirmed record, written into the worktree; never committed.
        from cairnmind.memory_published import Publisher
        pid = project(body.project)["id"]
        if body.preview:
            scope = await asyncio.to_thread(read_scope, pid, body.worktree, body.session_id)
            return await asyncio.to_thread(Publisher(store).publish, pid, scope, preview=True,
                                           new_package=body.new_package)
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, pid, body.worktree, body.session_id, body.packet_id)
        return await asyncio.to_thread(Publisher(store).publish, pid, scope, authorize=authorize,
                                       new_package=body.new_package)

    @app.post("/memory/carrier", dependencies=auth)
    async def memory_carrier(body: MemoryCarrierBody):
        # Where published memory goes (#173): a person's choice, never a coding session's; nothing is moved.
        from cairnmind.memory_published_carrier import configure
        pid = project(body.project)["id"]
        if body.session_id:
            raise ValueError("Where published memory goes is chosen only by a person, not inside a coding session")
        worktree = str(Path(body.worktree).expanduser().resolve()) if body.worktree else None
        return await asyncio.to_thread(configure, store, pid, body.kind, body.path, worktree)

    @app.post("/memory/import-published", dependencies=auth)
    async def memory_import_published(body: MemoryImportPublishedBody):
        from cairnmind.memory_published_import import PublishedImporter
        pid = project(body.project)["id"]
        authorize = None
        if (body.take_published or body.replace_confirmed) and body.session_id:
            # Putting the published side over a record that diverged here, or over one confirmed or disputed
            # here, replaces a person's own verdict (#177): that choice is a person's, never a coding
            # session's, and a session cannot preview it as if it were one.
            raise ValueError("Taking the published side of a record that diverged here, or that was confirmed or "
                             "disputed here, is chosen only by a person, not inside a coding session")
        person_imports(body)
        if body.dry_run:
            await asyncio.to_thread(read_scope, pid, None, body.session_id)
        else:
            _, authorize = await asyncio.to_thread(memory_write_authorization, pid, None, body.session_id,
                                                   body.packet_id)
        result = await asyncio.to_thread(PublishedImporter(store).import_published, body.path, pid,
                                         dry_run=body.dry_run, bind=body.bind, take_published=body.take_published,
                                         authorize=authorize, replace_confirmed=body.replace_confirmed)
        if not body.dry_run:
            cache.clear()
        return result

    @app.post("/memory/import-commit", dependencies=auth)
    async def memory_import_commit(body: MemoryImportCommitBody):
        # The main-branch import (#172): a commit's published memory, re-checked against that commit's code.
        from cairnmind.memory_published_main import import_commit
        pid = project(body.project)["id"]
        authorize = None
        person_imports(body)
        if body.dry_run:
            scope = await asyncio.to_thread(read_scope, pid, body.worktree, body.session_id)
        else:
            scope, authorize = await asyncio.to_thread(memory_write_authorization, pid, body.worktree,
                                                       body.session_id, body.packet_id)
        result = await asyncio.to_thread(import_commit, store, pid, body.revision, worktree=scope,
                                         dry_run=body.dry_run, bind=body.bind, authorize=authorize,
                                         replace_confirmed=body.replace_confirmed)
        if not body.dry_run:
            cache.clear()
        return result

    @app.get("/memory/status", dependencies=auth)
    async def memory_status(project: str, worktree: str | None = None, session_id: str | None = None):
        pid = store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, pid, worktree, session_id)
        result = await asyncio.to_thread(memory.status, pid, scope)
        from cairnmind.memory_published import publication_status
        result["published"] = await asyncio.to_thread(publication_status, store, pid, scope)
        return result

    @app.patch("/memory/settings", dependencies=auth)
    async def memory_settings(body: MemorySettingsBody):
        pid = project(body.project)["id"]
        return await asyncio.to_thread(
            write_scoped, pid, body.session_id, body.packet_id,
            lambda scope: memory.configure_auto(pid, body.enabled, body.interval_minutes,
                selected_memory_scope(pid, body.worktree, scope)))

    @app.get("/jobs", dependencies=auth)
    async def jobs_list(project: str, state: str | None = None, kind: str | None = None,
                        limit: int = 50, session_id: str | None = None):
        try:
            pid = store.resolve_project(project)["id"]
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown project") from None
        await asyncio.to_thread(read_scope, pid, None, session_id)
        rows = await asyncio.to_thread(store.list_jobs, pid, state, kind, limit)
        return {"jobs": rows, "summary": await asyncio.to_thread(store.job_summary, pid), "limit": limit}

    @app.get("/jobs/{job_id}", dependencies=auth)
    async def job_show(job_id: str, project: str, session_id: str | None = None):
        from cairnmind.store import JobNotFoundError
        try:
            pid = store.resolve_project(project)["id"]
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown project") from None
        await asyncio.to_thread(read_scope, pid, None, session_id)
        try:
            return await asyncio.to_thread(store.job_status, pid, job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/jobs/{job_id}/retry", dependencies=auth)
    async def job_retry(job_id: str, body: JobRetryBody):
        from cairnmind.store import JobNotFoundError
        try:
            pid = project(body.project)["id"]
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown project") from None
        try:
            result = await asyncio.to_thread(write_scoped, pid, body.session_id, body.packet_id,
                                            lambda scope: store.retry_job(pid, job_id, body.expected_attempts,
                                                include_omissions=body.include_omissions))
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        response = {"job": result, "retried": True}
        if result.get("kind") in {"embed", "extract"}:
            response["background_inference"] = enable_background(pid)
        return response

    @app.get("/context", dependencies=auth)
    async def context(project: str, worktree: str | None = None, session_id: str | None = None):
        pid = store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, pid, worktree, session_id)
        result = await asyncio.to_thread(retriever.context, pid, scope)
        return result

    @app.get("/brief", dependencies=auth)
    async def brief(project: str, worktree: str | None = None, session_id: str | None = None):
        # Built from stored rows and Git only; this route never reaches the model queue.
        from cairnmind.brief import build_brief
        pid = store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, pid, worktree, session_id)
        return await asyncio.to_thread(build_brief, store, pid, scope)

    @app.get("/plans", dependencies=auth)
    async def list_plans(project: str):
        return {"plans": await asyncio.to_thread(plans.list, store.resolve_project(project)["id"])}

    @app.get("/plans/{plan_id}", dependencies=auth)
    async def get_plan(plan_id: str, project: str):
        return await asyncio.to_thread(plans.get, store.resolve_project(project)["id"], plan_id)

    @app.post("/plans/save", dependencies=auth)
    async def save_plan(body: PlanSaveBody):
        pid = project(body.project)["id"]
        def save(scope):
            if scope and body.worktree and str(Path(body.worktree).expanduser().resolve()) != scope:
                raise ValueError("resume_required: worktree does not match the session")
            return plans.save(pid, body.plan, body.steps, body.idempotency_key, body.expected_version,
                              body.task_versions, scope or body.worktree, body.activate)
        result = await asyncio.to_thread(write_scoped, pid, body.session_id, body.packet_id, save)
        cache.clear()
        return result

    @app.post("/plans/next", dependencies=auth)
    async def next_task(body: PlanNextBody):
        pid = project(body.project)["id"]
        scope = await asyncio.to_thread(read_scope, pid, body.worktree, body.session_id)

        def read():
            result = plans.next_task(pid, body.plan_id, scope)
            if result.get("task") and result["task"].get("metadata", {}).get("scope"):
                from cairnmind.scope import drift_warning, scope_drift
                warning = drift_warning(scope_drift(store, result["task"], scope))
                if warning:
                    result["warnings"] = [warning]
            return result
        return await asyncio.to_thread(read)

    @app.post("/plans/activate", dependencies=auth)
    async def activate_plan(body: PlanActivateBody):
        pid = project(body.project)["id"]
        def activate(scope):
            if scope and body.worktree and str(Path(body.worktree).expanduser().resolve()) != scope:
                raise ValueError("resume_required: worktree does not match the session")
            return plans.activate(pid, body.plan_id, scope or body.worktree)
        result = await asyncio.to_thread(write_scoped, pid, body.session_id, body.packet_id, activate)
        cache.clear()
        return result

    @app.post("/sessions/prepare", dependencies=auth)
    async def prepare_session(body: SessionPrepareBody):
        from cairnmind.integrations import drain_hook_spool
        await asyncio.to_thread(drain_hook_spool, settings, store)
        return await asyncio.to_thread(sessions.prepare, project(body.project)["id"], body.client,
                                       body.worktree, body.session_id, body.reason)

    @app.post("/sessions/context", dependencies=auth)
    async def resume_context(body: SessionContextBody):
        from cairnmind.integrations import drain_hook_spool
        await asyncio.to_thread(drain_hook_spool, settings, store)
        if (body.packet_id is not None and body.expected_packet_id is not None
                and body.packet_id != body.expected_packet_id):
            raise ValueError("resume_required: conflicting requested packet identities")
        expected = body.packet_id if body.packet_id is not None else body.expected_packet_id
        if expected is not None:
            return await asyncio.to_thread(sessions.exact_context, body.session_id, expected)
        return await asyncio.to_thread(sessions.resume_context, body.session_id)

    @app.post("/sessions/ack", dependencies=auth)
    async def acknowledge_resume(body: SessionAckBody):
        from cairnmind.integrations import drain_hook_spool
        await asyncio.to_thread(drain_hook_spool, settings, store)
        return await asyncio.to_thread(sessions.ack, body.session_id, body.packet_id, body.packet_hash, body.readback)

    @app.post("/sessions/agent/archive", dependencies=auth)
    async def archive_agent_session(body: AgentArchiveBody):
        # The body selects only a bound logical session. Paths, events and
        # claims of successful archiving are never supplied by the caller.
        result = await asyncio.to_thread(sessions.archive_agent_journal, body.session_id)
        cache.clear()
        return result

    @app.get("/sessions/{session_id}", dependencies=auth)
    async def session_status(session_id: str):
        return await asyncio.to_thread(sessions.status, session_id)

    @app.get("/checks", dependencies=auth)
    async def checks(project: str, task_id: str | None = None, worktree: str | None = None,
                     session_id: str | None = None):
        from cairnmind.verification import recover_interrupted_runs, verification_status
        pid = store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, pid, worktree, session_id)
        await asyncio.to_thread(recover_interrupted_runs, store, settings)
        rows = await asyncio.to_thread(store.query, "SELECT FROM check_runs WHERE project_id=:project "
                                      + ("AND task_id=:task " if task_id else "")
                                      + ("AND worktree=:worktree " if scope else "")
                                      + "ORDER BY ended_at DESC,id LIMIT 100",
                                      {"project": pid, "task": task_id, "worktree": scope})
        fields = ("id", "project_id", "task_id", "profile_id", "worktree", "outcome", "passed", "returncode",
                  "started_at", "ended_at", "duration_seconds", "source_ids", "reasons", "profile_hash",
                  "task_definition_hash", "origin")
        result = {"checks": [{key: row.get(key) for key in fields if key in row} for row in rows]}
        if task_id:
            task = await asyncio.to_thread(store.query,
                "SELECT FROM records WHERE id=:id AND project_id=:project AND kind='task'", {"id": task_id, "project": pid})
            if not task:
                raise ValueError("Task does not exist in this project")
            result["verification"] = await asyncio.to_thread(verification_status, store, task[0], scope)
        return result

    async def log_answer(pid, body, scope, answer_schema, code, result):
        # Runs after the response is sent: a slow digest lookup or disk never delays an answer.
        from cairnmind import answer_log
        digest = None
        identity = getattr(model, "model_identity", None)
        if identity is not None:
            try:
                digest = await asyncio.wait_for(identity(settings.chat_model), 5)
            except Exception:  # noqa: BLE001 - the digest is optional in a log entry
                digest = None
        try:
            entry = answer_log.make_entry(settings, pid, body.question, result, worktree=scope,
                                          answer_schema=answer_schema, code=code, model_digest=digest)
            if not await asyncio.to_thread(answer_log.append, settings, entry):
                log.warning("An answer was too large for the answer log and was not kept")
        except Exception as exc:  # noqa: BLE001 - the answer was sent; a log failure is only a warning
            log.warning("The answer log could not be written: %s", exc)

    @app.post("/ask", dependencies=auth)
    async def ask(body: AskBody, background: BackgroundTasks):
        from cairnmind.answer_contract import prepare_answer_schema
        from cairnmind.ingest import index_pending_sources, index_project_if_changed
        from cairnmind.verification import capture_code_state, load_profiles
        pid = project(body.project)["id"]
        answer_schema = prepare_answer_schema(body.answer_schema)
        contract_key = json.dumps(answer_schema, sort_keys=True, separators=(",", ":"))
        scope = await asyncio.to_thread(read_scope, pid, body.worktree, body.session_id)
        if not models_on:  # no model is asked, and nothing is indexed, cached or logged (#274)
            return knowledge_mode.unavailable_answer()
        keep = body.log if body.log is not None else getattr(settings, "answer_log", False) is True
        code_state: dict = {}
        def code_identity():
            try:
                profiles = load_profiles(store, pid, scope)
                inputs = sorted({path for profile in profiles.values() for path in profile["extra_inputs"]})
                code = capture_code_state(store, pid, scope, inputs)
                code_state.update(code)
                if code["status"] != "fresh":
                    return None
                return code["fingerprint"], tuple(sorted((name, p["hash"]) for name, p in profiles.items()))
            except (ValueError, OSError):
                return None
        identity = await asyncio.to_thread(code_identity)
        # An unchanged tree is not indexed again (the code identity above still hashes the
        # worktree); the cache stamp below is a counter, not a scan.
        indexed = await asyncio.to_thread(index_project_if_changed, store, pid)
        index_errors = indexed.get("errors", [])
        pending = await asyncio.to_thread(index_pending_sources, store)
        index_errors.extend(pending.get("errors", []))
        stamp = await asyncio.to_thread(store.cache_stamp, pid)
        key = (pid, body.question, scope, stamp, identity, contract_key)
        hit = cache.get(key)
        if hit and identity is not None and not index_errors and time.monotonic() - hit[0] < 60:
            if keep:
                background.add_task(log_answer, pid, body, scope, answer_schema, code_state, {**hit[1], "cached": True})
            return {**hit[1], "cached": True}
        try:
            result = await asyncio.wait_for(
                inference.submit(lambda: agent.ask(pid, body.question, worktree=scope,
                                                   answer_schema=answer_schema), priority=0),
                timeout=settings.ask_timeout + 5,
            )
        except TimeoutError:
            result = {"status": "partial", "answer": "Waiting for the local agent timed out.",
                      "sources": [], "unknowns": ["The queue or the analysis exceeded the time limit."],
                      "freshness": {}, "elapsed_seconds": settings.ask_timeout + 5, "cached": False,
                      "diagnostics": {"stage": "ask", "code": "queue_timeout",
                                      "budget": {"timeout_seconds": settings.ask_timeout + 5}}}
            if keep:
                background.add_task(log_answer, pid, body, scope, answer_schema, code_state, result)
            return result
        if index_errors:
            result["unknowns"] = [*result.get("unknowns", []), *map(str, index_errors[:5])]
            result.setdefault("freshness", {}).setdefault("warnings", []).extend(map(str, index_errors[:5]))
            if result.get("status") == "complete":
                result["status"] = "partial"
        if result.get("status") == "complete" and identity is not None:
            if len(cache) >= 128:
                cache.pop(next(iter(cache)))
            cache[key] = (time.monotonic(), result)
        if keep:
            background.add_task(log_answer, pid, body, scope, answer_schema, code_state, {**result, "cached": False})
        return {**result, "cached": False}

    @app.post("/remember", dependencies=auth)
    async def remember(body: RememberBody):
        from cairnmind.remember_input import external_report_source, record_error
        error = record_error(body.record)
        if error:
            # A plain 400: the word-based mapping of ValueError would read a named field as a conflict. Marked,
            # so that the built-in agent hands the model which field is wrong (#240).
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=400, content={"detail": error, "refusal": "record"})
        pid = project(body.project)["id"]
        def save(scope):
            record = dict(body.record)
            if scope:
                requested = record.get("worktree")
                if requested and str(Path(requested).expanduser().resolve()) != scope:
                    raise ValueError("resume_required: completion scope differs from session")
                record["worktree"] = scope
            sources = body.sources
            if not sources and (store.current_origin() or {}).get("origin_client") == "external":
                # An external session has no archived conversation to cite (README, external sessions).
                sources = [external_report_source(store, pid, record)]
            return store.remember(pid, record, sources)
        result = await asyncio.to_thread(write_scoped, pid, body.session_id, body.packet_id, save)
        cache.clear()
        return result

    @app.post("/findings", dependencies=auth)
    async def finding(body: FindingBody):
        """A finding with a source created here, so it cannot claim another origin's id.

        Without a session it is a person's own write (origin workspace_form); with one, it is the
        session's (origin session_finding), after the same resume gate as /remember.
        """
        from cairnmind.findings import finding_error, key_error, save_finding
        selected = project(body.project)
        pid = selected["id"]
        error = finding_error(body.content, body.kind, body.status) or key_error(body.key)
        if error:
            raise ValueError(error)

        def save(scope):
            # write_scoped calls this only after an acknowledged session check when a session is named.
            # The worktree only checks that the caller's view is genuine; the finding is project-wide.
            requested = selected_memory_scope(pid, body.worktree, scope) or selected["root"]
            root = str(Path(requested).expanduser().resolve())
            if not store.query("SELECT path FROM project_roots WHERE project_id=:project AND path=:root",
                               {"project": pid, "root": root}):
                raise ValueError("A finding needs a registered project worktree")
            return save_finding(store, pid, body.key, body.content, body.kind, body.status,
                                session_id=body.session_id or None)
        result = await asyncio.to_thread(write_scoped, pid, body.session_id, body.packet_id, save)
        cache.clear()
        return result

    @app.post("/checkpoint", dependencies=auth)
    async def checkpoint(body: CheckpointBody):
        pid = project(body.project)["id"]
        result = await asyncio.to_thread(write_scoped, pid, body.session_id, body.packet_id,
                                         lambda scope: store.checkpoint(pid, body.state))
        cache.clear()
        return result

    @app.post("/ingest", dependencies=auth)
    async def ingest(body: ProjectBody):
        from cairnmind.ingest import index_project
        result = await asyncio.to_thread(index_project, store, project(body.project)["id"])
        cache.clear()
        return result

    @app.post("/import", dependencies=auth)
    async def import_sources(body: ImportBody):
        from pathlib import Path
        from cairnmind.ingest import import_history
        pid = project(body.project)["id"] if body.project else None
        result = await asyncio.to_thread(import_history, store, Path(body.path), pid, body.client)
        cache.clear()
        return result

    @app.post("/shutdown", dependencies=auth)
    async def shutdown():
        callback = getattr(app.state, "shutdown", None)
        if callback:
            callback()
        return {"stopping": bool(callback)}

    from cairnmind.inspector_api import attach_inspector_routes
    attach_inspector_routes(app, store, settings)
    return app
