from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import inspect
import threading

import httpx
import pytest

from cairnmind.api_providers import ProviderError, make_provider


TOOL = {"name": "read_file", "description": "Read a project file.", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}
PROVIDERS = ("openai", "anthropic", "openai_compatible", "ollama")


def transport(handler):
    """A valid, non-inferential Ollama show response precedes model requests."""
    async def dispatch(request):
        if request.url.path == "/api/show":
            assert json.loads(request.content) == {"model": "model-fixture"}
            return httpx.Response(200, json={"model_info": {
                "general.architecture": "qwen3", "qwen3.context_length": 262144}})
        result = handler(request)
        return await result if inspect.isawaitable(result) else result
    return httpx.MockTransport(dispatch)


def profile(provider):
    return {"id": "api-fixture", "client": "api", "label": "Fixture", "provider": provider,
            "model": "model-fixture", "base_url": "https://provider.test/v1" if provider != "ollama" else "http://127.0.0.1:9911"}


def sse(event):
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


class Chunked(httpx.AsyncByteStream):
    def __init__(self, text):
        self.raw = text.encode("utf-8")

    async def __aiter__(self):
        # Boundaries deliberately split UTF-8 and JSON tokens.
        for start in range(0, len(self.raw), 7):
            yield self.raw[start:start + 7]


