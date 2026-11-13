"""Loopback browser cockpit, isolated from memory inference and native authority."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from . import __version__
from .config import BROKER_NAMES, SESSION_NAMES, drop_env
from .cockpit_state import (CockpitState, ProjectDiscovery, _atomic, _private_directory, _private_lock, _snapshot,
                            normalize_path)


COOKIE = "cairnmind_cockpit"
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
        return Path(f"/proc/{int(pid)}/stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


class BrowserSessions:
    """Short-lived one-use launch tickets; browser never sees the service bearer."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.tickets = {}
        self.sessions = {}

    def _prune(self):
        now = self.clock()
        self.tickets = {k: v for k, v in self.tickets.items() if v > now}
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}

    def ticket(self):
        self._prune()
        if len(self.tickets) >= 100:
            raise ValueError("Zbyt wiele oczekujących otwarć kokpitu.")
        value = secrets.token_urlsafe(32)
        self.tickets[hashlib.sha256(value.encode()).hexdigest()] = self.clock() + TICKET_SECONDS
        return value

    def exchange(self, ticket):
        self._prune()
        if not isinstance(ticket, str) or len(ticket) > 200:
            raise HTTPException(401, "Niepoprawny bilet uruchomienia.")
        expiry = self.tickets.pop(hashlib.sha256(ticket.encode()).hexdigest(), 0)
        if expiry <= self.clock():
            raise HTTPException(401, "Bilet wygasł lub został już użyty. Uruchom ponownie cairnmind.")
        token = secrets.token_urlsafe(32)
        session = {"csrf": secrets.token_urlsafe(32), "expires": self.clock() + SESSION_SECONDS}
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = session
        return token, session

    def get(self, token):
        self._prune()
        if not isinstance(token, str) or len(token) > 200:
            return None
        return self.sessions.get(hashlib.sha256(token.encode()).hexdigest())


