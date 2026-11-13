"""ArcadeDB HTTP DAL: native SQL, graph records and server-side transactions.

Credentials are local config data, never interpolated into SQL, URLs or errors.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
import re
import threading
from typing import Iterator
from urllib.parse import quote

import httpx

from .config import Settings


class ArcadeError(RuntimeError):
    """The local ArcadeDB engine rejected or could not execute an operation."""

    def __init__(self, message: str, *, status_code: int | None = None, exception_name: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.exception_name = exception_name


class _Connections:
    """The HTTP connections of one database client and of its transactions, kept open between requests.

    Opening a connection for every request cost more than the request itself. The pool is safe to use
    from several threads. A process that was forked makes its own, so no socket is shared across processes.
    """

    def __init__(self, base_url: str, auth: tuple[str, str], transport: httpx.BaseTransport | None):
        self._base_url, self._auth, self._transport = base_url, auth, transport
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._pid: int | None = None

    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None or self._pid != os.getpid():
                # HTTP is enforced by the caller; no TLS peer exists here.
                # Skip unused CA loading, leaving provider HTTPS unchanged.
                self._client = httpx.Client(base_url=self._base_url, auth=self._auth, verify=False,
                                            follow_redirects=False, trust_env=False, transport=self._transport)
                self._pid = os.getpid()
            return self._client

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
            if client is not None and self._pid == os.getpid():
                client.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - nothing useful can be done while an object is collected
            pass


class ArcadeClient:
    def __init__(self, settings: Settings, *, session_id: str | None = None,
                 transport: httpx.BaseTransport | None = None, _connections: _Connections | None = None):
        parsed = httpx.URL(settings.arcade_url)
        if (parsed.scheme != "http" or parsed.host not in {"127.0.0.1", "localhost", "::1"}
                or parsed.userinfo or parsed.query or parsed.fragment):
            raise ValueError("ArcadeDB must use a loopback HTTP address")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,100}", settings.arcade_database):
            raise ValueError("Invalid ArcadeDB database name")
        # Bind credentials and endpoint for this client and its transactions.
        # Mutating the caller's settings must not redirect an existing session.
        self.settings = replace(settings)
        self._base_url = str(parsed).rstrip("/")
        self.database = settings.arcade_database
        self.session_id = session_id
        self.transport = transport
        self._connections = _connections or _Connections(
            self._base_url, (self.settings.arcade_user, self.settings.arcade_password), transport)

    def close(self) -> None:
        """Close the kept connections. The client stays usable: the next request opens a new one."""
        self._connections.close()

    def _request(self, method: str, path: str, *, timeout: float = 30, **kwargs) -> httpx.Response:
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or "\\" in path or any(ord(char) < 32 for char in path)):
            raise ValueError("ArcadeDB requests require a relative endpoint path")
        target = httpx.URL(path)
        base = httpx.URL(self._base_url)
        resolved = base.join(target)
        if (target.scheme or target.host or target.fragment
                or (resolved.scheme, resolved.host, resolved.port) != (base.scheme, base.host, base.port)):
            raise ValueError("ArcadeDB requests must stay on the configured HTTP origin")
        if kwargs.pop("follow_redirects", False) is not False:
            raise ValueError("ArcadeDB redirects are not permitted")
        headers = kwargs.pop("headers", {})
        if self.session_id:
            headers["arcadedb-session-id"] = self.session_id
        # A read outside a transaction may be sent once more when a kept connection turns out to be dead,
        # for example after the engine was restarted. A write is never repeated: it may have been applied.
        repeatable = not self.session_id and (method == "GET" or path.startswith("/api/v1/query/"))
        try:
            try:
                response = self._connections.client().request(method, path, headers=headers, timeout=timeout,
                                                              follow_redirects=False, **kwargs)
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
                if not repeatable:
                    raise
                # The pool drops the dead connection by itself; other threads keep theirs.
                response = self._connections.client().request(method, path, headers=headers, timeout=timeout,
                                                              follow_redirects=False, **kwargs)
            if not response.is_success:
                error = {}
                try:
                    error = response.json()
                    if not isinstance(error, dict):
                        error = {}
                    message = error.get("detail") or error.get("error") or error.get("exception") or "query failed"
                except ValueError:
                    message = "invalid response"
                safe_message = str(message)
                if self.settings.arcade_password:
                    safe_message = safe_message.replace(self.settings.arcade_password, "[redacted]")
                raise ArcadeError(f"ArcadeDB HTTP {response.status_code}: {safe_message[:500]}",
                                  status_code=response.status_code,
                                  exception_name=str(error.get("exception", "")) if isinstance(error, dict) else "")
            return response
        except httpx.HTTPError as exc:
            raise ArcadeError(f"Local ArcadeDB is unavailable ({type(exc).__name__}); "
                              "run cairnmind engine status or cairnmind engine start") from None

    @staticmethod
    def _result(response: httpx.Response) -> list[dict]:
        if response.status_code == 204 or not response.content:
            return []
        try:
            data = response.json()
        except ValueError as exc:
            raise ArcadeError("ArcadeDB returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ArcadeError("ArcadeDB returned an invalid envelope")
        result = data.get("result", [])
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
        return [{"result": result}]

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        return self._result(self._request("POST", f"/api/v1/query/{quote(self.database)}",
            json={"language": "sql", "command": sql, "params": params or {}, "limit": -1}))

    def command(self, sql: str, params: dict | None = None) -> list[dict]:
        return self._result(self._request("POST", f"/api/v1/command/{quote(self.database)}",
            json={"language": "sql", "command": sql, "params": params or {}, "limit": -1}))

    def server_info(self) -> dict:
        return self._request("GET", "/api/v1/server?mode=basic", timeout=2).json()

    def server_command(self, command: str) -> dict:
        return self._request("POST", "/api/v1/server", json={"command": command}).json()

    def database_exists(self) -> bool:
        return self._request("GET", f"/api/v1/exists/{quote(self.database)}").json().get("result") is True

    def ensure_database(self) -> None:
        response = self._request("GET", f"/api/v1/exists/{quote(self.database)}")
        if response.json().get("result") is True:
            return
        try:
            self.server_command(f"create database {self.database}")
        except ArcadeError:
            if self._request("GET", f"/api/v1/exists/{quote(self.database)}").json().get("result") is not True:
                raise

    @contextmanager
    def transaction(self, *, commit: bool = True) -> Iterator[ArcadeClient]:
        """A server transaction, committed at the end; with ``commit=False`` it is always rolled back."""
        if self.session_id:
            yield self
            return
        response = self._request("POST", f"/api/v1/begin/{quote(self.database)}",
                                 json={"isolationLevel": "REPEATABLE_READ"})
        session = response.headers.get("arcadedb-session-id")
        if not session:
            raise ArcadeError("ArcadeDB transaction returned no session ID")
        child = ArcadeClient(self.settings, session_id=session, transport=self.transport,
                             _connections=self._connections)
        try:
            yield child
            child._request("POST", f"/api/v1/{'commit' if commit else 'rollback'}/{quote(self.database)}")
        except BaseException:
            try:
                child._request("POST", f"/api/v1/rollback/{quote(self.database)}")
            except ArcadeError:
                pass
            raise
