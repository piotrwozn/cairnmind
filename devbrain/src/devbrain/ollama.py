"""Small, explicit client for the *local* Ollama runtime.

No cloud fallback exists here. A missing model is an operational error, never a
reason to silently send a private project to another provider.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings


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
    "http_error": "runtime returned an HTTP error",
    "runtime_error": "runtime returned a model error",
}


def _api_error_code(response: httpx.Response) -> str:
    """Classify a bounded JSON error; never publish server text that can echo prompts/secrets."""
    error = ""
    if len(response.content) <= 4096:
        try:
            document = response.json()
            if isinstance(document, dict) and isinstance(document.get("error"), str):
                error = document["error"].casefold()
        except (ValueError, UnicodeError):
            pass
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


class Ollama:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        parsed = urlparse(settings.ollama_url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ollama must use a loopback HTTP address; no remote provider is permitted")
        self._client = client
        self._model_cache: tuple[float, dict] | None = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        # Diagnostic paths must not expose query parameters, credentials or arbitrary URLs.
        endpoint = path if path in {"/api/chat", "/api/embed", "/api/tags"} else "/api/unknown"
        try:
            if self._client is not None:
                response = await self._client.request(method, path, **kwargs)
            else:
                # Local requests must not be routed through a configured cloud proxy.
                async with httpx.AsyncClient(
                    base_url=self.settings.ollama_url,
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
                f"Local Ollama {endpoint} request failed ({type(exc).__name__}): {reason}",
                endpoint=endpoint, error_code=code,
            ) from None
        if not response.is_success:
            code = _api_error_code(response)
            raise OllamaUnavailable(
                f"Local Ollama {endpoint} returned HTTP {response.status_code}: {_ERROR_REASONS[code]}",
                endpoint=endpoint, status_code=response.status_code, error_code=code,
            ) from None
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise OllamaUnavailable(
                f"Local Ollama {endpoint} returned HTTP {response.status_code} with invalid JSON",
                endpoint=endpoint, status_code=response.status_code, error_code="invalid_response",
            ) from None
        if not isinstance(data, dict):
            raise OllamaUnavailable(
                f"Local Ollama {endpoint} returned HTTP {response.status_code} with an invalid response object",
                endpoint=endpoint, status_code=response.status_code, error_code="invalid_response",
            )
        if data.get("error"):
            code = _api_error_code(response)
            raise OllamaUnavailable(
                f"Local Ollama {endpoint} returned HTTP {response.status_code}: {_ERROR_REASONS[code]}",
                endpoint=endpoint, status_code=response.status_code, error_code=code,
            )
        return data

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        options = {
            "num_ctx": self.settings.num_ctx,
            "num_predict": self.settings.answer_tokens,
            "temperature": 0,
        }
        options.update(kwargs.pop("options", {}))
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
        data = await self._request("POST", "/api/chat", json=payload)
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
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
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

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
