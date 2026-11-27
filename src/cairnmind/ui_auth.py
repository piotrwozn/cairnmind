"""Loopback session authentication, CSRF, and Host/Origin guards for standalone `cairnmind ui`.

Standalone `cairnmind ui` binds to a loopback interface and serves both the static SPA bundle
and `/api/ui/*` endpoints. To prevent cross-site requests, DNS rebinding, and unauthorized
local processes from reading repository files or mutating project state:

1. Every launch generates an in-memory 256-bit `session_token` and `csrf_token` (never written
   to disk or `config.json`).
2. `Host` headers are strictly restricted to loopback host:port pairs for the bound port.
3. `Origin` and `Sec-Fetch-Site` headers reject cross-site / cross-origin requests.
4. All `/api/ui/*` routes require either `Authorization: Bearer <session_token>` or the
   `HttpOnly; SameSite=Strict` session cookie set by `GET /auth/bootstrap?token=...`.
5. Cookie-authenticated mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) and repository
   file reads (`GET .../file-context`) additionally require `X-Cairnmind-UI-Token` matching
   the `SameSite=Strict` CSRF cookie (double-submit defense).
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
import secrets
from typing import Iterable
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_COOKIE = "cairnmind_ui_session"
CSRF_COOKIE = "cairnmind_ui_csrf"
CSRF_HEADER = "X-Cairnmind-UI-Token"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

UI_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


def _format_host(host: str) -> str:
    stripped = host.strip()
    if ":" in stripped and not stripped.startswith("["):
        return f"[{stripped}]"
    return stripped


def _loopback_dev_origin() -> str | None:
    raw = os.environ.get("CAIRNMIND_UI_DEV_ORIGIN", "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None
    return f"http://{parsed.netloc}".lower()


@dataclass(frozen=True)
class UiSessionAuth:
    """In-memory per-process session secrets and allowed loopback host/origin set."""

    session_token: str
    csrf_token: str
    port: int
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]

    @classmethod
    def create(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 4310,
        session_token: str | None = None,
        csrf_token: str | None = None,
        extra_origins: Iterable[str] = (),
    ) -> UiSessionAuth:
        tok = session_token or secrets.token_urlsafe(32)
        csrf = csrf_token or secrets.token_urlsafe(32)
        hosts = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        }
        formatted = _format_host(host).lower()
        if formatted in {"127.0.0.1", "localhost", "[::1]"}:
            hosts.add(f"{formatted}:{port}")
        if port == 80:
            hosts.update({"127.0.0.1", "localhost", "[::1]"})
        origins = {f"http://{h}" for h in hosts}
        dev_origin = _loopback_dev_origin()
        if dev_origin:
            origins.add(dev_origin)
        for origin in extra_origins:
            cleaned = origin.strip().rstrip("/").lower()
            if cleaned:
                origins.add(cleaned)
        return cls(
            session_token=tok,
            csrf_token=csrf,
            port=port,
            allowed_hosts=frozenset(hosts),
            allowed_origins=frozenset(origins),
        )

    def verify_session_token(self, candidate: str | None) -> bool:
        if not candidate or not isinstance(candidate, str):
            return False
        return hmac.compare_digest(candidate, self.session_token)

    def verify_csrf_token(self, candidate: str | None) -> bool:
        if not candidate or not isinstance(candidate, str):
            return False
        return hmac.compare_digest(candidate, self.csrf_token)

    def is_allowed_host(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        return host_header.strip().lower() in self.allowed_hosts

    def is_allowed_origin(self, origin_header: str | None) -> bool:
        if not origin_header:
            return True
        return origin_header.strip().rstrip("/").lower() in self.allowed_origins

    def bootstrap_url(
        self,
        host: str = "127.0.0.1",
        *,
        mock: bool = False,
        project: str | None = None,
    ) -> str:
        params: dict[str, str] = {"token": self.session_token}
        if mock:
            params["mock"] = "1"
        if project:
            params["project"] = project
        return f"http://{_format_host(host)}:{self.port}/auth/bootstrap?{urlencode(params)}"


def _requires_csrf(method: str, path: str) -> bool:
    if method.upper() in MUTATING_METHODS:
        return True
    return path.rstrip("/").endswith("/file-context")


def _extract_bearer(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def install_ui_security(app: FastAPI, auth: UiSessionAuth) -> None:
    """Register Host/Origin/CSRF/session-token middleware and `/auth/bootstrap` on `app`."""

    @app.get("/auth/bootstrap", include_in_schema=False)
    async def ui_auth_bootstrap(
        token: str = "",
        mock: str | None = None,
        project: str | None = None,
    ):
        if not auth.verify_session_token(token):
            return JSONResponse(
                {"detail": "Invalid UI session token."},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        target_params: dict[str, str] = {}
        if mock in {"1", "true", "True"}:
            target_params["mock"] = "1"
        if project and project.strip():
            target_params["project"] = project.strip()
        target = f"/?{urlencode(target_params)}" if target_params else "/"
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=auth.session_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.set_cookie(
            key=CSRF_COOKIE,
            value=auth.csrf_token,
            httponly=False,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def ui_security_middleware(request: Request, call_next):
        def _apply_headers(resp):
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Content-Security-Policy"] = UI_CSP
            if request.url.path.startswith(("/api/", "/auth/")):
                resp.headers["Cache-Control"] = "no-store"
            return resp

        host_header = request.headers.get("host")
        if not auth.is_allowed_host(host_header):
            return _apply_headers(
                JSONResponse({"detail": "Invalid Host header for loopback UI."}, status_code=403)
            )

        sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if sec_fetch_site == "cross-site":
            return _apply_headers(
                JSONResponse({"detail": "Cross-site requests to Cairnmind UI are forbidden."}, status_code=403)
            )

        origin_header = request.headers.get("origin")
        if origin_header and not auth.is_allowed_origin(origin_header):
            return _apply_headers(
                JSONResponse({"detail": "Cross-origin requests to Cairnmind UI are forbidden."}, status_code=403)
            )

        path = request.url.path
        if path.startswith("/api/ui/") or path == "/api/ui":
            bearer = _extract_bearer(request.headers.get("authorization"))
            if bearer is not None:
                if not auth.verify_session_token(bearer):
                    return _apply_headers(
                        JSONResponse({"detail": "Invalid bearer token for Cairnmind UI."}, status_code=401)
                    )
            else:
                session_cookie = request.cookies.get(SESSION_COOKIE)
                if not auth.verify_session_token(session_cookie):
                    return _apply_headers(
                        JSONResponse(
                            {
                                "detail": (
                                    "UI session authentication required. "
                                    "Open the bootstrap URL printed by `cairnmind ui`."
                                )
                            },
                            status_code=401,
                        )
                    )
                if _requires_csrf(request.method, path):
                    csrf_header = request.headers.get(CSRF_HEADER)
                    csrf_cookie = request.cookies.get(CSRF_COOKIE)
                    if not (auth.verify_csrf_token(csrf_header) and auth.verify_csrf_token(csrf_cookie)):
                        return _apply_headers(
                            JSONResponse(
                                {"detail": f"Missing or invalid {CSRF_HEADER} CSRF token."},
                                status_code=403,
                            )
                        )

        response = await call_next(request)
        return _apply_headers(response)
