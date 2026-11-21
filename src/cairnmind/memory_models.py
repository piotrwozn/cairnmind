"""Which model servers the knowledge agent uses: one for chat (answers, extraction), one for embeddings.

Each role is the local Ollama of the settings unless a profile names another server. A profile holds
no secret: a key is named by a reference (env:NAME or profile:ID, as for coding connections) and read
only when a request is made. A server that is not on this computer receives memory content, so it
needs HTTPS and the person's explicit consent (allow_remote), given with `cairnmind models set`; it
receives only the sensitivity classes the setting memory_remote_classes allows (memory_egress.py).

Vectors of two models are kept apart: their namespace is the embedding model's identity, and every vector
index is keyed by namespace and dimension. An OpenAI-compatible server reports no digest, so there the
identity is the server and the model name; for the embedding role a fixed request detects other weights
served under the same name (memory_fingerprint.py). Evaluations that pass are recorded per pair of model
identities and setup.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .api_profiles import MODEL_ID, _credential_ref, normalize_base_url
from .config import Settings
from .memory_openai import OpenAICompatible, server_tag
from .ollama import Ollama, OllamaUnavailable

ROLES = ("chat", "embedding")
PROVIDERS = ("ollama", "openai_compatible")
REASONING_EFFORTS = ("none", "low", "medium", "high")
FIELDS = {"chat": "memory_chat", "embedding": "memory_embedding"}
MODEL_FIELDS = {"chat": "chat_model", "embedding": "embedding_model"}
EVALUATIONS = ("recall", "memory", "repository")
REQUIRED_EVALUATIONS = ("memory", "recall")
EVALUATED_FILE = "models.evaluated.json"
MAX_RECORDED_PAIRS = 50
LABEL_SECONDS = 2.0  # naming the models for an answer's label never holds back a finished answer


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


def normalize_memory_model(row: Any, role: str) -> dict:
    """A canonical profile for one role, or ValueError. It never holds a key."""
    if role not in ROLES:
        raise ValueError("The role is chat or embedding.")
    if not isinstance(row, dict):
        raise ValueError("Invalid memory model profile.")
    if set(row) - {"provider", "base_url", "credential_ref", "context_window", "allow_remote", "reasoning_effort"}:
        raise ValueError("The memory model profile has unsupported fields; keys are named by a credential reference.")
    provider = row.get("provider")
    if provider not in PROVIDERS:
        raise ValueError("Choose a memory model provider: ollama or openai_compatible.")
    base_url = row.get("base_url", "http://127.0.0.1:11434" if provider == "ollama" else None)
    if not base_url:
        raise ValueError("An OpenAI-compatible server needs its address (--base-url).")
    result: dict[str, Any] = {"provider": provider, "base_url": normalize_base_url(base_url)}
    allow_remote = row.get("allow_remote", False)
    if type(allow_remote) is not bool:
        raise ValueError("allow_remote is true or false.")
    if not is_loopback(result["base_url"]):
        if not allow_remote:
            raise ValueError("A model server on another computer receives memory content: your questions and "
                             "the text of the classes in memory_remote_classes (by default project files "
                             "only). Confirm that with --allow-remote.")
        result["allow_remote"] = True
    reference = _credential_ref(row.get("credential_ref"))
    if reference is not None:
        result["credential_ref"] = reference
    window = row.get("context_window")
    if window is not None:
        if role != "chat":
            raise ValueError("A context window is set only for the chat role.")
        if type(window) is not int or not 4096 <= window <= 262144:
            raise ValueError("The context window is a number of tokens from 4096 to 262144.")
        result["context_window"] = window
    effort = row.get("reasoning_effort")
    if effort is not None:
        if role != "chat" or provider != "openai_compatible":
            raise ValueError("A reasoning effort is set only for the chat role on an OpenAI-compatible server.")
        if effort not in REASONING_EFFORTS:
            raise ValueError("The reasoning effort is none, low, medium or high.")
        result["reasoning_effort"] = effort
    return result


def profile(settings: Settings, role: str) -> dict | None:
    """The validated profile of one role, or None for the local Ollama of the settings."""
    row = getattr(settings, FIELDS[role], None)
    return None if row is None else normalize_memory_model(row, role)


def credential_for(settings: Settings, role: str, credentials: Settings | None = None):
    """A function that reads the role's key when a request needs it, or None when it names none.

    The service is its own process: a key kept only in the broker's session memory cannot reach it,
    so only an environment variable or a key saved in the local credential file works here.
    """
    row = profile(settings, role)
    if row is None or "credential_ref" not in row:
        return None
    from .api_credentials import CredentialStore
    store = CredentialStore(credentials or settings)
    return lambda: store.resolve(row)


def server_identity(settings: Settings, role: str) -> str:
    row = profile(settings, role) or {"provider": "ollama", "base_url": settings.ollama_url}
    return server_tag(row["base_url"]) if row["provider"] == "openai_compatible" else "ollama:" + row["base_url"]


def is_default(settings: Settings) -> bool:
    """The release's models on the local Ollama: what the release evaluation covers."""
    fields = Settings.__dataclass_fields__
    return (settings.memory_chat is None and settings.memory_embedding is None
            and settings.chat_model == fields["chat_model"].default
            and settings.embedding_model == fields["embedding_model"].default)


