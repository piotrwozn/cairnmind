"""Authenticated local knowledge service and single-owner inference scheduler."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cairnmind import __version__
from cairnmind.cli_cmds.service_version import instance_id  # noqa: F401 - moved there (#319); cli and tests use it from here
from cairnmind.config import Settings
from cairnmind.mcp.service_models import (  # noqa: F401 - re-exported for callers and tests
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
    MemoryScopeBody,
    MemorySettingsBody,
    MemorySnapshotBody,
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
from cairnmind.mcp.service_routes import register_all_routes
from cairnmind.mcp.service_workers import (  # noqa: F401 - re-exported for callers and tests
    DEFAULT_WRITER_LOST_RETRY_SECONDS,
    InferenceQueue,
    ServiceContext,
    compact_job_history,
    daily_backups,
    finish_job_if_present,
    run_fingerprint_start,
    run_memory_snapshots,
    run_model_worker,
    run_scanner,
    run_scip_worker,
    schedule_embeddings,
)
from cairnmind.storage.store_backup import stale_temporaries  # noqa: F401 - re-exported for callers and tests

# How often background work that lost the writer role looks again whether a person took it back (#70).
WRITER_LOST_RETRY_SECONDS = DEFAULT_WRITER_LOST_RETRY_SECONDS


def create_app(
    settings: Settings | None = None,
    *,
    start_worker: bool = True,
    background_inference: bool = True,
    ollama=None,
    store=None,
) -> FastAPI:
    from cairnmind.agents import knowledge_mode
    from cairnmind.agents.agent import KnowledgeAgent
    from cairnmind.agents.retrieval import Retriever
    from cairnmind.memory import memory_fingerprint
    from cairnmind.memory.memory import MemoryManager
    from cairnmind.memory.memory_models import from_settings
    from cairnmind.memory.plans import PlanManager
    from cairnmind.memory.sessions import SessionManager
    from cairnmind.storage.store_writer import StoreNotWriter
    from cairnmind.store import Store

    settings = settings or Settings.load(create=True)
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Cairnmind must bind to a loopback address")
    if not settings.token:
        raise ValueError("Run cairnmind init to create a local authentication token")
    models_on = knowledge_mode.enabled(settings)  # read once, here; an invalid value stops the start (#274)
    store = store or Store(settings.data_dir)  # a caller that holds the store (the evaluation) keeps its own database
    # A caller sends a path as an absolute one; a relative one would be read from the service's directory (#238).
    store.relative_paths = False
    if store.not_writer:
        # The service writes sessions, jobs and the queue: it runs only for the store's writer (#70).
        raise StoreNotWriter(store.not_writer)
    model = ollama or from_settings(settings)
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

    ctx = ServiceContext(
        settings=settings,
        store=store,
        model=model,
        retriever=retriever,
        agent=agent,
        plans=plans,
        sessions=sessions,
        memory=memory,
        inference=inference,
        models_on=models_on,
        start_worker=start_worker,
        background_inference=background_inference,
        fingerprint_client=fingerprint_client,
        retry_seconds_fn=lambda: WRITER_LOST_RETRY_SECONDS,
        schedule_embeddings_fn=lambda *args, **kwargs: schedule_embeddings(*args, **kwargs),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if ollama is None:  # the models this service made keep one client each, on this loop, until it stops (#407)
            await model.open()
        tasks = [asyncio.create_task(inference.run())]
        if fingerprint_client is not None and models_on:
            tasks.append(asyncio.create_task(run_fingerprint_start(ctx)))
        if start_worker:
            tasks += [
                asyncio.create_task(run_scanner(ctx)),
                asyncio.create_task(run_scip_worker(ctx)),
                asyncio.create_task(run_memory_snapshots(ctx)),
            ]
            if models_on:  # held: without models the embed and extract jobs stay pending, asked of nobody (#274)
                tasks.append(asyncio.create_task(run_model_worker(ctx)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if ollama is None:
                await model.aclose()

    app = FastAPI(title="Cairnmind local knowledge agent", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.agent = agent
    app.state.plans = plans
    app.state.sessions = sessions
    app.state.inference = inference
    app.state.memory = memory
    register_all_routes(app, ctx)
    return app
