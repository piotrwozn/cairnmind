"""Domain-grouped FastAPI route registrations for the local Cairnmind HTTP service."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from cairnmind import __version__
from cairnmind.cli_cmds.service_version import instance_id
from cairnmind.config import SERVICE_NAMES, Settings
from cairnmind.mcp.service_models import (
    AgentArchiveBody,
    AskBody,
    BackgroundInferenceBody,
    CheckpointBody,
    FindingBody,
    ImportBody,
    JobRetryBody,
    MemoryCarrierBody,
    MemoryHealBody,
    MemoryImportBody,
    MemoryImportCommitBody,
    MemoryImportPublishedBody,
    MemoryInspectBody,
    MemoryPreviewBody,
    MemoryPublishBody,
    MemorySettingsBody,
    MemorySnapshotExportBody,
    PlanActivateBody,
    PlanNextBody,
    PlanSaveBody,
    ProjectBody,
    RegisterProjectBody,
    RememberBody,
    SessionAckBody,
    SessionContextBody,
    SessionPrepareBody,
)
from cairnmind.mcp.service_workers import ServiceContext
from cairnmind.storage.storage_query import asc, db_find, db_find_one, desc
from cairnmind.terminal import person_proof

log = logging.getLogger(__name__)


def make_auth_dependency(ctx: ServiceContext) -> list[Any]:
    async def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {ctx.settings.token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Invalid local service token")

    return [Depends(authorize)]


def resolve_project(ctx: ServiceContext, identifier: str) -> dict:
    try:
        return ctx.store.resolve_project(identifier)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def write_scoped(ctx: ServiceContext, pid: str, session_id: str | None, packet_id: str | None, operation):
    """Session validation and the resulting mutation share one writer transaction.

    The worktree's code state is hashed before that transaction begins: inside it, a slow worktree let
    ArcadeDB expire the transaction (#255).
    """
    from cairnmind.memory.sessions import origin_mark

    code = None
    if session_id:
        from cairnmind.memory.integrations import drain_hook_spool

        drain_hook_spool(ctx.settings, ctx.store)
        code = ctx.sessions.code_scope(session_id, packet_id)
    with ctx.store.transaction():
        scope, mark = None, None
        if session_id:
            ctx.sessions.require_ack(session_id, packet_id, code)
            session = db_find_one(
                ctx.store,
                "sessions",
                {"id": session_id},
                columns=["id", "project_id", "worktree", "client"],
            )
            if not session or session["project_id"] != pid:
                raise ValueError("resume_required: session belongs to another project")
            scope = session["worktree"]
            # Derived from the session row, never from the payload: an external session's writes are
            # stamped as such, and nothing a client sends can add, change or remove that stamp.
            mark = origin_mark(session)
            if mark is None and session.get("client") in {"api", "copilot"}:
                # An own-agent session that received a third-party MCP result writes as unverified (#126).
                from cairnmind.agents.agent_memory import third_party_origin

                mark = third_party_origin(ctx.store, pid, session_id)
        # A write that names a session is never a person's act (#177), whatever the session's client.
        with ctx.store.write_origin(mark), ctx.store.session_write(session_id):
            return operation(scope)


def with_dropped(ctx: ServiceContext, result: dict) -> dict:
    """A copy of a write's result for the answer only (#325): the metadata keys the version before it held
    and this one does not (reported proof, criteria links, an external checkpoint's fields). Read from the
    immutable revision before, so the store's result, and everything stored from it, stays as it was.
    """
    if result.get("version", 1) < 2:
        return result
    prev = db_find_one(
        ctx.store,
        "record_revisions",
        {"id": f"{result['id']}:{result['version'] - 1}"},
        columns=["snapshot"],
    )
    held = ((prev.get("snapshot") or {}).get("metadata") or {}) if prev else {}
    dropped = sorted(name for name in held if name not in (result.get("metadata") or {}))
    return {**result, "metadata_dropped": dropped} if dropped else result


def read_scope(
    ctx: ServiceContext,
    pid: str,
    requested: str | None,
    session_id: str | None = None,
) -> str | None:
    scope = str(Path(requested).expanduser().resolve()) if requested else None
    if session_id:
        session = db_find_one(ctx.store, "sessions", {"id": session_id}, columns=["project_id", "worktree"])
        if not session or session["project_id"] != pid:
            raise ValueError("Session read belongs to another project")
        if scope and scope != session["worktree"]:
            raise ValueError("Requested read worktree differs from session scope")
        scope = session["worktree"]
    return scope


def selected_memory_scope(pid: str, requested: str | None, scope: str | None):
    if scope and requested and str(Path(requested).expanduser().resolve()) != scope:
        raise ValueError("resume_required: memory worktree does not match the session")
    return scope or requested


def person_imports(body) -> None:
    """A memory import writes records that count as confirmed and can move on what a person confirmed
    here, so only a person runs it (#177): never a request that names a session. A dry run writes
    nothing and stays open to a session.
    """
    if body.session_id and not body.dry_run:
        from cairnmind.memory.confirmation import IMPORT_IN_SESSION

        raise ValueError(IMPORT_IN_SESSION)


def needs_shown_list(body) -> None:
    """A published import that replaces a person's verdicts carries the list the person was shown (#304)."""
    if body.expected_replaced is None:
        raise ValueError(
            "Replacing what a person confirmed or disputed needs the list the person was shown "
            "(`expected_replaced`). Nothing was imported."
        )


def memory_write_authorization(
    ctx: ServiceContext,
    pid: str,
    requested: str | None,
    session_id: str | None,
    packet_id: str | None,
):
    """Keep file-lock -> DB-lock ordering, rechecking the actor in core's transaction.

    The worktree is hashed outside any transaction (#255): here for the first check, and again by
    ``require.refresh()``, which core calls right before it opens the transaction it calls ``require`` in.
    So the recheck compares the code as it was then, not as it was before core's own file work.
    """
    if not session_id:
        return requested, None
    from cairnmind.memory.integrations import drain_hook_spool

    drain_hook_spool(ctx.settings, ctx.store)
    code = ctx.sessions.code_scope(session_id, packet_id)

    def require():
        ctx.sessions.require_ack(session_id, packet_id, code)
        session = db_find_one(ctx.store, "sessions", {"id": session_id}, columns=["project_id", "worktree"])
        if not session or session["project_id"] != pid:
            raise ValueError("resume_required: memory belongs to another project")
        return selected_memory_scope(pid, requested, session["worktree"])

    def refresh():
        nonlocal code
        if getattr(ctx.store._local, "transaction", None) is None:  # inside one, keep the earlier capture
            code = ctx.sessions.code_scope(session_id, packet_id)

    require.refresh = refresh
    # Reject an already-unacknowledged actor before creating filesystem output.
    # Core invokes require again while capturing/importing in its transaction.
    return require(), require


async def log_answer(ctx: ServiceContext, pid, body, scope, answer_schema, code, result) -> None:
    """Runs after the response is sent: a slow digest lookup or disk never delays an answer."""
    from cairnmind.agents import answer_log

    digest = None
    identity = getattr(ctx.model, "model_identity", None)
    if identity is not None:
        try:
            digest = await asyncio.wait_for(identity(ctx.settings.chat_model), 5)
        except Exception:  # noqa: BLE001 - the digest is optional in a log entry
            digest = None
    try:
        entry = answer_log.make_entry(
            ctx.settings,
            pid,
            body.question,
            result,
            worktree=scope,
            answer_schema=answer_schema,
            code=code,
            model_digest=digest,
        )
        if not await asyncio.to_thread(answer_log.append, ctx.settings, entry):
            log.warning("An answer was too large for the answer log and was not kept")
    except Exception as exc:  # noqa: BLE001 - the answer was sent; a log failure is only a warning
        log.warning("The answer log could not be written: %s", exc)


def register_middleware_and_handlers(app: FastAPI, ctx: ServiceContext) -> None:
    from cairnmind.memory.memory import MemoryConflict
    from cairnmind.storage.arcade import ArcadeError

    @app.middleware("http")
    async def migration_quiesce_middleware(request: Request, call_next):
        from cairnmind.storage.migration import is_migration_in_progress

        if request.url.path not in {"/health", "/shutdown", "/internal/reload-store"} and is_migration_in_progress(
            ctx.settings.data_dir
        ):
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Storage migration is currently in progress; retry after cutover.",
                    "error_code": "migration_in_progress",
                },
            )
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def bad_input(request: Request, exc: ValueError):
        from cairnmind.memory.confirmation import ConfirmationRefused
        from cairnmind.store import RecordRefused

        status = (
            409
            if (
                isinstance(exc, MemoryConflict)
                or any(word in str(exc).lower() for word in ("version", "conflict", "stale", "resume_required"))
            )
            else 400
        )
        if isinstance(exc, ConfirmationRefused):
            # Not something to read again and retry (#177), whatever words the refusal uses ("conflict inbox").
            # Marked, so that the built-in agent hands the model this reason and what to do instead (#240).
            return JSONResponse(status_code=400, content={"detail": str(exc), "refusal": "confirmation"})
        if isinstance(exc, RecordRefused):
            # The store's fixed sentence about the record's shape, marked like record_error's (#240).
            return JSONResponse(status_code=400, content={"detail": str(exc), "refusal": "record"})
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.exception_handler(ArcadeError)
    async def backend_unavailable(request: Request, exc: ArcadeError):
        return JSONResponse(status_code=503, content={"detail": str(exc), "backend": "arcadedb"})


def register_admin_routes(app: FastAPI, ctx: ServiceContext, auth: list[Any]) -> None:
    @app.get("/health")
    async def health():
        return {
            "service": SERVICE_NAMES[0],
            "version": __version__,
            "instance": instance_id(ctx.settings),
            "backend": ctx.active_backend_label(),
        }

    @app.post("/internal/reload-store", dependencies=auth)
    async def reload_store():
        from cairnmind.storage.store_writer import StoreNotWriter
        from cairnmind.store import Store

        reloaded_settings = Settings.load(ctx.settings.data_dir, create=False)
        new_store = await asyncio.to_thread(Store, reloaded_settings.data_dir, settings=reloaded_settings)
        new_store.relative_paths = False
        if new_store.not_writer:
            new_store.close()
            raise StoreNotWriter(new_store.not_writer)
        old_store = ctx.store
        ctx.settings = reloaded_settings
        ctx.store = new_store
        ctx.retriever.store = new_store
        ctx.retriever.settings = reloaded_settings
        ctx.agent.store = new_store
        ctx.agent.settings = reloaded_settings
        ctx.plans.store = new_store
        ctx.sessions.store = new_store
        ctx.sessions.settings = reloaded_settings
        ctx.memory.store = new_store
        app.state.store = new_store
        ctx.cache.clear()
        try:
            await asyncio.to_thread(old_store.close)
        except Exception:  # noqa: BLE001
            pass
        return {"reloaded": True, "backend": ctx.active_backend_label()}

    @app.get("/status", dependencies=auth)
    async def status(project: str | None = None):
        from cairnmind.agents import knowledge_mode
        from cairnmind.cli_cmds.topology import topology
        from cairnmind.memory.project_folders import unavailable
        from cairnmind.storage.database_mode import address, describe

        pid = ctx.store.resolve_project(project)["id"] if project else None
        result = await asyncio.to_thread(ctx.store.status, pid)
        error_rows = await asyncio.to_thread(ctx.store.recent_job_errors, pid)
        pipeline = await asyncio.to_thread(ctx.store.job_summary, pid)
        writer = await asyncio.to_thread(ctx.store.writer_state)
        # The service's own settings, loaded when it started; its model servers are not asked here, so the
        # evaluation reads "not checked" (cairnmind doctor checks it).
        report = {
            **result,
            "writer": writer,
            "storage": {**describe(ctx.settings), "url": address(ctx.settings)},
            "topology": topology(ctx.settings, writer=writer),
            "background": {**ctx.background_status, "inference": ctx.inference_status()},
            "pipeline_jobs": pipeline,
            "inference_queue": ctx.inference.queue.qsize(),
            "cached_answers": len(ctx.cache),
            "recent_job_errors": error_rows,
            "unavailable_folders": await asyncio.to_thread(unavailable, ctx.store, pid),
        }
        if not pid:  # what is left of a deleted project's memory (#347)
            from cairnmind.memory.project_forget import history

            report["deleted_projects"] = await asyncio.to_thread(history, ctx.store)
        if not ctx.models_on:
            report.update(knowledge_mode.status_fields(pipeline, ctx.settings))
        return report

    @app.post("/background-inference/enable", dependencies=auth)
    async def enable_background_inference(body: BackgroundInferenceBody):
        from cairnmind.agents import knowledge_mode
        from cairnmind.cli_cmds.first_start import PROCESS_OFF, PROCESS_RESTART

        selected = await asyncio.to_thread(resolve_project, ctx, body.project)
        pid = selected["id"]

        def authorize_enable(scope):
            requested = selected_memory_scope(pid, body.worktree, scope) or selected["root"]
            root = str(Path(requested).expanduser().resolve())
            if not db_find_one(ctx.store, "project_roots", {"project_id": pid, "path": root}, columns=["path"]):
                raise ValueError("Background processing requires a registered project worktree")

        await asyncio.to_thread(write_scoped, ctx, pid, body.session_id, body.packet_id, authorize_enable)
        notice = (
            {}
            if ctx.models_on
            else {"notice": PROCESS_RESTART if knowledge_mode.restart_pending(ctx.settings) else PROCESS_OFF}
        )
        return {**ctx.enable_background(pid), **notice}  # the panel shows it

    @app.get("/jobs", dependencies=auth)
    async def jobs_list(
        project: str,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
    ):
        try:
            pid = ctx.store.resolve_project(project)["id"]
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown project") from None
        await asyncio.to_thread(read_scope, ctx, pid, None, session_id)
        rows = await asyncio.to_thread(ctx.store.list_jobs, pid, state, kind, limit)
        return {"jobs": rows, "summary": await asyncio.to_thread(ctx.store.job_summary, pid), "limit": limit}

    @app.get("/jobs/{job_id}", dependencies=auth)
    async def job_show(job_id: str, project: str, session_id: str | None = None):
        from cairnmind.store import JobNotFoundError

        try:
            pid = ctx.store.resolve_project(project)["id"]
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown project") from None
        await asyncio.to_thread(read_scope, ctx, pid, None, session_id)
        try:
            return await asyncio.to_thread(ctx.store.job_status, pid, job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/jobs/{job_id}/retry", dependencies=auth)
    async def job_retry(job_id: str, body: JobRetryBody):
        from cairnmind.store import JobNotFoundError

        try:
            pid = resolve_project(ctx, body.project)["id"]
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown project") from None
        try:
            result = await asyncio.to_thread(
                write_scoped,
                ctx,
                pid,
                body.session_id,
                body.packet_id,
                lambda scope: ctx.store.retry_job(
                    pid, job_id, body.expected_attempts, include_omissions=body.include_omissions
                ),
            )
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        response = {"job": result, "retried": True}
        if result.get("kind") in {"embed", "extract"}:
            response["background_inference"] = ctx.enable_background(pid)
        return response

    @app.post("/shutdown", dependencies=auth)
    async def shutdown():
        callback = getattr(app.state, "shutdown", None)
        if callback:
            callback()
        return {"stopping": bool(callback)}


def register_project_routes(app: FastAPI, ctx: ServiceContext, auth: list[Any]) -> None:
    @app.get("/projects", dependencies=auth)
    async def projects():
        return await asyncio.to_thread(ctx.store.list_projects)

    @app.post("/projects", dependencies=auth)
    async def register_project(body: RegisterProjectBody):
        try:
            root = Path(body.root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("Project root is unavailable") from exc
        if not root.is_dir():
            raise ValueError("Project root must be an existing directory")
        from cairnmind.memory.project_removal import add_outcome, removed_rows

        known = {row["id"] for row in await asyncio.to_thread(ctx.store.list_projects)}
        removed = await asyncio.to_thread(removed_rows, ctx.store)
        created = await asyncio.to_thread(ctx.store.add_project, body.name, root, not body.fresh)
        ctx.cache.clear()
        if body.no_scip:
            scip_info = {"mode": "skipped", "detected_languages": [], "queued": False, "job_id": None}
        else:
            from cairnmind.scip.scip_ingest import enqueue_project_scip

            scip_info = await asyncio.to_thread(enqueue_project_scip, ctx.store, ctx.settings, created["id"], root)
        scip_extra = {"scip": scip_info} if (scip_info.get("queued") or scip_info.get("detected_languages")) else {}
        # Said, never silent (#327): a removed project that came back is not a new one, and one that keeps its
        # memory under this name or folder was not brought back.
        return {"project": created, **scip_extra, **add_outcome(removed, known, created)[2]}

    @app.get("/project-roots", dependencies=auth)
    async def project_roots(project: str):
        found = ctx.store.resolve_project(project)
        rows = await asyncio.to_thread(
            db_find,
            ctx.store,
            "project_roots",
            {"project_id": found["id"]},
            columns=["id", "path", "machine", "host"],
            order_by=[asc("path")],
        )
        from cairnmind.memory.project_folders import entry, folder_states

        # From the rows read above, so the answer costs no second query on a workspace that polls it.
        away = await asyncio.to_thread(
            lambda: [
                entry(found, folder)
                for folder in folder_states(ctx.store, found, rows)
                if folder["state"] != "available"
            ]
        )
        return {"roots": [row["path"] for row in rows], "unavailable": sorted(away, key=lambda i: i["root"])}

    @app.post("/ingest", dependencies=auth)
    async def ingest(body: ProjectBody):
        from cairnmind.scip.ingest import index_project

        result = await asyncio.to_thread(index_project, ctx.store, resolve_project(ctx, body.project)["id"])
        ctx.cache.clear()
        return result

    @app.post("/import", dependencies=auth)
    async def import_sources(body: ImportBody):
        from cairnmind.scip.ingest import import_history

        pid = resolve_project(ctx, body.project)["id"] if body.project else None
        result = await asyncio.to_thread(import_history, ctx.store, Path(body.path), pid, body.client)
        ctx.cache.clear()
        return result


def register_memory_routes(app: FastAPI, ctx: ServiceContext, auth: list[Any]) -> None:
    @app.post("/memory/preview", dependencies=auth)
    async def memory_preview(body: MemoryPreviewBody):
        pid = resolve_project(ctx, body.project)["id"]
        scope = await asyncio.to_thread(read_scope, ctx, pid, body.worktree, body.session_id)
        return await asyncio.to_thread(
            ctx.memory.preview,
            pid,
            scope,
            profile=body.profile,
            include_transcripts=body.include_transcripts,
            skip_uncommitted=body.skip_uncommitted,
            redact=body.redact,
        )

    @app.post("/memory/snapshot", dependencies=auth)
    async def memory_snapshot(body: MemorySnapshotExportBody):
        pid = resolve_project(ctx, body.project)["id"]
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, ctx, pid, body.worktree, body.session_id, body.packet_id
        )
        return await asyncio.to_thread(
            ctx.memory.snapshot,
            pid,
            scope,
            authorize=authorize,
            profile=body.profile,
            include_transcripts=body.include_transcripts,
            skip_uncommitted=body.skip_uncommitted,
            redact=body.redact,
        )

    @app.post("/memory/inspect", dependencies=auth)
    async def memory_inspect(body: MemoryInspectBody):
        from cairnmind.memory.memory import inspect_snapshot

        result = await asyncio.to_thread(inspect_snapshot, body.path)
        if body.session_id:
            await asyncio.to_thread(read_scope, ctx, result["project"]["id"], None, body.session_id)
        return result

    @app.post("/memory/import", dependencies=auth)
    async def memory_import(body: MemoryImportBody, request: Request):
        from cairnmind.memory.memory import inspect_snapshot

        if body.replace_confirmed and not body.dry_run and not body.session_id:
            # A request that names a session is refused below with its own sentence; it never carries the proof.
            await person_proof.require(request, ctx.settings, person_proof.IMPORT_REPLACE)
            needs_shown_list(body)
        # Bind authorization to the exact fully validated package. The importer
        # rechecks this identity, so swapping a manifest cannot change scope.
        inspected = await asyncio.to_thread(inspect_snapshot, body.path)
        pid = inspected["project"]["id"]

        def restore(scope, authorize=None):
            selected_memory_scope(pid, body.root, scope)
            person_imports(body)  # After the scope checks: a session learns of another project's memory as before.
            return ctx.memory.import_snapshot(
                body.path,
                body.root,
                body.name,
                body.dry_run,
                expected_snapshot_id=inspected["snapshot_id"],
                expected_project_id=pid,
                authorize=authorize,
                replace_confirmed=body.replace_confirmed,
                expected_replaced=body.expected_replaced,
                heal_quotes=body.heal_quotes,
            )

        if body.dry_run:
            scope = await asyncio.to_thread(read_scope, ctx, pid, body.root, body.session_id)
            return await asyncio.to_thread(restore, scope)
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, ctx, pid, body.root, body.session_id, body.packet_id
        )
        result = await asyncio.to_thread(restore, scope, authorize)
        ctx.cache.clear()
        from cairnmind.scip.ingest import forget_index_signature

        forget_index_signature(ctx.store, pid)
        return result

    @app.post("/memory/publish", dependencies=auth)
    async def memory_publish(body: MemoryPublishBody):
        # Team memory (#170): one file per confirmed record, written into the worktree; never committed.
        from cairnmind.memory.memory_published import Publisher

        pid = resolve_project(ctx, body.project)["id"]
        if body.preview:
            scope = await asyncio.to_thread(read_scope, ctx, pid, body.worktree, body.session_id)
            return await asyncio.to_thread(
                Publisher(ctx.store).publish, pid, scope, preview=True, new_package=body.new_package
            )
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, ctx, pid, body.worktree, body.session_id, body.packet_id
        )
        return await asyncio.to_thread(
            Publisher(ctx.store).publish, pid, scope, authorize=authorize, new_package=body.new_package
        )

    @app.post("/memory/carrier", dependencies=auth)
    async def memory_carrier(body: MemoryCarrierBody):
        # Where published memory goes (#173): a person's choice, never a coding session's; nothing is moved.
        from cairnmind.memory.memory_published_carrier import configure

        pid = resolve_project(ctx, body.project)["id"]
        if body.session_id:
            raise ValueError("Where published memory goes is chosen only by a person, not inside a coding session")
        worktree = str(Path(body.worktree).expanduser().resolve()) if body.worktree else None
        return await asyncio.to_thread(configure, ctx.store, pid, body.kind, body.path, worktree)

    @app.post("/memory/import-published", dependencies=auth)
    async def memory_import_published(body: MemoryImportPublishedBody, request: Request):
        if body.replace_confirmed and not body.dry_run and not body.session_id:
            # A request that names a session is refused below with its own sentence; it never carries the proof.
            await person_proof.require(request, ctx.settings, person_proof.IMPORT_REPLACE)
            needs_shown_list(body)
        from cairnmind.memory.memory_published_import import PublishedImporter

        if not Path(body.path).is_absolute():
            # Never read against the service's own working directory (#304).
            raise ValueError("The published memory folder must be given as a full path. Nothing was imported.")
        pid = resolve_project(ctx, body.project)["id"]
        authorize = None
        if (body.take_published or body.replace_confirmed) and body.session_id:
            # Putting the published side over a record that diverged here, or over one confirmed or disputed
            # here, replaces a person's own verdict (#177): that choice is a person's, never a coding
            # session's, and a session cannot preview it as if it were one.
            raise ValueError(
                "Taking the published side of a record that diverged here, or that was confirmed or "
                "disputed here, is chosen only by a person, not inside a coding session"
            )
        person_imports(body)
        if body.dry_run:
            await asyncio.to_thread(read_scope, ctx, pid, None, body.session_id)
        else:
            _, authorize = await asyncio.to_thread(
                memory_write_authorization, ctx, pid, None, body.session_id, body.packet_id
            )
        result = await asyncio.to_thread(
            PublishedImporter(ctx.store).import_published,
            body.path,
            pid,
            dry_run=body.dry_run,
            bind=body.bind,
            take_published=body.take_published,
            authorize=authorize,
            replace_confirmed=body.replace_confirmed,
            expected_replaced=body.expected_replaced,
        )
        if not body.dry_run:
            ctx.cache.clear()
        return result

    @app.post("/memory/import-commit", dependencies=auth)
    async def memory_import_commit(body: MemoryImportCommitBody, request: Request):
        if body.replace_confirmed and not body.dry_run and not body.session_id:
            # A request that names a session is refused below with its own sentence; it never carries the proof.
            await person_proof.require(request, ctx.settings, person_proof.IMPORT_REPLACE)
            needs_shown_list(body)
        # The main-branch import (#172): a commit's published memory, re-checked against that commit's code.
        from cairnmind.memory.memory_published_main import import_commit

        pid = resolve_project(ctx, body.project)["id"]
        authorize = None
        person_imports(body)
        if body.dry_run:
            scope = await asyncio.to_thread(read_scope, ctx, pid, body.worktree, body.session_id)
        else:
            scope, authorize = await asyncio.to_thread(
                memory_write_authorization, ctx, pid, body.worktree, body.session_id, body.packet_id
            )
        result = await asyncio.to_thread(
            import_commit,
            ctx.store,
            pid,
            body.revision,
            worktree=scope,
            dry_run=body.dry_run,
            bind=body.bind,
            authorize=authorize,
            replace_confirmed=body.replace_confirmed,
            expected_replaced=body.expected_replaced,
        )
        if not body.dry_run:
            ctx.cache.clear()
        return result

    @app.post("/memory/heal", dependencies=auth)
    async def memory_heal(body: MemoryHealBody):
        from cairnmind.memory.quote_healing import heal_project_quotes

        pid = resolve_project(ctx, body.project)["id"]
        if body.dry_run:
            scope = await asyncio.to_thread(read_scope, ctx, pid, body.worktree, body.session_id)
            return await asyncio.to_thread(
                heal_project_quotes,
                ctx.store,
                pid,
                scope,
                target_commit=body.target_commit,
                dry_run=True,
                expected_versions=body.expected_versions,
                expected_snapshot_id=body.expected_snapshot_id,
                record_ids=body.record_ids,
                actor=body.session_id or "service_api",
            )
        scope, authorize = await asyncio.to_thread(
            memory_write_authorization, ctx, pid, body.worktree, body.session_id, body.packet_id
        )
        if authorize is not None:
            if hasattr(authorize, "refresh"):
                await asyncio.to_thread(authorize.refresh)
            await asyncio.to_thread(authorize)
        result = await asyncio.to_thread(
            heal_project_quotes,
            ctx.store,
            pid,
            scope,
            target_commit=body.target_commit,
            dry_run=False,
            expected_versions=body.expected_versions,
            expected_snapshot_id=body.expected_snapshot_id,
            record_ids=body.record_ids,
            actor=body.session_id or "service_api",
        )
        ctx.cache.clear()
        return result

    @app.get("/memory/heal-audit", dependencies=auth)
    async def memory_heal_audit(
        project: str,
        record_id: str | None = None,
        limit: int = 100,
        session_id: str | None = None,
    ):
        from cairnmind.memory.quote_healing import healing_audit_log

        pid = ctx.store.resolve_project(project)["id"]
        await asyncio.to_thread(read_scope, ctx, pid, None, session_id)
        items = await asyncio.to_thread(healing_audit_log, ctx.store, pid, record_id=record_id, limit=limit)
        return {"project_id": pid, "items": items, "total": len(items)}

    @app.get("/memory/status", dependencies=auth)
    async def memory_status(project: str, worktree: str | None = None, session_id: str | None = None):
        pid = ctx.store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, ctx, pid, worktree, session_id)
        result = await asyncio.to_thread(ctx.memory.status, pid, scope)
        from cairnmind.memory.memory_published import publication_status

        result["published"] = await asyncio.to_thread(publication_status, ctx.store, pid, scope)
        return result

    @app.patch("/memory/settings", dependencies=auth)
    async def memory_settings(body: MemorySettingsBody):
        pid = resolve_project(ctx, body.project)["id"]
        return await asyncio.to_thread(
            write_scoped,
            ctx,
            pid,
            body.session_id,
            body.packet_id,
            lambda scope: ctx.memory.configure_auto(
                pid,
                body.enabled,
                body.interval_minutes,
                selected_memory_scope(pid, body.worktree, scope),
            ),
        )


