"""ArcadeDB HTTP DAL: native SQL, graph records and server-side transactions.

Credentials are never interpolated into SQL, URLs or errors. Where the password comes from and which
addresses are allowed is decided in database_credentials.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
import re
import ssl
import threading
from typing import Iterator
from urllib.parse import quote

import httpx

from .config import Settings
from .database_credentials import problem, tls_context
from .database_mode import connect_only, missing_database, sign_in_refused, unavailable


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

    def __init__(self, base_url: str, auth: tuple[str, str], transport: httpx.BaseTransport | None,
                 verify: ssl.SSLContext | bool = False):
        self._base_url, self._auth, self._transport, self._verify = base_url, auth, transport, verify
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._pid: int | None = None

    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None or self._pid != os.getpid():
                # For https, verify is a context that checks the certificate and the host name, always.
                # For http, which the caller allows only on loopback, no TLS peer exists: verify=False
                # only skips loading authorities that would never be used. Redirects are never followed.
                options = {"transport": self._transport} if self._transport is not None else {}
                self._client = httpx.Client(base_url=self._base_url, auth=self._auth, verify=self._verify,
                                            follow_redirects=False, trust_env=False, **options)
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


def _certificate_refused(exc: BaseException) -> bool:
    """Whether a connection failed because the server's certificate was not trusted."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class ArcadeClient:
    def __init__(self, settings: Settings, *, session_id: str | None = None,
                 transport: httpx.BaseTransport | None = None, _connections: _Connections | None = None,
                 administrator: bool = False):
        """``administrator`` is only for ``cairnmind db create-user``: root, once, over https, on any host."""
        parsed = httpx.URL(settings.arcade_url)
        # The rules of #72: http only on loopback, https with verification anywhere, and on another host
        # neither root nor a password from config.json. The message says which rule was broken.
        refused = problem(replace(settings, arcade_user="cairnmind") if administrator else settings)
        if refused:
            raise ValueError(refused)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,100}", settings.arcade_database):
            raise ValueError("Invalid ArcadeDB database name")
        # Bind credentials and endpoint for this client and its transactions.
        # Mutating the caller's settings must not redirect an existing session.
        self.settings = replace(settings)
        self._base_url = str(parsed).rstrip("/")
        self._administrator = bool(administrator)
        self.database = settings.arcade_database
        self.session_id = session_id
        self.transport = transport
        self._connections = _connections or _Connections(
            self._base_url, (self.settings.arcade_user, self.settings.arcade_password), transport,
            tls_context(settings) if parsed.scheme == "https" else False)

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
            # httpx binds a response's stream to the response, a reference cycle that only the garbage
            # collector frees: request and answer bodies would pile up between its runs (a restore sends
            # groups of rows, #219). The body is read and the stream closed already, so the cycle is cut.
            response.stream = httpx.ByteStream(response.content)
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
                safe_message = safe_message[:500]
                if response.status_code == 401 and not self._administrator:
                    if connect_only(self.settings):
                        # The usual first run of a connect-only installation (#68): the user does not exist yet.
                        safe_message += sign_in_refused(self.settings)
                    else:
                        from .first_start import FOREIGN_ENGINE  # another data directory's engine on this port (#313)
                        safe_message = safe_message.rstrip(".") + ". " + FOREIGN_ENGINE.format(url=self._base_url, data_dir=self.settings.data_dir)
                raise ArcadeError(f"ArcadeDB HTTP {response.status_code}: {safe_message}",
                                  status_code=response.status_code,
                                  exception_name=str(error.get("exception", "")) if isinstance(error, dict) else "")
            return response
        except httpx.HTTPError as exc:
            if _certificate_refused(exc):
                raise ArcadeError(f"The certificate of ArcadeDB at {self._base_url} could not be verified; "
                                  "name a private certificate authority with arcade_ca_file") from None
            if connect_only(self.settings):
                # Never "start the engine": this installation starts none (#68).
                raise ArcadeError(unavailable(self.settings, type(exc).__name__)) from None
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

    def script(self, sql: str, params: dict | None = None) -> list[dict]:
        """Several statements separated by semicolons, in one request; the result is the last statement's."""
        return self._result(self._request("POST", f"/api/v1/command/{quote(self.database)}",
            json={"language": "sqlscript", "command": sql, "params": params or {}, "limit": -1}))

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
        if connect_only(self.settings) and not self._administrator:
            # A connect-only installation creates no database (#68): `cairnmind db create-user` does, once.
            raise ArcadeError(missing_database(self.settings))
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
