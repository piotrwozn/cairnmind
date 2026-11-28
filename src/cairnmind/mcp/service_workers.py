"""Single-owner inference queue, service runtime context, and background workers."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairnmind.config import BACKUP_SUFFIXES, Settings
from cairnmind.storage.storage_query import db_find_one
from cairnmind.storage.store_backup import stale_temporaries

log = logging.getLogger(__name__)
DEFAULT_WRITER_LOST_RETRY_SECONDS = 30


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
                current = asyncio.current_task()
                if current is not None and current.cancelling():
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


def compact_job_history(store, settings) -> dict | None:
    """The daily compaction of old completed jobs (store_prune.compact_old_jobs); None when the setting turns it off."""
    from cairnmind.storage.store_prune import compact_old_jobs, valid_days

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
            written = path.name[: -len(suffix)]
            if written.isdigit():
                found.append((int(written), path.name, path))
    return [path for _, _, path in sorted(found)]


def schedule_embeddings(
    store,
    namespace: str,
    *,
    project_ids=None,
    settings=None,
    report=None,
    known_held=None,
) -> int:
    """Repair missing/digest-changed derived vectors without touching archived evidence.

    With ``settings``, chunks a remote embedding server may not receive are not queued (#181); their
    number goes to ``report["embedding_held_back"]``. They are queued by the first repair that allows them.
    ``known_held`` keeps the held chunks between repairs (memory_egress.held_chunk_ids).
    """
    rows = (
        store.missing_embeddings(namespace)
        if project_ids is None
        else [row for pid in project_ids for row in store.missing_embeddings(namespace, pid)]
    )
    from cairnmind.memory.project_removal import removed_ids

    removed = removed_ids(store)  # no embedding job is queued for a removed project (#327)
    rows = [row for row in rows if row["project_id"] not in removed]
    if settings is not None:
        from cairnmind.memory.memory_egress import held_chunk_ids

        held = held_chunk_ids(store, settings, rows, known_held)
        rows = [row for row in rows if row["id"] not in held]
        if report is not None:
            report["embedding_held_back"] = len(held)
    from cairnmind.storage.embed_jobs import repair_embed_jobs

    # One job per chunk under the key ingest uses serves a new chunk, a changed namespace and a rebuild.
    repair_embed_jobs(store, rows, namespace)
    return len(rows)


def finish_job_if_present(
    store,
    job_id: str,
    error: str | None = None,
    *,
    worker=None,
    expected_attempts=None,
) -> None:
    """Project deletion cancels in-flight work instead of creating a model failure."""
    try:
        store.finish_job(job_id, error, worker=worker, expected_attempts=expected_attempts)
    except ValueError:
        current = db_find_one(store, "jobs", {"id": job_id}, columns=["state", "worker", "attempts"])
        if not current:
            return
        if (
            expected_attempts is not None
            and (current.get("attempts", 0) != expected_attempts or current["state"] != "running")
        ) or (worker is not None and (current.get("worker") != worker or current["state"] != "running")):
            return  # A superseded lease cannot complete or fail its replacement.
        raise


@dataclass
class ServiceContext:
    """Shared mutable state and coordination helpers for the local knowledge service."""

    settings: Settings
    store: Any
    model: Any
    retriever: Any
    agent: Any
    plans: Any
    sessions: Any
    memory: Any
    inference: InferenceQueue
    models_on: bool
    start_worker: bool
    background_inference: bool
    fingerprint_client: Any = None
    background_status: dict[str, Any] = field(
        default_factory=lambda: {
            "scanner_error": None,
            "model_error": None,
            "memory_error": None,
            "scip_error": None,
        }
    )
    cache: dict[Any, Any] = field(default_factory=dict)
    inference_projects: set[str] = field(default_factory=set)
    background_ready: asyncio.Event = field(default_factory=asyncio.Event)
    retry_seconds_fn: Callable[[], float] = field(default=lambda: DEFAULT_WRITER_LOST_RETRY_SECONDS)
    schedule_embeddings_fn: Callable[..., int] = field(default=lambda *a, **kw: schedule_embeddings(*a, **kw))

    def __post_init__(self) -> None:
        if self.background_inference:
            self.background_ready.set()

    def active_backend_label(self) -> str:
        kind = getattr(getattr(self.store, "backend", None), "backend_kind", "arcade")
        return "sqlite" if kind == "sqlite" else "arcadedb"

    async def writer_lost(self, exc: BaseException) -> None:
        """Pause background work when another installation takes over the writer role."""
        # Each retry costs one read of the writer row (store_writer.recheck); when a person takes writing back
        # here, the next pass writes again, so the service needs no restart.
        if self.background_status.get("writer_lost") != str(exc):
            log.warning("Background work paused: %s", exc)
        self.background_status["writer_lost"] = str(exc)
        await asyncio.sleep(self.retry_seconds_fn())

    def writer_back(self) -> None:
        if self.background_status.pop("writer_lost", None):
            log.warning("Background work resumed: this installation writes the store again")

    def inference_status(self) -> dict[str, Any]:
        running = self.start_worker and self.models_on  # without models nothing processes memory, so none is claimed
        return {
            "worker_running": running,
            "mode": (
                "disabled"
                if not running
                else "all_projects"
                if self.background_inference
                else "selected_projects"
                if self.inference_projects
                else "deferred"
            ),
            "project_ids": sorted(self.inference_projects),
        }

    def enable_background(self, pid: str) -> dict[str, Any]:
        # Called only on the event-loop thread, after scoped authorization succeeds.
        self.inference_projects.add(pid)
        self.background_ready.set()
        return {"accepted": True, "project_id": pid, **self.inference_status()}


async def writer_lost(ctx: ServiceContext, exc: BaseException) -> None:
    await ctx.writer_lost(exc)


def writer_back(ctx: ServiceContext) -> None:
    ctx.writer_back()


def inference_status(ctx: ServiceContext) -> dict[str, Any]:
    return ctx.inference_status()


def enable_background(ctx: ServiceContext, pid: str) -> dict[str, Any]:
    return ctx.enable_background(pid)


async def run_memory_snapshots(ctx: ServiceContext) -> None:
    """Filesystem snapshots have their own clock and never enter the GPU queue."""
    from cairnmind.storage.store_writer import StoreNotWriter

    # tick() coalesces missed intervals and preserves each last good manifest.
    while True:
        try:
            result = await asyncio.to_thread(ctx.memory.tick)
            ctx.writer_back()
            ctx.background_status["memory_error"] = result.get("errors") or None
        except asyncio.CancelledError:
            raise
        except StoreNotWriter as exc:
            await ctx.writer_lost(exc)
            continue
        except Exception as exc:
            ctx.background_status["memory_error"] = {"code": type(exc).__name__}
            log.exception("Portable memory scheduler failed")
        await asyncio.sleep(min(60, max(1, ctx.settings.poll_seconds)))


async def run_scanner(ctx: ServiceContext) -> None:
    """Poll hooks, imports, repository changes, recovery, and daily backups."""
    from cairnmind.scip.ingest import (
        forget_missing_projects,
        import_history,
        index_pending_sources,
        index_project_if_changed,
        poll_imports,
    )
    from cairnmind.storage.arcade import ArcadeError
    from cairnmind.storage.store_writer import StoreNotWriter

    while True:
        try:
            from cairnmind.memory.integrations import drain_hook_spool

            scan_errors: list[str] = []
            drained = await asyncio.to_thread(drain_hook_spool, ctx.settings, ctx.store)
            scan_errors.extend(f"hook: {err}" for err in drained.get("errors", []))
            # CPU/archive jobs never wait for the model, including end-hook spools.
            for _ in range(20):
                job = await asyncio.to_thread(ctx.store.claim_job, "archive", 120, ["import"])
                if not job:
                    break
                try:
                    payload = job["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    imported = await asyncio.to_thread(
                        import_history,
                        ctx.store,
                        Path(payload["path"]),
                        job["project_id"],
                        payload.get("client", "auto"),
                    )
                    errors = imported.get("errors", [])
                    scan_errors.extend(f"import: {err}" for err in errors)
                    await asyncio.to_thread(
                        finish_job_if_present,
                        ctx.store,
                        job["id"],
                        "; ".join(map(str, errors))[:2000] if errors else None,
                        worker=job["worker"],
                        expected_attempts=job["attempts"],
                    )
                except Exception as exc:
                    log.warning("Import job %s failed: %s", job["id"], exc)
                    await asyncio.to_thread(
                        finish_job_if_present,
                        ctx.store,
                        job["id"],
                        str(exc),
                        worker=job["worker"],
                        expected_attempts=job["attempts"],
                    )
            polled = await asyncio.to_thread(poll_imports, ctx.store)
            scan_errors.extend(f"history: {err}" for err in polled.get("errors", []))
            projects = await asyncio.to_thread(ctx.store.list_projects)
            forget_missing_projects(ctx.store, [project["id"] for project in projects])
            for project in projects:
                # One project that fails does not stop the round for the projects after it (#371); the store
                # itself being down is one error of the round, not one traceback per project.
                try:
                    indexed = await asyncio.to_thread(index_project_if_changed, ctx.store, project["id"])
                except (asyncio.CancelledError, StoreNotWriter):
                    raise
                except Exception as exc:
                    if isinstance(exc, ArcadeError) and exc.status_code is None:
                        raise  # no answer at all: the store is down for every project, not for this one
                    scan_errors.append(f"{project['name']}: {exc}")
                    log.exception("Indexing project %s failed", project["name"])
                    continue
                scan_errors.extend(f"{project['name']}: {err}" for err in indexed.get("errors", []))
                if indexed.get("changed", 0) > 0 and ctx.settings.scip_mode == "auto":
                    try:
                        from cairnmind.scip.ingest import tree_signature
                        from cairnmind.scip.scip_ingest import enqueue_project_scip

                        sig = await asyncio.to_thread(tree_signature, ctx.store, project["id"])
                        await asyncio.to_thread(
                            enqueue_project_scip,
                            ctx.store,
                            ctx.settings,
                            project["id"],
                            Path(project["root"]),
                            dedupe_suffix=f"tree:{sig[:16]}",
                        )
                    except (asyncio.CancelledError, StoreNotWriter):
                        raise
                    except Exception as exc:
                        log.warning("Failed to enqueue SCIP refresh for %s: %s", project["name"], exc)
            pending = await asyncio.to_thread(index_pending_sources, ctx.store)
            scan_errors.extend(f"memory index: {err}" for err in pending.get("errors", []))
            from cairnmind.memory.verification import recover_interrupted_runs

            await asyncio.to_thread(recover_interrupted_runs, ctx.store, ctx.settings)
            backup_dir = ctx.settings.data_dir / "backups"
            # A backup killed midway leaves its temporary file; one a day old is no longer being written.
            for stale in stale_temporaries(backup_dir, 86400):
                stale.unlink(missing_ok=True)
            backups = daily_backups(backup_dir)
            if not backups or time.time() - backups[-1].stat().st_mtime > 86400:
                backup_dir.mkdir(exist_ok=True)
                await asyncio.to_thread(ctx.store.backup, backup_dir / f"{time.time_ns()}{BACKUP_SUFFIXES[0]}")
                for old in daily_backups(backup_dir)[:-7]:
                    old.unlink()
                # Once a day, right after the backup that still holds it: the attempt history of old
                # completed jobs goes; every job row stays (#190, store_prune).
                await asyncio.to_thread(compact_job_history, ctx.store, ctx.settings)
            ctx.background_status["scanner_error"] = "; ".join(scan_errors[:5]) or None
            ctx.background_status["scan_errors"] = scan_errors[:20]
            ctx.background_status["last_scan_at"] = time.time()
            ctx.writer_back()
        except asyncio.CancelledError:
            raise
        except StoreNotWriter as exc:
            await ctx.writer_lost(exc)
            continue
        except Exception as exc:
            ctx.background_status["scanner_error"] = str(exc)
            log.exception("Archive scan failed")
        await asyncio.sleep(ctx.settings.poll_seconds)


async def run_scip_worker(ctx: ServiceContext) -> None:
    """Dedicated background worker for multi-language SCIP indexing jobs (never blocks scanner or GPU)."""
    if ctx.settings.scip_mode == "off":
        return
    from cairnmind.scip.scip_ingest import index_project_scip
    from cairnmind.storage.store_writer import StoreNotWriter

    lease_seconds = max(120, int(ctx.settings.scip_timeout) + 60)
    while True:
        try:
            job = await asyncio.to_thread(ctx.store.claim_job, "scip", lease_seconds, ["scip_index"])
            ctx.writer_back()
            if not job:
                await asyncio.sleep(min(10, max(1, ctx.settings.poll_seconds)))
                continue
            try:
                payload = job.get("payload") or {}
                if isinstance(payload, str):
                    payload = json.loads(payload)
                result = await asyncio.to_thread(
                    index_project_scip,
                    ctx.store,
                    ctx.settings,
                    job["project_id"],
                    languages=payload.get("languages"),
                    worktree=payload.get("root"),
                )
                errors = result.get("errors") or []
                ctx.background_status["scip_error"] = "; ".join(errors[:5]) or None
                await asyncio.to_thread(
                    finish_job_if_present,
                    ctx.store,
                    job["id"],
                    "; ".join(map(str, errors))[:2000] if errors else None,
                    worker=job["worker"],
                    expected_attempts=job["attempts"],
                )
            except Exception as exc:
                log.warning("SCIP job %s failed: %s", job["id"], exc)
                ctx.background_status["scip_error"] = str(exc) or type(exc).__name__
                await asyncio.to_thread(
                    finish_job_if_present,
                    ctx.store,
                    job["id"],
                    str(exc) or type(exc).__name__,
                    worker=job["worker"],
                    expected_attempts=job["attempts"],
                )
        except asyncio.CancelledError:
            raise
        except StoreNotWriter as exc:
            await ctx.writer_lost(exc)
            continue
        except Exception as exc:
            ctx.background_status["scip_error"] = str(exc)
            log.exception("SCIP worker failed")
            await asyncio.sleep(10)


async def run_fingerprint_start(ctx: ServiceContext) -> None:
    """The fixed request to an OpenAI-compatible embedding role at start (#212), retried while it is down.

    It holds no memory content, so it needs no consent beyond the role's own: search stays text-only
    and embed jobs wait until it answers. Unlike other model calls it does not wait for background work
    to be allowed: in the deferred mode (the cockpit) search would otherwise stay text-only until a
    project allows it. Nor does it enter the inference queue: a server that hangs on it would hold every
    extraction and question behind it, often on another server, at every retry.
    """
    from cairnmind.memory import memory_fingerprint

    while True:
        try:
            result = await memory_fingerprint.check(ctx.fingerprint_client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a server that is down is checked again later
            log.debug("Embedding fingerprint check deferred (%s); retrying", type(exc).__name__)
            ctx.background_status["embedding_fingerprint"] = {"status": "unchecked", "error": type(exc).__name__}
            await asyncio.sleep(30)
            continue
        ctx.background_status["embedding_fingerprint"] = result
        if result["status"] == "changed":
            log.warning("The embedding model answers the fixed check differently: run cairnmind rebuild-vectors")
        return


async def run_model_worker(ctx: ServiceContext) -> None:
    """Claim and run embed and extraction jobs through the single-owner inference queue."""
    from cairnmind.agents.agent import extraction_outer_timeout, inference_lease_seconds
    from cairnmind.agents.ollama import OllamaUnavailable
    from cairnmind.memory import memory_fingerprint
    from cairnmind.memory.memory_models import ready_kinds
    from cairnmind.storage.store_writer import StoreNotWriter

    last_namespace, last_repair, last_projects = None, 0.0, None
    released, known_held = False, {}
    while True:
        try:
            # Opening a UI or acknowledging context does not authorize model work.
            await ctx.background_ready.wait()
            if not released:
                # Extractions held back for a remote chat server whose class is allowed now (#181).
                from cairnmind.memory.memory_egress import release_held_extractions

                await asyncio.to_thread(release_held_extractions, ctx.store, ctx.settings)
                released = True
            selected = None if ctx.background_inference else tuple(sorted(ctx.inference_projects))
            # Keep pending work pending when Ollama is offline, instead of failing the whole queue.
            # With split model servers, each job kind waits only for the server of its own role.
            kinds, role_error = await ready_kinds(ctx.model)
            identity = getattr(ctx.model, "model_identity", None)
            namespace = None
            if identity and "embed" in kinds:
                try:
                    namespace = await identity(ctx.settings.embedding_model)
                except OllamaUnavailable as exc:
                    if exc.error_code not in (memory_fingerprint.CHANGED, memory_fingerprint.UNCHECKED):
                        raise
                    # Other weights behind the same name, or not checked yet: embed jobs wait (#212).
                    kinds = [kind for kind in kinds if kind != "embed"]
                    role_error = role_error or exc
            if namespace is not None and (
                namespace != last_namespace
                or selected != last_projects
                or time.monotonic() - last_repair > 60
            ):
                held: dict = {}
                missing = await asyncio.to_thread(
                    ctx.schedule_embeddings_fn,
                    ctx.store,
                    namespace,
                    project_ids=selected,
                    settings=ctx.settings,
                    report=held,
                    known_held=known_held,
                )
                ctx.background_status.update(embedding_namespace=namespace, embedding_missing=missing, **held)
                last_namespace, last_repair, last_projects = namespace, time.monotonic(), selected
            ctx.background_status["model_error"] = str(role_error) if role_error else None
            job = await asyncio.to_thread(
                ctx.store.claim_job,
                "inference",
                inference_lease_seconds(),
                kinds,
                project_ids=selected,
            )
            ctx.writer_back()
            if job:
                try:

                    async def process(current_job=job):
                        if current_job["kind"] == "embed":
                            return await ctx.retriever.embed_pending(current_job)
                        return await ctx.agent.extract(current_job)

                    timeout = 30 if job["kind"] == "embed" else extraction_outer_timeout()
                    await ctx.inference.submit(
                        lambda proc=process, limit=timeout: asyncio.wait_for(proc(), limit),
                        priority=10,
                    )
                    await asyncio.to_thread(
                        finish_job_if_present,
                        ctx.store,
                        job["id"],
                        worker=job["worker"],
                        expected_attempts=job["attempts"],
                    )
                except Exception as exc:
                    log.warning("Model job %s (%s) failed: %s", job["id"], job["kind"], exc)
                    await asyncio.to_thread(
                        finish_job_if_present,
                        ctx.store,
                        job["id"],
                        str(exc) or type(exc).__name__,
                        worker=job["worker"],
                        expected_attempts=job["attempts"],
                    )
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except StoreNotWriter as exc:
            await ctx.writer_lost(exc)
            continue
        except Exception as exc:
            ctx.background_status["model_error"] = str(exc)
            log.exception("Model worker loop failed")
            await asyncio.sleep(10)