def create_cockpit_app(settings, *, runtime=None, proxy_transport=None, public_url=None):
    """Serve native controls and a tightly scoped server-side memory API proxy."""
    public_url = (public_url or f"http://127.0.0.1:{getattr(settings, 'cockpit_port', 8766)}").rstrip("/")
    parsed = urlparse(public_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Kokpit musi używać lokalnego adresu HTTP.")
    if not settings.token:
        raise ValueError("Najpierw zainicjalizuj Cairnmind.")
    from .api_credentials import CredentialStore
    credentials = getattr(runtime, "credentials", None) or CredentialStore(settings)
    if runtime is None:
        from .api_runtime import RuntimeSupervisor
        runtime = RuntimeSupervisor(settings, credentials=credentials)
    state = CockpitState(settings)
    discovery = ProjectDiscovery()
    browser = BrowserSessions()
    launches = OrderedDict()
    launch_lock = asyncio.Lock()
    readiness = {}
    project_cache = []
    completed_auth = set()
    profile_observations = {}
    cookie_name = COOKIE + "_" + instance_id(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        discovery.cancel()
        credentials.clear_session()
        close = getattr(runtime, "aclose", getattr(runtime, "close", None))
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    app = FastAPI(title="Cairnmind cockpit", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    app.state.browser_sessions = browser
    app.state.preferences = state
    app.state.discovery = discovery
    app.state.public_url = public_url
    app.state.cookie_name = cookie_name
    app.state.credentials = credentials
    app.state.shutdown = None

    def origin_allowed(headers):
        return headers.get("host", "").lower() == parsed.netloc.lower() and headers.get("origin") == public_url

    def session_for(request):
        session = browser.get(request.cookies.get(cookie_name))
        if session is None:
            raise HTTPException(401, "Sesja kokpitu wygasła. Uruchom cairnmind.")
        return session

    @app.middleware("http")
    async def browser_guard(request: Request, call_next):
        if request.headers.get("host", "").lower() != parsed.netloc.lower():
            return JSONResponse({"detail": "Niepoprawny Host."}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.url.path.startswith("/_control/"):
            if not origin_allowed(request.headers):
                return JSONResponse({"detail": "Niepoprawny Origin."}, status_code=403)
            if request.url.path != "/auth/exchange":
                try:
                    session = session_for(request)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                if not _same(request.headers.get("x-csrf-token", ""), session["csrf"]):
                    return JSONResponse({"detail": "Niepoprawny token CSRF."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; worker-src 'self' blob:; "
            f"connect-src 'self' ws://{parsed.netloc}; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if request.url.path.startswith(("/api/", "/auth/", "/_control/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        message = str(exc)[:600]
        code = 409 if any(s in message.lower() for s in ("conflict", "generation", "stale", "already", "trwa")) else 400
        return JSONResponse({"detail": message}, status_code=code)

    def control(request):
        if request.headers.get("origin"):
            raise HTTPException(403, "Sterowanie procesem wymaga lokalnego CLI.")
        if not _same(request.headers.get("authorization", ""), f"Bearer {settings.token}"):
            raise HTTPException(401, "Niepoprawny token lokalnej usługi.")

    async def body(request):
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > MAX_BODY:
                raise HTTPException(413, "Żądanie przekracza limit.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Niepoprawny JSON.") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Oczekiwano obiektu JSON.")
        return value

    def fields(value, allowed, required=()):
        if set(value) - set(allowed) or any(key not in value for key in required):
            raise HTTPException(422, "Niepoprawne pola żądania.")

    async def upstream(method, path, *, params=None, data=None):
        try:
            async with httpx.AsyncClient(base_url=settings.url, transport=proxy_transport, trust_env=False,
                                       timeout=settings.ask_timeout + 120,
                                       headers={"Authorization": f"Bearer {settings.token}"}) as client:
                return await client.request(method, path, params=params, json=data)
        except httpx.HTTPError:
            raise HTTPException(503, "Usługa pamięci jest niedostępna. Uruchom ponownie cairnmind.") from None

    def forward(response):
        try:
            value = response.json()
        except ValueError:
            raise HTTPException(502, "Usługa pamięci zwróciła niepoprawną odpowiedź.") from None
        return JSONResponse(value, status_code=response.status_code)

    async def projects():
        response = await upstream("GET", "/projects")
        if response.is_error:
            raise HTTPException(response.status_code, "Nie można odczytać projektów.")
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(502, "Niepoprawna odpowiedź usługi pamięci.")
        project_cache[:] = rows
        return rows

    async def project(identifier):
        rows = await projects()
        matches = [row for row in rows if identifier in (row.get("id"), row.get("name"), row.get("root"))]
        if len(matches) != 1:
            raise HTTPException(404, "Nieznany projekt.")
        return matches[0]

    async def enrich(run):
        result = dict(run)
        sid = result.get("session_id")
        if not sid or result.get("purpose") == "auth":
            return result
        cached = readiness.get(sid)
        if cached and time.monotonic() - cached[0] < 5:
            result["memory"] = cached[1]
            return result
        try:
            response = await upstream("GET", "/sessions/" + sid)
        except HTTPException:
            response = None
        if response is not None and response.is_success:
            value = response.json()
            details = {k: value.get(k) for k in ("status", "acknowledged", "stale", "unknowns", "native_session_id")}
            details["checked_at"] = time.time()
        else:
            details = {"status": "unavailable", "acknowledged": False, "stale": False}
        readiness[sid] = (time.monotonic(), details)
        result["memory"] = details
        return result

    @app.get("/health")
    async def health():
        return {"service": BROKER_NAMES[0], "version": __version__, "instance": instance_id(settings),
                "pid": os.getpid(), "birth": _birth(os.getpid())}

    @app.post("/_control/ticket")
    async def issue_ticket(request: Request):
        control(request)
        return {"ticket": browser.ticket(), "expires_in": TICKET_SECONDS}

    @app.post("/_control/agent/{action}")
    async def worker_configuration(request: Request, action: str):
        control(request)
        if action not in {"config", "credential"}:
            raise HTTPException(404, "Nieobsługiwana operacja agenta.")
        value = await body(request)
        fields(value, {"run_id", "generation"}, {"run_id", "generation"})
        if not isinstance(value["run_id"], str) or type(value["generation"]) is not int:
            raise HTTPException(422, "Niepoprawna tożsamość agenta.")
        method = getattr(runtime, "agent_configuration" if action == "config" else "agent_credential", None)
        if method is None:
            raise HTTPException(409, "Silnik agenta jest niedostępny.")
        result = await asyncio.to_thread(method, value["run_id"], value["generation"])
        return JSONResponse(result if action == "config" else {"key": result},
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/_control/stop")
    async def request_stop(request: Request):
        control(request)
        value = await body(request)
        fields(value, {"stop_clients"})
        if type(value.get("stop_clients", False)) is not bool:
            raise HTTPException(422, "stop_clients musi być wartością logiczną.")
        if value.get("stop_clients"):
            for run in await asyncio.to_thread(runtime.list_runs):
                if run.get("lifecycle", run.get("state")) in {"running", "starting", "preparing"}:
                    await asyncio.to_thread(runtime.stop, run["id"], generation=run.get("generation"))
        async def shutdown():
            await asyncio.sleep(.1)
            if app.state.shutdown is not None:
                app.state.shutdown()
        asyncio.create_task(shutdown())
        return {"stopping": True, "clients_stopped": bool(value.get("stop_clients"))}

    @app.post("/auth/exchange")
    async def exchange(request: Request):
        value = await body(request)
        fields(value, {"ticket"}, {"ticket"})
        token, session = browser.exchange(value["ticket"])
        response = JSONResponse({"csrf": session["csrf"], "expires_in": SESSION_SECONDS})
        response.set_cookie(cookie_name, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/auth/session")
    async def auth_session(request: Request):
        session = session_for(request)
        return {"csrf": session["csrf"], "expires_in": max(0, int(session["expires"] - browser.clock()))}

    @app.get("/api/state")
    async def cockpit_state(request: Request):
        session_for(request)
        values = state.load()
        memory_available = True
        try:
            rows = await projects()
        except HTTPException:
            rows = list(project_cache)
            memory_available = False
        for row in rows:
            try:
                response = await upstream("GET", "/project-roots", params={"project": row["id"]})
            except HTTPException:
                response = None
            row["roots"] = response.json().get("roots", [row["root"]]) if response is not None and response.is_success else [row["root"]]
            row["memory_snapshot"] = _snapshot(Path(row["root"]))
        from .client_auth import clear_probe_cache, probe_client
        runs = await asyncio.to_thread(runtime.list_runs)
        for run in runs:
            if (run.get("purpose") == "auth" and run.get("lifecycle", run.get("state")) in {"exited", "failed", "interrupted"}
                    and run.get("id") not in completed_auth):
                completed_auth.add(run.get("id"))
                if run["client"] in {"codex", "claude"}:
                    clear_probe_cache(run["client"])
        runs = await asyncio.gather(*(enrich(row) for row in runs))
        auth = await asyncio.gather(*(asyncio.to_thread(probe_client, name) for name in ("codex", "claude")))
        capabilities = await asyncio.to_thread(getattr(runtime, "diagnostics", lambda: {}))
        return {"version": __version__, "projects": rows, "profiles": values["profiles"], "settings": values,
                "runs": runs, "auth": {row["client"]: row for row in auth}, "capabilities": capabilities,
                "memory_available": memory_available}

    @app.get("/api/projects")
    async def list_projects(request: Request):
        session_for(request)
        return {"projects": await projects()}

    @app.post("/api/projects")
    async def register_project(request: Request):
        session_for(request)
        value = await body(request)
        fields(value, {"name", "root"}, {"name", "root"})
        value["root"] = str(normalize_path(value["root"]))
        return forward(await upstream("POST", "/projects", data=value))

    @app.post("/api/projects/inspect")
    async def inspect_project_directory(request: Request):
        session_for(request)
        value = await body(request)
        fields(value, {"root"}, {"root"})
        if not isinstance(value["root"], str):
            raise HTTPException(422, "Podaj ścieżkę istniejącego katalogu projektu.")
        root = await asyncio.to_thread(normalize_path, value["root"])
        if not await asyncio.to_thread(root.is_dir):
            raise HTTPException(422, "Katalog projektu nie istnieje.")
        from .memory_paths import MEMORY_RELATIVE, snapshot_directory
        manifest = (snapshot_directory(root) or root / MEMORY_RELATIVE) / "manifest.json"
        # Presence is enough: parsing/validation belongs to memory inspect and
        # import dry-run. A corrupt snapshot must not cause a new UUID to be
        # registered before the user can resolve or import it.
        present = await asyncio.to_thread(lambda: manifest.exists() or manifest.is_symlink())
        return {"root": str(root), "memory_snapshot": str(manifest.parent) if present else None}

    @app.get("/api/settings")
    async def get_settings(request: Request):
        session_for(request)
        return state.load()

    @app.put("/api/settings")
    async def save_settings(request: Request):
        session_for(request)
        return await asyncio.to_thread(state.update, await body(request))

    @app.get("/api/profiles")
    async def profiles(request: Request):
        session_for(request)
        return {"profiles": state.load()["profiles"]}

    @app.put("/api/profiles")
    async def save_profiles(request: Request):
        session_for(request)
        value = await body(request)
        fields(value, {"profiles"}, {"profiles"})
        return await asyncio.to_thread(state.update, value)

    def selected_profile(identifier):
        row = next((p for p in state.load()["profiles"] if p["id"] == identifier), None)
        if row is None:
            raise HTTPException(404, "Nieznany profil.")
        return row

    def profile_signature(profile):
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    @app.get("/api/profiles/{profile_id}/status")
    async def profile_status(request: Request, profile_id: str):
        session_for(request)
        profile = selected_profile(profile_id)
        if profile["client"] in {"codex", "claude"}:
            from .client_auth import probe_client
            return {"profile_id": profile_id, **await asyncio.to_thread(probe_client, profile["client"])}
        result = {"profile_id": profile_id, "client": profile["client"],
                  **await asyncio.to_thread(credentials.status, profile)}
        if profile["client"] == "copilot":
            from .copilot_adapter import runtime_status
            result["runtime"] = await asyncio.to_thread(runtime_status, settings)
        cached = profile_observations.get(profile_id)
        if cached and cached[0] == profile_signature(profile):
            result["last_probe"] = cached[1]
        return result

    @app.post("/api/profiles/{profile_id}/credential")
    async def profile_credential(request: Request, profile_id: str):
        session_for(request)
        profile = selected_profile(profile_id)
        if profile["client"] not in {"api", "copilot"}:
            raise HTTPException(422, "Użyj natywnego logowania tego klienta.")
        value = await body(request)
        fields(value, {"key", "persist"}, {"key"})
        if not isinstance(value["key"], str) or type(value.get("persist", False)) is not bool:
            raise HTTPException(422, "Niepoprawna konfiguracja poświadczeń.")
        if value.get("persist"):
            for registered in await projects():
                roots_response = await upstream("GET", "/project-roots", params={"project": registered["id"]})
                if roots_response.is_error:
                    raise HTTPException(503, "Nie można sprawdzić lokalizacji magazynu kluczy.")
                roots = roots_response.json().get("roots", [registered["root"]])
                if any(settings.data_dir.resolve().is_relative_to(Path(root).resolve()) for root in roots):
                    raise HTTPException(409, "Magazyn kluczy musi znajdować się poza katalogami projektów.")
        await asyncio.to_thread(credentials.set, profile_id, value["key"], persist=value.get("persist", False))
        config = state.load()
        rows = [{**p, "credential_ref": "profile:" + profile_id} if p["id"] == profile_id else p
                for p in config["profiles"]]
        await asyncio.to_thread(state.update, {"profiles": rows})
        profile_observations.pop(profile_id, None)
        return {"profile_id": profile_id, "credential_ref": "profile:" + profile_id,
                "status": await asyncio.to_thread(credentials.status, selected_profile(profile_id))}

    @app.delete("/api/profiles/{profile_id}/credential")
    async def delete_profile_credential(request: Request, profile_id: str):
        session_for(request)
        profile = selected_profile(profile_id)
        if profile["client"] not in {"api", "copilot"}:
            raise HTTPException(422, "Użyj natywnego logowania tego klienta.")
        await asyncio.to_thread(credentials.delete, profile_id)
        profile_observations.pop(profile_id, None)
        return {"profile_id": profile_id, "status": await asyncio.to_thread(credentials.status, profile)}

    async def profile_operation(profile, operation):
        from .api_providers import ProviderError, make_provider
        from .copilot_adapter import CopilotAdapter, CopilotError
        key = await asyncio.to_thread(credentials.resolve, profile)
        if profile["client"] == "copilot":
            adapter = CopilotAdapter(profile, key, settings=settings)
        elif profile["client"] == "api":
            adapter = make_provider(profile, key)
        else:
            raise HTTPException(422, "Modele natywnego klienta wybierasz w jego profilu lub terminalu.")
        try:
            return await getattr(adapter, operation)()
        except (ProviderError, CopilotError) as exc:
            safe = (ProviderError(exc.code, context_details=getattr(exc, "context_details", None))
                    if isinstance(exc, ProviderError) else CopilotError(exc.code))
            result = {"status": "failed", "code": safe.code,
                      "hint": getattr(safe, "hint", str(safe))}
            details = getattr(safe, "context_details", None)
            if isinstance(details, dict):
                result["context_details"] = {key: number for key, number in details.items()
                    if key in {"configured_context_window", "model_context_window"}
                    and type(number) is int and 0 < number <= 10**9}
            return result
        except (httpx.HTTPError, OSError, ValueError):
            return {"status": "failed", "code": "provider_unavailable"}
        finally:
            close = getattr(adapter, "close", getattr(adapter, "aclose", None))
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    @app.get("/api/profiles/{profile_id}/models")
    async def profile_models(request: Request, profile_id: str):
        session_for(request)
        result = await profile_operation(selected_profile(profile_id), "list_models")
        return {"models": result} if isinstance(result, list) else {"models": [], **result}

    @app.post("/api/profiles/{profile_id}/probe")
    async def profile_probe(request: Request, profile_id: str):
        session_for(request)
        value = await body(request)
        fields(value, set())
        profile = selected_profile(profile_id)
        result = await profile_operation(profile, "probe")
        profile_observations[profile_id] = (profile_signature(profile), result)
        return {"profile_id": profile_id, **result}

    @app.post("/api/copilot/prepare")
    async def copilot_prepare(request: Request):
        session_for(request)
        fields(await body(request), set())
        from .copilot_adapter import prepare_runtime
        return await asyncio.to_thread(prepare_runtime, settings)

    @app.post("/api/copilot/login")
    async def copilot_login(request: Request):
        session_for(request)
        fields(await body(request), set())
        return {"run": await asyncio.to_thread(runtime.start_auth, "copilot")}

    @app.get("/api/discovery")
    async def discovery_status(request: Request):
        session_for(request)
        result = discovery.status()
        registered = {row.get("root") for row in project_cache}
        for row in result["projects"]:
            row["registered"] = row["root"] in registered
        result["candidates"] = result["projects"]
        return result

    @app.post("/api/discovery")
    async def begin_discovery(request: Request):
        session_for(request)
        value = await body(request)
        fields(value, {"roots"}, {"roots"})
        selected = await asyncio.to_thread(state.update, {"scan_roots": value["roots"]})
        return discovery.start(selected["scan_roots"])

    @app.delete("/api/discovery")
    async def cancel_discovery(request: Request):
        session_for(request)
        return discovery.cancel()

    @app.get("/api/auth")
    async def client_auth(request: Request, client: str, force: bool = False):
        session_for(request)
        from .client_auth import probe_client
        return await asyncio.to_thread(probe_client, client, force=force)

    @app.post("/api/auth/login")
    async def login(request: Request):
        session_for(request)
        value = await body(request)
        fields(value, {"client"}, {"client"})
        if not isinstance(value["client"], str) or value["client"] not in {"codex", "claude"}:
            raise HTTPException(422, "Nieobsługiwany klient logowania.")
        return {"run": await asyncio.to_thread(runtime.start_auth, value["client"])}

    @app.get("/api/client-runs")
    async def list_runs(request: Request, project: str | None = None):
        session_for(request)
        runs = await asyncio.to_thread(runtime.list_runs)
        if project:
            # Existing native processes remain attachable during a memory-service
            # outage. Their bound project ID is sufficient for this read.
            known = next((row["id"] for row in project_cache if project in (row.get("id"), row.get("name"), row.get("root"))), project)
            runs = [row for row in runs if row.get("project_id") == known]
        return {"runs": await asyncio.gather(*(enrich(row) for row in runs))}

    @app.post("/api/client-runs")
    async def start_runs(request: Request):
        session_for(request)
        value = await body(request)
        fields(value, {"project", "worktree", "slots", "profile_id", "slot_id", "idempotency_key", "resume"},
               {"project", "idempotency_key"})
        key = value["idempotency_key"]
        if not isinstance(key, str) or not 1 <= len(key) <= 160 or type(value.get("resume", False)) is not bool:
            raise HTTPException(422, "Niepoprawny identyfikator uruchomienia.")
        selected = await project(value["project"])
        worktree = str(normalize_path(value.get("worktree") or selected["root"]))
        response = await upstream("GET", "/project-roots", params={"project": selected["id"]})
        roots = response.json().get("roots", [selected["root"]]) if response.is_success else [selected["root"]]
        if worktree not in roots:
            raise HTTPException(409, "Wybierz zarejestrowany katalog tego projektu.")
        config = state.load()
        slots = value.get("slots")
        if slots is None and value.get("profile_id"):
            slots = [{"id": value.get("slot_id") or value["profile_id"], "profile_id": value["profile_id"]}]
        if slots is None:
            slots = config["project_slots"].get(selected["id"], config["default_slots"])
        known = {row["id"]: row for row in config["profiles"]}
        slots = CockpitState._slots(slots, known)
        mapped = [{**known[row["profile_id"]], "id": row["id"], "profile_id": row["profile_id"]}
                  for row in slots]
        signature = hashlib.sha256(json.dumps([selected["id"], worktree, mapped, value.get("resume", False)], sort_keys=True).encode()).hexdigest()
        async with launch_lock:
            old = launches.get(key)
            if old:
                if old[0] != signature:
                    raise HTTPException(409, "Identyfikator uruchomienia został użyty dla innych parametrów.")
                return old[1]
            result = await asyncio.to_thread(runtime.start_project, selected, worktree=worktree,
                                            slots=mapped, resume=value.get("resume", False))
            result = {**result, "errors": list(result.get("errors", []))}
            inference_warning = {"code": "background_inference_deferred",
                "message": "Assistant started. Memory processing may still be paused. Use More → Queue → Process memory."}
            if result.get("runs"):
                result["errors"].append(inference_warning)
            # Commit the launch result before the independent memory operation.
            # An outage or cancelled response must never repeat native side effects.
            launches[key] = (signature, result)
            while len(launches) > 1000:
                launches.popitem(last=False)
            if result.get("runs"):
                try:
                    response = await asyncio.wait_for(upstream("POST", "/background-inference/enable",
                        data={"project": selected["id"], "worktree": worktree}), timeout=3)
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
        session_for(request)
        value = await body(request)
        fields(value, {"project", "worktree"}, {"project"})
        selected = await project(value["project"])
        response = await upstream("POST", "/background-inference/enable", data={
            "project": selected["id"], "worktree": value.get("worktree") or selected["root"]})
        return forward(response)

    @app.get("/api/client-runs/{run_id}")
    async def get_run(request: Request, run_id: str):
        session_for(request)
        return await enrich(await asyncio.to_thread(runtime.get_run, run_id))

    @app.post("/api/client-runs/{run_id}/{action}")
    async def control_run(request: Request, run_id: str, action: str):
        session_for(request)
        if action not in {"stop", "interrupt"}:
            raise HTTPException(404, "Nieobsługiwana akcja terminala.")
        value = await body(request)
        fields(value, {"generation"}, {"generation"})
        if not isinstance(value["generation"], (str, int)) or isinstance(value["generation"], bool):
            raise HTTPException(422, "Niepoprawna generacja terminala.")
        return await asyncio.to_thread(getattr(runtime, action), run_id, generation=value["generation"])

    @app.websocket("/api/client-runs/{run_id}/terminal")
    async def terminal(websocket: WebSocket, run_id: str):
        session = browser.get(websocket.cookies.get(cookie_name))
        csrf = websocket.headers.get("x-csrf-token") or websocket.query_params.get("csrf", "")
        if (not origin_allowed(websocket.headers) or session is None
                or not _same(csrf, session["csrf"])):
            await websocket.close(code=1008)
            return
        generation = websocket.query_params.get("generation")
        if not generation:
            await websocket.close(code=1008)
            return
        try:
            run = await asyncio.to_thread(runtime.get_run, run_id)
            if str(run.get("generation")) != generation:
                await websocket.close(code=1008)
                return
            attachment = await runtime.subscribe(run_id, run.get("generation"), uuid4().hex)
        except (OSError, ValueError, RuntimeError):
            await websocket.close(code=1008)
            return
        await websocket.accept()

        async def output():
            previous_controller = None
            while True:
                if browser.clock() >= session["expires"]:
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

        async def incoming():
            while True:
                text = await websocket.receive_text()
                if browser.clock() >= session["expires"]:
                    await websocket.close(code=1008)
                    return
                if len(text.encode()) > MAX_FRAME:
                    raise ValueError("Zbyt duża ramka terminala.")
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError("Niepoprawna ramka terminala.")
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
                        raise ValueError("Niepoprawne dane klawiatury.")
                    if len(data) > MAX_FRAME:
                        raise ValueError("Zbyt dużo danych klawiatury.")
                    await attachment.write(data)
                elif kind == "resize":
                    cols, rows = value.get("cols"), value.get("rows")
                    if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500 or not 5 <= rows <= 200:
                        raise ValueError("Niepoprawny rozmiar terminala.")
                    attachment.resize(cols, rows)
                else:
                    raise ValueError("Nieobsługiwana ramka terminala.")
        tasks = [asyncio.create_task(output()), asyncio.create_task(incoming())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
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

    @app.api_route("/api/inspector/{path:path}", methods=["GET", "POST"])
    async def inspector_proxy(request: Request, path: str):
        session_for(request)
        if not re.fullmatch(r"[A-Za-z0-9_:/-]+", path) or ".." in path:
            raise HTTPException(404)
        # The person's decisions are the only writes: a review, a conflict decision, an impact verdict.
        if request.method == "POST" and not (path.endswith("/review")
                                             or path in ("conflicts/resolve", "impact/verdict")):
            raise HTTPException(405)
        return forward(await upstream(request.method, "/inspector/" + path, params=request.query_params,
                                      data=await body(request) if request.method == "POST" else None))

    @app.api_route("/api/memory/{path:path}", methods=["GET", "POST", "PATCH"])
    async def memory_proxy(request: Request, path: str):
        session_for(request)
        allowed = {"GET": {"status"}, "POST": {"preview", "snapshot", "inspect", "import"}, "PATCH": {"settings"}}
        if path not in allowed[request.method]:
            raise HTTPException(404)
        return forward(await upstream(request.method, "/memory/" + path, params=request.query_params,
                                      data=await body(request) if request.method != "GET" else None))

    @app.get("/api/status")
    async def memory_status(request: Request):
        session_for(request)
        return forward(await upstream("GET", "/status", params=request.query_params))

    @app.post("/api/ask")
    async def ask_memory(request: Request):
        session_for(request)
        value = await body(request)
        fields(value, {"project", "question", "worktree"}, ("project", "question"))
        question = value["question"]
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise HTTPException(422, "Pytanie musi mieć od 1 do 4000 znaków.")
        if not isinstance(value["project"], str) or not isinstance(value.get("worktree", ""), str):
            raise HTTPException(422, "Niepoprawne pola żądania.")
        return forward(await upstream("POST", "/ask", data=value))

    @app.post("/api/findings")
    async def save_finding(request: Request):
        # A person's own write from the workspace form. No session field is accepted, so this
        # route cannot speak for a coding session; the service still validates every field.
        from .findings import finding_error, key_error
        session_for(request)
        value = await body(request)
        required = ("project", "content", "kind", "status", "key")
        fields(value, {*required, "worktree"}, required)
        if not isinstance(value["project"], str) or not isinstance(value.get("worktree", ""), str):
            raise HTTPException(422, "Niepoprawne pola żądania.")
        error = finding_error(value["content"], value["kind"], value["status"]) or key_error(value["key"])
        if error:
            raise HTTPException(422, error)
        return forward(await upstream("POST", "/findings", data=value))

    @app.get("/api/brief")
    async def brief(request: Request):
        session_for(request)
        unknown = set(request.query_params) - {"project", "worktree"}
        if unknown or "project" not in request.query_params:
            raise HTTPException(422, "Niepoprawne pola żądania.")
        return forward(await upstream("GET", "/brief", params=request.query_params))

    @app.get("/api/stage")
    async def next_stage(request: Request):
        # Read-only: the next step and what the owner mapped to its stage. Starting it is a
        # separate request through /api/client-runs; a check runs only through `check run`.
        session_for(request)
        query = request.query_params
        if set(query) - {"project", "worktree"} or "project" not in query:
            raise HTTPException(422, "Niepoprawne pola żądania.")
        selected = await project(query["project"])
        worktree = str(normalize_path(query.get("worktree") or selected["root"]))
        response = await upstream("GET", "/project-roots", params={"project": selected["id"]})
        roots = response.json().get("roots", [selected["root"]]) if response.is_success else [selected["root"]]
        if worktree not in roots:
            raise HTTPException(409, "Wybierz zarejestrowany katalog tego projektu.")
        response = await upstream("POST", "/plans/next", data={"project": selected["id"], "worktree": worktree})
        if response.is_error:
            return forward(response)
        from .api_runtime import stage_readiness
        from .stages import project_routing, resolve_stage
        # The built-in agent's credential is read from this cockpit's store, which also holds
        # keys saved only for its session; nothing is sent to a provider.
        result = await asyncio.to_thread(resolve_stage, response.json(), *project_routing(state.load(), selected["id"]),
                                         readiness=stage_readiness(settings, credentials))
        return {**result, "project_id": selected["id"], "worktree": worktree}

    @app.get("/api/plans")
    async def list_plans(request: Request):
        session_for(request)
        return forward(await upstream("GET", "/plans", params=request.query_params))

    @app.get("/api/plans/{plan_id}")
    async def plan_detail(request: Request, plan_id: str):
        session_for(request)
        return forward(await upstream("GET", "/plans/" + plan_id, params=request.query_params))

    @app.get("/api/events")
    async def event_proxy(request: Request):
        session = session_for(request)
        stream_headers = {"Authorization": f"Bearer {settings.token}"}
        last_event = request.headers.get("last-event-id")
        if last_event:
            if len(last_event) > 200 or not re.fullmatch(r"[A-Za-z0-9:_-]+", last_event):
                raise HTTPException(400, "Niepoprawny kursor zdarzeń.")
            stream_headers["Last-Event-ID"] = last_event
        async def stream():
            try:
                async with httpx.AsyncClient(base_url=settings.url, transport=proxy_transport, trust_env=False,
                    timeout=httpx.Timeout(15, read=None), headers=stream_headers) as client:
                    async with client.stream("GET", "/events", params=request.query_params) as response:
                        if response.is_error:
                            yield 'event: unavailable\ndata: {"reason":"memory_service"}\n\n'
                            return
                        async for chunk in response.aiter_bytes():
                            if browser.clock() >= session["expires"] or await request.is_disconnected():
                                return
                            yield chunk
            except httpx.HTTPError:
                yield 'event: unavailable\ndata: {"reason":"memory_service"}\n\n'
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return app


def _runtime_file(settings):
    return settings.data_dir / "cockpit" / "server.json"


def cockpit_status(settings):
    path = _runtime_file(settings)
    result = {"available": False, "version": __version__, "instance": instance_id(settings)}
    try:
        if path.is_symlink():
            return result
        info = json.loads(path.read_text())
        if (info.get("instance") != result["instance"] or not info.get("birth")
                or info.get("birth") != _birth(info.get("pid"))):
            return result
        parsed = urlparse(info["url"])
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            return result
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(info["url"] + "/health")
            health = response.json()
        if (response.is_success and health.get("service") in BROKER_NAMES
                and health.get("instance") == result["instance"] and health.get("pid") == info["pid"]
                and health.get("birth") == info["birth"]):
            return {**result, **info, "available": True, "version_matches": health.get("version") == __version__}
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        pass
    return result


def ensure_cockpit(settings):
    """Start/reuse the private broker without opening a browser or minting a ticket."""
    from .cli import ensure_service
    from .integrations import native_environment
    ensure_service(settings, background_inference=False)
    folder = settings.data_dir / "cockpit"
    _private_directory(folder)
    with _private_lock(folder / "launch.lock"):
        status = cockpit_status(settings)
        if not status["available"]:
            env = native_environment()
            drop_env(env, *SESSION_NAMES)
            for key in ("TMUX", "TMUX_PANE"):
                env.pop(key, None)
            fd = os.open(folder / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab") as log:
                subprocess.Popen([sys.executable, "-I", "-m", "cairnmind", "--data-dir", str(settings.data_dir), "cockpit", "serve"],
                                 cwd=settings.data_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, env=env)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = cockpit_status(settings)
                if status["available"]:
                    break
                time.sleep(.1)
            if not status["available"]:
                raise ValueError("Kokpit nie uruchomił się; sprawdź lokalny plik cockpit/server.log.")
        if not status.get("version_matches", True):
            raise ValueError("Uruchomiony kokpit ma inną wersję. Zatrzymaj go i otwórz ponownie.")
        return status


def stop_cockpit(settings, stop_clients=False):
    status = cockpit_status(settings)
    if not status["available"]:
        return {"stopping": False, "available": False}
    with httpx.Client(trust_env=False, timeout=20) as client:
        response = client.post(status["url"] + "/_control/stop", json={"stop_clients": stop_clients},
                               headers={"Authorization": f"Bearer {settings.token}"})
        response.raise_for_status()
        return response.json()


def serve_cockpit(settings, port=None):
    folder = settings.data_dir / "cockpit"
    _private_directory(folder)
    try:
        return _serve_locked(settings, folder, port)
    except BlockingIOError:
        raise ValueError("Kokpit już działa dla tego magazynu.") from None


def _serve_locked(settings, folder, port):
    import uvicorn
    with _private_lock(folder / "server.lock", blocking=False):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port if port is not None else getattr(settings, "cockpit_port", 8766)))
        except OSError:
            if port is not None:
                listener.close()
                raise
            listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        chosen = listener.getsockname()[1]
        url = f"http://127.0.0.1:{chosen}"
        app = create_cockpit_app(settings, public_url=url)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=chosen, proxy_headers=False,
                                               access_log=False, log_level="warning", timeout_graceful_shutdown=5))
        app.state.shutdown = lambda: setattr(server, "should_exit", True)
        info = {"url": url, "pid": os.getpid(), "birth": _birth(os.getpid()), "instance": instance_id(settings),
                "version": __version__}
        _atomic(_runtime_file(settings), info)
        try:
            server.run(sockets=[listener])
        finally:
            listener.close()
            path = _runtime_file(settings)
            try:
                if json.loads(path.read_text()).get("pid") == os.getpid():
                    path.unlink()
            except (OSError, ValueError):
                pass