def context_tokens(settings: Settings) -> int:
    row = profile(settings, "chat")
    return row["context_window"] if row and "context_window" in row else settings.num_ctx


def setup_tag(settings: Settings) -> str:
    """What a pair of model identities does not name: each role's server, the context window, the reasoning effort."""
    rows = {}
    for role in ROLES:
        row = profile(settings, role) or {"provider": "ollama", "base_url": settings.ollama_url}
        rows[role] = {key: value for key, value in row.items() if key != "credential_ref"}
    rows["chat"]["context_window"] = context_tokens(settings)
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]


def destinations(settings: Settings) -> dict:
    """Per role: which kind of server runs it and whether content leaves this computer."""
    result = {}
    for role in ROLES:
        row = profile(settings, role) or {"provider": "ollama", "base_url": settings.ollama_url}
        result[role] = {"provider": row["provider"], "destination": _destination(row, role)["destination"]}
    return result


def inference_report(settings: Settings) -> dict:
    """The inference fields of an evaluation report: unchanged for the local Ollama of the settings."""
    if settings.memory_chat is None and settings.memory_embedding is None:
        return {"inference": "local_ollama"}
    return {"inference": "configured_model_servers", "model_servers": destinations(settings)}


def _client(settings: Settings, role: str, credentials: Settings | None):
    row = profile(settings, role)
    if row is None:
        return None
    credential = credential_for(settings, role, credentials)
    if row["provider"] == "ollama":
        return Ollama(settings, base_url=row["base_url"], credential=credential, num_ctx=row.get("context_window"))
    client = OpenAICompatible(settings, role, row, credential=credential)
    # An evaluation copy compares with the installation's stored fingerprint, not an empty one (#212).
    client.fingerprint_dir = (credentials or settings).data_dir
    return client


class MemoryModels:
    """The methods of the Ollama client, with chat and embeddings on their own servers."""

    def __init__(self, settings: Settings, chat, embedding):
        self.settings = settings
        self.chat_client = chat
        self.embedding_client = embedding

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        return await self.chat_client.chat(messages, tools, **kwargs)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.embedding_client.embed(texts)

    async def models(self) -> dict:
        chat = await self.chat_client.models()
        if self.embedding_client is self.chat_client:
            return chat
        embedding = await self.embedding_client.models()
        return {"models": [*chat.get("models", []), *embedding.get("models", [])]}

    async def model_identity(self, name: str) -> str:
        if name == self.settings.embedding_model:
            return await self.embedding_client.model_identity(name)
        return await self.chat_client.model_identity(name)

    async def model_digest(self, name: str) -> str | None:
        if name == self.settings.embedding_model:
            return await self.embedding_client.model_digest(name)
        return await self.chat_client.model_digest(name)

    async def identities(self) -> dict:
        return {"chat": await self.chat_client.model_identity(self.settings.chat_model),
                "embedding": await self.embedding_client.model_identity(self.settings.embedding_model)}

    async def aclose(self) -> None:
        await self.chat_client.aclose()
        if self.embedding_client is not self.chat_client:
            await self.embedding_client.aclose()


