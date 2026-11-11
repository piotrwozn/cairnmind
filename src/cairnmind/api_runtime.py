"""Typed API/Copilot launch boundary using the existing private terminal transport.

Public profile snapshots contain references, never credential values. Only a
bearer-authenticated owned worker can resolve the referenced key in memory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

import httpx

from .client_runtime import LIVE, NativeRuntime, _ID, _atomic
from .config import ENV_PREFIXES, Settings

AGENT_CLIENTS = {"api", "copilot"}
NATIVE_CLIENTS = {"codex", "claude"}


def _read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 256 * 1024):
            raise ValueError("agent_configuration_invalid")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("agent_configuration_invalid")
    return value


def _worker_environment(profiles=()):
    # Keys are resolved after exec through the private worker endpoint, and are
    # not serialized in the tmux launch specification or inherited by shells.
    from .integrations import native_environment
    from .api_credentials import credential_environment_names
    environment = native_environment()
    names = set(credential_environment_names())
    for profile in profiles:
        reference = profile.get("credential_ref", "") or ""
        if reference.startswith("env:"):
            names.add(reference[4:])
    for name in list(environment):
        if (name in names or name.startswith(ENV_PREFIXES) or name in {"CLAUDECODE", "CODEX_THREAD_ID"}
                or name.endswith(("_API_KEY", "_AUTH_TOKEN", "_GITHUB_TOKEN"))):
            environment.pop(name, None)
    return environment


class RuntimeSupervisor(NativeRuntime):
    """All adapters share one generation-scoped registry and controller lease."""

    def __init__(self, settings, tmux_binary=None, *, credentials=None, session_transport=None):
        super().__init__(settings, tmux_binary)
        from .api_credentials import CredentialStore
        self.credentials = credentials if credentials is not None else CredentialStore(settings)
        self.session_transport = session_transport

    def diagnostics(self):
        result = super().diagnostics()
        return {**result, "adapters": {
            "native.codex": {"supported": True, "runtime": "native"},
            "native.claude": {"supported": True, "runtime": "native"},
            "cairnmind.api": {"supported": True, "runtime": "api",
                              "providers": ["openai", "anthropic", "openai_compatible", "ollama"]},
            "github.copilot": {"supported": True, "runtime": "sdk"}}}

    def _prepare_session(self, project, client, root, session_id=None):
        try:
            with httpx.Client(base_url=self.settings.url, transport=self.session_transport,
                              trust_env=False, timeout=40,
                              headers={"Authorization": f"Bearer {self.settings.token}"}) as client_http:
                response = client_http.post("/sessions/prepare", json={
                    "project": project["id"], "client": client, "worktree": root,
                    "session_id": session_id, "reason": "resume" if session_id else "startup"})
            if response.is_error:
                raise ValueError("memory_unavailable: cannot prepare the scoped agent session")
            result = response.json()
            if (not isinstance(result, dict) or not isinstance(result.get("session_id"), str)
                    or result.get("status") not in {"ready", "partial"}):
                raise ValueError("memory_unavailable: invalid preparation result")
            return result
        except (httpx.HTTPError, json.JSONDecodeError):
            raise ValueError("memory_unavailable: local memory service is unavailable") from None

    def start_project(self, project, clients=("codex", "claude"), worktree=None, *, slots=None, resume=False):
        from .integrations import install_integrations
        from .cockpit_state import CockpitState
        if not self.diagnostics()["available"]:
            raise ValueError("tmux_missing_or_unsupported: install tmux >= 3.4")
        selected = slots if slots is not None else [{"id": name, "client": name, "profile_id": name} for name in clients]
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 8
                or any(not isinstance(s, dict) or not isinstance(s.get("client"), str)
                       or s["client"] not in NATIVE_CLIENTS | AGENT_CLIENTS
                       or not _ID.fullmatch(str(s.get("id", ""))) for s in selected)
                or len({s["id"] for s in selected}) != len(selected) or type(resume) is not bool):
            raise ValueError("Select one to eight supported client slots")
        current = {**project, "root": str(Path(worktree or project["root"]).expanduser().resolve())}
        native = tuple(dict.fromkeys(s["client"] for s in selected if s["client"] in NATIVE_CLIENTS))
        installation = install_integrations(self.settings, current, clients=native) if native else None
        profiles = {p["id"]: p for p in CockpitState(self.settings).load()["profiles"]}
        runs, errors = [], []
        for slot in selected:
            try:
                if slot["client"] in NATIVE_CLIENTS:
                    run = self.start(current, slot["client"], slot_id=slot["id"], resume=resume,
                                     installation=installation, model=slot.get("model"),
                                     profile_id=slot.get("profile_id"))
                else:
                    profile = profiles.get(slot.get("profile_id"))
                    if not profile or profile["client"] != slot["client"]:
                        raise ValueError("profile_invalid: select a registered profile")
                    run = self.start_agent(current, profile, slot_id=slot["id"], resume=resume)
                runs.append(run)
            except (OSError, ValueError) as exc:
                code = str(exc).partition(":")[0]
                allowed = {"login_required", "client_missing", "client_unsupported", "native_resume_unavailable",
                           "native_resume_unsupported", "tmux_missing_or_unsupported", "credential_missing",
                           "copilot_runtime_missing", "copilot_sdk_missing", "profile_invalid",
                           "copilot_sdk_unsupported", "copilot_runtime_invalid", "agent_storage_in_project",
                           "memory_unavailable", "agent_resume_unavailable"}
                errors.append({"slot_id": slot["id"], "client": slot["client"],
                               "code": code if code in allowed else "agent_launch_failed"})
        return {"runs": runs, "errors": errors, "installation": installation or {"clients": {}, "warnings": []}}

    def start_agent(self, project, profile, *, slot_id, resume=False):
        from .api_profiles import normalize_profile
        profile = normalize_profile(profile)
        client = profile["client"]
        if (client not in AGENT_CLIENTS or not isinstance(slot_id, str) or not _ID.fullmatch(slot_id)
                or type(resume) is not bool):
            raise ValueError("profile_invalid: invalid agent profile")
        root = str(Path(project["root"]).resolve())
        if self.settings.data_dir.resolve().is_relative_to(Path(root)):
            raise ValueError("agent_storage_in_project: move the Cairnmind data directory outside the worktree")
        with self._locked():
            rows = self._read()
            matches = [r for r in rows if (r.get("project_id"), r.get("worktree"), r.get("client"),
                       r.get("slot_id"), r.get("purpose")) == (project["id"], root, client, slot_id, "coding")]
            for old in reversed(matches):
                self._refresh(old)
                if old["lifecycle"] in LIVE:
                    self._save(rows)
                    return dict(old)
            if (client == "api" and (profile.get("credential_ref") or profile.get("provider") in {"openai", "anthropic"})
                    and not self.credentials.resolve(profile)):
                raise ValueError("credential_missing: configure the profile key")
            if client == "copilot":
                from .copilot_adapter import runtime_status
                status = runtime_status(self.settings)
                if not status.get("available"):
                    raise ValueError(str(status.get("code", "copilot_runtime_missing")) + ": prepare the Copilot runtime")
            previous = matches[-1] if matches and resume else None
            if resume and (not previous or not previous.get("session_id")):
                raise ValueError("agent_resume_unavailable: choose a new agent session")
            if previous:
                from .agent_journal import AgentJournal
                journal = AgentJournal(self.settings, previous["session_id"])
                if not journal.path.is_file() or journal.path.is_symlink():
                    raise ValueError("agent_resume_unavailable: local journal is missing; start a new session from shared memory")
                try:
                    journal.events()
                except (ValueError, OSError):
                    raise ValueError("agent_resume_unavailable: local journal is incomplete; start a new session from shared memory") from None
            generation = max((r["generation"] for r in matches), default=0) + 1
            prepared = self._prepare_session(project, client, root, (previous or {}).get("session_id"))
            run_id = uuid4().hex
            configuration = {"run_id": run_id, "generation": generation, "session_id": prepared["session_id"],
                             "project_id": project["id"], "worktree": root, "client": client, "profile": profile}
            configs = self.directory / "agent-configs"
            configs.mkdir(mode=0o700, exist_ok=True)
            info = configs.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("agent_configuration_invalid")
            _atomic(configs / (run_id + ".json"), configuration)
            launch = {"command": [sys.executable, "-I", "-m", "cairnmind.api_worker", "--data-dir",
                                  str(self.settings.data_dir), "--run-id", run_id, "--generation", str(generation)],
                      "env": _worker_environment(self._credential_profiles(profile)), "cwd": root,
                      "session_id": prepared["session_id"],
                      "packet_id": (prepared.get("packet") or {}).get("id"), "profile_id": profile["id"],
                      "model": profile.get("model")}
            return self._spawn(rows, project, client, slot_id, launch, generation, "coding", run_id=run_id)

    def agent_configuration(self, run_id, generation):
        run = self._guard(run_id, generation)
        if run.get("client") not in AGENT_CLIENTS or run.get("purpose") != "coding" or run["lifecycle"] == "stopping":
            raise ValueError("agent_configuration_unavailable")
        config = _read_private(self.directory / "agent-configs" / (run_id + ".json"))
        for key in ("generation", "session_id", "project_id", "worktree", "client"):
            if config.get(key) != run.get(key):
                raise ValueError("agent_configuration_invalid")
        if config.get("run_id") != run_id:
            raise ValueError("agent_configuration_invalid")
        return {"run": run, "profile": config["profile"]}

    def _credential_profiles(self, profile=None):
        from .cockpit_state import CockpitState
        return [*CockpitState(self.settings).load()["profiles"], *([profile] if profile else [])]

    def agent_credential(self, run_id, generation):
        config = self.agent_configuration(run_id, generation)
        return self.credentials.resolve(config["profile"])

    def start_auth(self, client):
        if client != "copilot":
            return super().start_auth(client)
        from .copilot_adapter import login_command
        command = login_command(self.settings)
        with self._locked():
            rows = self._read()
            for row in rows:
                if row.get("purpose") == "auth" and row["client"] == client and self._refresh(row)["lifecycle"] in LIVE:
                    return dict(row)
            return self._spawn(rows, {"id": None, "root": str(Path.home())}, client, "auth-copilot",
                               {"command": command, "cwd": str(Path.home()),
                                "env": _worker_environment(self._credential_profiles()),
                                "session_id": None, "packet_id": None}, 1, "auth")


def api_worker_config(settings: Settings, run_id: str, generation: int) -> dict:
    """Private worker bootstrap; not exposed through normal TUI/browser APIs."""
    from .cockpit import cockpit_status
    from .terminal_client import BrokerClient
    url = BrokerClient._validated(cockpit_status(settings))
    try:
        with httpx.Client(base_url=url, trust_env=False, timeout=20,
                          headers={"Authorization": f"Bearer {settings.token}"}) as client:
            body = {"run_id": run_id, "generation": generation}
            response = client.post("/_control/agent/config", json=body)
            response.raise_for_status()
            value = response.json()
            secret = client.post("/_control/agent/credential", json=body)
            secret.raise_for_status()
            key = secret.json().get("key")
            if key is not None and not isinstance(key, str):
                raise ValueError("agent_credential_invalid")
            return {**value, "key": key}
    except (httpx.HTTPError, ValueError):
        raise ValueError("Nie można odtworzyć prywatnej konfiguracji agenta. Uruchom go ponownie z Cairnmind.") from None


def coding_profiles_status(settings: Settings) -> dict:
    """Inspect configured connections without inference, login or downloads."""
    from .api_credentials import CredentialStore
    from .cockpit import cockpit_status
    from .cockpit_state import CockpitState
    from .terminal_client import BrokerClient
    broker = None
    try:
        profiles = CockpitState(settings).load()["profiles"]
        credentials = CredentialStore(settings)
        if cockpit_status(settings).get("available"):
            broker = BrokerClient(settings)
        rows = []
        for profile in profiles:
            row = {key: profile[key] for key in ("id", "client", "label", "provider", "model") if key in profile}
            if broker is not None:
                try:
                    row["status"] = broker.request("GET", "/api/profiles/" + profile["id"] + "/status", timeout=5)
                except (ValueError, httpx.HTTPError):
                    row["status"] = {"code": "unavailable"}
            elif profile["client"] in AGENT_CLIENTS:
                row["status"] = credentials.status(profile)
                if profile["client"] == "copilot":
                    from .copilot_adapter import runtime_status
                    row["runtime"] = runtime_status(settings)
            else:
                row["status"] = {"code": "native_client_status", "client": profile["client"]}
            rows.append(row)
        return {"profiles": rows, "observed_only": True}
    except (ValueError, OSError):
        return {"profiles": [], "code": "profile_configuration_invalid"}
    finally:
        if broker is not None:
            broker.close()
