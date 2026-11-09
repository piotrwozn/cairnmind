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

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from devbrain import __version__
from devbrain.config import Settings

log = logging.getLogger(__name__)


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


class RememberBody(BaseModel):
    project: str
    record: dict
    sources: list[str] = Field(default_factory=list)
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
    packet_id: str | None = None


class MemorySettingsBody(MemorySnapshotBody):
    enabled: bool = Field(strict=True)
    interval_minutes: int = Field(default=15, ge=1, le=10080, strict=True)


def instance_id(settings: Settings) -> str:
    return hashlib.sha256(str(settings.data_dir).encode()).hexdigest()[:16]


def schedule_embeddings(store, namespace: str, *, project_ids=None) -> int:
    """Repair missing/digest-changed derived vectors without touching archived evidence."""
    rows = (store.missing_embeddings(namespace) if project_ids is None else
            [row for pid in project_ids for row in store.missing_embeddings(namespace, pid)])
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
    from devbrain.agent import KnowledgeAgent, extraction_outer_timeout, inference_lease_seconds
    from devbrain.ollama import Ollama
    from devbrain.retrieval import Retriever
    from devbrain.store import Store
    from devbrain.plans import PlanManager
    from devbrain.sessions import SessionManager
    from devbrain.memory import MemoryConflict, MemoryManager, inspect_snapshot

    settings = settings or Settings.load(create=True)
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("DevBrain must bind to a loopback address")
    if not settings.token:
        raise ValueError("Run devbrain init to create a local authentication token")
    store = Store(settings.data_dir)
    model = ollama or Ollama(settings)
    retriever = Retriever(store, settings, ollama=model)
    agent = KnowledgeAgent(store, settings, ollama=model, retriever=retriever)
    plans = PlanManager(store)
    sessions = SessionManager(store, settings)
    memory = MemoryManager(store)
    inference = InferenceQueue()
    background_status: dict = {"scanner_error": None, "model_error": None, "memory_error": None}
    cache: dict = {}
    inference_projects: set[str] = set()
    background_ready = asyncio.Event()
    if background_inference:
        background_ready.set()

    def inference_status():
        return {"worker_running": start_worker,
                "mode": ("disabled" if not start_worker else "all_projects" if background_inference
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
                background_status["memory_error"] = result.get("errors") or None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                background_status["memory_error"] = {"code": type(exc).__name__}
                log.exception("Portable memory scheduler failed")
            await asyncio.sleep(min(60, max(1, settings.poll_seconds)))

    async def scanner() -> None:
        from pathlib import Path
        from devbrain.ingest import import_history, index_pending_sources, index_project, poll_imports

        while True:
            try:
                from devbrain.integrations import drain_hook_spool
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
                for project in await asyncio.to_thread(store.list_projects):
                    indexed = await asyncio.to_thread(index_project, store, project["id"])
                    scan_errors.extend(f"{project['name']}: {err}" for err in indexed.get("errors", []))
                pending = await asyncio.to_thread(index_pending_sources, store)
                scan_errors.extend(f"memory index: {err}" for err in pending.get("errors", []))
                from devbrain.verification import recover_interrupted_runs
                await asyncio.to_thread(recover_interrupted_runs, store, settings)
                backup_dir = settings.data_dir / "backups"
                backups = sorted(backup_dir.glob("*.devbrain.json.gz")) if backup_dir.exists() else []
                if not backups or time.time() - backups[-1].stat().st_mtime > 86400:
                    backup_dir.mkdir(exist_ok=True)
                    await asyncio.to_thread(store.backup, backup_dir / f"{time.time_ns()}.devbrain.json.gz")
                    for old in sorted(backup_dir.glob("*.devbrain.json.gz"))[:-7]:
                        old.unlink()
                background_status["scanner_error"] = "; ".join(scan_errors[:5]) or None
                background_status["scan_errors"] = scan_errors[:20]
                background_status["last_scan_at"] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                background_status["scanner_error"] = str(exc)
                log.exception("Archive scan failed")
            await asyncio.sleep(settings.poll_seconds)

    async def jobs() -> None:
        last_namespace, last_repair, last_projects = None, 0.0, None
        while True:
            try:
                # Opening a UI or acknowledging context does not authorize model work.
                await background_ready.wait()
                selected = None if background_inference else tuple(sorted(inference_projects))
                # Keep pending work pending when Ollama is offline, instead of failing the whole queue.
                await model.models()
                identity = getattr(model, "model_identity", None)
                if identity:
                    namespace = await identity(settings.embedding_model)
                    if (namespace != last_namespace or selected != last_projects
                            or time.monotonic() - last_repair > 60):
                        missing = await asyncio.to_thread(schedule_embeddings, store, namespace, project_ids=selected)
                        background_status.update(embedding_namespace=namespace, embedding_missing=missing)
                        last_namespace, last_repair, last_projects = namespace, time.monotonic(), selected
                background_status["model_error"] = None
                job = await asyncio.to_thread(store.claim_job, "inference", inference_lease_seconds(), ["embed", "extract"],
                                              project_ids=selected)
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
            except Exception as exc:
                background_status["model_error"] = str(exc)
                await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(inference.run())]
        if start_worker:
            tasks += [asyncio.create_task(scanner()), asyncio.create_task(jobs()),
                      asyncio.create_task(memory_snapshots())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="DevBrain local knowledge agent", version=__version__, lifespan=lifespan)
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
        """Session validation and the resulting mutation share one writer transaction."""
        if session_id:
            from devbrain.integrations import drain_hook_spool
            drain_hook_spool(settings, store)
        with store.transaction():
            scope = None
            if session_id:
                sessions.require_ack(session_id, packet_id)
                row = store.query("SELECT project_id,worktree FROM sessions WHERE id=:id", {"id": session_id})
                if not row or row[0]["project_id"] != pid:
                    raise ValueError("resume_required: session belongs to another project")
                scope = row[0]["worktree"]
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
        status = 409 if (isinstance(exc, MemoryConflict) or any(
            word in str(exc).lower() for word in ("version", "conflict", "stale", "resume_required"))) else 400
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    from devbrain.arcade import ArcadeError

    @app.exception_handler(ArcadeError)
    async def backend_unavailable(request: Request, exc: ArcadeError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": str(exc), "backend": "arcadedb"})

    @app.get("/health")
    async def health():
        return {"service": "devbrain", "version": __version__, "instance": instance_id(settings),
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
        return {**result, "background": {**background_status, "inference": inference_status()}, "pipeline_jobs": pipeline,
                "inference_queue": inference.queue.qsize(), "cached_answers": len(cache),
                "recent_job_errors": error_rows}

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

    def memory_write_authorization(pid: str, requested: str | None,
                                   session_id: str | None, packet_id: str | None):
        """Keep file-lock -> DB-lock ordering, rechecking the actor in core's transaction."""
        if not session_id:
            return requested, None
        from devbrain.integrations import drain_hook_spool
        drain_hook_spool(settings, store)

        def require():
            sessions.require_ack(session_id, packet_id)
            rows = store.query("SELECT project_id,worktree FROM sessions WHERE id=:id", {"id": session_id})
            if not rows or rows[0]["project_id"] != pid:
                raise ValueError("resume_required: memory belongs to another project")
            return selected_memory_scope(pid, requested, rows[0]["worktree"])

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
            return memory.import_snapshot(
                body.path, body.root, body.name, body.dry_run,
                expected_snapshot_id=inspected["snapshot_id"], expected_project_id=pid, authorize=authorize)
        if body.dry_run:
            scope = await asyncio.to_thread(read_scope, pid, body.root, body.session_id)
            return await asyncio.to_thread(restore, scope)
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, pid, body.root, body.session_id, body.packet_id)
        result = await asyncio.to_thread(restore, scope, authorize)
        cache.clear()
        return result

    @app.get("/memory/status", dependencies=auth)
    async def memory_status(project: str, worktree: str | None = None, session_id: str | None = None):
        pid = store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, pid, worktree, session_id)
        return await asyncio.to_thread(memory.status, pid, scope)

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
        from devbrain.store import JobNotFoundError
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
        from devbrain.store import JobNotFoundError
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
        return await asyncio.to_thread(plans.next_task, pid, body.plan_id, scope)

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
        from devbrain.integrations import drain_hook_spool
        await asyncio.to_thread(drain_hook_spool, settings, store)
        return await asyncio.to_thread(sessions.prepare, project(body.project)["id"], body.client,
                                       body.worktree, body.session_id, body.reason)

    @app.post("/sessions/context", dependencies=auth)
    async def resume_context(body: SessionContextBody):
        from devbrain.integrations import drain_hook_spool
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
        from devbrain.integrations import drain_hook_spool
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
        from devbrain.verification import recover_interrupted_runs, verification_status
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

    @app.post("/ask", dependencies=auth)
    async def ask(body: AskBody):
        from devbrain.answer_contract import prepare_answer_schema
        from devbrain.ingest import index_pending_sources, index_project
        from devbrain.verification import capture_code_state, load_profiles
        pid = project(body.project)["id"]
        answer_schema = prepare_answer_schema(body.answer_schema)
        contract_key = json.dumps(answer_schema, sort_keys=True, separators=(",", ":"))
        scope = await asyncio.to_thread(read_scope, pid, body.worktree, body.session_id)
        def code_identity():
            try:
                profiles = load_profiles(store, pid, scope)
                inputs = sorted({path for profile in profiles.values() for path in profile["extra_inputs"]})
                code = capture_code_state(store, pid, scope, inputs)
                if code["status"] != "fresh":
                    return None
                return code["fingerprint"], tuple(sorted((name, p["hash"]) for name, p in profiles.items()))
            except (ValueError, OSError):
                return None
        identity = await asyncio.to_thread(code_identity)
        indexed = await asyncio.to_thread(index_project, store, pid)
        index_errors = indexed.get("errors", [])
        pending = await asyncio.to_thread(index_pending_sources, store)
        index_errors.extend(pending.get("errors", []))
        stamp = await asyncio.to_thread(store.cache_stamp, pid)
        key = (pid, body.question, scope, stamp, identity, contract_key)
        hit = cache.get(key)
        if hit and identity is not None and not index_errors and time.monotonic() - hit[0] < 60:
            return {**hit[1], "cached": True}
        try:
            result = await asyncio.wait_for(
                inference.submit(lambda: agent.ask(pid, body.question, worktree=scope,
                                                   answer_schema=answer_schema), priority=0),
                timeout=settings.ask_timeout + 5,
            )
        except TimeoutError:
            return {"status": "partial", "answer": "Przekroczono czas oczekiwania na lokalnego agenta.",
                    "sources": [], "unknowns": ["Kolejka lub analiza przekroczyła limit czasu."],
                    "freshness": {}, "elapsed_seconds": settings.ask_timeout + 5, "cached": False,
                    "diagnostics": {"stage": "ask", "code": "queue_timeout",
                                    "budget": {"timeout_seconds": settings.ask_timeout + 5}}}
        if index_errors:
            result["unknowns"] = [*result.get("unknowns", []), *map(str, index_errors[:5])]
            result.setdefault("freshness", {}).setdefault("warnings", []).extend(map(str, index_errors[:5]))
            if result.get("status") == "complete":
                result["status"] = "partial"
        if result.get("status") == "complete" and identity is not None:
            if len(cache) >= 128:
                cache.pop(next(iter(cache)))
            cache[key] = (time.monotonic(), result)
        return {**result, "cached": False}

    @app.post("/remember", dependencies=auth)
    async def remember(body: RememberBody):
        pid = project(body.project)["id"]
        def save(scope):
            record = dict(body.record)
            if scope:
                requested = record.get("worktree")
                if requested and str(Path(requested).expanduser().resolve()) != scope:
                    raise ValueError("resume_required: completion scope differs from session")
                record["worktree"] = scope
            return store.remember(pid, record, body.sources)
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
        from devbrain.ingest import index_project
        result = await asyncio.to_thread(index_project, store, project(body.project)["id"])
        cache.clear()
        return result

    @app.post("/import", dependencies=auth)
    async def import_sources(body: ImportBody):
        from pathlib import Path
        from devbrain.ingest import import_history
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

    from devbrain.inspector_api import attach_inspector_routes
    attach_inspector_routes(app, store, settings)
    return app
