"""Loopback browser cockpit, isolated from memory inference and native authority."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
import httpx

from cairnmind import __version__
from cairnmind.config import BROKER_NAMES, SESSION_NAMES, drop_env, tls_verify
from cairnmind.terminal.cockpit_handlers import (
    MAX_BODY as MAX_BODY,
    MAX_FRAME as MAX_FRAME,
    SESSION_SECONDS,
    TICKET_SECONDS,
    CockpitContext,
    _birth,
    _same as _same,
    instance_id,
    register_cockpit_routes,
)
from cairnmind.terminal.cockpit_state import (
    CockpitState,
    ProjectDiscovery,
    _atomic,
    _private_directory,
    _private_lock,
)


COOKIE = "cairnmind_cockpit"


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
            raise ValueError("Too many pending cockpit openings.")
        value = secrets.token_urlsafe(32)
        self.tickets[hashlib.sha256(value.encode()).hexdigest()] = self.clock() + TICKET_SECONDS
        return value

    def exchange(self, ticket):
        self._prune()
        if not isinstance(ticket, str) or len(ticket) > 200:
            raise HTTPException(401, "Invalid launch ticket.")
        expiry = self.tickets.pop(hashlib.sha256(ticket.encode()).hexdigest(), 0)
        if expiry <= self.clock():
            raise HTTPException(401, "The ticket expired or was already used. Run cairnmind again.")
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
        raise ValueError("The cockpit must use a local HTTP address.")
    if not settings.token:
        raise ValueError("Initialize Cairnmind first.")
    from cairnmind.agents.api_credentials import CredentialStore
    credentials = getattr(runtime, "credentials", None) or CredentialStore(settings)
    if runtime is None:
        from cairnmind.agents.api_runtime import RuntimeSupervisor
        runtime = RuntimeSupervisor(settings, credentials=credentials)
    state = CockpitState(settings)
    discovery = ProjectDiscovery()
    browser = BrowserSessions()
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

    async def upstream(method, path, *, params=None, data=None, person=None):
        # `person` is the workspace's proof of a person's act, passed on as the panel sent it; the broker
        # holds and checks nothing (person_proof, #214).
        try:
            async with httpx.AsyncClient(base_url=settings.url, transport=proxy_transport, trust_env=False,
                                         timeout=settings.ask_timeout + 120, verify=tls_verify(settings.url),
                                         headers={"Authorization": f"Bearer {settings.token}"}) as client:
                return await client.request(method, path, params=params, json=data, headers=person)
        except httpx.HTTPError:
            from cairnmind.terminal.setup_mark import refusal as setup_refusal
            # A change of setup stopped the service on purpose: the panels say that, not "restart" (#339).
            raise HTTPException(503, str(setup_refusal(settings.data_dir)
                                         or "The memory service is unavailable. Restart cairnmind.")) from None

    async def stream(request, session, stream_headers):
        try:
            async with httpx.AsyncClient(base_url=settings.url, transport=proxy_transport, trust_env=False,
                                         timeout=httpx.Timeout(15, read=None), headers=stream_headers,
                                         verify=tls_verify(settings.url)) as client:
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

    ctx = CockpitContext(
        settings=settings,
        runtime=runtime,
        state=state,
        discovery=discovery,
        browser=browser,
        launches=OrderedDict(),
        launch_lock=asyncio.Lock(),
        readiness={},
        project_cache=[],
        completed_auth=set(),
        profile_observations={},
        cookie_name=cookie_name,
        public_url=public_url,
        credentials=credentials,
        proxy_transport=proxy_transport,
        upstream_fn=upstream,
        stream_fn=stream,
    )
    register_cockpit_routes(app, ctx)
    return app


def _runtime_file(settings):
    return settings.data_dir / "cockpit" / "server.json"


def cockpit_status(settings):
    path = _runtime_file(settings)
    result = {"available": False, "version": __version__, "instance": instance_id(settings)}
    try:
        if path.is_symlink():
            return result
        info = json.loads(path.read_text(encoding="utf-8"))
        if (info.get("instance") != result["instance"] or not info.get("birth")
                or info.get("birth") != _birth(info.get("pid"))):
            return result
        parsed = urlparse(info["url"])
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            return result
        with httpx.Client(trust_env=False, timeout=1, verify=tls_verify(info["url"])) as client:
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
    from cairnmind.cli_cmds import upgrade_mark
    from cairnmind.cli import UpgradeInProgress, ensure_service, say_service_started
    reason = upgrade_mark.refusal()
    if reason:
        # ensure_service reads the mark only when it starts the service: beside a running service a broker and a
        # workspace started now would run the version being replaced and outlive the installer's stop (#319).
        raise UpgradeInProgress(reason)
    started = ensure_service(settings, background_inference=False)
    # Said once the broker is up or failed: a start ended for an upgrade below is never announced (#298, #319).
    announce = started
    try:
        return _start_broker(settings, started)
    except UpgradeInProgress:
        announce = False
        raise
    finally:
        if announce:
            say_service_started(settings)


def _start_broker(settings, started):
    from cairnmind.cli_cmds import upgrade_mark
    from cairnmind.memory.integrations import native_environment
    folder = settings.data_dir / "cockpit"
    _private_directory(folder)
    with _private_lock(folder / "launch.lock"):
        status = cockpit_status(settings)
        if not status["available"]:
            env = native_environment()
            drop_env(env, *SESSION_NAMES)
            for key in ("TMUX", "TMUX_PANE"):
                env.pop(key, None)
            from cairnmind.pal.private_files import open_no_follow
            from cairnmind.pal.process_control import detached_popen_kwargs
            fd = open_no_follow(folder / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as log:
                subprocess.Popen([sys.executable, "-I", "-m", "cairnmind", "--data-dir", str(settings.data_dir), "cockpit", "serve"],
                                 cwd=settings.data_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 env=env, **detached_popen_kwargs())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = cockpit_status(settings)
                if status["available"]:
                    break
                time.sleep(.1)
            if not status["available"]:
                raise ValueError("The cockpit did not start; check the local file cockpit/server.log.")
        reason = upgrade_mark.refusal()
        if reason:
            _stop_for_upgrade(settings, reason, started)
        if not status.get("version_matches", True):
            raise ValueError("The running cockpit has another version. Stop it and open it again.")
        return status


def _stop_for_upgrade(settings, reason, started=False):
    """An installer began while this broker started, maybe after its own `cockpit stop`: the broker is stopped here,
    and the service too when this call started it, so none runs the version being replaced (#319)."""
    from cairnmind.cli import UpgradeInProgress, _data_dir_arguments, _stop_started
    from cairnmind.cli_cmds.service_version import Sentence
    from cairnmind.cli_cmds.upgrade_mark import BROKER_STOP_FAILED
    try:
        stop_cockpit(settings)
    except (httpx.HTTPError, OSError):
        from cairnmind.pal.process_control import format_command
        command = format_command(["cairnmind", *_data_dir_arguments(settings.data_dir), "cockpit", "stop"])
        raise UpgradeInProgress(Sentence(BROKER_STOP_FAILED, command=command)) from None
    if started:
        _stop_started(settings)  # raises, with the stop command when the service does not stop
    raise UpgradeInProgress(reason)


def stop_cockpit(settings, stop_clients=False):
    status = cockpit_status(settings)
    if not status["available"]:
        return {"stopping": False, "available": False}
    with httpx.Client(trust_env=False, timeout=20, verify=tls_verify(status["url"])) as client:
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
        raise ValueError("The cockpit is already running for this store.") from None


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
                if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    path.unlink()
            except (OSError, ValueError):
                pass