def stream_response(provider, *, arguments=None, tool_name="read_file", call_id="call_fixture", text="Czytam łódź."):
    arguments = {"path": "note.txt"} if arguments is None else arguments
    if provider == "openai":
        final = {"status": "completed", "output": [
            {"type": "reasoning", "id": "reasoning-fixture", "encrypted_content": "opaque-fixture"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
            {"type": "function_call", "call_id": call_id, "name": tool_name, "arguments": json.dumps(arguments)}],
            "usage": {"input_tokens": 101, "output_tokens": 12}}
        body = sse({"type": "response.output_text.delta", "delta": text}) + sse({"type": "response.completed", "response": final})
    elif provider == "anthropic":
        body = sse({"type": "message_start", "message": {"usage": {"input_tokens": 101}}})
        body += sse({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
        body += sse({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private reasoning"}})
        body += sse({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque-signature"}})
        body += sse({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
        body += sse({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}})
        body += sse({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": call_id, "name": tool_name, "input": {}}})
        encoded = json.dumps(arguments)
        body += sse({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": encoded[:5]}})
        body += sse({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": encoded[5:]}})
        body += sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}})
        body += sse({"type": "message_stop"})
    elif provider == "openai_compatible":
        body = sse({"choices": [{"index": 0, "delta": {"content": text}}]})
        encoded = json.dumps(arguments)
        body += sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": call_id, "function": {"name": tool_name, "arguments": encoded[:5]}}]}}]})
        body += sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": encoded[5:]}}]}, "finish_reason": "tool_calls"}]})
        body += sse({"choices": [], "usage": {"prompt_tokens": 101, "completion_tokens": 12}}) + "data: [DONE]\n\n"
    else:
        body = json.dumps({"message": {"content": text, "thinking": "private reasoning", "tool_calls": [
            {"function": {"name": tool_name, "arguments": arguments}}]}, "done": False}, ensure_ascii=False) + "\n"
        body += json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 101, "eval_count": 12}) + "\n"
    return httpx.Response(200, headers={"content-type": "application/x-ndjson" if provider == "ollama" else "text/event-stream"}, stream=Chunked(body))


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_four_wire_protocols_stream_tools_tokens_and_replay_continuations(provider):
    requests = []

    async def handle(request):
        requests.append((request, json.loads(request.content)))
        return stream_response(provider)

    async with httpx.AsyncClient(transport=transport(handle)) as client:
        adapter = make_provider(profile(provider), "PRIVATE_TEST_KEY", client=client)
        seen = []

        async def delta(text):
            seen.append(text)

        initial = [{"role": "system", "content": "Use shared memory."}, {"role": "user", "content": "Read note.txt"}]
        reply = await adapter.complete(initial, [TOOL], on_delta=delta)
        assert reply["text"] == "Czytam łódź." and "".join(seen) == reply["text"]
        assert len(reply["tool_calls"]) == 1
        call = reply["tool_calls"][0]
        assert call["id"] and call["name"] == "read_file" and call["arguments"] == {"path": "note.txt"}
        assert reply["usage"] == {"input_tokens": 101, "output_tokens": 12, "total_tokens": 113}
        assert "private reasoning" not in "".join(seen)
        assistant = {"role": "assistant", "content": reply["text"], "tool_calls": reply["tool_calls"]}
        if "continuation" in reply:
            assistant["continuation"] = reply["continuation"]
        await adapter.complete(initial + [assistant, {"role": "tool", "tool_call_id": call["id"], "name": "read_file", "content": "Exact source text."}], [TOOL])
        request, body = requests[-1]
        assert len(requests) == 2
        if provider == "openai":
            assert request.url.path == "/v1/responses"
            assert body["store"] is False and body["max_output_tokens"] == 4096
            assert any(row.get("type") == "reasoning" for row in body["input"])
            assert body["input"][-1] == {"type": "function_call_output", "call_id": call["id"], "output": "Exact source text."}
            assert body["tools"][0]["name"] == "read_file"
        elif provider == "anthropic":
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == "PRIVATE_TEST_KEY"
            assert "authorization" not in request.headers
            assert body["system"] == "Use shared memory."
            assert body["messages"][-2]["content"][0]["signature"] == "opaque-signature"
            assert body["messages"][-1]["content"][0] == {"type": "tool_result", "tool_use_id": call["id"], "content": "Exact source text."}
        elif provider == "openai_compatible":
            assert request.url.path == "/v1/chat/completions"
            assert json.loads(body["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {"path": "note.txt"}
            assert body["messages"][-1]["tool_call_id"] == call["id"]
        else:
            assert request.url.path == "/api/chat"
            assert body["options"]["num_ctx"] == 32768
            assert body["truncate"] is False and body["shift"] is False
            assert body["messages"][-1]["tool_name"] == "read_file"
            assert body["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"path": "note.txt"}
            assert body["messages"][-2]["thinking"] == "private reasoning"
        await adapter.aclose()
        assert not client.is_closed  # Injected clients retain caller ownership.


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_explicit_probe_uses_one_side_effect_free_nonce_tool(provider):
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        tool = body["tools"][0]
        definition = tool.get("function", tool)
        schema = definition.get("parameters", definition.get("input_schema"))
        nonce = schema["properties"]["nonce"]["enum"][0]
        return stream_response(provider, arguments={"nonce": nonce}, tool_name="devbrain_connection_probe", text="")

    async with httpx.AsyncClient(transport=transport(handle)) as client:
        adapter = make_provider(profile(provider), "PRIVATE_TEST_KEY" if provider != "ollama" else None, client=client)
        assert count == 0
        result = await adapter.probe()
        assert result["status"] == "ready" and result["tools"] is True
        assert count == 1


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_model_list_is_read_only_and_local_ollama_needs_no_dummy_key(provider):
    seen = []

    async def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}]} if provider == "ollama" else {"data": [{"id": "remote-model"}]})

    async with httpx.AsyncClient(transport=transport(handle)) as client:
        adapter = make_provider(profile(provider), "SAFE" if provider != "ollama" else None, client=client)
        models = await adapter.list_models()
        assert models == [{"id": "qwen3.5:9b" if provider == "ollama" else "remote-model"}]
        assert seen[0].method == "GET"
        assert seen[0].url.path == ("/api/tags" if provider == "ollama" else "/v1/models")
        if provider == "ollama":
            assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status,code", [(401, "auth_rejected"), (402, "quota_exhausted"), (403, "access_denied"),
    (404, "model_or_endpoint_missing"), (408, "network_timeout"), (413, "context_limit"), (422, "unsupported_request"),
    (429, "rate_limited"), (500, "provider_unavailable"), (307, "provider_redirect_denied")])
async def test_errors_are_classified_without_secret_or_body_echo_and_without_retry(provider, status, code):
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "PRIVATE_RESPONSE_BODY PRIVATE_TEST_KEY"}}, headers={"location": "https://foreign.test"})

    async with httpx.AsyncClient(transport=transport(handle), follow_redirects=False) as client:
        adapter = make_provider(profile(provider), "PRIVATE_TEST_KEY", client=client)
        with pytest.raises(ProviderError) as error:
            await adapter.complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == code
        assert "PRIVATE" not in str(error.value) and "PRIVATE" not in repr(error.value)
        assert len(calls) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_quota_response_is_distinct_from_rate_limit(provider):
    async with httpx.AsyncClient(transport=transport(lambda request: httpx.Response(429,
        json={"error": {"code": "insufficient_quota", "message": "PRIVATE_QUOTA_DETAIL"}}))) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile(provider), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == "quota_exhausted"
        assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_network_failures_do_not_retry_an_uncertain_generation(provider):
    count = 0

    async def fail(request):
        nonlocal count
        count += 1
        raise httpx.ReadError("PRIVATE_TRANSPORT_MESSAGE", request=request)

    async with httpx.AsyncClient(transport=transport(fail)) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile(provider), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == "network_unavailable"
        assert "PRIVATE" not in str(error.value)
        assert count == 1


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_truncated_stream_never_returns_partial_tool_calls(provider):
    if provider == "openai":
        body = sse({"type": "response.output_text.delta", "delta": "Partial"})
    elif provider == "anthropic":
        body = sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_x", "name": "read_file", "input": {}}})
    elif provider == "openai_compatible":
        body = sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_x", "function": {"name": "read_file", "arguments": "{}"}}]}}]})
    else:
        body = json.dumps({"message": {"content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}]}, "done": False}) + "\n"
    async with httpx.AsyncClient(transport=transport(lambda request: httpx.Response(200,
        headers={"content-type": "application/x-ndjson" if provider == "ollama" else "text/event-stream"}, stream=Chunked(body)))) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile(provider), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], [TOOL])
        assert error.value.code == "invalid_response"


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_malformed_tool_arguments_are_rejected(provider):
    async with httpx.AsyncClient(transport=transport(lambda request: stream_response(provider, arguments=["not", "an", "object"]))) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile(provider), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], [TOOL])
        assert error.value.code == "invalid_response"


async def test_probe_empty_answer_is_not_a_success():
    response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "I am ready."}]}]}
    async with httpx.AsyncClient(transport=transport(lambda request: httpx.Response(200, json=response))) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile("openai"), "SAFE", client=client).probe()
        assert error.value.code == "unsupported_tools"