def from_settings(settings: Settings, *, credentials: Settings | None = None):
    """The client the knowledge agent uses. With no profile it is exactly the local Ollama client.

    credentials names the installation whose saved keys are read, when settings is an evaluation copy.
    """
    chat, embedding = _client(settings, "chat", credentials), _client(settings, "embedding", credentials)
    if chat is None and embedding is None:
        return Ollama(settings)
    if chat is None or embedding is None:
        local = Ollama(settings)
        chat, embedding = chat or local, embedding or local
    return MemoryModels(settings, chat, embedding)


async def identities(models, settings: Settings) -> dict:
    if hasattr(models, "identities"):
        return await models.identities()
    return {"chat": await models.model_identity(settings.chat_model),
            "embedding": await models.model_identity(settings.embedding_model)}


async def ready_kinds(models) -> tuple[list[str], Exception | None]:
    """Background job kinds whose server answers now (embed: the embedding role, extract: the chat role).

    With split servers one role that is down does not stop the other; it is returned as the error.
    Raises when no role answers, as the single Ollama client does.
    """
    if not isinstance(models, MemoryModels):
        await models.models()
        return ["embed", "extract"], None
    kinds, error = [], None
    for kind, client in (("embed", models.embedding_client), ("extract", models.chat_client)):
        try:
            await client.models()
            kinds.append(kind)
        except OllamaUnavailable as exc:
            error = exc
    if not kinds:
        raise error
    return kinds, error


# Evaluations ------------------------------------------------------------------------------------------

