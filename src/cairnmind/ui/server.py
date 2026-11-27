"""FastAPI router, static SPA bundle resolver, and app factory for the Engineer Cockpit UI.

Mock fixtures (`cairnmind.ui.mock_state`) are imported lazily ONLY when mock mode is
requested; live production servers never import mock state.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cairnmind.ui.event_bus import UiEventBus
from cairnmind.ui.graph import _utc_now
from cairnmind.ui.live_adapter import LiveUiAdapter
from cairnmind.ui.schemas import (
    CloneVersionRequest,
    CreateFactRequest,
    CreatePlanRequest,
    CreateTaskRequest,
    DecisionActionRequest,
    ExportSnapshotRequest,
    PromotePlanRequest,
    SimulateConflictRequest,
    SimulateFileEditRequest,
    UiAskRequest,
    UpdatePlanRequest,
)


def resolve_ui_dist(override: Path | None = None) -> Path | None:
    """Locate the compiled Vite bundle (`ui/dist`) beside the repo or installed package."""
    candidates: list[Path] = []
    if override is not None:
        candidates.append(Path(override))
    env_dir = os.environ.get("CAIRNMIND_UI_DIST")
    if env_dir:
        candidates.append(Path(env_dir))
    resolved_file = Path(__file__).resolve()
    package_root = resolved_file.parents[1]
    repo_root = resolved_file.parents[3] if len(resolved_file.parents) > 3 else package_root.parent.parent
    candidates.extend([
        package_root / "ui_dist",
        repo_root / "ui" / "dist",
        Path.cwd() / "ui" / "dist",
    ])
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if (resolved / "index.html").is_file():
            return resolved
    return None


_ORIG_RESOLVE_UI_DIST = resolve_ui_dist


def _resolve_ui_dist_for_app(override: Path | None = None) -> Path | None:
    """Honor monkeypatches applied either to `server.resolve_ui_dist` or `ui_server.resolve_ui_dist`."""
    if resolve_ui_dist is not _ORIG_RESOLVE_UI_DIST:
        return resolve_ui_dist(override)
    facade = sys.modules.get("cairnmind.ui.ui_server")
    facade_fn = getattr(facade, "resolve_ui_dist", None) if facade is not None else None
    if facade_fn is not None and facade_fn is not _ORIG_RESOLVE_UI_DIST:
        return facade_fn(override)
    return resolve_ui_dist(override)


def _bind_cockpit_routes(
    router: APIRouter,
    *,
    event_bus: UiEventBus,
    mock_state: Any | None = None,
    live_adapter: LiveUiAdapter | None = None,
    default_mock: bool = False,
    read_only: bool = False,
    project_path_param: bool = False,
    get_mock_state: Callable[[], Any] | None = None,
) -> None:
    """Register all Engineer Cockpit REST + SSE routes on `router` (with optional `{project_id}` prefix)."""

    local_lock = threading.Lock()
    local_holder: list[Any] = [mock_state]

    def _use_mock(mock: int | bool | None) -> bool:
        if mock is not None:
            return bool(mock)
        return default_mock or live_adapter is None

    def _mock() -> Any:
        if get_mock_state is not None:
            return get_mock_state()
        if local_holder[0] is None:
            with local_lock:
                if local_holder[0] is None:
                    from cairnmind.ui.mock_state import MockUiState

                    local_holder[0] = MockUiState(event_bus, read_only=read_only)
        return local_holder[0]

    def _pick_project(project_id: str | None, project: str | None) -> str | None:
        return project_id or project

    p = "/projects/{project_id}" if project_path_param else ""

    @router.get(f"{p}/snapshot")
    async def ui_snapshot(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().snapshot()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.snapshot, _pick_project(project_id, project))

    @router.get(f"{p}/file-context")
    async def ui_file_context(
        path: str = Query(min_length=1, max_length=2048),
        lineStart: int = Query(default=1, ge=1),
        lineEnd: int = Query(default=60, ge=1),
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().file_context(path, lineStart, lineEnd)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.file_context, path, lineStart, lineEnd, _pick_project(project_id, project)
        )

    @router.get(f"{p}/events")
    async def ui_events(
        request: Request,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
        once: bool = Query(default=False),
    ):
        use_mock = _use_mock(mock)
        selected_project = _pick_project(project_id, project)

        async def stream():
            token = event_bus.subscribe()
            _, queue = token
            try:
                if use_mock:
                    snap = _mock().snapshot()
                else:
                    assert live_adapter is not None
                    snap = await asyncio.to_thread(live_adapter.snapshot, selected_project)
                init_payload = {
                    "id": "ui-init",
                    "type": "state.updated",
                    "timestamp": _utc_now(),
                    "payload": {
                        "state_rev": snap["state_rev"],
                        "mode": snap["project"]["mode"],
                        "reason": "connected",
                    },
                }
                yield f"event: state.updated\nid: ui-init\ndata: {json.dumps(init_payload)}\n\n"
                if once:
                    return
                last_heartbeat = time.monotonic()
                while not await request.is_disconnected():
                    try:
                        envelope = await asyncio.wait_for(queue.get(), timeout=1.0)
                        yield (
                            f"event: {envelope['type']}\n"
                            f"id: {envelope['id']}\n"
                            f"data: {json.dumps(envelope)}\n\n"
                        )
                    except TimeoutError:
                        if time.monotonic() - last_heartbeat >= 15.0:
                            yield ": heartbeat\n\n"
                            last_heartbeat = time.monotonic()
            finally:
                event_bus.unsubscribe(token)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(f"{p}/ask")
    async def ui_ask(
        body: UiAskRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().ask(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.ask, body, _pick_project(project_id, project))

    @router.get(f"{p}/drift")
    async def ui_drift(
        source: str = Query(default="all"),
        directory: str | None = Query(default=None),
        status: str = Query(default="all"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=5, ge=1, le=50),
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().drift_monitor(
                source=source,
                directory=directory,
                status=status,
                page=page,
                page_size=page_size,
            )
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.drift_monitor,
            project_arg=_pick_project(project_id, project),
            source=source,
            directory=directory,
            status=status,
            page=page,
            page_size=page_size,
        )

    @router.post(f"{p}/detected-decisions/{{decision_id}}/decide")
    async def ui_decide_detected(
        decision_id: str,
        body: DecisionActionRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().decide_detected_decision(decision_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.decide_detected_decision,
            decision_id,
            body,
            _pick_project(project_id, project),
        )

    @router.post(f"{p}/plans")
    async def ui_create_plan(
        body: CreatePlanRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().create_plan(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.create_plan, body, _pick_project(project_id, project))

    @router.patch(f"{p}/plans/{{plan_id}}")
    async def ui_update_plan(
        plan_id: str,
        body: UpdatePlanRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().update_plan(plan_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.update_plan, plan_id, body, _pick_project(project_id, project)
        )

    @router.post(f"{p}/plans/{{plan_id}}/promote")
    async def ui_promote_plan(
        plan_id: str,
        body: PromotePlanRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().promote_plan(plan_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.promote_plan, plan_id, body, _pick_project(project_id, project)
        )

    @router.post(f"{p}/tasks")
    async def ui_create_task(
        body: CreateTaskRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().create_task(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.create_task, body, _pick_project(project_id, project))

    @router.post(f"{p}/facts")
    async def ui_create_fact(
        body: CreateFactRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().create_fact(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.create_fact, body, _pick_project(project_id, project))

    @router.get(f"{p}/versions/{{entity_kind}}/{{entity_id}}")
    async def ui_get_versions(
        entity_kind: Literal["plan", "task", "claim"],
        entity_id: str,
        from_version: int | None = Query(default=None, ge=1),
        to_version: int | None = Query(default=None, ge=1),
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().get_versions(entity_kind, entity_id, from_version=from_version, to_version=to_version)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.get_versions,
            entity_kind,
            entity_id,
            from_version=from_version,
            to_version=to_version,
            project_arg=_pick_project(project_id, project),
        )

    @router.post(f"{p}/versions/{{entity_kind}}/{{entity_id}}/clone-draft")
    async def ui_clone_version(
        entity_kind: Literal["plan", "task", "claim"],
        entity_id: str,
        body: CloneVersionRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().clone_version_as_draft(entity_kind, entity_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.clone_version_as_draft,
            entity_kind,
            entity_id,
            body,
            _pick_project(project_id, project),
        )

    @router.post(f"{p}/actions/reverify")
    async def ui_reverify(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().reverify()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.reverify, _pick_project(project_id, project))

    @router.post(f"{p}/actions/reindex")
    async def ui_reindex(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().reindex()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.reindex, _pick_project(project_id, project))

    @router.get(f"{p}/doctor")
    async def ui_doctor(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().doctor()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.doctor, _pick_project(project_id, project))

    @router.post(f"{p}/actions/export-snapshot")
    async def ui_export_snapshot(
        body: ExportSnapshotRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().export_snapshot(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.export_snapshot, body, _pick_project(project_id, project))

    @router.post(f"{p}/actions/sessions/{{session_id}}/drop")
    async def ui_drop_session(
        session_id: str,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return _mock().drop_session(session_id)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.drop_session, session_id, _pick_project(project_id, project)
        )

    @router.post(f"{p}/mock/simulate-conflict")
    async def ui_mock_simulate_conflict(
        body: SimulateConflictRequest,
        mock: int | None = Query(default=None),
    ):
        if not _use_mock(mock):
            raise HTTPException(400, "Simulation endpoints are only available in mock mode.")
        return _mock().simulate_conflict(body)

    @router.post(f"{p}/mock/simulate-file-edit")
    async def ui_mock_simulate_file_edit(
        body: SimulateFileEditRequest,
        mock: int | None = Query(default=None),
    ):
        if not _use_mock(mock):
            raise HTTPException(400, "Simulation endpoints are only available in mock mode.")
        return _mock().simulate_file_edit(body)


def create_ui_router(
    *,
    store: Any = None,
    settings: Any = None,
    default_project: str | None = None,
    default_mock: bool = False,
    read_only: bool = False,
    bus: UiEventBus | None = None,
    dependencies: list[Any] | None = None,
    prefix: str = "/api/ui",
    mock_state: Any | None = None,
    live_adapter: LiveUiAdapter | None = None,
) -> APIRouter:
    event_bus = bus or UiEventBus()
    adapter = live_adapter or (
        LiveUiAdapter(store, settings, event_bus, default_project, read_only=read_only)
        if store is not None
        else None
    )
    use_mock = bool(default_mock or adapter is None)
    state: Any = mock_state
    if use_mock and state is None:
        from cairnmind.ui.mock_state import MockUiState

        state = MockUiState(event_bus, read_only=read_only)

    state_lock = threading.Lock()
    state_holder: list[Any] = [state]
    router = APIRouter(prefix=prefix, tags=["ui"], dependencies=dependencies or [])

    def _get_mock_state() -> Any:
        if state_holder[0] is None:
            with state_lock:
                if state_holder[0] is None:
                    from cairnmind.ui.mock_state import MockUiState

                    state_holder[0] = MockUiState(event_bus, read_only=read_only)
                    router.mock_state = state_holder[0]  # type: ignore[attr-defined]
        return state_holder[0]

    _bind_cockpit_routes(
        router,
        event_bus=event_bus,
        mock_state=state,
        live_adapter=adapter,
        default_mock=default_mock,
        read_only=read_only,
        project_path_param=False,
        get_mock_state=_get_mock_state,
    )
    _bind_cockpit_routes(
        router,
        event_bus=event_bus,
        mock_state=state,
        live_adapter=adapter,
        default_mock=default_mock,
        read_only=read_only,
        project_path_param=True,
        get_mock_state=_get_mock_state,
    )
    router.event_bus = event_bus  # type: ignore[attr-defined]
    router.mock_state = state  # type: ignore[attr-defined]
    router.live_adapter = adapter  # type: ignore[attr-defined]
    return router


def attach_ui_routes(
    app: FastAPI,
    store: Any,
    settings: Any,
    *,
    bus: UiEventBus | None = None,
    dependencies: list[Any] | None = None,
    read_only: bool = False,
) -> UiEventBus:
    """Attach `/api/ui/*` (and `/api/ui/projects/{project_id}/*`) routes to the main Cairnmind service."""
    router = create_ui_router(
        store=store,
        settings=settings,
        default_mock=False,
        read_only=read_only,
        bus=bus,
        dependencies=dependencies,
    )
    app.include_router(router)
    event_bus: UiEventBus = router.event_bus  # type: ignore[attr-defined]
    app.state.ui_event_bus = event_bus
    return event_bus


def create_ui_app(
    *,
    store: Any = None,
    settings: Any = None,
    default_project: str | None = None,
    default_mock: bool = False,
    read_only: bool = False,
    static_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 4310,
    auth: Any = None,
) -> FastAPI:
    """Standalone loopback FastAPI app for `cairnmind ui --port 4310`."""
    from cairnmind import __version__
    from cairnmind.ui.ui_auth import UiSessionAuth, install_ui_security

    app = FastAPI(title="Cairnmind Engineer Cockpit & Inspector", version=__version__)
    ui_auth = auth if isinstance(auth, UiSessionAuth) else UiSessionAuth.create(host=host, port=port)
    install_ui_security(app, ui_auth)
    app.state.ui_auth = ui_auth

    ui_router = create_ui_router(
        store=store,
        settings=settings,
        default_project=default_project,
        default_mock=default_mock,
        read_only=read_only,
        prefix="/api/ui",
    )
    app.include_router(ui_router)
    app.state.ui_event_bus = ui_router.event_bus  # type: ignore[attr-defined]

    dist = _resolve_ui_dist_for_app(static_dir)
    if dist is not None:
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="ui-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            if full_path.startswith("api/") or full_path == "api":
                return JSONResponse({"detail": "Not found"}, status_code=404)
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def missing_bundle():
            parents = Path(__file__).resolve().parents
            repo_root = parents[3] if len(parents) > 3 else parents[-1]
            has_source_ui = (repo_root / "ui" / "package.json").is_file()
            hint = (
                "Run <code>cd ui &amp;&amp; npm ci &amp;&amp; npm run build</code>, then restart <code>cairnmind ui</code>."
                if has_source_ui
                else "Reinstall Cairnmind from a release wheel built with bundled UI assets (or set <code>CAIRNMIND_UI_DIST</code>)."
            )
            return HTMLResponse(
                "<!doctype html><html><body style='background:#060709;color:#e4e4e7;font-family:monospace;padding:24px'>"
                "<h3>Cairnmind UI bundle not built</h3>"
                f"<p>{hint}</p>"
                "<p>Authenticated API endpoints are live under <code>/api/ui/snapshot</code>.</p>"
                "</body></html>",
                status_code=503,
            )

    return app