async def test_output_limit_and_size_limit_do_not_return_a_success(monkeypatch):
    incomplete = {"status": "incomplete", "output": []}
    async with httpx.AsyncClient(transport=transport(lambda request: httpx.Response(200, json=incomplete))) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile("openai"), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == "output_limit"
    monkeypatch.setattr("cairnmind.api_providers.MAX_RESPONSE_BYTES", 100)
    async with httpx.AsyncClient(transport=transport(lambda request: stream_response("openai"))) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile("openai"), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == "response_too_large"


async def test_missing_official_key_does_not_make_a_network_request():
    async def forbidden(request):
        pytest.fail("Missing credentials must not reach the network")
    async with httpx.AsyncClient(transport=transport(forbidden)) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile("openai"), client=client).complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == "credential_missing"


async def test_redirect_is_blocked_even_when_injected_client_would_follow_it():
    requests = []

    async def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://foreign.test/collect"})

    async with httpx.AsyncClient(transport=transport(redirect), follow_redirects=True) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile("openai"), "PRIVATE_KEY_MARKER", client=client).complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == "provider_redirect_denied"
        assert len(requests) == 1 and requests[0].url.host == "provider.test"


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_context_limit_on_http_400_is_classified_for_engine_recovery(provider):
    async with httpx.AsyncClient(transport=transport(lambda request: httpx.Response(400,
        json={"error": {"type": "invalid_request_error", "message": "prompt is too long: PRIVATE_INPUT"}}))) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile(provider), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], [])
        assert error.value.code == "context_limit"
        assert "PRIVATE" not in str(error.value)