def _read_evaluations(settings: Settings) -> list[dict]:
    try:
        document = json.loads((settings.data_dir / EVALUATED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pairs = document.get("pairs") if isinstance(document, dict) and document.get("version") == 1 else None
    return [pair for pair in pairs if isinstance(pair, dict) and isinstance(pair.get("chat"), str)
            and isinstance(pair.get("embedding"), str) and isinstance(pair.get("passed"), dict)] if isinstance(
        pairs, list) else []


def _write_json(path: Path, document: Any) -> None:
    """Atomic and owner-readable."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.stem + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _matches(entry: dict, pair: dict, setup: str) -> bool:
    return entry["chat"] == pair.get("chat") and entry["embedding"] == pair.get("embedding") and (
        entry.get("setup") == setup)


def record_evaluation(settings: Settings, kind: str, pair: dict) -> None:
    """Remember that an evaluation passed with exactly this pair of model identities and this setup."""
    if kind not in EVALUATIONS:
        raise ValueError("Unknown evaluation.")
    setup = setup_tag(settings)
    pairs = _read_evaluations(settings)
    entry = next((p for p in pairs if _matches(p, pair, setup)), None)
    if entry is None:
        entry = {"chat": pair["chat"], "embedding": pair["embedding"], "setup": setup, "passed": {}}
        pairs.append(entry)
    entry["passed"][kind] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pairs = [p for p in pairs if p is not entry][-(MAX_RECORDED_PAIRS - 1):] + [entry]
    _write_json(settings.data_dir / EVALUATED_FILE, {"version": 1, "pairs": pairs})


def evaluation_status(settings: Settings, pair: dict) -> dict:
    try:
        setup = setup_tag(settings)
    except ValueError:  # an invalid saved profile: nothing was evaluated with it
        return {"evaluated": False, "passed": []}
    entry = next((p for p in _read_evaluations(settings) if _matches(p, pair, setup)), None)
    passed = sorted(kind for kind in (entry or {}).get("passed", {}) if kind in EVALUATIONS)
    return {"evaluated": all(kind in passed for kind in REQUIRED_EVALUATIONS), "passed": passed}


def record_passed(settings: Settings, kind: str, pair: dict | None = None) -> bool:
    """After a passing evaluation: record it for the models in use now. False when they cannot be named."""
    try:
        pair = pair if pair and all(isinstance(pair.get(r), str) for r in ROLES) else asyncio.run(
            identities(from_settings(settings), settings))
        record_evaluation(settings, kind, pair)
    except (OllamaUnavailable, ValueError, OSError):
        return False
    return True


async def answer_label(settings: Settings, models) -> str | None:
    """None for the release's models; else whether this pair passed recall and memory evaluations."""
    if is_default(settings):
        return None
    try:
        pair = await asyncio.wait_for(identities(models, settings), LABEL_SECONDS)
    except Exception:  # an unnamed or slow-to-name model is not an evaluated one; the answer is unaffected
        return "unevaluated"
    return "evaluated" if evaluation_status(settings, pair)["evaluated"] else "unevaluated"


# doctor and `models set` -----------------------------------------------------------------------------

def _destination(row: dict, role: str) -> dict:
    if is_loopback(row["base_url"]):
        return {"destination": "this computer", "content_sent_to": "Memory content stays on this computer."}
    host = urlsplit(row["base_url"]).hostname
    return {"destination": "remote", "content_sent_to": (
        f"Memory content (sources, conversation text, questions) is sent to {host} for {role}.")}


def _doctor_role(settings: Settings, role: str, http: httpx.Client) -> dict:
    model = getattr(settings, MODEL_FIELDS[role])
    try:
        row = profile(settings, role) or {"provider": "ollama", "base_url": settings.ollama_url}
    except ValueError as exc:
        return {"model": model, "reachable": False, "error": f"Invalid profile: {exc}"}
    from .memory_egress import describe
    entry = {"provider": row["provider"], "base_url": row["base_url"], "model": model, **_destination(row, role),
             **describe(settings, role), "reachable": False}
    fingerprinted = None
    if role == "embedding" and row["provider"] != "ollama":
        # Other weights behind the same name (#212): the stored mark holds embeddings in every process,
        # so it is shown even while the server is down or does not list the model.
        from .memory_fingerprint import doctor_block
        fingerprinted, entry["fingerprint"] = doctor_block(settings.data_dir, f"{model}@{server_tag(row['base_url'])}")
    headers = {}
    if "credential_ref" in row:
        from .api_credentials import CredentialStore
        store = CredentialStore(settings)
        try:
            entry["credential"] = store.status(row)
            key = store.resolve(row)
        except ValueError:
            entry["credential"], key = {"configured": False, "storage": "none", "code": "credential_invalid"}, None
        if key:
            headers["Authorization"] = "Bearer " + key
    path = "/api/tags" if row["provider"] == "ollama" else "/models"
    try:
        response = http.get(row["base_url"].rstrip("/") + path, headers=headers)
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        entry["error"] = type(exc).__name__
        return entry
    entry["reachable"] = True
    rows = document.get("models" if row["provider"] == "ollama" else "data") if isinstance(document, dict) else None
    if not isinstance(rows, list):  # a server outside Cairnmind's control: reported, never trusted
        entry.update(error="invalid_response", model_available=False)
        return entry
    if row["provider"] == "ollama":
        listed = [m for m in rows if isinstance(m, dict) and model in (m.get("name"), m.get("model"))]
        if listed and isinstance(listed[0].get("digest"), str) and listed[0]["digest"]:
            entry["identity"] = f"{model}@{listed[0]['digest']}"
    else:
        listed = [m for m in rows if isinstance(m, dict) and m.get("id") == model]
        if listed:
            entry["identity"] = f"{model}@{server_tag(row['base_url'])}"
            if "fingerprint" in entry:  # the generation in use; no identity while changed (#212)
                if fingerprinted is None:
                    del entry["identity"]
                else:
                    entry["identity"] = fingerprinted
    entry["model_available"] = bool(listed)
    if role == "chat" and listed:
        # What the service saw (#231); doctor itself sends no model request, so nothing seen is not "available".
        from .structured_output import doctor_note
        entry["structured_output"] = doctor_note(settings.data_dir, row["base_url"], model) or {
            "refused": False, "text": "structured output: no refusal seen"}
    return entry


def doctor_models(settings: Settings, http: httpx.Client) -> dict:
    """Read-only: where each role's content goes, whether its server answers, and the evaluation record."""
    result: dict[str, Any] = {role: _doctor_role(settings, role, http) for role in ROLES}
    pair = {role: result[role].get("identity") for role in ROLES}
    status = (evaluation_status(settings, pair) if all(pair.values())
              else {"evaluated": False, "passed": []})
    from .memory_egress import doctor_block
    result.update(release_models=is_default(settings), evaluated=status["evaluated"],
                  evaluations_passed=status["passed"], remote_classes=doctor_block(settings))
    return result


CHECK_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                "additionalProperties": False}


async def _check(settings: Settings, role: str) -> tuple[dict, str]:
    client = _client(settings, role, None) or Ollama(settings)
    try:
        identity = await client.model_identity(getattr(settings, MODEL_FIELDS[role]))
        if role == "embedding":
            vector = (await client.embed(["Cairnmind embedding check."]))[0]
            return {"embedding_dimension": len(vector)}, identity
        reply = await client.chat([
            {"role": "system", "content": "Reply with JSON only."},
            {"role": "user", "content": 'Return {"ok": true}.'}], tools=None, format=dict(CHECK_SCHEMA),
            options={"num_predict": 256})
        try:
            document = json.loads(str(reply["message"].get("content", "")).strip())
        except ValueError:
            document = None
        if not isinstance(document, dict) or not isinstance(document.get("ok"), bool):
            raise ValueError("The chat model did not return structured JSON output for a JSON schema; extraction "
                             "and answer repair need it. Nothing was saved.")
        if reply.get("structured_output") == "prompted":  # the server refused `format`; the JSON was checked (#231)
            from .structured_output import DOCTOR_TEXT
            return {"structured_output": False, "note": DOCTOR_TEXT}, identity
        return {"structured_output": True}, identity
    finally:
        await client.aclose()


def save_role(data_dir: Path, role: str, row: dict | None, model: str) -> None:
    """Change one role in config.json as stored, under the settings lock.

    Settings in memory also hold environment overrides (engine address, password, executables);
    saving them would make a shell's temporary values permanent, so only this role's two fields change.
    """
    path = data_dir / "config.json"
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data_dir / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        values = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(values, dict):
            raise ValueError("config.json is not a settings object; nothing was saved.")
        values[FIELDS[role]] = row
        values[MODEL_FIELDS[role]] = model
        _write_json(path, values)


async def configure_role(settings: Settings, role: str, *, provider: str, model: str, base_url: str | None = None,
                         credential_ref: str | None = None, context_window: int | None = None,
                         reasoning_effort: str | None = None, allow_remote: bool = False) -> dict:
    """Check one role on its server, then save it. Nothing is saved when the check fails."""
    if role not in ROLES:
        raise ValueError("The role is chat or embedding.")
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ValueError("Invalid model identifier.")
    from_environment = False
    if provider == "ollama" and base_url is None:
        base_url = settings.ollama_url  # "back to the local Ollama" means the one the settings name
        from_environment = "ollama_url" in (settings._origin or {}).get("kept", {})
    row: dict[str, Any] = {"provider": provider}
    for key, value in (("base_url", base_url), ("credential_ref", credential_ref),
                       ("context_window", context_window), ("reasoning_effort", reasoning_effort)):
        if value is not None:
            row[key] = value
    if allow_remote:
        row["allow_remote"] = True
    row = normalize_memory_model(row, role)
    local_default = row == {"provider": "ollama", "base_url": normalize_base_url(settings.ollama_url)}
    saved_row = None if local_default else row
    candidate = replace(settings, **{FIELDS[role]: saved_row, MODEL_FIELDS[role]: model})
    checked, identity = await _check(candidate, role)
    previous = None
    if role == "embedding":
        try:
            previous = await (_client(settings, role, None) or Ollama(settings)).model_identity(settings.embedding_model)
        except (OllamaUnavailable, ValueError):
            previous = None  # unknown: treated as changed
    save_role(settings.data_dir, role, saved_row, model)
    from .memory_egress import describe
    report = {"role": role, "provider": row["provider"], "base_url": row["base_url"], "model": model,
              **_destination(row, role), **describe(candidate, role), "checked": checked, "identity": identity,
              "saved": True}
    steps = ["Restart the knowledge service (cairnmind stop, then start Cairnmind again) so that it uses this model."]
    if from_environment and local_default:
        steps.append(f"The model was checked on {settings.ollama_url}, taken from the environment for this command. The "
                     "saved setting means the Ollama at ollama_url, so a service started without that variable uses "
                     "ollama_url from config.json, where it was not checked.")
    if role == "embedding":
        report["embedding_changed"] = previous != identity
        if report["embedding_changed"]:
            steps.append("Run cairnmind rebuild-vectors PROJECT for each project: vectors of the earlier model are "
                         "kept apart and never searched with this one.")
    steps.append("Run cairnmind eval run and cairnmind eval memory: answers are labelled unevaluated until both "
                 "pass with these models.")
    report["next"] = steps
    return report