def register_plan_routes(app: FastAPI, ctx: ServiceContext, auth: list[Any]) -> None:
    @app.get("/plans", dependencies=auth)
    async def list_plans(project: str):
        return {"plans": await asyncio.to_thread(ctx.plans.list, ctx.store.resolve_project(project)["id"])}

    @app.get("/plans/{plan_id}", dependencies=auth)
    async def get_plan(plan_id: str, project: str):
        return await asyncio.to_thread(ctx.plans.get, ctx.store.resolve_project(project)["id"], plan_id)

    @app.post("/plans/save", dependencies=auth)
    async def save_plan(body: PlanSaveBody):
        pid = resolve_project(ctx, body.project)["id"]

        def save(scope):
            if scope and body.worktree and str(Path(body.worktree).expanduser().resolve()) != scope:
                raise ValueError("resume_required: worktree does not match the session")
            return ctx.plans.save(
                pid,
                body.plan,
                body.steps,
                body.idempotency_key,
                body.expected_version,
                body.task_versions,
                scope or body.worktree,
                body.activate,
            )

        result = await asyncio.to_thread(write_scoped, ctx, pid, body.session_id, body.packet_id, save)
        ctx.cache.clear()
        return result

    @app.post("/plans/next", dependencies=auth)
    async def next_task(body: PlanNextBody):
        pid = resolve_project(ctx, body.project)["id"]
        scope = await asyncio.to_thread(read_scope, ctx, pid, body.worktree, body.session_id)

        def read():
            result = ctx.plans.next_task(pid, body.plan_id, scope)
            if result.get("task") and result["task"].get("metadata", {}).get("scope"):
                from cairnmind.memory.scope import drift_warning, scope_drift

                warning = drift_warning(scope_drift(ctx.store, result["task"], scope))
                if warning:
                    result["warnings"] = [warning]
            return result

        return await asyncio.to_thread(read)

    @app.post("/plans/activate", dependencies=auth)
    async def activate_plan(body: PlanActivateBody):
        pid = resolve_project(ctx, body.project)["id"]

        def activate(scope):
            if scope and body.worktree and str(Path(body.worktree).expanduser().resolve()) != scope:
                raise ValueError("resume_required: worktree does not match the session")
            return ctx.plans.activate(pid, body.plan_id, scope or body.worktree)

        result = await asyncio.to_thread(write_scoped, ctx, pid, body.session_id, body.packet_id, activate)
        ctx.cache.clear()
        return result


