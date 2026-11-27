"""Modular HTTP, middleware, and WebSocket terminal handlers for the loopback cockpit."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from cairnmind import __version__
from cairnmind.config import BROKER_NAMES
from cairnmind.pal.path_spelling import lies_in
from cairnmind.pal.process_identity import start as process_start
from cairnmind.pal.service_token import refused_token
from cairnmind.terminal import person_proof
from cairnmind.terminal.cockpit_state import CockpitState, ProjectDiscovery, _snapshot, normalize_path


SESSION_SECONDS = 12 * 60 * 60
TICKET_SECONDS = 60
MAX_BODY = 256 * 1024
MAX_FRAME = 64 * 1024


def _same(left, right):
    return isinstance(left, str) and isinstance(right, str) and hmac.compare_digest(left.encode(), right.encode())


def instance_id(settings):
    return hashlib.sha256(str(settings.data_dir.resolve()).encode()).hexdigest()[:24]


def _birth(pid):
    try:
        return process_start(int(pid))
    except (ValueError, TypeError):
        return None


@dataclass
class CockpitContext:
    """Shared state and upstream callables bound to one cockpit FastAPI application."""

    settings: Any
    runtime: Any
    state: CockpitState
    discovery: ProjectDiscovery
    browser: Any
    launches: OrderedDict
    launch_lock: asyncio.Lock
    readiness: dict
    project_cache: list
    completed_auth: set
    profile_observations: dict
    cookie_name: str
    public_url: str
    credentials: Any
    proxy_transport: Any
    upstream_fn: Callable[..., Awaitable[Any]]
    stream_fn: Callable[..., AsyncIterator[bytes | str]]
    netloc: str = field(init=False)

    def __post_init__(self) -> None:
        self.netloc = urlparse(self.public_url).netloc


def origin_allowed(ctx: CockpitContext, headers) -> bool:
    return headers.get("host", "").lower() == ctx.netloc.lower() and headers.get("origin") == ctx.public_url


def session_for(ctx: CockpitContext, request: Request) -> dict:
    session = ctx.browser.get(request.cookies.get(ctx.cookie_name))
    if session is None:
        raise HTTPException(401, "The broker session expired. Run cairnmind.")
    return session


def require_control(ctx: CockpitContext, request: Request) -> None:
    if request.headers.get("origin"):
        raise HTTPException(403, "Controlling a process needs the local CLI.")
    if not _same(request.headers.get("authorization", ""), f"Bearer {ctx.settings.token}"):
        raise HTTPException(401, "Invalid local service token.")


async def read_body(request: Request) -> dict:
    chunks, length = [], 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > MAX_BODY:
            raise HTTPException(413, "The request is too large.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON.") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "A JSON object was expected.")
    return value


def validate_fields(value: dict, allowed, required=()) -> None:
    if set(value) - set(allowed) or any(key not in value for key in required):
        raise HTTPException(422, "Invalid request fields.")


def forward_response(response) -> JSONResponse:
    try:
        value = response.json()
    except ValueError:
        raise HTTPException(502, "The memory service gave an invalid answer.") from None
    return JSONResponse(value, status_code=response.status_code)


async def fetch_projects(ctx: CockpitContext) -> list:
    response = await ctx.upstream_fn("GET", "/projects")
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if refused_token(response.status_code, detail):
            # The client turns this into the foreign-service sentence in the panel's language (#308).
            raise HTTPException(401, "Invalid local service token")
        raise HTTPException(response.status_code, "The projects could not be read.")
    rows = response.json()
    if not isinstance(rows, list):
        raise HTTPException(502, "The memory service gave an invalid response.")
    ctx.project_cache[:] = rows
    return rows


async def resolve_project(ctx: CockpitContext, identifier: str) -> dict:
    rows = await fetch_projects(ctx)
    matches = [row for row in rows if identifier in (row.get("id"), row.get("name"), row.get("root"))]
    if len(matches) != 1:
        raise HTTPException(404, "Unknown project.")
    return matches[0]


async def enrich_run(ctx: CockpitContext, run: dict) -> dict:
    result = dict(run)
    sid = result.get("session_id")
    if not sid or result.get("purpose") == "auth":
        return result
    cached = ctx.readiness.get(sid)
    if cached and time.monotonic() - cached[0] < 5:
        result["memory"] = cached[1]
        return result
    try:
        response = await ctx.upstream_fn("GET", "/sessions/" + sid)
    except HTTPException:
        response = None
    if response is not None and response.is_success:
        value = response.json()
        details = {k: value.get(k) for k in ("status", "acknowledged", "stale", "stale_reason", "unknowns",
                                             "native_session_id")}
        details["checked_at"] = time.time()
    else:
        details = {"status": "unavailable", "acknowledged": False, "stale": False}
    ctx.readiness[sid] = (time.monotonic(), details)
    result["memory"] = details
    return result


def register_middleware(app: FastAPI, ctx: CockpitContext) -> None:
    """Register Host/Origin/CSRF/CSP middleware and ValueError status mapping."""

    @app.middleware("http")
    async def browser_guard(request: Request, call_next):
        if request.headers.get("host", "").lower() != ctx.netloc.lower():
            return JSONResponse({"detail": "Invalid Host."}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.url.path.startswith("/_control/"):
            if not origin_allowed(ctx, request.headers):
                return JSONResponse({"detail": "Invalid Origin."}, status_code=403)
            if request.url.path != "/auth/exchange":
                try:
                    session = session_for(ctx, request)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                if not _same(request.headers.get("x-csrf-token", ""), session["csrf"]):
                    return JSONResponse({"detail": "Invalid CSRF token."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; worker-src 'self' blob:; "
            f"connect-src 'self' ws://{ctx.netloc}; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if request.url.path.startswith(("/api/", "/auth/", "/_control/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        message = str(exc)[:600]
        from cairnmind.agents.client_runtime import UnknownRun
        if isinstance(exc, UnknownRun):
            return JSONResponse({"detail": message}, status_code=404)  # no such run here, not a refusal (#161)
        code = 409 if any(s in message.lower() for s in ("conflict", "generation", "stale", "already", "trwa")) else 400
        return JSONResponse({"detail": message}, status_code=code)


async def _record_mcp_failure(ctx: CockpitContext, value: dict):
    """The agent worker's report of a third-party MCP server that did not start (#209): exactly these
    fields, so nothing the server said can come along; the runtime checks the run, project, server
    and code."""
    names = {"run_id", "generation", "project_id", "server_id", "code"}
    validate_fields(value, names, names)
    if (type(value["generation"]) is not int
            or not all(isinstance(value[name], str) for name in names - {"generation"})):
        raise HTTPException(422, "Invalid MCP server report.")
    method = getattr(ctx.runtime, "record_mcp_failure", None)
    if method is None:
        raise HTTPException(409, "The agent engine is unavailable.")
    return await asyncio.to_thread(method, **value)


def register_control_and_auth_routes(app: FastAPI, ctx: CockpitContext) -> None:
    """Register health, local CLI control, browser ticket exchange, and client auth routes."""

    @app.get("/health")
    async def health():
        return {"service": BROKER_NAMES[0], "version": __version__, "instance": instance_id(ctx.settings),
                "pid": os.getpid(), "birth": _birth(os.getpid())}

    @app.post("/_control/ticket")
    async def issue_ticket(request: Request):
        require_control(ctx, request)
        return {"ticket": ctx.browser.ticket(), "expires_in": TICKET_SECONDS}

    @app.post("/_control/agent/{action}")
    async def worker_configuration(request: Request, action: str):
        require_control(ctx, request)
        if action not in {"config", "credential", "mcp_failure"}:
            raise HTTPException(404, "Unsupported agent operation.")
        value = await read_body(request)
        if action == "mcp_failure":
            return await _record_mcp_failure(ctx, value)
        validate_fields(value, {"run_id", "generation"}, {"run_id", "generation"})
        if not isinstance(value["run_id"], str) or type(value["generation"]) is not int:
            raise HTTPException(422, "Invalid agent identity.")
        method = getattr(ctx.runtime, "agent_configuration" if action == "config" else "agent_credential", None)
        if method is None:
            raise HTTPException(409, "The agent engine is unavailable.")
        result = await asyncio.to_thread(method, value["run_id"], value["generation"])
        return JSONResponse(result if action == "config" else {"key": result},
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/_control/stop")
    async def request_stop(request: Request):
        require_control(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"stop_clients"})
        if type(value.get("stop_clients", False)) is not bool:
            raise HTTPException(422, "stop_clients must be a boolean.")
        if value.get("stop_clients"):
            for run in await asyncio.to_thread(ctx.runtime.list_runs):
                if run.get("lifecycle", run.get("state")) in {"running", "starting", "preparing"}:
                    await asyncio.to_thread(ctx.runtime.stop, run["id"], generation=run.get("generation"))

        async def shutdown():
            await asyncio.sleep(.1)
            if app.state.shutdown is not None:
                app.state.shutdown()

        asyncio.create_task(shutdown())
        return {"stopping": True, "clients_stopped": bool(value.get("stop_clients"))}

    @app.post("/auth/exchange")
    async def exchange(request: Request):
        value = await read_body(request)
        validate_fields(value, {"ticket"}, {"ticket"})
        token, session = ctx.browser.exchange(value["ticket"])
        response = JSONResponse({"csrf": session["csrf"], "expires_in": SESSION_SECONDS})
        response.set_cookie(ctx.cookie_name, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/auth/session")
    async def auth_session(request: Request):
        session = session_for(ctx, request)
        return {"csrf": session["csrf"], "expires_in": max(0, int(session["expires"] - ctx.browser.clock()))}

    @app.get("/api/auth")
    async def client_auth(request: Request, client: str, force: bool = False):
        session_for(ctx, request)
        from cairnmind.agents.client_auth import probe_client
        return await asyncio.to_thread(probe_client, client, force=force)

    @app.post("/api/auth/login")
    async def login(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"client"}, {"client"})
        if not isinstance(value["client"], str) or value["client"] not in {"codex", "claude"}:
            raise HTTPException(422, "Unsupported sign-in client.")
        return {"run": await asyncio.to_thread(ctx.runtime.start_auth, value["client"])}


def register_state_and_project_routes(app: FastAPI, ctx: CockpitContext) -> None:
    """Register cockpit state, project registration/cloning, settings, and discovery routes."""

    @app.get("/api/state")
    async def cockpit_state(request: Request):
        session_for(ctx, request)
        values = ctx.state.load()
        memory_available, foreign = True, False
        try:
            rows = await fetch_projects(ctx)
        except HTTPException as exc:
            # The state stays whole for the supervisor and the live terminals; the panels name the cause (#308).
            foreign = refused_token(exc.status_code, exc.detail)
            rows = list(ctx.project_cache)
            memory_available = False
        for row in rows:
            try:
                response = await ctx.upstream_fn("GET", "/project-roots", params={"project": row["id"]})
            except HTTPException:
                response = None
            row["roots"] = response.json().get("roots", [row["root"]]) if response is not None and response.is_success else [row["root"]]
            row["unavailable"] = response.json().get("unavailable", []) if response is not None and response.is_success else []
            row["memory_snapshot"] = _snapshot(Path(row["root"]))
        from cairnmind.agents.client_auth import clear_probe_cache, probe_client
        runs = await asyncio.to_thread(ctx.runtime.list_runs)
        for run in runs:
            if (run.get("purpose") == "auth" and run.get("lifecycle", run.get("state")) in {"exited", "failed", "interrupted"}
                    and run.get("id") not in ctx.completed_auth):
                ctx.completed_auth.add(run.get("id"))
                if run["client"] in {"codex", "claude"}:
                    clear_probe_cache(run["client"])
        runs = await asyncio.gather(*(enrich_run(ctx, row) for row in runs))
        auth = await asyncio.gather(*(asyncio.to_thread(probe_client, name) for name in ("codex", "claude")))
        capabilities = await asyncio.to_thread(getattr(ctx.runtime, "diagnostics", lambda: {}))
        return {"version": __version__, "projects": rows, "profiles": values["profiles"], "settings": values,
                "runs": runs, "auth": {row["client"]: row for row in auth}, "capabilities": capabilities,
                "memory_available": memory_available, "foreign_service": foreign}

    @app.get("/api/projects")
    async def list_projects(request: Request):
        session_for(ctx, request)
        return {"projects": await fetch_projects(ctx)}

    @app.post("/api/projects")
    async def register_project(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"name", "root", "fresh"}, {"name", "root"})
        value["root"] = str(normalize_path(value["root"]))
        return forward_response(await ctx.upstream_fn("POST", "/projects", data=value))

    @app.post("/api/projects/inspect")
    async def inspect_project_directory(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"root"}, {"root"})
        if not isinstance(value["root"], str):
            raise HTTPException(422, "Give the path of an existing project folder.")
        root = await asyncio.to_thread(normalize_path, value["root"])
        if not await asyncio.to_thread(root.is_dir):
            raise HTTPException(422, "The project folder does not exist.")
        from cairnmind.memory.memory_paths import MEMORY_RELATIVE, snapshot_directory
        manifest = (snapshot_directory(root) or root / MEMORY_RELATIVE) / "manifest.json"
        # Presence is enough: parsing/validation belongs to memory inspect and
        # import dry-run. A corrupt snapshot must not cause a new UUID to be
        # registered before the user can resolve or import it.
        present = await asyncio.to_thread(lambda: manifest.exists() or manifest.is_symlink())
        return {"root": str(root), "memory_snapshot": str(manifest.parent) if present else None}

    @app.post("/api/projects/clone")
    async def clone_project_repository(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"repo", "parent", "destination", "name", "via", "branch"}, {"repo"})
        if not isinstance(value["repo"], str):
            raise HTTPException(422, "Give a repository as owner/repo, HTTPS or SSH URL.")
        from cairnmind.memory import project_clone
        via = value.get("via", "auto")
        branch = value.get("branch")
        raw_name = value.get("name")
        if (not isinstance(via, str)
                or (branch is not None and not isinstance(branch, str))
                or (raw_name is not None and not isinstance(raw_name, str))):
            raise HTTPException(422, "Invalid clone parameters.")
        via = via.strip() or "auto"
        branch = branch.strip() if isinstance(branch, str) and branch.strip() else None
        raw_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        try:
            source = project_clone.parse_clone_source(value["repo"])
            if branch is not None:
                branch = project_clone.validate_branch(branch)
            chosen_name = raw_name or source.default_name
            if not chosen_name or len(chosen_name) > 256:
                raise ValueError("Project name must be between 1 and 256 characters.")
            if value.get("destination") is not None:
                if not isinstance(value["destination"], str):
                    raise ValueError("Give a valid folder path.")
                target = await asyncio.to_thread(normalize_path, value["destination"])
            elif value.get("parent") is not None:
                if not isinstance(value["parent"], str):
                    raise ValueError("Give a valid folder path.")
                parent = await asyncio.to_thread(normalize_path, value["parent"])
                if not await asyncio.to_thread(parent.is_dir):
                    raise ValueError("The parent folder does not exist.")
                target = parent / source.default_name
            else:
                raise ValueError("Give a parent or destination folder.")
            destination = await asyncio.to_thread(project_clone.prepare_destination, target, ctx.settings.data_dir)
            await asyncio.to_thread(project_clone.choose_tool, source, via)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        from cairnmind.cli_cmds.first_start import PROJECT_NAME_TAKEN, PROJECT_ROOT_TAKEN
        from cairnmind.pal.terminal_safe import safe_text
        for row in await fetch_projects(ctx):
            if row.get("name") == chosen_name:
                raise HTTPException(409, PROJECT_NAME_TAKEN.format(
                    name=safe_text(chosen_name), root=safe_text(row.get("root") or "")))
            if row.get("root") and str(Path(row["root"]).resolve(strict=False)) == str(destination):
                raise HTTPException(409, PROJECT_ROOT_TAKEN.format(
                    name=safe_text(row.get("name") or ""), root=safe_text(str(destination))))
        try:
            cloned = await asyncio.to_thread(
                project_clone.clone_repository, source, destination, via=via, branch=branch, interactive=False)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        found = await asyncio.to_thread(project_clone.inspect_cloned_memory, destination)
        return {"cloned": cloned, "root": str(destination), "name": chosen_name, **found}

    @app.get("/api/settings")
    async def get_settings(request: Request):
        session_for(ctx, request)
        return ctx.state.load()

    @app.put("/api/settings")
    async def save_settings(request: Request):
        session_for(ctx, request)
        return await asyncio.to_thread(ctx.state.update, await read_body(request))

    @app.get("/api/discovery")
    async def discovery_status(request: Request):
        session_for(ctx, request)
        result = ctx.discovery.status()
        registered = {row.get("root") for row in ctx.project_cache}
        for row in result["projects"]:
            row["registered"] = row["root"] in registered
        result["candidates"] = result["projects"]
        return result

    @app.post("/api/discovery")
    async def begin_discovery(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"roots"}, {"roots"})
        selected = await asyncio.to_thread(ctx.state.update, {"scan_roots": value["roots"]})
        return ctx.discovery.start(selected["scan_roots"])

    @app.delete("/api/discovery")
    async def cancel_discovery(request: Request):
        session_for(ctx, request)
        return ctx.discovery.cancel()


def _selected_profile(ctx: CockpitContext, identifier: str) -> dict:
    row = next((p for p in ctx.state.load()["profiles"] if p["id"] == identifier), None)
    if row is None:
        raise HTTPException(404, "Unknown profile.")
    return row


def _profile_signature(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


async def _profile_operation(ctx: CockpitContext, profile: dict, operation: str):
    from cairnmind.agents.api_providers import ProviderError, make_provider
    from cairnmind.agents.copilot_adapter import CopilotAdapter, CopilotError
    key = await asyncio.to_thread(ctx.credentials.resolve, profile)
    if profile["client"] == "copilot":
        adapter = CopilotAdapter(profile, key, settings=ctx.settings)
    elif profile["client"] == "api":
        adapter = make_provider(profile, key)
    else:
        raise HTTPException(422, "A native client's models are chosen in its profile or terminal.")
    try:
        return await getattr(adapter, operation)()
    except (ProviderError, CopilotError) as exc:
        safe = (ProviderError(exc.code, context_details=getattr(exc, "context_details", None))
                if isinstance(exc, ProviderError) else CopilotError(exc.code))
        result = {"status": "failed", "code": safe.code,
                  "hint": getattr(safe, "hint", str(safe))}
        details = getattr(safe, "context_details", None)
        if isinstance(details, dict):
            result["context_details"] = {
                key: number for key, number in details.items()
                if key in {"configured_context_window", "model_context_window"}
                and type(number) is int and 0 < number <= 10**9
            }
        return result
    except (httpx.HTTPError, OSError, ValueError):
        return {"status": "failed", "code": "provider_unavailable"}
    finally:
        close = getattr(adapter, "close", getattr(adapter, "aclose", None))
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def register_profile_routes(app: FastAPI, ctx: CockpitContext) -> None:
    """Register profile management, credential storage, model probing, and Copilot setup routes."""

    @app.get("/api/profiles")
    async def profiles(request: Request):
        session_for(ctx, request)
        return {"profiles": ctx.state.load()["profiles"]}

    @app.put("/api/profiles")
    async def save_profiles(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"profiles"}, {"profiles"})
        return await asyncio.to_thread(ctx.state.update, value)

    @app.get("/api/profiles/{profile_id}/status")
    async def profile_status(request: Request, profile_id: str):
        session_for(ctx, request)
        profile = _selected_profile(ctx, profile_id)
        if profile["client"] in {"codex", "claude"}:
            from cairnmind.agents.client_auth import probe_client
            return {"profile_id": profile_id, **await asyncio.to_thread(probe_client, profile["client"])}
        result = {"profile_id": profile_id, "client": profile["client"],
                  **await asyncio.to_thread(ctx.credentials.status, profile)}
        if profile["client"] == "copilot":
            from cairnmind.agents.copilot_adapter import runtime_status
            result["runtime"] = await asyncio.to_thread(runtime_status, ctx.settings)
        cached = ctx.profile_observations.get(profile_id)
        if cached and cached[0] == _profile_signature(profile):
            result["last_probe"] = cached[1]
        return result

    @app.post("/api/profiles/{profile_id}/credential")
    async def profile_credential(request: Request, profile_id: str):
        session_for(ctx, request)
        profile = _selected_profile(ctx, profile_id)
        if profile["client"] not in {"api", "copilot"}:
            raise HTTPException(422, "Use this client's own sign-in.")
        value = await read_body(request)
        validate_fields(value, {"key", "persist"}, {"key"})
        if not isinstance(value["key"], str) or type(value.get("persist", False)) is not bool:
            raise HTTPException(422, "Invalid credential configuration.")
        if value.get("persist"):
            for registered in await fetch_projects(ctx):
                roots_response = await ctx.upstream_fn("GET", "/project-roots", params={"project": registered["id"]})
                if roots_response.is_error:
                    raise HTTPException(503, "The location of the key store could not be checked.")
                roots = roots_response.json().get("roots", [registered["root"]])
                if any(lies_in(ctx.settings.data_dir.resolve(), Path(root).resolve()) for root in roots):
                    raise HTTPException(409, "The key store must be outside project folders.")
        await asyncio.to_thread(ctx.credentials.set, profile_id, value["key"], persist=value.get("persist", False))
        config = ctx.state.load()
        rows = [{**p, "credential_ref": "profile:" + profile_id} if p["id"] == profile_id else p
                for p in config["profiles"]]
        await asyncio.to_thread(ctx.state.update, {"profiles": rows})
        ctx.profile_observations.pop(profile_id, None)
        return {"profile_id": profile_id, "credential_ref": "profile:" + profile_id,
                "status": await asyncio.to_thread(ctx.credentials.status, _selected_profile(ctx, profile_id))}

    @app.delete("/api/profiles/{profile_id}/credential")
    async def delete_profile_credential(request: Request, profile_id: str):
        session_for(ctx, request)
        profile = _selected_profile(ctx, profile_id)
        if profile["client"] not in {"api", "copilot"}:
            raise HTTPException(422, "Use this client's own sign-in.")
        await asyncio.to_thread(ctx.credentials.delete, profile_id)
        ctx.profile_observations.pop(profile_id, None)
        return {"profile_id": profile_id, "status": await asyncio.to_thread(ctx.credentials.status, profile)}

    @app.get("/api/profiles/{profile_id}/models")
    async def profile_models(request: Request, profile_id: str):
        session_for(ctx, request)
        result = await _profile_operation(ctx, _selected_profile(ctx, profile_id), "list_models")
        return {"models": result} if isinstance(result, list) else {"models": [], **result}

    @app.post("/api/profiles/{profile_id}/probe")
    async def profile_probe(request: Request, profile_id: str):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, set())
        profile = _selected_profile(ctx, profile_id)
        result = await _profile_operation(ctx, profile, "probe")
        ctx.profile_observations[profile_id] = (_profile_signature(profile), result)
        return {"profile_id": profile_id, **result}

    @app.post("/api/copilot/prepare")
    async def copilot_prepare(request: Request):
        session_for(ctx, request)
        validate_fields(await read_body(request), set())
        from cairnmind.agents.copilot_adapter import prepare_runtime
        return await asyncio.to_thread(prepare_runtime, ctx.settings)

    @app.post("/api/copilot/login")
    async def copilot_login(request: Request):
        session_for(ctx, request)
        validate_fields(await read_body(request), set())
        return {"run": await asyncio.to_thread(ctx.runtime.start_auth, "copilot")}


async def _terminal_output_loop(ctx: CockpitContext, websocket: WebSocket, session: dict, attachment: Any) -> None:
    previous_controller = None
    while True:
        if ctx.browser.clock() >= session["expires"]:
            await websocket.close(code=1008)
            return
        controller = bool(attachment.controller)
        if controller != previous_controller:
            await websocket.send_json({"type": "control", "controller": controller})
            previous_controller = controller
        try:
            event = await asyncio.wait_for(attachment.queue.get(), 5)
        except TimeoutError:
            continue
        if event.get("type") == "output":
            raw = event.get("data", b"")
            await websocket.send_json({"type": "output", "encoding": "base64",
                                       "data": base64.b64encode(raw).decode()})
        elif event.get("type") == "control":
            # The broker distributes a connection ID, whereas each
            # browser sees only whether its own attachment owns input.
            previous_controller = bool(attachment.controller)
            await websocket.send_json({"type": "control", "controller": previous_controller})
        else:
            await websocket.send_json(event)
            if event.get("type") == "gap":
                await websocket.close(code=1013)
                return


async def _terminal_input_loop(ctx: CockpitContext, websocket: WebSocket, session: dict, attachment: Any) -> None:
    while True:
        text = await websocket.receive_text()
        if ctx.browser.clock() >= session["expires"]:
            await websocket.close(code=1008)
            return
        if len(text.encode()) > MAX_FRAME:
            raise ValueError("The terminal frame is too large.")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Invalid terminal frame.")
        kind = value.get("type")
        if kind == "takeover":
            attachment.take_control()
            continue
        if not attachment.controller:
            continue
        if kind == "input":
            if "text" in value and isinstance(value["text"], str):
                data = value["text"].encode()
            elif isinstance(value.get("data"), str):
                data = base64.b64decode(value["data"], validate=True)
            else:
                raise ValueError("Invalid keyboard data.")
            if len(data) > MAX_FRAME:
                raise ValueError("Too much keyboard data.")
            await attachment.write(data)
        elif kind == "resize":
            cols, rows = value.get("cols"), value.get("rows")
            if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500 or not 5 <= rows <= 200:
                raise ValueError("Invalid terminal size.")
            attachment.resize(cols, rows)
        else:
            raise ValueError("Unsupported terminal frame.")


def register_run_and_terminal_routes(app: FastAPI, ctx: CockpitContext) -> None:
    """Register native/API client run lifecycle routes and WebSocket PTY streaming."""

    @app.get("/api/client-runs")
    async def list_runs(request: Request, project: str | None = None):
        session_for(ctx, request)
        runs = await asyncio.to_thread(ctx.runtime.list_runs)
        if project:
            # Existing native processes remain attachable during a memory-service
            # outage. Their bound project ID is sufficient for this read.
            known = next((row["id"] for row in ctx.project_cache
                          if project in (row.get("id"), row.get("name"), row.get("root"))), project)
            runs = [row for row in runs if row.get("project_id") == known]
        return {"runs": await asyncio.gather(*(enrich_run(ctx, row) for row in runs))}

    @app.post("/api/client-runs")
    async def start_runs(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"project", "worktree", "slots", "profile_id", "slot_id", "idempotency_key", "resume"},
                        {"project", "idempotency_key"})
        key = value["idempotency_key"]
        if not isinstance(key, str) or not 1 <= len(key) <= 160 or type(value.get("resume", False)) is not bool:
            raise HTTPException(422, "Invalid run identifier.")
        selected = await resolve_project(ctx, value["project"])
        worktree = str(normalize_path(value.get("worktree") or selected["root"]))
        response = await ctx.upstream_fn("GET", "/project-roots", params={"project": selected["id"]})
        roots = response.json().get("roots", [selected["root"]]) if response.is_success else [selected["root"]]
        if worktree not in roots:
            raise HTTPException(409, "Choose a registered folder of this project.")
        config = ctx.state.load()
        slots = value.get("slots")
        if slots is None and value.get("profile_id"):
            slots = [{"id": value.get("slot_id") or value["profile_id"], "profile_id": value["profile_id"]}]
        if slots is None:
            slots = config["project_slots"].get(selected["id"], config["default_slots"])
        known = {row["id"]: row for row in config["profiles"]}
        slots = CockpitState._slots(slots, known)
        mapped = [{**known[row["profile_id"]], "id": row["id"], "profile_id": row["profile_id"]}
                  for row in slots]
        signature = hashlib.sha256(
            json.dumps([selected["id"], worktree, mapped, value.get("resume", False)], sort_keys=True).encode()
        ).hexdigest()
        async with ctx.launch_lock:
            old = ctx.launches.get(key)
            if old:
                if old[0] != signature:
                    raise HTTPException(409, "The run identifier was used with other parameters.")
                return old[1]
            result = await asyncio.to_thread(ctx.runtime.start_project, selected, worktree=worktree,
                                             slots=mapped, resume=value.get("resume", False))
            result = {**result, "errors": list(result.get("errors", []))}
            inference_warning = {
                "code": "background_inference_deferred",
                "message": "Assistant started. Memory processing may still be paused. Use More → Queue → Process memory.",
            }
            if result.get("runs"):
                result["errors"].append(inference_warning)
            # Commit the launch result before the independent memory operation.
            # An outage or cancelled response must never repeat native side effects.
            ctx.launches[key] = (signature, result)
            while len(ctx.launches) > 1000:
                ctx.launches.popitem(last=False)
            if result.get("runs"):
                try:
                    response = await asyncio.wait_for(
                        ctx.upstream_fn("POST", "/background-inference/enable",
                                        data={"project": selected["id"], "worktree": worktree}),
                        timeout=3,
                    )
                    enabled = response.json()
                    if response.is_error or enabled.get("accepted") is not True or enabled.get("project_id") != selected["id"]:
                        raise ValueError("Background inference was not accepted")
                    result["background_inference"] = enabled
                    result["errors"].remove(inference_warning)
                except (HTTPException, ValueError, AttributeError, TimeoutError):
                    pass  # Keep the actionable warning, including after cancellation.
            return result

    @app.post("/api/background-inference/enable")
    async def process_memory(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"project", "worktree"}, {"project"})
        selected = await resolve_project(ctx, value["project"])
        response = await ctx.upstream_fn("POST", "/background-inference/enable", data={
            "project": selected["id"], "worktree": value.get("worktree") or selected["root"]})
        return forward_response(response)

    @app.get("/api/client-runs/{run_id}")
    async def get_run(request: Request, run_id: str):
        session_for(ctx, request)
        return await enrich_run(ctx, await asyncio.to_thread(ctx.runtime.get_run, run_id))

    @app.post("/api/client-runs/{run_id}/{action}")
    async def control_run(request: Request, run_id: str, action: str):
        session_for(ctx, request)
        if action not in {"stop", "interrupt"}:
            raise HTTPException(404, "Unsupported terminal action.")
        value = await read_body(request)
        validate_fields(value, {"generation"}, {"generation"})
        if not isinstance(value["generation"], (str, int)) or isinstance(value["generation"], bool):
            raise HTTPException(422, "Invalid terminal generation.")
        return await asyncio.to_thread(getattr(ctx.runtime, action), run_id, generation=value["generation"])

    @app.websocket("/api/client-runs/{run_id}/terminal")
    async def terminal(websocket: WebSocket, run_id: str):
        session = ctx.browser.get(websocket.cookies.get(ctx.cookie_name))
        csrf = websocket.headers.get("x-csrf-token") or websocket.query_params.get("csrf", "")
        if (not origin_allowed(ctx, websocket.headers) or session is None
                or not _same(csrf, session["csrf"])):
            await websocket.close(code=1008)
            return
        generation = websocket.query_params.get("generation")
        if not generation:
            await websocket.close(code=1008)
            return
        try:
            run = await asyncio.to_thread(ctx.runtime.get_run, run_id)
            if str(run.get("generation")) != generation:
                await websocket.close(code=1008)
                return
            attachment = await ctx.runtime.subscribe(run_id, run.get("generation"), uuid4().hex)
        except (OSError, ValueError, RuntimeError):
            await websocket.close(code=1008)
            return
        await websocket.accept()

        tasks = [
            asyncio.create_task(_terminal_output_loop(ctx, websocket, session, attachment)),
            asyncio.create_task(_terminal_input_loop(ctx, websocket, session, attachment)),
        ]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except (ValueError, RuntimeError, OSError):
            try:
                await websocket.send_json({"type": "error", "code": "terminal_protocol_error"})
                await websocket.close(code=1008)
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass
        finally:
            for task in tasks:
                task.cancel()
            # Release the controller even when the ASGI connection task itself
            # is cancelled; detaching never terminates the native process.
            try:
                await asyncio.shield(attachment.close())
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)


def register_memory_proxy_routes(app: FastAPI, ctx: CockpitContext) -> None:
    """Register scoped server-side proxy routes for inspector, memory, plans, stages, and SSE events."""

    @app.api_route("/api/inspector/{path:path}", methods=["GET", "POST"])
    async def inspector_proxy(request: Request, path: str):
        session_for(ctx, request)
        if not re.fullmatch(r"[A-Za-z0-9_:/-]+", path) or ".." in path:
            raise HTTPException(404)
        # The person's decisions are the only writes: a review, a conflict decision, an impact verdict, and
        # accepting or withdrawing an unknown transcript record kind (#210), confirming a plan (#214), and
        # releasing a record's false alarms of the secrets gate (#306), adopting an old task group (#305), and
        # deleting a project's memory (#347), and replacing a record with a new text (#222).
        if request.method == "POST" and not (path.endswith("/review") or re.fullmatch(r"plans/[^/]+/confirm", path)
                                             or re.fullmatch(r"records/[^/]+/(release|replace)", path)
                                             or path in ("conflicts/resolve", "impact/verdict",
                                                         "record-kinds/accept", "record-kinds/withdraw",
                                                         "legacy-groups/adopt", "projects/forget")):
            raise HTTPException(405)
        post = request.method == "POST"
        return forward_response(await ctx.upstream_fn(
            request.method, "/inspector/" + path, params=request.query_params,
            data=await read_body(request) if post else None,
            person=person_proof.forwardable(request.headers) if post else None,
        ))

    @app.api_route("/api/memory/{path:path}", methods=["GET", "POST", "PATCH"])
    async def memory_proxy(request: Request, path: str):
        session_for(ctx, request)
        imports = {"import", "import-published", "import-commit"}
        allowed = {"GET": {"status"}, "POST": {"preview", "snapshot", "inspect"} | imports, "PATCH": {"settings"}}
        if path not in allowed[request.method]:
            raise HTTPException(404)
        return forward_response(await ctx.upstream_fn(
            request.method, "/memory/" + path, params=request.query_params,
            data=await read_body(request) if request.method != "GET" else None,
            person=person_proof.forwardable(request.headers) if path in imports else None,
        ))

    @app.get("/api/status")
    async def memory_status(request: Request):
        session_for(ctx, request)
        return forward_response(await ctx.upstream_fn("GET", "/status", params=request.query_params))

    @app.post("/api/ask")
    async def ask_memory(request: Request):
        session_for(ctx, request)
        value = await read_body(request)
        validate_fields(value, {"project", "question", "worktree"}, ("project", "question"))
        question = value["question"]
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise HTTPException(422, "A question has 1 to 4000 characters.")
        if not isinstance(value["project"], str) or not isinstance(value.get("worktree", ""), str):
            raise HTTPException(422, "Invalid request fields.")
        return forward_response(await ctx.upstream_fn("POST", "/ask", data=value))

    @app.post("/api/findings")
    async def save_finding(request: Request):
        # A person's own write from the workspace form. No session field is accepted, so this
        # route cannot speak for a coding session; the service still validates every field.
        from cairnmind.memory.findings import finding_error, key_error
        session_for(ctx, request)
        value = await read_body(request)
        required = ("project", "content", "kind", "status", "key")
        validate_fields(value, {*required, "worktree"}, required)
        if not isinstance(value["project"], str) or not isinstance(value.get("worktree", ""), str):
            raise HTTPException(422, "Invalid request fields.")
        error = finding_error(value["content"], value["kind"], value["status"]) or key_error(value["key"])
        if error:
            raise HTTPException(422, error)
        return forward_response(await ctx.upstream_fn(
            "POST", "/findings", data=value, person=person_proof.forwardable(request.headers)))

    @app.get("/api/brief")
    async def brief(request: Request):
        session_for(ctx, request)
        unknown = set(request.query_params) - {"project", "worktree"}
        if unknown or "project" not in request.query_params:
            raise HTTPException(422, "Invalid request fields.")
        return forward_response(await ctx.upstream_fn("GET", "/brief", params=request.query_params))

    @app.get("/api/stage")
    async def next_stage(request: Request):
        # Read-only: the next step and what the owner mapped to its stage. Starting it is a
        # separate request through /api/client-runs; a check runs only through `check run`.
        session_for(ctx, request)
        query = request.query_params
        if set(query) - {"project", "worktree"} or "project" not in query:
            raise HTTPException(422, "Invalid request fields.")
        selected = await resolve_project(ctx, query["project"])
        worktree = str(normalize_path(query.get("worktree") or selected["root"]))
        response = await ctx.upstream_fn("GET", "/project-roots", params={"project": selected["id"]})
        roots = response.json().get("roots", [selected["root"]]) if response.is_success else [selected["root"]]
        if worktree not in roots:
            raise HTTPException(409, "Choose a registered folder of this project.")
        response = await ctx.upstream_fn("POST", "/plans/next", data={"project": selected["id"], "worktree": worktree})
        if response.is_error:
            return forward_response(response)
        from cairnmind.agents.api_runtime import stage_readiness
        from cairnmind.memory.verification import declared_check_names
        from cairnmind.terminal.stages import check_declared, project_routing, resolve_stage
        # The built-in agent's credential is read from this cockpit's store, which also holds
        # keys saved only for its session; nothing is sent to a provider.
        result = await asyncio.to_thread(
            resolve_stage, response.json(), *project_routing(ctx.state.load(), selected["id"]),
            readiness=stage_readiness(ctx.settings, ctx.credentials),
        )
        if result.get("action") == "run_check":  # a profile the folder does not declare is not offered
            result = check_declared(result, await asyncio.to_thread(declared_check_names, worktree))
        return {**result, "project_id": selected["id"], "worktree": worktree}

    @app.get("/api/plans")
    async def list_plans(request: Request):
        session_for(ctx, request)
        return forward_response(await ctx.upstream_fn("GET", "/plans", params=request.query_params))

    @app.get("/api/plans/{plan_id}")
    async def plan_detail(request: Request, plan_id: str):
        session_for(ctx, request)
        return forward_response(await ctx.upstream_fn("GET", "/plans/" + plan_id, params=request.query_params))

    @app.get("/api/events")
    async def event_proxy(request: Request):
        session = session_for(ctx, request)
        stream_headers = {"Authorization": f"Bearer {ctx.settings.token}"}
        last_event = request.headers.get("last-event-id")
        if last_event:
            if len(last_event) > 200 or not re.fullmatch(r"[A-Za-z0-9:_-]+", last_event):
                raise HTTPException(400, "Invalid event cursor.")
            stream_headers["Last-Event-ID"] = last_event
        return StreamingResponse(
            ctx.stream_fn(request, session, stream_headers),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )


def register_cockpit_routes(app: FastAPI, ctx: CockpitContext) -> None:
    """Register cockpit middleware, exception handlers, HTTP routes, and terminal WebSocket."""
    register_middleware(app, ctx)
    register_control_and_auth_routes(app, ctx)
    register_state_and_project_routes(app, ctx)
    register_profile_routes(app, ctx)
    register_run_and_terminal_routes(app, ctx)
    register_memory_proxy_routes(app, ctx)
