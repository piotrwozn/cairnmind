"""Small, explicit client for the *local* Ollama runtime.

No cloud fallback exists here. A missing model is an operational error, never a
reason to silently send a private project to another provider. Another server is used
only when the person configured a memory model profile for it (memory_models.py).
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any

import httpx

from . import structured_output
from .config import Settings

# What a model list may give as a model's digest; anything else counts as no digest (#192).
MODEL_DIGEST = re.compile(r"(?:sha256:)?[0-9a-f]{12,128}")


class OllamaUnavailable(RuntimeError):
    """The configured local runtime or model could not satisfy a request."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 endpoint: str | None = None, error_code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
        self.error_code = error_code


_ERROR_REASONS = {
    "memory_exhausted": "runtime reported insufficient memory",
    "model_not_found": "runtime reported a missing model",
    "unsupported_capability": "runtime reported an unsupported model capability or option",
    "context_length": "runtime reported a context-length limit",
    "runner_failed": "runtime reported a model runner or loading failure",
    "runtime_busy": "runtime reported a busy or unavailable runtime",
    "invalid_request": "runtime rejected the request",
    "structured_output_unavailable": "runtime reported that structured output is unavailable",
    "http_error": "runtime returned an HTTP error",
    "runtime_error": "runtime returned a model error",
}


def _api_error_code(response: httpx.Response) -> str:
    """Classify a bounded JSON error; never publish server text that can echo prompts/secrets."""
    error = ""
    if len(response.content) <= 4096:
        try:
            document = response.json()
            found = document.get("error") if isinstance(document, dict) else None
            if isinstance(found, dict):  # the OpenAI-compatible shape: {"error": {"message": ...}}
                found = found.get("message")
            if isinstance(found, str):
                error = found.casefold()
        except (ValueError, UnicodeError):
            pass
    if response.status_code == 501 and structured_output.REFUSAL_TEXT.search(error):  # an MLX build (#231)
        return structured_output.REFUSED
    for code, pattern in (
        ("memory_exhausted", r"out of memory|insufficient (?:system |gpu |device )?memory|cannot allocate memory|"
                             r"model requires more (?:system |gpu |device )?memory[^\n]{0,128}than is available"),
        ("model_not_found", r"model[^\n]{0,256}(?:not found|does not exist|is missing)"),
        ("unsupported_capability", r"does not support|not supported|unsupported"),
        ("context_length", r"context (?:length|window)|input length[^\n]{0,80}exceed|too many tokens"),
        ("runner_failed", r"llama runner|runner process|model runner|(?:failed|unable) to load model|model load failed"),
        ("runtime_busy", r"server (?:is )?busy|queue (?:is )?full|too many requests|service unavailable"),
    ):
        if re.search(pattern, error):
            return code
    if response.status_code in {429, 503}:
        return "runtime_busy"
    if response.status_code == 400:
        return "invalid_request"
    return "http_error" if not response.is_success else "runtime_error"


def validate_embeddings(embeddings: Any, count: int) -> list[list[float]]:
    """One finite, nonzero vector per input text, all of one dimension."""
    if not isinstance(embeddings, list) or len(embeddings) != count:
        raise OllamaUnavailable("Embedding model returned an incorrect number of vectors")
    dimension = None
    for vector in embeddings:
        if not isinstance(vector, list) or not vector:
            raise OllamaUnavailable("Embedding model returned an empty vector")
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in vector):
            raise OllamaUnavailable("Embedding vector contains non-finite or nonnumeric values")
        if not any(vector):
            raise OllamaUnavailable("Embedding model returned a zero vector")
        dimension = len(vector) if dimension is None else dimension
        if len(vector) != dimension:
            raise OllamaUnavailable("Embedding dimensions changed within a batch")
    return embeddings


