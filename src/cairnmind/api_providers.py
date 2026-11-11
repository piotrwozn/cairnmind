"""Bounded, stateless protocol adapters for Cairnmind's own coding agent.

Only explicit ``complete`` and ``probe`` calls generate model output.  Adapters
never retry a generation: a lost transport does not prove the request was not
accepted. Provider bodies and authentication headers are not diagnostic text.
"""
from __future__ import annotations

import inspect
import json
import re
from uuid import uuid4

import httpx

from .api_profiles import normalize_profile


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_TOOL_CALLS = 64
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MESSAGES = {
    "credential_missing": "Brakuje poświadczeń API.",
    "auth_rejected": "Dostawca odrzucił poświadczenia.",
    "access_denied": "Konto nie ma dostępu do wybranego zasobu.",
    "quota_exhausted": "Brak środków lub dostępnego limitu dostawcy.",
    "rate_limited": "Dostawca ograniczył częstotliwość żądań.",
    "network_unavailable": "Nie można połączyć się z dostawcą.",
    "network_timeout": "Upłynął czas oczekiwania na dostawcę.",
    "model_or_endpoint_missing": "Model lub adres API jest niedostępny.",
    "context_limit": "Żądanie przekracza limit kontekstu. Zwiększ okno profilu Ollama lub skróć kontekst.",
    "context_unknown": "Ollama nie podała wiarygodnego limitu kontekstu modelu; sprawdź model i wersję serwera.",
    "output_limit": "Dostawca przerwał odpowiedź na limicie wyjścia.",
    "unsupported_request": "Dostawca nie obsługuje żądanej konfiguracji.",
    "unsupported_tools": "Model nie potwierdził poprawnej obsługi narzędzi.",
    "provider_unavailable": "Usługa dostawcy jest chwilowo niedostępna.",
    "provider_redirect_denied": "API zwróciło przekierowanie; sprawdź adres serwera.",
    "invalid_response": "Dostawca zwrócił niepoprawną lub niekompletną odpowiedź.",
    "response_too_large": "Odpowiedź dostawcy przekroczyła limit rozmiaru.",
    "policy_refused": "Dostawca odmówił wykonania żądania.",
    "request_conflict": "Dostawca odrzucił żądanie z powodu konfliktu.",
    "provider_error": "Dostawca odrzucił żądanie.",
}


class ProviderError(ValueError):
    def __init__(self, code: str, *, context_details=None):
        self.code = code if code in _MESSAGES else "provider_error"
        self.message = _MESSAGES[self.code]
        self.context_details = {}
        if (self.code == "context_limit" and isinstance(context_details, dict)
                and set(context_details) == {"configured_context_window", "model_context_window"}
                and all(type(v) is int and 0 < v <= 2**31 - 1 for v in context_details.values())):
            self.context_details = dict(context_details)
            self.message = "Wybrane okno przekracza limit modelu Ollama. Zmniejsz je lub wybierz model z większym kontekstem."
        self.hint = self.message
        super().__init__(self.message)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ProviderError("unsupported_request") from None


def _parse(value):
    try:
        return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("invalid_response") from None


def _arguments(value):
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_LINE_BYTES:
            raise ProviderError("response_too_large")
        value = _parse(value)
    if not isinstance(value, dict):
        raise ProviderError("invalid_response")
    _json(value)
    return value


def _calls(rows):
    if len(rows) > MAX_TOOL_CALLS:
        raise ProviderError("response_too_large")
    result, seen = [], set()
    for row in rows:
        call_id, name = row.get("id"), row.get("name")
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 256 or call_id in seen
                or any(ord(c) < 33 or ord(c) == 127 for c in call_id)
                or not isinstance(name, str) or not _TOOL_NAME.fullmatch(name)):
            raise ProviderError("invalid_response")
        seen.add(call_id)
        result.append({"id": call_id, "name": name, "arguments": _arguments(row.get("arguments", {}))})
    return result