async def test_ollama_large_context_is_rejected_before_request_until_window_explicitly_increased():
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return stream_response("ollama")

    messages = [{"role": "user", "content": "ą" * 18000}]
    async with httpx.AsyncClient(transport=transport(handle)) as client:
        adapter = make_provider(profile("ollama"), client=client)
        with pytest.raises(ProviderError) as error:
            await adapter.complete(messages, [TOOL])
        assert error.value.code == "context_limit"
        assert not requests  # No partial/truncated prompt reaches the model.
        larger = {**profile("ollama"), "limits": {"context_window_tokens": 65536}}
        reply = await make_provider(larger, client=client).complete(messages, [TOOL])
        assert reply["tool_calls"][0]["arguments"] == {"path": "note.txt"}
        assert requests[0]["options"]["num_ctx"] == 65536
        assert requests[0]["messages"][0]["content"] == messages[0]["content"]


@pytest.mark.parametrize("metadata", [{}, {"model_info": {}}, {"model_info": {"qwen3.context_length": 0}},
    {"model_info": {"qwen3.context_length": True}}, {"model_info": {"qwen3.context_length": "32768"}}])
async def test_ollama_unknown_trained_context_never_generates_or_claims_receipt(metadata):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json=metadata)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile("ollama"), client=client).complete([{"role": "user", "content": "Small prompt"}], [])
        assert error.value.code == "context_unknown"
        assert [request.url.path for request in requests] == ["/api/show"]


async def test_ollama_server_training_cap_is_not_mistaken_for_requested_context():
    requests = []

    async def handle(request):
        requests.append(request)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"general.architecture": "small", "small.context_length": 8192}})
        return stream_response("ollama")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as error:
            await make_provider(profile("ollama"), client=client).complete([{"role": "user", "content": "Small prompt"}], [])
        assert error.value.code == "context_limit"
        assert error.value.context_details == {"configured_context_window": 32768, "model_context_window": 8192}
        assert "Zmniejsz" in error.value.hint
        assert [request.url.path for request in requests] == ["/api/show"]
        requests.clear()
        smaller = {**profile("ollama"), "limits": {"context_window_tokens": 8192}}
        reply = await make_provider(smaller, client=client).complete([{"role": "user", "content": "Small prompt"}], [])
        assert reply["text"]
        assert [request.url.path for request in requests] == ["/api/show", "/api/chat"]
        payload = json.loads(requests[-1].content)
        assert payload["options"]["num_ctx"] == 8192
        assert payload["truncate"] is False and payload["shift"] is False


async def test_cancelled_stream_propagates_and_is_not_retried():
    started, parked = asyncio.Event(), asyncio.Event()
    calls = 0

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await parked.wait()
            yield b""

    async def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Waiting())

    async with httpx.AsyncClient(transport=transport(handle)) as client:
        task = asyncio.create_task(make_provider(profile("openai"), "SAFE", client=client).complete([{"role": "user", "content": "Test"}], []))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_adapter_against_actual_local_http_server(provider):
    observed = []
    wire = stream_response(provider, text="Local TCP proof")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["content-length"]))
            if self.path == "/api/show":
                assert json.loads(raw) == {"model": "model-fixture"}
                body = json.dumps({"model_info": {"general.architecture": "qwen3", "qwen3.context_length": 262144}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            observed.append((self.path, json.loads(raw), self.headers.get("authorization")))
            body = wire.stream.raw
            self.send_response(200)
            self.send_header("content-type", wire.headers["content-type"])
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    value = profile(provider)
    value["base_url"] = f"http://127.0.0.1:{server.server_port}" + ("" if provider == "ollama" else "/v1")
    adapter = make_provider(value, "TEST_LOCAL_KEY" if provider in {"openai", "anthropic"} else None)
    try:
        reply = await adapter.complete([{"role": "user", "content": "Controlled local test"}], [])
        assert reply["text"] == "Local TCP proof"
        assert observed[0][0] == {"openai": "/v1/responses", "anthropic": "/v1/messages",
                                  "openai_compatible": "/v1/chat/completions", "ollama": "/api/chat"}[provider]
        assert observed[0][1]["stream"] is True
        assert observed[0][2] == ("Bearer TEST_LOCAL_KEY" if provider == "openai" else None)
    finally:
        await adapter.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
