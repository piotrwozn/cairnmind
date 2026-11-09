"""Official Copilot SDK boundary with explicit preparation and host-owned tools.

Copilot is an agent runtime, not an OpenAI-compatible inference endpoint.  Its
only enabled tools are the exact custom tools supplied by DevBrain.  Memory
acknowledgements and command approvals are performed by their real handlers.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hashlib
import importlib.metadata
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import shutil
from types import SimpleNamespace
from uuid import uuid4

SDK_VERSION = "1.0.29"
RUNTIME_VERSION = "1.0.107"
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_MAX_TEXT = 2 * 1024 * 1024
_MESSAGES = {
    "copilot_sdk_missing": "Zainstaluj zależności DevBrain z obsługą Copilota.",
    "copilot_sdk_unsupported": "Wymagana jest przypięta wersja SDK Copilota.",
    "copilot_runtime_missing": "Przygotuj runtime Copilota w ustawieniach DevBrain.",
    "copilot_runtime_invalid": "Runtime Copilota zmienił się; przygotuj go ponownie.",
    "copilot_prepare_failed": "Nie udało się przygotować zweryfikowanego runtime Copilota.",
    "copilot_auth_required": "Zaloguj się do GitHub Copilot.",
    "copilot_auth_rejected": "GitHub odrzucił poświadczenia Copilota.",
    "copilot_policy_restricted": "Zasady konta lub organizacji ograniczają dostęp do Copilota.",
    "copilot_quota_exhausted": "Wyczerpano dostępny limit Copilota.",
    "copilot_rate_limited": "Copilot ograniczył częstotliwość żądań.",
    "copilot_network_unavailable": "Nie można połączyć się z usługą Copilota.",
    "copilot_failed": "Żądanie Copilota zakończyło się błędem; wynik może być niepewny.",
    "copilot_tool_limit": "Osiągnięto limit narzędzi tej tury. Kontynuuj jawnie.",
    "copilot_turn_timeout": "Osiągnięto limit czasu tury Copilota.",
    "copilot_output_limit": "Osiągnięto limit rozmiaru odpowiedzi Copilota.",
    "copilot_tools_unsupported": "Model Copilota nie potwierdził obsługi narzędzi.",
    "copilot_session_busy": "Poprzednia tura Copilota nadal trwa.",
}


class CopilotError(ValueError):
    def __init__(self, code="copilot_failed"):
        self.code = code if code in _MESSAGES else "copilot_failed"
        super().__init__(f"{self.code}: {_MESSAGES[self.code]}")


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _kind(value):
    return getattr(value, "value", value)


def _error_code(error):
    # Native/SDK error strings can contain credentials or account details.  They
    # are used for fixed classification only and never returned or logged.
    from .client_auth import normalize_runtime_error
    text = str(error)[:12000]
    normalized = normalize_runtime_error(text)
    code = {"auth_required": "copilot_auth_required", "auth_rejected": "copilot_auth_rejected",
            "quota_exhausted": "copilot_quota_exhausted", "rate_limited": "copilot_rate_limited",
            "network_unavailable": "copilot_network_unavailable"}.get(normalized)
    if code:
        return code
    lowered = text.casefold()
    if any(part in lowered for part in ("policy", "organization", "forbidden", "http 403", "not entitled")):
        return "copilot_policy_restricted"
    return "copilot_failed"


def _sdk():
    try:
        installed = importlib.metadata.version("github-copilot-sdk")
        if installed != SDK_VERSION:
            raise CopilotError("copilot_sdk_unsupported")
        from copilot import CopilotClient, RuntimeConnection, Tool, ToolResult
        from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
    except ImportError:
        raise CopilotError("copilot_sdk_missing") from None
    except importlib.metadata.PackageNotFoundError:
        raise CopilotError("copilot_sdk_missing") from None
    # The SDK logs raw JSON-RPC exception payloads, including provider HTTP
    # bodies, at WARNING.  Keep those out of terminal captures and project
    # logs; this boundary emits only our fixed diagnostic codes instead.
    logger = logging.getLogger("copilot")
    logger.setLevel(logging.CRITICAL + 1)
    logger.propagate = False
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    return SimpleNamespace(Client=CopilotClient, RuntimeConnection=RuntimeConnection, Tool=Tool, Result=ToolResult,
                           Approve=PermissionDecisionApproveOnce, Reject=PermissionDecisionReject)


def _root(settings):
    from .config import Settings
    settings = settings or Settings.load()
    return Path(settings.data_dir).expanduser().resolve()


def _runtime_root(settings):
    return _root(settings) / "runtime" / "copilot-runtime"


def _private(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CopilotError("copilot_runtime_invalid")


def _tree_manifest(root):
    rows = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if info.st_uid != os.getuid():
            raise CopilotError("copilot_runtime_invalid")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise CopilotError("copilot_runtime_invalid")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        rows.append({"path": path.relative_to(root).as_posix(), "bytes": info.st_size,
                     "sha256": digest})
    return rows


def _runtime_path(settings):
    directory = _runtime_root(settings)
    receipt = directory / "receipt.json"
    if not receipt.exists():
        raise CopilotError("copilot_runtime_missing")
    try:
        info = receipt.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2 * 1024 * 1024):
            raise ValueError
        value = json.loads(receipt.read_text())
        bundle = directory / RUNTIME_VERSION
        # Timestamps can collide for consecutive writes (including on WSL).
        # Matching metadata is not proof that executable bytes are unchanged;
        # verify the bundle whenever it is admitted, including status checks.
        if (value.get("sdk_version") != SDK_VERSION or value.get("runtime_version") != RUNTIME_VERSION
                or value.get("files") != _tree_manifest(bundle)):
            raise ValueError
        relative = value["entrypoint"]
        entry = bundle / relative
        if (not isinstance(relative, str) or not entry.resolve().is_relative_to(bundle.resolve())
                or not entry.is_file() or not os.access(entry, os.X_OK)):
            raise ValueError
        return str(entry)
    except (OSError, ValueError, KeyError, TypeError):
        raise CopilotError("copilot_runtime_invalid") from None


def runtime_status(settings=None):
    """Read local installation metadata only: no files created or downloads."""
    try:
        version = importlib.metadata.version("github-copilot-sdk")
    except importlib.metadata.PackageNotFoundError:
        return {"available": False, "code": "copilot_sdk_missing", "sdk_version": None,
                "required_sdk_version": SDK_VERSION, "runtime_version": RUNTIME_VERSION,
                "auth": "unchecked"}
    if version != SDK_VERSION:
        code, available = "copilot_sdk_unsupported", False
    else:
        try:
            _runtime_path(settings)
            code, available = "ready", True
        except CopilotError as error:
            code, available = error.code, False
    return {"available": available, "code": code, "sdk_version": version,
            "required_sdk_version": SDK_VERSION, "runtime_version": RUNTIME_VERSION,
            "auth": "unchecked"}


def prepare_runtime(settings):
    """Explicit preparation; the SDK verifies the official release SHA-256."""
    _sdk()
    if runtime_status(settings)["available"]:
        return {**runtime_status(settings), "prepared": False}
    directory = _runtime_root(settings)
    _private(directory.parent)
    _private(directory)
    from .cockpit_state import _private_lock
    try:
        # The worker's publication lock protects bundle integrity.  This
        # parent lock additionally prevents two timed-out HTTP requests from
        # queueing two complete downloads of the same pinned release.
        with _private_lock(directory / "prepare-request.lock", blocking=False):
            if runtime_status(settings)["available"]:
                return {**runtime_status(settings), "prepared": False}
            return _prepare_runtime_locked(settings)
    except BlockingIOError:
        return {**runtime_status(settings), "code": "already_preparing", "preparing": True, "prepared": False}


def _prepare_runtime_locked(settings):
    env = dict(os.environ)
    env.pop("COPILOT_CLI_PATH", None)
    env.pop("COPILOT_CLI_DOWNLOAD_BASE_URL", None)
    env["COPILOT_CLI_EXTRACT_DIR"] = str(_runtime_root(settings) / RUNTIME_VERSION)
    env["COPILOT_SKIP_CLI_DOWNLOAD"] = "0"
    try:
        result = subprocess.run([sys.executable, "-I", "-m", "devbrain.copilot_adapter", "--prepare-worker",
                                 "--data-dir", str(_root(settings))], env=env, capture_output=True,
                                text=True, timeout=660)
        if result.returncode:
            raise CopilotError("copilot_prepare_failed")
        path = _runtime_path(settings)
        if not path:
            raise CopilotError("copilot_prepare_failed")
        return {**runtime_status(settings), "prepared": True}
    except (OSError, subprocess.SubprocessError):
        raise CopilotError("copilot_prepare_failed") from None


def _prepare_worker(settings):
    _sdk()
    from copilot._cli_download import download_cli
    from copilot._cli_version import CLI_VERSION
    if CLI_VERSION != RUNTIME_VERSION:
        raise CopilotError("copilot_sdk_unsupported")
    root = _runtime_root(settings)
    _private(root.parent)
    _private(root)
    bundle = root / RUNTIME_VERSION
    # This worker is the only place automatic network preparation is enabled.
    os.environ.pop("COPILOT_CLI_DOWNLOAD_BASE_URL", None)
    os.environ.pop("COPILOT_CLI_PATH", None)
    os.environ["COPILOT_SKIP_CLI_DOWNLOAD"] = "0"
    from .cockpit_state import _atomic, _private_lock
    with _private_lock(root / "prepare.lock"):
        staged = Path(tempfile.mkdtemp(prefix=".prepare-", dir=root))
        previous = root / (".previous-" + uuid4().hex)
        try:
            # Never mix leftover files with a newly verified release bundle.
            os.environ["COPILOT_CLI_EXTRACT_DIR"] = str(staged)
            entrypoint = Path(download_cli(RUNTIME_VERSION, force=True)).resolve()
            if not entrypoint.is_relative_to(staged.resolve()):
                raise CopilotError("copilot_runtime_invalid")
            relative = entrypoint.relative_to(staged).as_posix()
            files = _tree_manifest(staged)
            if bundle.exists():
                if bundle.is_symlink() or bundle.lstat().st_uid != os.getuid():
                    raise CopilotError("copilot_runtime_invalid")
                os.replace(bundle, previous)
            os.replace(staged, bundle)
            _atomic(root / "receipt.json", {"sdk_version": SDK_VERSION, "runtime_version": RUNTIME_VERSION,
                                           "entrypoint": relative, "files": files})
        finally:
            if staged.exists():
                shutil.rmtree(staged)
            if previous.exists():
                if not bundle.exists():
                    os.replace(previous, bundle)
                else:
                    shutil.rmtree(previous)


def login_command(settings):
    _sdk()
    _runtime_path(settings)
    return [sys.executable, "-I", "-m", "devbrain.copilot_adapter", "--login", "--data-dir", str(_root(settings))]


async def _notify(callback, value):
    if callback is not None:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


class CopilotAdapter:
    """An isolated official SDK session whose side effects belong to DevBrain."""
    def __init__(self, profile, key=None, *, settings=None, session_id=None, client_factory=None, sdk=None):
        self.profile = dict(profile)
        self.key, self.settings = key, settings
        self.logical_session_id = session_id or uuid4().hex
        if not isinstance(self.logical_session_id, str) or not _SESSION.fullmatch(self.logical_session_id):
            raise ValueError("Invalid Copilot session ID")
        self.session_id = "devbrain-" + self.logical_session_id
        self._factory, self._sdk_override = client_factory, sdk
        self._client = self._session = None
        self._sdk_api = None
        self._handler = None
        self._allowed = {}
        self._on_delta = self._on_event = None
        self._active = False
        self._calls = 0
        self._limit_code = None
        self._seen_calls = set()
        self._usage = {}
        self._callbacks = []
        self._delta_buffer = ""
        self._output_chars = 0
        self._events = None
        self._event_consumer = None
        self._catalog_hash = None
        self._usage_ids = set()
        self._observed_usage_calls = self._reported_copilot_calls = 0
        self._tool_tasks = set()
        self._turn_task = None
        self._accept_tools = False

    def _redact(self, value):
        return value.replace(self.key, "[REDACTED]") if self.key and isinstance(value, str) else value

    async def _ensure_client(self):
        if self._client is not None:
            return
        self._sdk_api = self._sdk_override or _sdk()
        base = _root(self.settings) / "runtime" / "copilot"
        _private(base.parent)
        _private(base)
        env = dict(os.environ)
        for name in ("COPILOT_CLI_PATH", "COPILOT_CLI_DOWNLOAD_BASE_URL", "COPILOT_CLI_EXTRACT_DIR",
                     "GITHUB_COPILOT_API_TOKEN", "COPILOT_API_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                     "COPILOT_OFFLINE"):
            env.pop(name, None)
        # A Copilot profile uses GitHub account access. Ambient BYOK settings
        # override that routing even for a signed-in user; they belong to the
        # separate API profiles, never this runtime's inherited environment.
        for name in list(env):
            if name.startswith("COPILOT_PROVIDER_"):
                env.pop(name)
        env["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
        env["COPILOT_TELEMETRY_DISABLED"] = "true"
        if self.key:
            for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
                env.pop(name, None)
        options = {"mode": "empty", "base_directory": str(base), "working_directory": str(base),
                   "env": env, "log_level": "error", "use_logged_in_user": not bool(self.key)}
        if self.key:
            options["github_token"] = self.key
        if self._factory is None:
            options["connection"] = self._sdk_api.RuntimeConnection.for_stdio(path=_runtime_path(self.settings))
        factory = self._factory or self._sdk_api.Client
        self._client = factory(**options)
        try:
            await self._client.start()
        except Exception as error:
            self._client = None
            raise CopilotError(_error_code(error)) from None

    async def auth_status(self):
        await self._ensure_client()
        try:
            result = await self._client.get_auth_status()
            authenticated = _get(result, "isAuthenticated") is True
            return {"authenticated": authenticated, "auth": "signed_in" if authenticated else "signed_out",
                    "code": "ready" if authenticated else "copilot_auth_required"}
        except Exception as error:
            raise CopilotError(_error_code(error)) from None

    async def list_models(self):
        await self._ensure_client()
        try:
            rows = await self._client.list_models()
            return [{"id": str(_get(row, "id")), "name": str(_get(row, "name", _get(row, "id")))[:160],
                     "enabled": _get(_get(row, "policy", {}), "state", "enabled") != "disabled"}
                    for row in rows if isinstance(_get(row, "id"), str)][:1000]
        except Exception as error:
            raise CopilotError(_error_code(error)) from None

    async def _permission(self, request, invocation):
        permitted = (self._active and self._accept_tools
                     and _kind(_get(request, "kind")) == "custom-tool"
                     and _get(request, "tool_name") in self._allowed
                     and _get(request, "managed_approval_required") is not True
                     and _get(invocation, "session_id") == self.session_id)
        if permitted:
            # This only admits a host callback; that callback still performs
            # exact memory ACK and actual one-use command approval checks.
            return self._sdk_api.Approve()
        return self._sdk_api.Reject(feedback="Only registered DevBrain tools are permitted.")

    async def _pre_tool(self, data, invocation):
        allowed = (_get(data, "toolName") in self._allowed
                   and _get(invocation, "session_id") == self.session_id)
        return {"permissionDecision": "allow" if allowed else "deny"}

    async def _block_subagent(self, data, invocation):
        return {"decision": "block", "reason": "DevBrain does not enable Copilot subagents."}

    def _tool(self, descriptor):
        name = descriptor["name"]

        async def call(invocation):
            if not self._active or not self._accept_tools or _get(invocation, "session_id") != self.session_id:
                return {"error": "copilot_tool_scope_rejected"}
            if _get(invocation, "agent_id"):
                return {"error": "copilot_subagent_rejected"}
            call_id = _get(invocation, "tool_call_id")
            if not isinstance(call_id, str) or not call_id or call_id in self._seen_calls:
                return {"error": "copilot_duplicate_or_missing_tool_call"}
            self._seen_calls.add(call_id)
            args = _get(invocation, "arguments")
            if not isinstance(args, dict):
                return {"error": "copilot_invalid_tool_arguments"}
            from jsonschema import Draft202012Validator
            if not Draft202012Validator(descriptor["parameters"]).is_valid(args):
                return {"error": "copilot_invalid_tool_arguments"}
            self._calls += 1
            maximum = self.profile.get("limits", {}).get("max_tool_calls", 20)
            if self._calls > maximum:
                self._set_limit("copilot_tool_limit")
                return {"error": "copilot_tool_limit"}
            # Determine arity before dispatch.  Retrying after a TypeError
            # raised *inside* the handler could repeat an actual side effect.
            try:
                inspect.signature(self._handler).bind(name, args, call_id)
                with_call_id = True
            except (TypeError, ValueError):
                with_call_id = False
            if with_call_id:
                return await self._handler(name, args, call_id)
            return await self._handler(name, args)

        async def sdk_call(invocation):
            task = asyncio.current_task()
            if task is not None:
                self._tool_tasks.add(task)
            try:
                try:
                    payload = await call(invocation)
                    encoded = self._redact(json.dumps(payload, ensure_ascii=False, allow_nan=False))
                    failure = isinstance(payload, dict) and bool(payload.get("error"))
                    return self._sdk_api.Result(text_result_for_llm=encoded,
                                                result_type="failure" if failure else "success")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The SDK passes ToolResult to its pending-tool RPC.  A
                    # plain dict is not normalized by direct Tool handlers.
                    # Do not leak arbitrary exception text into its logs or
                    # back into the model after an uncertain side effect.
                    return self._sdk_api.Result(text_result_for_llm='{"error":"devbrain_tool_failed"}',
                                                result_type="failure", error="devbrain_tool_failed")
            finally:
                if task is not None:
                    self._tool_tasks.discard(task)

        return self._sdk_api.Tool(name=name, description=descriptor["description"],
                                  parameters=descriptor["parameters"], handler=sdk_call,
                                  skip_permission=False, defer="never")

    def _session_options(self, tools, system):
        return {"model": self.profile.get("model"), "client_name": "devbrain",
                "tools": [self._tool(row) for row in tools],
                "available_tools": ["custom:" + row["name"] for row in tools],
                "excluded_tools": ["builtin:*", "mcp:*"],
                "on_permission_request": self._permission,
                "hooks": {"on_pre_tool_use": self._pre_tool, "on_subagent_start": self._block_subagent},
                "system_message": {"mode": "replace", "content": system},
                "mcp_servers": {}, "custom_agents": [], "skill_directories": [], "plugin_directories": [],
                "agent_directories": [], "hook_directories": [], "instruction_directories": [],
                "enable_skills": False, "included_builtin_skills": [], "enable_config_discovery": False,
                "enable_on_demand_instruction_discovery": False, "skip_custom_instructions": True,
                "enable_file_hooks": False, "enable_host_git_operations": False, "enable_session_store": False,
                "skip_embedding_retrieval": True, "enable_session_telemetry": False,
                "memory": {"enabled": False}, "tool_search": {"enabled": False},
                "infinite_sessions": {"enabled": False}, "large_output": {"enabled": False},
                "autopilot": {"continuation_enabled": False, "max_continues": 0},
                "streaming": True, "on_event": self._event}

    async def _ensure_session(self, tools, system):
        await self._ensure_client()
        if self._session is not None:
            return
        opts = self._session_options(tools, system)
        try:
            sessions = await self._client.list_sessions()
            exists = any(_get(row, "sessionId", _get(row, "session_id")) == self.session_id for row in sessions)
            if exists:
                self._session = await self._client.resume_session(self.session_id, continue_pending_work=False,
                                                                  allow_transcript_recovery=False, **opts)
            else:
                self._session = await self._client.create_session(session_id=self.session_id, **opts)
        except Exception as error:
            raise CopilotError(_error_code(error)) from None

    def _event(self, event):
        kind = _kind(_get(event, "type"))
        data = _get(event, "data", {})
        if _get(data, "agent_id") or _get(event, "agent_id"):
            return
        if kind == "assistant.message_delta":
            text = _get(data, "delta_content", "")
            if isinstance(text, str):
                self._output_chars += len(text)
                if self._output_chars > _MAX_TEXT:
                    self._set_limit("copilot_output_limit")
                else:
                    self._enqueue(("delta", text))
        elif kind == "assistant.usage":
            identity = _get(data, "accounting")
            identity_key = (_get(identity, "source_session_id"), _get(identity, "usage_id"))
            if all(isinstance(part, str) and part for part in identity_key):
                if identity_key in self._usage_ids:
                    return
                self._usage_ids.add(identity_key)
            usage = {}
            for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost"):
                value = _get(data, name)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    self._usage[name] = self._usage.get(name, 0) + value
                    usage[name] = value
            self._observed_usage_calls += 1
            amount = _get(_get(data, "copilot_usage"), "total_nano_aiu")
            if type(amount) in (int, float) and math.isfinite(amount) and amount >= 0:
                self._reported_copilot_calls += 1
                copilot = self._usage.setdefault("copilot", {})
                copilot["total_nano_aiu"] = copilot.get("total_nano_aiu", 0) + amount
                usage["copilot"] = {"total_nano_aiu": amount, "unit": "nano_aiu"}
            copilot = self._usage.setdefault("copilot", {})
            copilot.update(unit="nano_aiu", observed_calls=self._observed_usage_calls,
                           reported_calls=self._reported_copilot_calls,
                           status="unavailable" if self._reported_copilot_calls == 0 else
                           "complete" if self._reported_copilot_calls == self._observed_usage_calls else "partial")
            self._enqueue(("event", {"type": "usage", "usage": usage}))
        elif kind == "session.error":
            self._limit_code = _error_code(_get(data, "message", ""))

    def _set_limit(self, code):
        if self._limit_code is None:
            self._limit_code = code
            self._callbacks.append(asyncio.create_task(self.interrupt()))

    def _enqueue(self, value):
        if self._events is None:
            return
        try:
            self._events.put_nowait(value)
        except asyncio.QueueFull:
            self._set_limit("copilot_output_limit")

    async def _consume_events(self):
        while True:
            entry = await self._events.get()
            if entry is None:
                return
            kind, value = entry
            if kind == "delta":
                await self._emit_delta(value)
            else:
                await _notify(self._on_event, value)

    async def _emit_delta(self, text):
        self._delta_buffer += text
        if len(self._delta_buffer) > _MAX_TEXT:
            self._limit_code = "copilot_output_limit"
            await self.interrupt()
            return
        if self.key:
            hold = max(0, len(self.key) - 1)
            if len(self._delta_buffer) <= hold:
                return
            # Keep a possible split secret prefix until the next fragment.
            safe = self._redact(self._delta_buffer)
            if len(safe) <= hold:
                self._delta_buffer = safe
                return
            emitted, self._delta_buffer = safe[:-hold] if hold else safe, safe[-hold:] if hold else ""
        else:
            emitted, self._delta_buffer = text, ""
        await _notify(self._on_delta, emitted)

    async def run_turn(self, prompt, tools, handler, *, system="", on_delta=None, on_event=None):
        if self._active:
            raise CopilotError("copilot_session_busy")
        if not isinstance(prompt, str) or len(prompt) > _MAX_TEXT or not callable(handler):
            raise ValueError("Invalid Copilot turn")
        allowed = {}
        for row in tools:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                    or not _NAME.fullmatch(row["name"]) or row["name"] in allowed
                    or not isinstance(row.get("parameters"), dict) or not isinstance(row.get("description"), str)):
                raise ValueError("Invalid Copilot custom tool schema")
            allowed[row["name"]] = row
        # Recreate the SDK session when the host's tool catalog changes.  A
        # former broader allowlist must never survive a narrowed host catalog.
        catalog_hash = hashlib.sha256(json.dumps([tools, system], sort_keys=True).encode()).hexdigest()
        if self._session is not None and catalog_hash != self._catalog_hash:
            await self.reset_context()
        self._catalog_hash = catalog_hash
        self._allowed, self._handler = allowed, handler
        self._on_delta, self._on_event = on_delta, on_event
        self._calls, self._limit_code = 0, None
        self._usage, self._callbacks, self._delta_buffer = {}, [], ""
        self._usage_ids = set()
        self._observed_usage_calls = self._reported_copilot_calls = 0
        self._output_chars = 0
        self._events = asyncio.Queue(maxsize=1024)
        self._event_consumer = asyncio.create_task(self._consume_events())
        self._active = True
        self._accept_tools = True
        self._turn_task = asyncio.current_task()
        try:
            await self._ensure_session(tools, system)
            timeout = self.profile.get("limits", {}).get("max_turn_seconds", 900)
            result = await self._session.send_and_wait(prompt, agent_mode="interactive", timeout=timeout)
            await self._events.put(None)
            await self._event_consumer
            if self._callbacks:
                await asyncio.gather(*self._callbacks)
            if self._delta_buffer:
                await _notify(self._on_delta, self._redact(self._delta_buffer))
                self._delta_buffer = ""
            if self._limit_code:
                raise CopilotError(self._limit_code)
            text = _get(_get(result, "data", {}), "content", "") if result else ""
            if not isinstance(text, str) or len(text) > _MAX_TEXT:
                raise CopilotError("copilot_output_limit")
            return {"text": self._redact(text), "tool_calls": [], "usage": dict(self._usage),
                    "copilot_session_id": self.session_id}
        except asyncio.CancelledError:
            await self.interrupt()
            raise
        except TimeoutError:
            await self.interrupt()
            raise CopilotError("copilot_turn_timeout") from None
        except CopilotError:
            raise
        except Exception as error:
            await self.interrupt()
            raise CopilotError(self._limit_code or _error_code(error)) from None
        finally:
            self._accept_tools = False
            await self._cancel_tools()
            if self._event_consumer and not self._event_consumer.done():
                self._event_consumer.cancel()
                with suppress(asyncio.CancelledError):
                    await self._event_consumer
            self._events = self._event_consumer = None
            self._active = False
            self._turn_task = None
            self._handler = None
            self._on_delta = self._on_event = None

    async def probe(self):
        nonce = uuid4().hex
        observed = []

        async def handler(name, args):
            if name == "devbrain_connection_probe" and args == {"nonce": nonce}:
                observed.append(nonce)
                return {"ok": True}
            return {"error": "invalid_probe"}

        tools = [{"name": "devbrain_connection_probe", "description": "Confirm a DevBrain connection.",
                  "parameters": {"type": "object", "properties": {"nonce": {"type": "string", "enum": [nonce]}},
                                 "required": ["nonce"], "additionalProperties": False}}]
        reply = await self.run_turn(f"Call devbrain_connection_probe exactly once with nonce {nonce}.", tools, handler,
                                    system="You are testing a connection. Call only the supplied probe tool.")
        return {"status": "ready" if observed else "unsupported", "code": "ready" if observed else "copilot_tools_unsupported",
                "tool_call_verified": bool(observed), "usage": reply.get("usage", {})}

    async def interrupt(self):
        self._accept_tools = False
        await self._cancel_tools()
        if self._session is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._session.abort(), 5)

    async def _cancel_tools(self):
        current = asyncio.current_task()
        tasks = [task for task in self._tool_tasks if not task.done()
                 and task is not current and task is not self._turn_task]
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)

    async def reset_context(self):
        await self.interrupt()
        if self._session is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._session.disconnect(), 5)
        self._session = None
        self._catalog_hash = None
        self.session_id = "devbrain-" + uuid4().hex

    async def close(self):
        await self.interrupt()
        if self._session is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._session.disconnect(), 5)
            self._session = None
        if self._client is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._client.stop(), 5)
            self._client = None


async def _login(settings):
    """Device codes exist only in this operational terminal, never in memory."""
    from .terminal_safe import plain
    from copilot.generated.rpc import (AuthLoginAdvanceRequest, AuthLoginBeginRequest, AuthLoginCancelRequest,
                                       AuthLoginMethod, LoginProviderKind)
    adapter = CopilotAdapter({"client": "copilot"}, settings=settings)
    flow_id = None
    try:
        await adapter._ensure_client()
        # An auth-only session has no model tools and never sends a prompt.
        adapter._allowed = {}
        await adapter._ensure_session([], "Authentication only. No model turn is permitted.")
        api = adapter._session.rpc.accounts.login
        begun = await api.begin(AuthLoginBeginRequest(kind=LoginProviderKind.GITHUB_DOT_COM,
                                                      method=AuthLoginMethod.DEVICE_CODE), timeout=30)
        flow_id, step = begun.flow_id, begun.step
        for _ in range(20):
            kind = _kind(step.kind)
            if kind == "device-code":
                print("Otwórz:", plain(step.verification_uri or "https://github.com/login/device"), flush=True)
                print("Kod:", plain(step.user_code or ""), flush=True)
            elif kind == "open-url":
                print("Otwórz:", plain(step.url or ""), flush=True)
            elif kind == "input-required":
                # No arbitrary provider text or secret is persisted.
                value = await asyncio.to_thread(input, plain(step.prompt or "Wartość: ") + " ")
                step = await api.advance(AuthLoginAdvanceRequest(flow_id=flow_id, input=value), timeout=300)
                continue
            elif kind == "completed":
                disposition = _kind(_get(step.result, "status", "completed"))
                if disposition == "completed":
                    print("Logowanie Copilot zakończone.", flush=True)
                    flow_id = None
                    return
                # The runtime may request plaintext storage/account selection.
                # Keep that explicit rather than accepting any storage silently.
                if disposition == "needs-plaintext-consent":
                    value = await asyncio.to_thread(input, "Zapisać poświadczenia lokalnie bez szyfrowania? [tak/N]: ")
                    # The pinned runtime's login protocol accepts "allow";
                    # a human-facing "yes" is interpreted as declined.
                    value = "allow" if value.strip().casefold() in {"tak", "yes", "y"} else "deny"
                    step = await api.advance(AuthLoginAdvanceRequest(flow_id=flow_id, input=value), timeout=300)
                    continue
                raise CopilotError("copilot_auth_required")
            elif kind == "error":
                raise CopilotError("copilot_auth_rejected")
            step = await api.advance(AuthLoginAdvanceRequest(flow_id=flow_id), timeout=600)
        raise CopilotError("copilot_auth_required")
    finally:
        if flow_id and adapter._session is not None:
            with suppress(Exception):
                await adapter._session.rpc.accounts.login.cancel(AuthLoginCancelRequest(flow_id=flow_id), timeout=5)
        await adapter.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--prepare-worker", action="store_true")
    operation.add_argument("--login", action="store_true")
    args = parser.parse_args()
    from .config import Settings
    settings = Settings.load(args.data_dir)
    try:
        if args.prepare_worker:
            _prepare_worker(settings)
        else:
            asyncio.run(_login(settings))
    except (CopilotError, KeyboardInterrupt) as error:
        print(str(error) if isinstance(error, CopilotError) else "Anulowano logowanie.", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(_MESSAGES["copilot_failed"], file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