def _usage(raw, *, style="openai"):
    if not isinstance(raw, dict):
        return {}
    aliases = ({"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"}
               if style == "chat" else {"input_tokens": "prompt_eval_count", "output_tokens": "eval_count"}
               if style == "ollama" else {"input_tokens": "input_tokens", "output_tokens": "output_tokens"})
    result = {}
    for dest, src in aliases.items():
        value = raw.get(src)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            result[dest] = value
    if "input_tokens" in result and "output_tokens" in result:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = raw.get(name)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            result[name] = value
    return result


async def _delta(callback, text):
    if callback is not None and text:
        value = callback(text)
        if inspect.isawaitable(value):
            await value


async def _lines(response):
    """Decode incrementally with a bound before accumulating an unended line."""
    buffer, total = b"", 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ProviderError("response_too_large")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if len(line) > MAX_LINE_BYTES:
                raise ProviderError("response_too_large")
            try:
                yield line.rstrip(b"\r").decode("utf-8")
            except UnicodeError:
                raise ProviderError("invalid_response") from None
        if len(buffer) > MAX_LINE_BYTES:
            raise ProviderError("response_too_large")
    if buffer:
        try:
            yield buffer.rstrip(b"\r").decode("utf-8")
        except UnicodeError:
            raise ProviderError("invalid_response") from None


async def _sse(response):
    parts, size = [], 0
    async for line in _lines(response):
        if not line:
            if parts:
                data = "\n".join(parts)
                if data == "[DONE]":
                    yield None
                else:
                    value = _parse(data)
                    if not isinstance(value, dict):
                        raise ProviderError("invalid_response")
                    yield value
                parts, size = [], 0
        elif line.startswith("data:"):
            part = line[5:]
            if part.startswith(" "):
                part = part[1:]
            size += len(part.encode("utf-8"))
            if size > MAX_LINE_BYTES:
                raise ProviderError("response_too_large")
            parts.append(part)
    if parts:
        data = "\n".join(parts)
        if data == "[DONE]":
            yield None
        else:
            value = _parse(data)
            if not isinstance(value, dict):
                raise ProviderError("invalid_response")
            yield value


