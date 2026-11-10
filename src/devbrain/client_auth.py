"""Read-only native-client readiness without model calls or credential disclosure.

Status commands describe what a CLI reports; they do not establish connectivity,
remaining usage allowance, or permission to execute an agent task. Login itself
belongs in the runtime's separate, unarchived operational terminal.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import Mapping

from .integrations import _probe_cli


CLIENTS = ("codex", "claude")
CACHE_SECONDS = 30
STATUS_TIMEOUT = 8
MAX_STATUS_CHARS = 16384
_CACHE_LIMIT = 64
_CAPABILITY_KEYS = ("mcp", "hooks", "packet_passing", "session_binding", "isolated_process", "verified_resume")
_CACHE: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()
_LOCK = threading.Lock()
_API_ENV = {
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
}


def _client(value: str) -> str:
    if value not in CLIENTS:
        raise ValueError("Only the native codex and claude clients are supported")
    return value


def _environment(env: Mapping[str, str] | None) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    if not all(isinstance(k, str) and k and "=" not in k and isinstance(v, str)
               and "\0" not in k + v for k, v in result.items()):
        raise ValueError("Native client environment must contain string names and values")
    return result


def _bounded(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return ""
    if len(value) > MAX_STATUS_CHARS:
        value = value[:MAX_STATUS_CHARS // 2] + "\n" + value[-MAX_STATUS_CHARS // 2:]
    return value


def normalize_runtime_error(text: str | bytes | None) -> str:
    """Return a fixed diagnostic hint, never a native message or account detail.

    This classification is an observation of explicit errors, not an authority
    to log out, change permissions, or infer that a request was never executed.
    """
    value = _bounded(text).casefold().replace("\u2019", "'")
    patterns = (
        ("quota_exhausted", r"insufficient[_ ]quota|quota (?:has been )?(?:exceeded|exhausted)|"
                            r"(?:out of|not enough) (?:usage )?credits|credit balance is too low|"
                            r"you(?:'ve| have) hit your (?:usage )?limit|usage[_ ]limit(?: reached)?|"
                            r"hit your usage limit|usage credits (?:have been |are )?exhausted"),
        ("rate_limited", r"rate[_ -]limit(?:ed|_error)?|too many requests|\bhttp\s*429\b"),
        ("auth_rejected", r"invalid[_ -](?:api[_ -])?key|invalid (?:access |auth )?token|"
                          r"(?:access |auth |authentication )?token (?:has )?expired|"
                          r"credentials (?:have )?expired|authentication[_ -]error|"
                          r"\bunauthorized\b|\bhttp\s*401\b|authentication failed"),
        ("auth_required", r"not (?:logged|signed) in|login required|log[ -]?in required|"
                          r"please (?:log|sign) in|authentication required|run (?:codex login|claude auth login)"),
        ("network_unavailable", r"connection (?:refused|reset|failed)|network (?:is )?unreachable|"
                                r"could not (?:connect|resolve)|unable to connect|connecterror|"
                                r"\b(?:econnrefused|econnreset|enotfound|enetunreach|etimedout)\b|"
                                r"(?:request|connection) timed out|dns (?:lookup )?failed"),
        ("unsupported_client", r"unknown (?:option|command|subcommand)|unrecognized (?:option|command|subcommand)|"
                               r"unexpected argument|no such (?:command|subcommand)|unsupported (?:option|command)"),
    )
    return next((code for code, pattern in patterns if re.search(pattern, value)), "unknown")


def clear_probe_cache(client: str | None = None) -> None:
    """Invalidate readiness after an explicit login or account change."""
    if client is not None:
        _client(client)
    with _LOCK:
        for key in list(_CACHE):
            if client is None or key[0] == client:
                del _CACHE[key]


def _version(value) -> str | None:
    # Version stdout is not an approved channel for arbitrary native text.
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", value) if isinstance(value, str) else None
    return match.group(1) if match else None


def _method(value) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = re.sub(r"[-_ ]", "", value.casefold())
    return {"chatgpt": "chatgpt", "apikey": "api_key", "claude.ai": "claude.ai", "oauth": "oauth",
            "environment": "environment"}.get(normalized, "unknown")


def _expiry(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _apply_error(info: dict, code: str) -> None:
    if code == "auth_required":
        info["auth"] = "signed_out"
    elif code == "auth_rejected":
        info["auth"] = "expired_or_rejected"
    elif code == "network_unavailable":
        info["connectivity"] = "unavailable"
    elif code in {"quota_exhausted", "rate_limited"}:
        info["usage"] = code
    elif code == "unsupported_client":
        info["auth_status_supported"] = False
    if code != "unknown":
        info["error_code"] = code


def _auth_result(client: str, result, info: dict) -> None:
    stdout, stderr = _bounded(result.stdout), _bounded(result.stderr)
    code = normalize_runtime_error(stdout + "\n" + stderr)
    _apply_error(info, code)
    if client == "codex":
        text = stdout + "\n" + stderr
        if result.returncode == 0 and re.search(r"\blogged in\b", text, re.I):
            info["auth"] = "reported_signed_in"
            info["auth_method"] = ("chatgpt" if re.search(r"\bchatgpt\b", text, re.I)
                                   else "api_key" if re.search(r"api[ _-]?key", text, re.I) else "unknown")
        elif result.returncode != 0 and code == "unknown":
            info["error_code"] = "auth_status_failed"
        return
    try:
        document = json.loads(stdout)
    except (ValueError, TypeError):
        if code == "unknown":
            info["error_code"] = "auth_status_invalid"
        return
    if not isinstance(document, dict) or type(document.get("loggedIn")) is not bool:
        if code == "unknown":
            info["error_code"] = "auth_status_invalid"
        return
    if result.returncode == 0 and document["loggedIn"]:
        info["auth"] = "reported_signed_in"
        info["auth_method"] = _method(document.get("authMethod"))
        expiry = _expiry(document.get("loginExpiresAt"))
        if expiry:
            info["login_expires_at"] = expiry.isoformat()
            if expiry <= datetime.now(timezone.utc):
                info["auth"] = "expired_or_rejected"
                info["error_code"] = "login_expired"
    elif not document["loggedIn"] and code not in {"auth_rejected", "network_unavailable", "unsupported_client"}:
        info["auth"] = "signed_out"
    elif result.returncode != 0 and code == "unknown":
        info["error_code"] = "auth_status_failed"


def probe_client(client: str, env: Mapping[str, str] | None = None, force: bool = False) -> dict:
    """Probe native capabilities and saved authentication, with a 30-second cache.

    ``env`` is the complete environment that the native profile will receive.
    Only its digest is used as a cache key; values never enter returned metadata.
    A configured API environment is a hint, not proof of a valid key or balance.
    """
    client, environment = _client(client), _environment(env)
    environment_hash = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    key, moment = (client, environment_hash), time.monotonic()
    with _LOCK:
        cached = _CACHE.get(key)
        if not force and cached and 0 <= moment - cached[0] < CACHE_SECONDS:
            value = deepcopy(cached[1])
            value.update(cached=True, age_seconds=round(moment - cached[0], 3))
            _CACHE.move_to_end(key)
            return value
    capability_error = None
    try:
        capabilities = _probe_cli(client, env=environment)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        # Bad native output is an unsupported capability observation, not a
        # reason to expose an exception that may contain native stdout.
        binary = shutil.which(client, path=environment.get("PATH", os.defpath))
        capabilities = {"installed": bool(binary), "path": binary}
        capability_error = "capability_probe_failed"
    installed = capabilities.get("installed") is True
    flags = {name: capabilities.get(name) is True for name in _CAPABILITY_KEYS}
    info = {
        "client": client, "installed": installed, "path": capabilities.get("path") if installed else None,
        "version": _version(capabilities.get("version")),
        "installation": "installed" if installed and flags["verified_resume"] else "unsupported" if installed else "missing",
        "auth": "unknown", "auth_method": "unknown", "auth_status_supported": installed,
        "connectivity": "unchecked", "usage": "unknown", "capabilities": flags,
        "credential_configuration": "environment_present" if any(environment.get(name, "").strip() for name in _API_ENV[client])
                                    else "not_observed",
        "login_expires_at": None, "error_code": capability_error,
        "observed_at": datetime.now(timezone.utc).isoformat(), "cached": False, "age_seconds": 0,
        "cache_ttl_seconds": CACHE_SECONDS,
    }
    if installed:
        argv = ([info["path"], "login", "status"] if client == "codex"
                else [info["path"], "auth", "status", "--json"])
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=STATUS_TIMEOUT,
                                    env=environment, check=False)
            _auth_result(client, result, info)
        except subprocess.TimeoutExpired:
            info["error_code"] = "auth_status_timeout"
        except (OSError, UnicodeError):
            info["error_code"] = "auth_status_unavailable"
    else:
        info["error_code"] = "client_missing"
    # Some native profiles obtain their key directly from the launch environment
    # while `login status` describes only the saved account. Do not erase that
    # legitimate native configuration or claim the key has been validated.
    environment_configured = info["credential_configuration"] == "environment_present"
    info["login_required"] = info["auth"] in {"signed_out", "expired_or_rejected"} and not environment_configured
    info["login_supported"] = installed and info["auth_status_supported"]
    info["ready_to_launch"] = (info["installation"] == "installed"
        and (info["auth"] == "reported_signed_in" or environment_configured)
        and info["auth"] != "expired_or_rejected"
        and info["usage"] == "unknown" and info["connectivity"] != "unavailable")
    with _LOCK:
        _CACHE[key] = (time.monotonic(), deepcopy(info))
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return info


def auth_command(client: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Return only an allowed native login command; never execute login here."""
    client, environment = _client(client), _environment(env)
    binary = shutil.which(client, path=environment.get("PATH", os.defpath))
    if not binary:
        raise ValueError(f"The native {client} client is not installed")
    if client == "claude":
        return [binary, "auth", "login"]
    command = [binary, "login"]
    if "microsoft" in platform.release().casefold():
        command.append("--device-auth")
    return command