def register_session_routes(app: FastAPI, ctx: ServiceContext, auth: list[Any]) -> None:
    @app.post("/sessions/prepare", dependencies=auth)
    async def prepare_session(body: SessionPrepareBody):
        from cairnmind.memory.integrations import drain_hook_spool

        await asyncio.to_thread(drain_hook_spool, ctx.settings, ctx.store)
        return await asyncio.to_thread(
            ctx.sessions.prepare,
            resolve_project(ctx, body.project)["id"],
            body.client,
            body.worktree,
            body.session_id,
            body.reason,
            held_classes=body.held_classes,
        )

    @app.post("/sessions/context", dependencies=auth)
    async def resume_context(body: SessionContextBody):
        from cairnmind.memory.integrations import drain_hook_spool

        await asyncio.to_thread(drain_hook_spool, ctx.settings, ctx.store)
        if (
            body.packet_id is not None
            and body.expected_packet_id is not None
            and body.packet_id != body.expected_packet_id
        ):
            raise ValueError("resume_required: conflicting requested packet identities")
        expected = body.packet_id if body.packet_id is not None else body.expected_packet_id
        if expected is not None:
            if body.short:
                raise ValueError(
                    "short is only for a read of the current packet; an exact packet read "
                    "always returns the whole packet"
                )
            return await asyncio.to_thread(ctx.sessions.exact_context, body.session_id, expected)
        return await asyncio.to_thread(ctx.sessions.resume_context, body.session_id, body.short)

    @app.post("/sessions/ack", dependencies=auth)
    async def acknowledge_resume(body: SessionAckBody):
        from cairnmind.memory.integrations import drain_hook_spool

        await asyncio.to_thread(drain_hook_spool, ctx.settings, ctx.store)
        return await asyncio.to_thread(
            ctx.sessions.ack, body.session_id, body.packet_id, body.packet_hash, body.readback
        )

    @app.post("/sessions/agent/archive", dependencies=auth)
    async def archive_agent_session(body: AgentArchiveBody):
        # The body selects only a bound logical session. Paths, events and
        # claims of successful archiving are never supplied by the caller.
        result = await asyncio.to_thread(ctx.sessions.archive_agent_journal, body.session_id)
        ctx.cache.clear()
        return result

    @app.get("/sessions/{session_id}", dependencies=auth)
    async def session_status(session_id: str):
        return await asyncio.to_thread(ctx.sessions.status, session_id)


