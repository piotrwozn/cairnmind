"""The knowledge agent's model on a server that speaks the OpenAI-compatible API.

It covers LM Studio, vLLM, llama.cpp, an Ollama on another host (its /v1 endpoint) and most model
servers. Requests are Chat Completions and /embeddings; replies are translated into the shape of
Ollama's /api/chat, which the knowledge agent reads, so the agent itself does not change.

Only a profile validated by memory_models.normalize_memory_model reaches this class: the address is
HTTPS unless it is loopback, and a remote address needs the person's explicit consent.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from .config import Settings
from .ollama import Ollama, OllamaUnavailable, validate_embeddings

FINISH_REASONS = {"stop": "stop", "length": "length", "tool_calls": "tool_calls", "function_call": "tool_calls",
                  "content_filter": "other"}


def server_tag(base_url: str) -> str:
    """Names a server inside a vector namespace without writing its address into the store."""
    return "openai_compatible:" + hashlib.sha256(base_url.encode()).hexdigest()[:16]


def request_messages(messages: list[dict]) -> list[dict]:
    """Ollama-shaped history (tool calls with object arguments, tool replies by name) to Chat Completions.

    Each tool reply is matched, in order, to the call of the assistant turn before it, as the agent appends them.
    """
    result, pending, counter = [], [], 0
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls, pending = [], []
            for call in message["tool_calls"]:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                arguments = function.get("arguments", {}) if isinstance(function, dict) else {}
                counter += 1
                identifier = call.get("id") if isinstance(call, dict) and isinstance(call.get("id"), str) else (
                    f"call_{counter}")
                calls.append({"id": identifier, "type": "function", "function": {
                    "name": function.get("name", "") if isinstance(function, dict) else "",
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                }})
                pending.append(identifier)
            result.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            counter += 1
            result.append({"role": "tool", "tool_call_id": pending.pop(0) if pending else f"call_{counter}",
                           "content": message.get("content", "")})
        else:
            result.append({key: value for key, value in message.items() if key in {"role", "content"}})
    return result


def ollama_reply(data: dict) -> dict:
    """A Chat Completions reply in the shape of Ollama's /api/chat."""
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    message = choice.get("message") if choice else None
    if not isinstance(message, dict):
        raise OllamaUnavailable("Model server returned no chat message", endpoint="/chat/completions",
                                error_code="invalid_response")
    content = message.get("content")
    result_message: dict[str, Any] = {"role": "assistant", "content": content if isinstance(content, str) else ""}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            calls.append({})  # the agent reports a broken tool protocol; nothing is invented here
            continue
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                pass  # kept as text: the agent answers the call with an error
        converted = {"function": {"name": function.get("name", ""), "arguments": arguments}}
        if isinstance(call.get("id"), str):
            converted = {"id": call["id"], **converted}
        calls.append(converted)
    if calls:
        result_message["tool_calls"] = calls
    reply: dict[str, Any] = {"message": result_message}
    reason = choice.get("finish_reason")
    if isinstance(reason, str):
        reply["done_reason"] = FINISH_REASONS.get(reason, reason)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    for source, target in (("prompt_tokens", "prompt_eval_count"), ("completion_tokens", "eval_count")):
        if type(usage.get(source)) is int:
            reply[target] = usage[source]
    return reply


class OpenAICompatible(Ollama):
    """One role (chat or embedding) of the knowledge agent on an OpenAI-compatible server."""

    ENDPOINTS = frozenset({"/chat/completions", "/embeddings", "/models"})
    UNKNOWN_ENDPOINT = "/unknown"

    def __init__(self, settings: Settings, role: str, profile: dict, client: httpx.AsyncClient | None = None, *,
                 credential=None):
        super().__init__(settings, client, base_url=profile["base_url"], credential=credential,
                         num_ctx=profile.get("context_window"))
        self.role = role
        self.label = "Model server" if self.label == "Ollama" else "Local model server"
        self.tag = server_tag(profile["base_url"])

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        options = {"num_predict": self.settings.answer_tokens, "temperature": 0}
        options.update(kwargs.pop("options", {}))
        schema = kwargs.pop("format", None)
        if kwargs:
            raise ValueError("Unsupported option for an OpenAI-compatible model server")
        payload: dict[str, Any] = {"model": self.settings.chat_model, "messages": request_messages(messages),
                                   "stream": False, "temperature": options.get("temperature", 0),
                                   "max_tokens": options["num_predict"]}
        if tools:
            payload["tools"] = tools
        if schema is not None:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "cairnmind_response", "schema": schema}}
        return ollama_reply(await self._request("POST", "/chat/completions", json=payload))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._request("POST", "/embeddings", json={
            "model": self.settings.embedding_model, "input": texts, "encoding_format": "float"})
        rows = data.get("data")
        if (not isinstance(rows, list) or len(rows) != len(texts)
                or not all(isinstance(row, dict) and type(row.get("index")) is int for row in rows)
                or sorted(row["index"] for row in rows) != list(range(len(texts)))):
            raise OllamaUnavailable("Embedding model returned an incorrect number of vectors")
        return validate_embeddings([row.get("embedding") for row in sorted(rows, key=lambda row: row["index"])],
                                   len(texts))

    async def models(self) -> dict:
        data = await self._request("GET", "/models")
        rows = data.get("data")
        if not isinstance(rows, list):
            raise OllamaUnavailable("Model server list is invalid", endpoint="/models", error_code="invalid_response")
        result = {"models": [{"name": row["id"], "model": row["id"]} for row in rows
                             if isinstance(row, dict) and isinstance(row.get("id"), str)]}
        self._model_cache = (time.monotonic(), result)
        return result

    async def model_identity(self, name: str) -> str:
        """The server and the model name. Such servers report no digest, so swapped weights behind
        the same name are not detected; the vector dimension is still part of every vector index."""
        cached = self._model_cache
        data = cached[1] if cached and time.monotonic() - cached[0] < 30 else await self.models()
        if any(model.get("name") == name for model in data["models"]):
            return f"{name}@{self.tag}"
        raise OllamaUnavailable(f"{self.label} does not list the configured {self.role} model",
                                endpoint="/models", error_code="model_not_found")
