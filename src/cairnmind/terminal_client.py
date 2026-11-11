"""Private local broker client shared by terminal panels and raw pane bridges.

Credentials live only in memory. Neither a display URL nor a browser is needed.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json as jsonlib
import threading
from urllib.parse import urlencode, urlparse

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


class BrokerError(ValueError):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Broker {status_code}: {detail}")


class BrokerClient:
    def __init__(self, settings, ensure=False, *, transport=None, async_transport=None):
        self.settings = settings
        self._lock = threading.RLock()
        self._client = None
        self._url = None
        self._csrf = None
        self._transport = transport
        self._async_transport = async_transport if async_transport is not None else transport
        self._allow_start = False
        if ensure:
            self.ensure()

    @staticmethod
    def _validated(status):
        if not status.get("available") or not status.get("version_matches", True):
            raise BrokerError(503, "Lokalny broker jest niedostępny lub wymaga ponownego uruchomienia.")
        url = status.get("url", "")
        parsed = urlparse(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise BrokerError(503, "Niepoprawny adres lokalnego brokera.")
        return url.rstrip("/")

    def _connect(self, status):
        url = self._validated(status)
        if self._client is None or self._url != url:
            if self._client is not None:
                self._client.close()
            self._client = httpx.Client(base_url=url, timeout=20, trust_env=False, transport=self._transport)
            self._url, self._csrf = url, None
        if self._csrf is None:
            self._authenticate()

    def ensure(self):
        from .cockpit import ensure_cockpit
        with self._lock:
            try:
                status = ensure_cockpit(self.settings)
                self._allow_start = True
                self._connect(status)
                return status
            except httpx.HTTPError:
                raise BrokerError(503, "Lokalny broker nie odpowiada.") from None

    def _existing(self):
        from .cockpit import cockpit_status
        self._connect(cockpit_status(self.settings))

    @staticmethod
    def _decode(response):
        try:
            value = response.json()
        except ValueError:
            raise BrokerError(response.status_code, "Niepoprawna odpowiedź lokalnego brokera.") from None
        if response.is_error:
            detail = value.get("detail", "Żądanie zostało odrzucone.") if isinstance(value, dict) else "Żądanie zostało odrzucone."
            raise BrokerError(response.status_code, str(detail)[:2000])
        return value

    def _authenticate(self):
        # The CLI control endpoint deliberately receives no Origin header.
        try:
            ticket_response = self._client.post("/_control/ticket", timeout=5,
                headers={"Authorization": f"Bearer {self.settings.token}"})
        except httpx.HTTPError:
            raise BrokerError(503, "Nie można uwierzytelnić lokalnej sesji.") from None
        ticket = self._decode(ticket_response)
        if not isinstance(ticket, dict) or not isinstance(ticket.get("ticket"), str):
            raise BrokerError(503, "Niepoprawny bilet lokalnej sesji.")
        try:
            session_response = self._client.post("/auth/exchange", timeout=5,
                json={"ticket": ticket["ticket"]}, headers={"Origin": self._url})
        except httpx.HTTPError:
            raise BrokerError(503, "Nie można uwierzytelnić lokalnej sesji.") from None
        session = self._decode(session_response)
        if not isinstance(session, dict) or not isinstance(session.get("csrf"), str):
            raise BrokerError(503, "Niepoprawna lokalna sesja.")
        self._csrf = session["csrf"]

    def _ready(self):
        if self._client is None:
            self._existing()

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if not isinstance(path, str) or not (path.startswith("/api/") or path == "/auth/session") or path.startswith("//"):
            raise ValueError("Broker request requires a relative API path")
        if isinstance(params, dict):
            # Optional filters are absent, not explicit empty identifiers.
            # False, zero, empty strings and JSON nulls retain their meaning.
            params = {key: value for key, value in params.items() if value is not None}
        with self._lock:
            self._ready()
            for attempt in range(2):
                try:
                    response = self._client.request(method, path, params=params, json=json, timeout=timeout,
                        headers={"Origin": self._url, "X-CSRF-Token": self._csrf})
                except httpx.HTTPError:
                    # Never retry a mutation after a transport error: its
                    # outcome may already have committed on the server.
                    raise BrokerError(503, "Połączenie z lokalnym brokerem zostało przerwane.") from None
                if response.status_code == 401 and attempt == 0:
                    self._authenticate()
                    continue
                return self._decode(response)

    def _session(self, renew=False):
        with self._lock:
            self._ready()
            if renew:
                self._authenticate()
            cookie = "; ".join(f"{key}={value}" for key, value in self._client.cookies.items())
            return self._url, {"Cookie": cookie, "Origin": self._url, "X-CSRF-Token": self._csrf}

    def reconnect(self):
        with self._lock:
            self._csrf = None
            if self._allow_start:
                return self.ensure()
            self._existing()

    @asynccontextmanager
    async def terminal(self, run_id, generation):
        # Only run identity is in the URL. Session/CSRF credentials are headers.
        if not isinstance(run_id, str) or not run_id or not all(c.isalnum() or c in "_-" for c in run_id):
            raise ValueError("Invalid native run ID")
        if type(generation) is not int or generation < 1:
            raise ValueError("Invalid native generation")
        websocket = None
        for attempt in range(2):
            url, headers = await asyncio.to_thread(self._session, attempt == 1)
            endpoint = url.replace("http://", "ws://", 1) + "/api/client-runs/" + run_id + "/terminal?" + urlencode({"generation": generation})
            try:
                websocket = await connect(endpoint, additional_headers=headers, proxy=None,
                    max_size=256 * 1024, max_queue=16, open_timeout=10, close_timeout=3)
                break
            except InvalidStatus as exc:
                if attempt or exc.response.status_code not in {401, 403}:
                    raise BrokerError(exc.response.status_code, "Terminal odrzucił lokalną sesję lub generację.") from None
        try:
            yield websocket
        finally:
            if websocket is not None:
                await websocket.close()

    @staticmethod
    def _cursor(after):
        if isinstance(after, dict):
            return f"{after.get('epoch')}:{after.get('seq')}"
        return str(after) if after is not None else None

    async def events(self, after=None, project=None):
        cursor = self._cursor(after)
        while True:
            try:
                url, headers = await asyncio.to_thread(self._session)
                if cursor:
                    headers["Last-Event-ID"] = cursor
                async with httpx.AsyncClient(base_url=url, transport=self._async_transport, trust_env=False,
                    timeout=httpx.Timeout(10, read=None)) as client:
                    async with client.stream("GET", "/api/events", headers=headers,
                                             params={"project": project} if project else {}) as response:
                        if response.status_code == 401:
                            await asyncio.to_thread(self._session, True)
                            continue
                        if response.is_error:
                            raise BrokerError(response.status_code, "Strumień zdarzeń jest niedostępny.")
                        event, data, identifier = "message", [], None
                        size = 0
                        async for line in response.aiter_lines():
                            size += len(line)
                            if size > 256 * 1024:
                                raise BrokerError(503, "Zdarzenie przekracza limit rozmiaru.")
                            if not line:
                                if data:
                                    raw = "\n".join(data)
                                    try:
                                        value = jsonlib.loads(raw)
                                    except ValueError:
                                        value = raw
                                    if identifier:
                                        cursor = identifier
                                    yield {"event": event, "data": value, "id": identifier}
                                event, data, identifier, size = "message", [], None, 0
                            elif not line.startswith(":"):
                                field, _, value = line.partition(":")
                                value = value.removeprefix(" ")
                                if field == "event":
                                    event = value
                                elif field == "data":
                                    data.append(value)
                                elif field == "id" and "\0" not in value:
                                    identifier = value
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, httpx.HTTPError):
                yield {"event": "unavailable", "data": {"reason": "broker"}, "id": cursor}
                try:
                    await asyncio.to_thread(self.reconnect)
                except (OSError, ValueError, httpx.HTTPError):
                    pass
            await asyncio.sleep(1)

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client, self._csrf = None, None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