def register_core_routes(app: FastAPI, ctx: ServiceContext, auth: list[Any]) -> None:
    @app.get("/context", dependencies=auth)
    async def context(project: str, worktree: str | None = None, session_id: str | None = None):
        pid = ctx.store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, ctx, pid, worktree, session_id)
        return await asyncio.to_thread(ctx.retriever.context, pid, scope)

    @app.get("/brief", dependencies=auth)
    async def brief(project: str, worktree: str | None = None, session_id: str | None = None):
        # Built from stored rows and Git only; this route never reaches the model queue.
        from cairnmind.memory.brief import build_brief

        pid = ctx.store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, ctx, pid, worktree, session_id)
        return await asyncio.to_thread(build_brief, ctx.store, pid, scope)

    @app.get("/checks", dependencies=auth)
    async def checks(
        project: str,
        task_id: str | None = None,
        worktree: str | None = None,
        session_id: str | None = None,
    ):
        from cairnmind.memory.verification import recover_interrupted_runs, verification_status

        pid = ctx.store.resolve_project(project)["id"]
        scope = await asyncio.to_thread(read_scope, ctx, pid, worktree, session_id)
        await asyncio.to_thread(recover_interrupted_runs, ctx.store, ctx.settings)
        check_where: dict[str, Any] = {"project_id": pid}
        if task_id:
            check_where["task_id"] = task_id
        if scope:
            check_where["worktree"] = scope
        rows = await asyncio.to_thread(
            db_find,
            ctx.store,
            "check_runs",
            check_where,
            order_by=[desc("ended_at"), asc("id")],
            limit=100,
        )
        fields = (
            "id",
            "project_id",
            "task_id",
            "profile_id",
            "worktree",
            "outcome",
            "passed",
            "returncode",
            "started_at",
            "ended_at",
            "duration_seconds",
            "source_ids",
            "reasons",
            "profile_hash",
            "task_definition_hash",
            "origin",
        )
        result = {"checks": [{key: row.get(key) for key in fields if key in row} for row in rows]}
        if task_id:
            task = await asyncio.to_thread(
                db_find_one,
                ctx.store,
                "records",
                {"id": task_id, "project_id": pid, "kind": "task"},
            )
            if not task:
                raise ValueError("Task does not exist in this project")
            result["verification"] = await asyncio.to_thread(verification_status, ctx.store, task, scope)
        return result

    @app.post("/ask", dependencies=auth)
    async def ask(body: AskBody, background: BackgroundTasks):
        from cairnmind.agents import knowledge_mode
        from cairnmind.agents.answer_contract import prepare_answer_schema
        from cairnmind.memory.verification import capture_code_state, load_profiles
        from cairnmind.scip.ingest import index_pending_sources, index_project_if_changed

        pid = resolve_project(ctx, body.project)["id"]
        answer_schema = prepare_answer_schema(body.answer_schema)
        contract_key = json.dumps(answer_schema, sort_keys=True, separators=(",", ":"))
        scope = await asyncio.to_thread(read_scope, ctx, pid, body.worktree, body.session_id)
        also_held = frozenset(body.held_classes or ())
        if not ctx.models_on:
            # No model is asked; full-text matches, never an answer: not cached, logged or evaluated (#274).
            indexed = await asyncio.to_thread(index_project_if_changed, ctx.store, pid)
            pending = await asyncio.to_thread(index_pending_sources, ctx.store)
            try:
                found = await asyncio.wait_for(
                    ctx.inference.submit(
                        lambda: knowledge_mode.text_matches(ctx.agent, pid, body.question, scope, also_held)
                    ),
                    timeout=ctx.settings.ask_timeout + 5,
                )
            except TimeoutError:  # bounded as the models path is; the timeout is said, not hidden
                found = {
                    "matches": [],
                    "held_back": None,
                    "left_out": False,
                    "timed_out": True,
                    "warnings": [],
                    "elapsed_seconds": ctx.settings.ask_timeout + 5,
                }
            return knowledge_mode.unavailable_answer(found, [*indexed.get("errors", []), *pending.get("errors", [])])
        keep = body.log if body.log is not None else getattr(ctx.settings, "answer_log", False) is True
        code_state: dict = {}

        def code_identity():
            try:
                profiles = load_profiles(ctx.store, pid, scope)
                inputs = sorted({path for profile in profiles.values() for path in profile["extra_inputs"]})
                code = capture_code_state(ctx.store, pid, scope, inputs)
                code_state.update(code)
                if code["status"] != "fresh":
                    return None
                return code["fingerprint"], tuple(sorted((name, p["hash"]) for name, p in profiles.items()))
            except (ValueError, OSError):
                return None

        identity = await asyncio.to_thread(code_identity)
        # An unchanged tree is not indexed again (the code identity above still hashes the
        # worktree); the cache stamp below is a counter, not a scan.
        indexed = await asyncio.to_thread(index_project_if_changed, ctx.store, pid)
        index_errors = indexed.get("errors", [])
        pending = await asyncio.to_thread(index_pending_sources, ctx.store)
        index_errors.extend(pending.get("errors", []))
        stamp = await asyncio.to_thread(ctx.store.cache_stamp, pid)
        key = (pid, body.question, scope, stamp, identity, contract_key, also_held, body.verifier)
        hit = ctx.cache.get(key)
        if hit and identity is not None and not index_errors and time.monotonic() - hit[0] < 60:
            if keep:
                background.add_task(
                    log_answer, ctx, pid, body, scope, answer_schema, code_state, {**hit[1], "cached": True}
                )
            return {**hit[1], "cached": True}
        try:
            result = await asyncio.wait_for(
                ctx.inference.submit(
                    lambda: ctx.agent.ask(
                        pid,
                        body.question,
                        worktree=scope,
                        answer_schema=answer_schema,
                        also_held=also_held,
                        verifier=body.verifier,
                    ),
                    priority=0,
                ),
                timeout=ctx.settings.ask_timeout + 5,
            )
        except TimeoutError:
            result = {
                "status": "partial",
                "answer": "Waiting for the local agent timed out.",
                "sources": [],
                "unknowns": ["The queue or the analysis exceeded the time limit."],
                "freshness": {},
                "elapsed_seconds": ctx.settings.ask_timeout + 5,
                "cached": False,
                "diagnostics": {
                    "stage": "ask",
                    "code": "queue_timeout",
                    "budget": {"timeout_seconds": ctx.settings.ask_timeout + 5},
                },
            }
            if keep:
                background.add_task(log_answer, ctx, pid, body, scope, answer_schema, code_state, result)
            return result
        if index_errors:
            result["unknowns"] = [*result.get("unknowns", []), *map(str, index_errors[:5])]
            result.setdefault("freshness", {}).setdefault("warnings", []).extend(map(str, index_errors[:5]))
            if result.get("status") == "complete":
                result["status"] = "partial"
        if result.get("status") == "complete" and identity is not None:
            if len(ctx.cache) >= 128:
                ctx.cache.pop(next(iter(ctx.cache)))
            ctx.cache[key] = (time.monotonic(), result)
        if keep:
            background.add_task(
                log_answer, ctx, pid, body, scope, answer_schema, code_state, {**result, "cached": False}
            )
        return {**result, "cached": False}

    @app.post("/remember", dependencies=auth)
    async def remember(body: RememberBody):
        from cairnmind.memory.remember_input import external_report_source, record_error

        error = record_error(body.record)
        if error:
            # A plain 400: the word-based mapping of ValueError would read a named field as a conflict. Marked,
            # so that the built-in agent hands the model which field is wrong (#240).
            return JSONResponse(status_code=400, content={"detail": error, "refusal": "record"})
        pid = resolve_project(ctx, body.project)["id"]

        def save(scope):
            record = dict(body.record)
            if scope:
                requested = record.get("worktree")
                if requested and str(Path(requested).expanduser().resolve()) != scope:
                    raise ValueError("resume_required: completion scope differs from session")
                record["worktree"] = scope
            sources = body.sources
            if not sources and (ctx.store.current_origin() or {}).get("origin_client") == "external":
                # An external session has no archived conversation to cite (README, external sessions).
                sources = [external_report_source(ctx.store, pid, record)]
            return ctx.store.remember(pid, record, sources)

        result = await asyncio.to_thread(write_scoped, ctx, pid, body.session_id, body.packet_id, save)
        ctx.cache.clear()
        return await asyncio.to_thread(with_dropped, ctx, result) if body.name_dropped else result

    @app.post("/findings", dependencies=auth)
    async def finding(body: FindingBody, request: Request):
        """A finding with a source created here, so it cannot claim another origin's id.

        Without a session it is a person's own write (origin workspace_form); with one, it is the
        session's (origin session_finding), after the same resume gate as /remember.
        """
        from cairnmind.memory.findings import finding_error, key_error, save_finding

        if not body.session_id:
            # The workspace form is a person's act (#214): it needs the open workspace's key, and nothing is written
            # without it, a proposed finding included.
            await person_proof.require(request, ctx.settings)
        selected = resolve_project(ctx, body.project)
        pid = selected["id"]
        error = finding_error(body.content, body.kind, body.status) or key_error(body.key)
        if error:
            raise ValueError(error)

        def save(scope):
            # write_scoped calls this only after an acknowledged session check when a session is named.
            # The worktree only checks that the caller's view is genuine; the finding is project-wide.
            requested = selected_memory_scope(pid, body.worktree, scope) or selected["root"]
            root = str(Path(requested).expanduser().resolve())
            if not db_find_one(ctx.store, "project_roots", {"project_id": pid, "path": root}, columns=["path"]):
                raise ValueError("A finding needs a registered project worktree")
            return save_finding(
                ctx.store,
                pid,
                body.key,
                body.content,
                body.kind,
                body.status,
                session_id=body.session_id or None,
                worktree=root,
            )

        result = await asyncio.to_thread(write_scoped, ctx, pid, body.session_id, body.packet_id, save)
        ctx.cache.clear()
        return result

    @app.post("/checkpoint", dependencies=auth)
    async def checkpoint(body: CheckpointBody):
        pid = resolve_project(ctx, body.project)["id"]
        result = await asyncio.to_thread(
            write_scoped,
            ctx,
            pid,
            body.session_id,
            body.packet_id,
            lambda scope: ctx.store.checkpoint(pid, body.state),
        )
        ctx.cache.clear()
        return await asyncio.to_thread(with_dropped, ctx, result) if body.name_dropped else result


def register_all_routes(app: FastAPI, ctx: ServiceContext) -> None:
    """Register all middleware, exception handlers, domain routes, and read-only sub-apps."""
    auth = make_auth_dependency(ctx)
    register_middleware_and_handlers(app, ctx)
    register_admin_routes(app, ctx, auth)
    register_project_routes(app, ctx, auth)
    register_memory_routes(app, ctx, auth)
    register_plan_routes(app, ctx, auth)
    register_session_routes(app, ctx, auth)
    register_core_routes(app, ctx, auth)

    from cairnmind.terminal.inspector_api import attach_inspector_routes
    from cairnmind.ui.ui_server import attach_ui_routes

    attach_inspector_routes(app, ctx.store, ctx.settings)
    # Main service mounts UI routes strictly read-only so service-token-only callers can never bypass person_proof.
    attach_ui_routes(app, ctx.store, ctx.settings, dependencies=auth, read_only=True)