async def _body(response, limit=MAX_RESPONSE_BYTES):
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ProviderError("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _error_code(status, raw=None):
    # Classification uses only bounded provider observations; its raw text
    # never becomes an exception, public status or journal event.
    observed = ""
    if isinstance(raw, dict):
        detail = raw.get("error", raw)
        if isinstance(detail, dict):
            observed = " ".join(str(detail.get(k, ""))[:2000] for k in ("type", "code", "message")).casefold()
        elif isinstance(detail, str):
            observed = detail[:2000].casefold()
    if any(term in observed for term in ("insufficient_quota", "credit balance", "quota_exhausted",
                                         "billing_hard_limit", "out of credits", "credit_balance")):
        return "quota_exhausted"
    if any(term in observed for term in ("context_length_exceeded", "maximum context length", "prompt is too long",
                                         "input is too long", "model_context_window_exceeded")):
        return "context_limit"
    return {401: "auth_rejected", 402: "quota_exhausted", 403: "access_denied",
            404: "model_or_endpoint_missing", 408: "network_timeout", 409: "request_conflict",
            413: "context_limit", 422: "unsupported_request", 429: "rate_limited"}.get(
                status, "provider_unavailable" if status >= 500 else "provider_redirect_denied"
                if 300 <= status < 400 else "provider_error")


def _text_content(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProviderError("unsupported_request")
    return value


def _chat_messages(messages):
    result = []
    for message in messages:
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ProviderError("unsupported_request")
        row = {"role": role, "content": _text_content(message.get("content", ""))}
        if role == "assistant" and message.get("tool_calls"):
            row["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                "name": c["name"], "arguments": _json(c["arguments"])}} for c in _calls(message["tool_calls"])]
        if role == "tool":
            row["tool_call_id"] = message["tool_call_id"]
        result.append(row)
    return result


def _response_messages(messages):
    result = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": _text_content(message.get("content", ""))})
            continue
        if role not in {"system", "user", "assistant"}:
            raise ProviderError("unsupported_request")
        if role == "assistant":
            continuation = message.get("continuation")
            if isinstance(continuation, list):
                result.extend(row for row in continuation if isinstance(row, dict) and row.get("type") == "reasoning")
        text = _text_content(message.get("content", ""))
        if text or role != "assistant":
            result.append({"role": role, "content": text})
        if role == "assistant":
            for call in _calls(message.get("tool_calls", [])):
                result.append({"type": "function_call", "call_id": call["id"], "name": call["name"],
                               "arguments": _json(call["arguments"])})
    return result


def _anthropic_messages(messages):
    result, systems = [], []
    for message in messages:
        role, blocks = message.get("role"), []
        text = _text_content(message.get("content", ""))
        if role == "system":
            systems.append(text)
            continue
        if role == "tool":
            role = "user"
            blocks.append({"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": text})
        elif role in {"user", "assistant"}:
            if role == "assistant" and isinstance(message.get("continuation"), list):
                blocks.extend(b for b in message["continuation"] if isinstance(b, dict)
                              and b.get("type") in {"thinking", "redacted_thinking"})
            if text:
                blocks.append({"type": "text", "text": text})
            if role == "assistant":
                blocks.extend({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                              for c in _calls(message.get("tool_calls", [])))
        else:
            raise ProviderError("unsupported_request")
        if not blocks:
            blocks = [{"type": "text", "text": " "}]
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    return result, "\n\n".join(systems)


def _ollama_messages(messages):
    result = []
    for message in messages:
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ProviderError("unsupported_request")
        row = {"role": role, "content": _text_content(message.get("content", ""))}
        if role == "assistant":
            if message.get("tool_calls"):
                row["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                     for c in _calls(message["tool_calls"])]
            continuation = message.get("continuation")
            if isinstance(continuation, dict) and isinstance(continuation.get("thinking"), str):
                row["thinking"] = continuation["thinking"]
        elif role == "tool" and message.get("name"):
            row["tool_name"] = message["name"]
        result.append(row)
    return result


class Provider:
    def __init__(self, profile, key=None, *, client=None):
        self.profile = normalize_profile(profile)
        if self.profile["client"] != "api":
            raise ValueError("Wybierz profil bezpośredniego API.")
        if key is not None and (not isinstance(key, str) or not key or len(key) > 16384
                                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise ProviderError("auth_rejected")
        self._key = key
        self._client = client
        self._owns_client = client is None

    def _http(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False)
        return self._client

    def _headers(self):
        provider = self.profile["provider"]
        if provider in {"openai", "anthropic"} and not self._key:
            raise ProviderError("credential_missing")
        result = {"Content-Type": "application/json", "Accept": "application/json"}
        if provider == "anthropic":
            result.update({"x-api-key": self._key, "anthropic-version": "2023-06-01"})
        elif self._key:
            result["Authorization"] = "Bearer " + self._key
        return result

    def _url(self, path):
        return self.profile["base_url"] + "/" + path.lstrip("/")

    async def _check(self, response):
        if response.is_success:
            return
        raw = None
        try:
            body = await _body(response, 65536)
            raw = json.loads(body)
        except (ValueError, UnicodeError, ProviderError):
            pass
        raise ProviderError(_error_code(response.status_code, raw))

    async def _post(self, path, payload, parser, on_delta):
        try:
            async with self._http().stream("POST", self._url(path), json=payload, headers=self._headers(),
                                           follow_redirects=False) as response:
                await self._check(response)
                if "application/json" in response.headers.get("content-type", "").lower():
                    raw = _parse(await _body(response))
                    if not isinstance(raw, dict):
                        raise ProviderError("invalid_response")
                    return await parser(raw, None, on_delta)
                return await parser(None, response, on_delta)
        except httpx.TimeoutException:
            raise ProviderError("network_timeout") from None
        except httpx.HTTPError:
            raise ProviderError("network_unavailable") from None
        except (KeyError, TypeError, IndexError, AttributeError):
            raise ProviderError("invalid_response") from None

    async def _ollama_context(self):
        """Metadata only: requesting a larger context cannot override training.

        Ollama clamps num_ctx to the model's trained window. Read the actual
        model metadata before generation instead of treating the configured
        number as evidence that the complete prompt will reach the model.
        """
        try:
            async with self._http().stream("POST", self._url("api/show"),
                    json={"model": self.profile["model"]}, headers=self._headers(), follow_redirects=False) as response:
                await self._check(response)
                raw = _parse(await _body(response))
        except httpx.TimeoutException:
            raise ProviderError("network_timeout") from None
        except httpx.HTTPError:
            raise ProviderError("network_unavailable") from None
        if not isinstance(raw, dict):
            raise ProviderError("context_unknown")
        info = raw.get("model_info")
        values = []
        if isinstance(info, dict):
            architecture = info.get("general.architecture")
            if isinstance(architecture, str) and architecture + ".context_length" in info:
                values = [info[architecture + ".context_length"]]
            else:
                values = [value for name, value in info.items() if name.endswith(".context_length")]
        if not values:
            details = raw.get("details")
            if isinstance(details, dict) and "context_length" in details:
                values = [details["context_length"]]
        if not values or any(type(value) is not int or not 1 <= value <= 2**31 - 1 for value in values):
            raise ProviderError("context_unknown")
        trained = min(values)
        if self.profile["limits"]["context_window_tokens"] > trained:
            raise ProviderError("context_limit", context_details={
                "configured_context_window": self.profile["limits"]["context_window_tokens"],
                "model_context_window": trained})
        return trained

    async def complete(self, messages, tools, *, on_delta=None):
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ProviderError("unsupported_request")
        _json(messages)
        _json(tools)
        if (len(tools) > MAX_TOOL_CALLS or any(not isinstance(t, dict)
                or set(t) - {"name", "description", "parameters"}
                or not isinstance(t.get("name"), str) or not _TOOL_NAME.fullmatch(t["name"])
                or not isinstance(t.get("parameters"), dict)
                or not isinstance(t.get("description", ""), str) for t in tools)
                or any(not isinstance(m, dict) for m in messages)):
            raise ProviderError("unsupported_request")
        provider = self.profile["provider"]
        maximum = self.profile["limits"]["max_output_tokens"]
        if provider == "openai":
            payload = {"model": self.profile["model"], "input": _response_messages(messages), "stream": True,
                       "store": False, "max_output_tokens": maximum}
            if tools:
                payload["tools"] = [{"type": "function", **t, "strict": False} for t in tools]
            return await self._post("responses", payload, self._responses, on_delta)
        if provider == "anthropic":
            converted, system = _anthropic_messages(messages)
            payload = {"model": self.profile["model"], "messages": converted, "stream": True, "max_tokens": maximum}
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                                     "input_schema": t["parameters"]} for t in tools]
            return await self._post("messages", payload, self._anthropic, on_delta)
        payload = {"model": self.profile["model"], "stream": True}
        if provider == "ollama":
            window = self.profile["limits"]["context_window_tokens"]
            payload.update(messages=_ollama_messages(messages), options={"num_predict": maximum, "num_ctx": window},
                           truncate=False, shift=False)
            if tools:
                payload["tools"] = [{"type": "function", "function": t} for t in tools]
            # Ollama otherwise silently truncates old chat messages to its
            # configured context. Use an explicit window and a conservative
            # byte-based upper estimate (not a fabricated tokenizer count),
            # with room for role/tool template framing and generated output.
            prompt = _json({"messages": payload["messages"], "tools": payload.get("tools", [])}).encode("utf-8")
            budget = len(prompt) + 128 * len(messages) + 256 * len(tools) + 1024 + maximum
            if budget > window:
                raise ProviderError("context_limit")
            await self._ollama_context()
            return await self._post("api/chat", payload, self._ollama, on_delta)
        payload.update(messages=_chat_messages(messages), max_tokens=maximum, stream_options={"include_usage": True})
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        return await self._post("chat/completions", payload, self._chat, on_delta)

    @staticmethod
    def _response_result(raw):
        if not isinstance(raw, dict):
            raise ProviderError("invalid_response")
        if raw.get("status") == "incomplete":
            raise ProviderError("output_limit")
        if raw.get("status") in {"failed", "cancelled"}:
            raise ProviderError(_error_code(400, raw))
        if raw.get("status") != "completed":
            raise ProviderError("invalid_response")
        output = raw.get("output")
        if not isinstance(output, list):
            raise ProviderError("invalid_response")
        text, calls, continuation = [], [], []
        for item in output:
            if item.get("type") == "function_call":
                calls.append({"id": item.get("call_id"), "name": item.get("name"), "arguments": item.get("arguments")})
            elif item.get("type") == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        if not isinstance(block.get("text"), str):
                            raise ProviderError("invalid_response")
                        text.append(block["text"])
                    elif block.get("type") == "refusal":
                        raise ProviderError("policy_refused")
            elif item.get("type") == "reasoning":
                continuation.append(item)
        return {"text": "".join(text), "tool_calls": _calls(calls), "usage": _usage(raw.get("usage")),
                **({"continuation": continuation} if continuation else {})}

    async def _responses(self, raw, response, on_delta):
        if raw is not None:
            result = self._response_result(raw)
            await _delta(on_delta, result["text"])
            return result
        emitted = False
        async for event in _sse(response):
            if event is None:
                continue
            kind = event.get("type")
            if kind == "response.output_text.delta":
                if not isinstance(event.get("delta"), str):
                    raise ProviderError("invalid_response")
                await _delta(on_delta, event["delta"])
                emitted = True
            elif kind == "response.completed":
                result = self._response_result(event.get("response", {}))
                if not emitted:
                    await _delta(on_delta, result["text"])
                return result
            elif kind in {"response.failed", "response.incomplete", "error"}:
                raise ProviderError("output_limit" if kind == "response.incomplete" else _error_code(400, event.get("response", event)))
        raise ProviderError("invalid_response")

    async def _chat(self, raw, response, on_delta):
        if raw is not None:
            choices = raw.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ProviderError("invalid_response")
            row = choices[0]
            finish = row.get("finish_reason")
            if finish == "length":
                raise ProviderError("output_limit")
            if finish not in {"stop", "tool_calls"}:
                raise ProviderError("invalid_response")
            message = row.get("message", {})
            if message.get("refusal"):
                raise ProviderError("policy_refused")
            text = _text_content(message.get("content"))
            calls = [{"id": c.get("id"), "name": c.get("function", {}).get("name"),
                      "arguments": c.get("function", {}).get("arguments")} for c in message.get("tool_calls", [])]
            await _delta(on_delta, text)
            return {"text": text, "tool_calls": _calls(calls), "usage": _usage(raw.get("usage"), style="chat")}
        text, calls, usage, finish = [], {}, {}, None
        async for event in _sse(response):
            if event is None:
                continue
            if "error" in event:
                raise ProviderError(_error_code(400, event))
            if isinstance(event.get("usage"), dict):
                usage = _usage(event["usage"], style="chat")
            choices = event.get("choices", [])
            if not isinstance(choices, list):
                raise ProviderError("invalid_response")
            for row in choices:
                if row.get("index", 0) != 0:
                    continue
                delta = row.get("delta", {})
                if delta.get("refusal"):
                    raise ProviderError("policy_refused")
                chunk = delta.get("content")
                if chunk is not None:
                    if not isinstance(chunk, str):
                        raise ProviderError("invalid_response")
                    text.append(chunk)
                    await _delta(on_delta, chunk)
                for call in delta.get("tool_calls", []):
                    index = call.get("index")
                    if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                        raise ProviderError("invalid_response")
                    current = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    function = call.get("function", {})
                    for dest, value in (("id", call.get("id")), ("name", function.get("name")),
                                        ("arguments", function.get("arguments"))):
                        if value is not None:
                            if not isinstance(value, str):
                                raise ProviderError("invalid_response")
                            current[dest] += value
                if row.get("finish_reason") is not None:
                    finish = row["finish_reason"]
        if finish == "length":
            raise ProviderError("output_limit")
        if finish == "content_filter":
            raise ProviderError("policy_refused")
        if finish not in {"stop", "tool_calls"}:
            raise ProviderError("invalid_response")
        return {"text": "".join(text), "tool_calls": _calls([calls[i] for i in sorted(calls)]), "usage": usage}

    async def _anthropic(self, raw, response, on_delta):
        if raw is not None:
            if raw.get("type") == "error":
                raise ProviderError(_error_code(400, raw))
            if raw.get("stop_reason") == "max_tokens":
                raise ProviderError("output_limit")
            if raw.get("stop_reason") not in {"end_turn", "tool_use", "stop_sequence"}:
                raise ProviderError("invalid_response")
            result = self._anthropic_result(raw.get("content"), raw.get("usage"))
            await _delta(on_delta, result["text"])
            return result
        blocks, partial, usage, finish = {}, {}, {}, None
        async for event in _sse(response):
            if event is None:
                continue
            kind = event.get("type")
            if kind == "error":
                raise ProviderError(_error_code(400, event))
            if kind == "message_start":
                usage.update(event.get("message", {}).get("usage", {}))
            elif kind == "content_block_start":
                index = event.get("index")
                if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS * 4 or index in blocks:
                    raise ProviderError("invalid_response")
                blocks[index] = dict(event["content_block"])
            elif kind == "content_block_delta":
                index, delta = event.get("index"), event.get("delta", {})
                if index not in blocks:
                    raise ProviderError("invalid_response")
                block = blocks[index]
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text")
                    if not isinstance(chunk, str) or block.get("type") != "text":
                        raise ProviderError("invalid_response")
                    block["text"] = block.get("text", "") + chunk
                    await _delta(on_delta, chunk)
                elif delta.get("type") == "input_json_delta":
                    if not isinstance(delta.get("partial_json"), str) or block.get("type") != "tool_use":
                        raise ProviderError("invalid_response")
                    partial[index] = partial.get(index, "") + delta["partial_json"]
                elif delta.get("type") == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                elif delta.get("type") == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif kind == "message_delta":
                finish = event.get("delta", {}).get("stop_reason", finish)
                usage.update(event.get("usage", {}))
            elif kind == "message_stop":
                if finish == "max_tokens":
                    raise ProviderError("output_limit")
                if finish not in {"end_turn", "tool_use", "stop_sequence"}:
                    raise ProviderError("invalid_response")
                for index, arguments in partial.items():
                    blocks[index]["input"] = _arguments(arguments)
                return self._anthropic_result([blocks[i] for i in sorted(blocks)], usage)
        raise ProviderError("invalid_response")

    @staticmethod
    def _anthropic_result(blocks, usage):
        if not isinstance(blocks, list):
            raise ProviderError("invalid_response")
        text, calls, continuation = [], [], []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                if not isinstance(block.get("text"), str):
                    raise ProviderError("invalid_response")
                text.append(block["text"])
            elif kind == "tool_use":
                calls.append({"id": block.get("id"), "name": block.get("name"), "arguments": block.get("input")})
            elif kind in {"thinking", "redacted_thinking"}:
                continuation.append(block)
        return {"text": "".join(text), "tool_calls": _calls(calls), "usage": _usage(usage),
                **({"continuation": continuation} if continuation else {})}

    async def _ollama(self, raw, response, on_delta):
        text, thinking, calls = [], [], {}

        async def consume(event):
            if not isinstance(event, dict):
                raise ProviderError("invalid_response")
            if "error" in event:
                raise ProviderError(_error_code(400, event))
            message = event.get("message", {})
            chunk = message.get("content", "")
            if not isinstance(chunk, str):
                raise ProviderError("invalid_response")
            text.append(chunk)
            await _delta(on_delta, chunk)
            if message.get("thinking"):
                if not isinstance(message["thinking"], str):
                    raise ProviderError("invalid_response")
                thinking.append(message["thinking"])
            for offset, call in enumerate(message.get("tool_calls", [])):
                function = call.get("function", {})
                index = call.get("index", function.get("index", offset))
                if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                    raise ProviderError("invalid_response")
                value = {"id": call.get("id") or (calls.get(index) or {}).get("id") or "call_" + uuid4().hex,
                         "name": function.get("name"), "arguments": _arguments(function.get("arguments", {}))}
                if index in calls and calls[index] != value:
                    raise ProviderError("invalid_response")
                calls[index] = value
            if event.get("done") is True:
                if event.get("done_reason") in {"length", "max_tokens"}:
                    raise ProviderError("output_limit")
                return {"text": "".join(text), "tool_calls": _calls([calls[i] for i in sorted(calls)]),
                        "usage": _usage(event, style="ollama"),
                        **({"continuation": {"thinking": "".join(thinking)}} if thinking else {})}
            return None

        if raw is not None:
            result = await consume(raw)
            if result is None:
                raise ProviderError("invalid_response")
            return result
        async for line in _lines(response):
            if line.strip():
                result = await consume(_parse(line))
                if result is not None:
                    return result
        raise ProviderError("invalid_response")

    async def list_models(self):
        path = "api/tags" if self.profile["provider"] == "ollama" else "models"
        try:
            async with self._http().stream("GET", self._url(path), headers=self._headers(), follow_redirects=False) as response:
                await self._check(response)
                raw = _parse(await _body(response))
        except httpx.TimeoutException:
            raise ProviderError("network_timeout") from None
        except httpx.HTTPError:
            raise ProviderError("network_unavailable") from None
        if not isinstance(raw, dict):
            raise ProviderError("invalid_response")
        rows = raw.get("models" if self.profile["provider"] == "ollama" else "data")
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ProviderError("invalid_response")
        models, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderError("invalid_response")
            name = row.get("model") or row.get("name") if self.profile["provider"] == "ollama" else row.get("id")
            if (not isinstance(name, str) or not 1 <= len(name) <= 200
                    or any(ord(c) < 33 or ord(c) == 127 for c in name)):
                raise ProviderError("invalid_response")
            if name not in seen:
                models.append({"id": name})
                seen.add(name)
        return models

    async def probe(self):
        """One explicit model call; a returned probe tool is evidence of tools."""
        nonce = uuid4().hex
        tools = [{"name": "cairnmind_connection_probe", "description": "Connection test only; no side effects.",
                  "parameters": {"type": "object", "properties": {"nonce": {"type": "string", "enum": [nonce]}},
                                 "required": ["nonce"], "additionalProperties": False}}]
        reply = await self.complete([{"role": "user", "content":
            "Test tool calling. Call cairnmind_connection_probe exactly once with nonce " + nonce +
            ". Do not answer in text. This probe cannot access files or execute commands."}], tools)
        calls = reply.get("tool_calls", [])
        if len(calls) != 1 or calls[0]["name"] != tools[0]["name"] or calls[0]["arguments"] != {"nonce": nonce}:
            raise ProviderError("unsupported_tools")
        return {"status": "ready", "tools": True, "usage": reply.get("usage", {})}

    async def aclose(self):
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    close = aclose


def make_provider(profile, key=None, *, client=None):
    return Provider(profile, key, client=client)