class Ollama:
    ENDPOINTS = frozenset({"/api/chat", "/api/embed", "/api/tags"})
    UNKNOWN_ENDPOINT = "/api/unknown"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, *,
                 base_url: str | None = None, credential=None, num_ctx: int | None = None):
        """base_url, credential and num_ctx come only from a validated memory model profile (memory_models.py).

        Without base_url this is the local Ollama of the settings: memory_models.local_server decides (#288, #302).
        """
        self.settings = settings
        if base_url is None:
            from .memory_models import local_server  # memory_models imports this module
            base_url = local_server(settings)
        self.base_url = base_url
        self.num_ctx = settings.num_ctx if num_ctx is None else num_ctx
        from .memory_models import is_loopback
        self.label = "Local Ollama" if is_loopback(base_url) else "Ollama"
        self._credential = credential
        self._client = client
        self._model_cache: tuple[float, dict] | None = None

    def _headers(self, endpoint: str) -> dict:
        """The key a profile names, read when it is needed; it never enters a message or a diagnostic."""
        if self._credential is None:
            return {}
        try:
            key = self._credential()
        except ValueError:
            key = None
        if not key:
            raise OllamaUnavailable(f"{self.label} {endpoint} needs the key its profile names, and it is not set",
                                    endpoint=endpoint, error_code="credential_missing")
        return {"Authorization": "Bearer " + key}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        # Diagnostic paths must not expose query parameters, credentials or arbitrary URLs.
        endpoint = path if path in self.ENDPOINTS else self.UNKNOWN_ENDPOINT
        read_timeout = kwargs.pop("read_timeout", None)
        headers = self._headers(endpoint)
        if read_timeout is not None:
            # An extraction call (#266) waits as long as its job has left, not ask_timeout; connect stays as it was.
            kwargs["timeout"] = httpx.Timeout(max(5, self.settings.ask_timeout), read=read_timeout)
        if headers:
            kwargs["headers"] = headers
        try:
            if self._client is not None:
                response = await self._client.request(method, path, **kwargs)
            else:
                # Local requests must not be routed through a configured cloud proxy.
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=max(5, self.settings.ask_timeout),
                    trust_env=False,
                ) as client:
                    response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.ReadTimeout):
                code, reason = "read_timeout", "response read timed out"
            elif isinstance(exc, httpx.ConnectError):
                code, reason = "connect_error", "could not connect to the local runtime"
            elif isinstance(exc, httpx.TimeoutException):
                code, reason = "request_timeout", "local request timed out"
            else:
                code, reason = "request_error", "local HTTP request failed"
            raise OllamaUnavailable(
                f"{self.label} {endpoint} request failed ({type(exc).__name__}): {reason}",
                endpoint=endpoint, error_code=code,
            ) from None
        if not response.is_success:
            code = _api_error_code(response)
            raise OllamaUnavailable(
                f"{self.label} {endpoint} returned HTTP {response.status_code}: {_ERROR_REASONS[code]}",
                endpoint=endpoint, status_code=response.status_code, error_code=code,
            ) from None
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise OllamaUnavailable(
                f"{self.label} {endpoint} returned HTTP {response.status_code} with invalid JSON",
                endpoint=endpoint, status_code=response.status_code, error_code="invalid_response",
            ) from None
        if not isinstance(data, dict):
            raise OllamaUnavailable(
                f"{self.label} {endpoint} returned HTTP {response.status_code} with an invalid response object",
                endpoint=endpoint, status_code=response.status_code, error_code="invalid_response",
            )
        if data.get("error"):
            code = _api_error_code(response)
            raise OllamaUnavailable(
                f"{self.label} {endpoint} returned HTTP {response.status_code}: {_ERROR_REASONS[code]}",
                endpoint=endpoint, status_code=response.status_code, error_code=code,
            )
        return data

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        """One chat request. A server that refuses ``format`` as unavailable is asked once more without it,
        the schema in the prompt; the caller validates the JSON as before (structured_output.py, #231)."""
        schema = kwargs.get("format")
        try:
            return await self._chat(messages, tools, **kwargs)
        except OllamaUnavailable as exc:
            if schema is None or exc.error_code != structured_output.REFUSED:
                raise
        await asyncio.to_thread(structured_output.remember, self.settings.data_dir, self.base_url,
                                self.settings.chat_model)
        plain = {key: value for key, value in kwargs.items() if key != "format"}
        reply = await self._chat(structured_output.prompted_messages(messages, schema), tools, **plain)
        return structured_output.unwrap(reply)

    async def _chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        options = {
            "num_ctx": self.num_ctx,
            "num_predict": self.settings.answer_tokens,
            "temperature": 0,
        }
        options.update(kwargs.pop("options", {}))
        read_timeout = kwargs.pop("read_timeout", None)
        payload = {
            "model": self.settings.chat_model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": options,
            **kwargs,
        }
        if tools:
            payload["tools"] = tools
        data = await self._request("POST", "/api/chat", json=payload, read_timeout=read_timeout)
        if not isinstance(data.get("message"), dict):
            raise OllamaUnavailable("Local model returned no chat message")
        return data

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._request(
            "POST", "/api/embed",
            json={"model": self.settings.embedding_model, "input": texts, "truncate": False},
        )
        return validate_embeddings(data.get("embeddings"), len(texts))

    async def models(self) -> dict:
        data = await self._request("GET", "/api/tags")
        if not isinstance(data.get("models"), list):
            raise OllamaUnavailable("Local model list is invalid")
        self._model_cache = (time.monotonic(), data)
        return data

    async def model_identity(self, name: str) -> str:
        """Digest namespaces prevent embeddings from different weights mixing."""
        cached = self._model_cache
        data = cached[1] if cached and time.monotonic() - cached[0] < 30 else await self.models()
        for model in data["models"]:
            if model.get("name") == name or model.get("model") == name:
                digest = model.get("digest")
                if not digest:
                    raise OllamaUnavailable("Local model has no digest; cannot version its embeddings")
                return f"{name}@{digest}"
        raise OllamaUnavailable(f"Required local model {name!r} is not installed")

    async def model_digest(self, name: str) -> str | None:
        """The digest the model list gives for ``name``, or None: not listed, listed without one (what a
        server other than Ollama lists, memory_openai.OpenAICompatible), or not shaped as a digest."""
        cached = self._model_cache
        data = cached[1] if cached and time.monotonic() - cached[0] < 30 else await self.models()
        for model in data["models"]:
            if isinstance(model, dict) and (model.get("name") == name or model.get("model") == name):
                digest = model.get("digest")
                return digest if isinstance(digest, str) and MODEL_DIGEST.fullmatch(digest) else None
        return None

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
