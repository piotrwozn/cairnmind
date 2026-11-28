"""Fast-tier edge-case and error-handling coverage tests for cairnmind.agents.api_providers."""
from __future__ import annotations

import json
from contextlib import contextmanager

import httpx
import pytest

from cairnmind.agents import api_providers as ap
from cairnmind.agents.api_providers import ProviderError, make_provider


@contextmanager
def _raises_code(code: str):
    with pytest.raises(ProviderError) as exc:
        yield
    assert exc.value.code == code


def _profile(provider: str) -> dict:
    return {
        "id": "p-edge",
        "client": "api",
        "label": "Edge",
        "provider": provider,
        "model": "m-edge",
        "base_url": "http://127.0.0.1:11434" if provider == "ollama" else "https://provider.test/v1",
    }


class _RawStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _resp(chunks: list[bytes], *, status: int = 200, content_type: str = "text/event-stream") -> httpx.Response:
    return httpx.Response(status, headers={"content-type": content_type}, stream=_RawStream(chunks))


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def test_low_level_validators_and_usage_helpers(monkeypatch):
    with _raises_code("unsupported_request"):
        ap._json({"x": float("nan")})
    with _raises_code("invalid_response"):
        ap._parse("NaN")
    with _raises_code("invalid_response"):
        ap._parse("{bad")

    monkeypatch.setattr(ap, "MAX_LINE_BYTES", 8)
    with _raises_code("response_too_large"):
        ap._arguments('{"a":"1234567890"}')
    monkeypatch.setattr(ap, "MAX_LINE_BYTES", 512 * 1024)

    with _raises_code("invalid_response"):
        ap._arguments([1, 2])

    with _raises_code("response_too_large"):
        ap._calls([{"id": f"c{i}", "name": "ok"} for i in range(ap.MAX_TOOL_CALLS + 1)])
    with _raises_code("invalid_response"):
        ap._calls([{"id": "bad id", "name": "ok"}])
    with _raises_code("invalid_response"):
        ap._calls([{"id": "c1", "name": "ok"}, {"id": "c1", "name": "ok"}])

    usage = ap._usage({
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    })
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    }
    assert ap._text_content(None) == ""
    with _raises_code("unsupported_request"):
        ap._text_content(123)
    with _raises_code("unsupported_request"):
        ap._chat_messages([{"role": "unknown", "content": "hi"}])


@pytest.mark.asyncio
async def test_lines_sse_and_body_bounds(monkeypatch):
    seen = []
    await ap._delta(lambda t: seen.append(t), "hi")
    assert seen == ["hi"]

    monkeypatch.setattr(ap, "MAX_LINE_BYTES", 5)
    with _raises_code("response_too_large"):
        async for _ in ap._lines(_resp([b"123456\n"])):
            pass
    with _raises_code("response_too_large"):
        async for _ in ap._lines(_resp([b"123456"])):
            pass
    monkeypatch.setattr(ap, "MAX_LINE_BYTES", 512 * 1024)

    with _raises_code("invalid_response"):
        async for _ in ap._lines(_resp([b"\xff\xfe\n"])):
            pass
    with _raises_code("invalid_response"):
        async for _ in ap._lines(_resp([b"\xff\xfe"])):
            pass
    got = [line async for line in ap._lines(_resp([b"tail"]))]
    assert got == ["tail"]

    with _raises_code("invalid_response"):
        async for _ in ap._sse(_resp([b"data: [1,2]\n\n"])):
            pass
    events = [ev async for ev in ap._sse(_resp([b"data:{\"ok\":true}"]))]
    assert events == [{"ok": True}]
    done_ev = [ev async for ev in ap._sse(_resp([b"data: [DONE]"]))]
    assert done_ev == [None]
    with _raises_code("invalid_response"):
        async for _ in ap._sse(_resp([b"data: 123"])):
            pass

    monkeypatch.setattr(ap, "MAX_LINE_BYTES", 4)
    with _raises_code("response_too_large"):
        async for _ in ap._sse(_resp([b"data: 12345\n\n"])):
            pass
    monkeypatch.setattr(ap, "MAX_LINE_BYTES", 512 * 1024)

    with _raises_code("response_too_large"):
        await ap._body(_resp([b"abcdef"]), limit=3)


@pytest.mark.asyncio
async def test_openai_responses_error_and_edge_branches():
    adapter = make_provider(_profile("openai"), "k")
    with _raises_code("invalid_response"):
        adapter._response_result("not-dict")
    with _raises_code("provider_error"):
        adapter._response_result({"status": "failed"})
    with _raises_code("invalid_response"):
        adapter._response_result({"status": "mystery"})
    with _raises_code("invalid_response"):
        adapter._response_result({"status": "completed", "output": "bad"})
    with _raises_code("invalid_response"):
        adapter._response_result({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": 5}]}]})
    with _raises_code("policy_refused"):
        adapter._response_result({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]})

    with _raises_code("invalid_response"):
        await adapter._responses(None, _resp([b"data: [DONE]\n\n", _sse({"type": "response.output_text.delta", "delta": 99})]), None)

    seen = []
    done_resp = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]}
    res = await adapter._responses(None, _resp([_sse({"type": "response.completed", "response": done_resp})]), seen.append)
    assert res["text"] == "hello" and seen == ["hello"]

    with _raises_code("output_limit"):
        await adapter._responses(None, _resp([_sse({"type": "response.incomplete"})]), None)
    with _raises_code("provider_error"):
        await adapter._responses(None, _resp([_sse({"type": "response.failed", "response": {}})]), None)


@pytest.mark.asyncio
async def test_chat_anthropic_ollama_and_list_models_error_branches():
    chat = make_provider(_profile("openai_compatible"), "k")
    with _raises_code("invalid_response"):
        await chat._chat({"choices": []}, None, None)
    with _raises_code("output_limit"):
        await chat._chat({"choices": [{"finish_reason": "length"}]}, None, None)
    with _raises_code("invalid_response"):
        await chat._chat({"choices": [{"finish_reason": "weird"}]}, None, None)
    with _raises_code("policy_refused"):
        await chat._chat({"choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}, None, None)

    with _raises_code("provider_error"):
        await chat._chat(None, _resp([_sse({"error": {"message": "fail"}})]), None)
    with _raises_code("invalid_response"):
        await chat._chat(None, _resp([_sse({"choices": "bad"})]), None)
    with _raises_code("policy_refused"):
        await chat._chat(None, _resp([_sse({"choices": [{"index": 1}, {"index": 0, "delta": {"refusal": "nope"}}]})]), None)
    with _raises_code("invalid_response"):
        await chat._chat(None, _resp([_sse({"choices": [{"index": 0, "delta": {"content": 123}}]})]), None)
    with _raises_code("invalid_response"):
        await chat._chat(None, _resp([_sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": -1}]}}]})]), None)
    with _raises_code("invalid_response"):
        await chat._chat(None, _resp([_sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": 123}]}}]})]), None)
    with _raises_code("output_limit"):
        await chat._chat(None, _resp([_sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})]), None)
    with _raises_code("policy_refused"):
        await chat._chat(None, _resp([_sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]})]), None)

    anth = make_provider(_profile("anthropic"), "k")
    with _raises_code("provider_error"):
        await anth._anthropic({"type": "error", "error": {"message": "bad"}}, None, None)
    with _raises_code("output_limit"):
        await anth._anthropic({"stop_reason": "max_tokens", "content": []}, None, None)
    with _raises_code("invalid_response"):
        await anth._anthropic({"stop_reason": "other", "content": []}, None, None)
    with _raises_code("invalid_response"):
        anth._anthropic_result("not-list", {})
    with _raises_code("invalid_response"):
        anth._anthropic_result([{"type": "text", "text": 5}], {})

    with _raises_code("provider_error"):
        await anth._anthropic(None, _resp([b"data: [DONE]\n\n", _sse({"type": "error"})]), None)
    with _raises_code("invalid_response"):
        await anth._anthropic(None, _resp([_sse({"type": "content_block_start", "index": -1, "content_block": {}})]), None)
    with _raises_code("invalid_response"):
        await anth._anthropic(None, _resp([_sse({"type": "content_block_delta", "index": 0, "delta": {}})]), None)
    with _raises_code("invalid_response"):
        await anth._anthropic(None, _resp([
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": 123}}),
        ]), None)
    with _raises_code("invalid_response"):
        await anth._anthropic(None, _resp([
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        ]), None)
    with _raises_code("output_limit"):
        await anth._anthropic(None, _resp([
            _sse({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}),
            _sse({"type": "message_stop"}),
        ]), None)
    with _raises_code("invalid_response"):
        await anth._anthropic(None, _resp([_sse({"type": "message_stop"})]), None)

    oll = make_provider(_profile("ollama"))
    with _raises_code("invalid_response"):
        await oll._ollama("not-dict", None, None)
    with _raises_code("provider_error"):
        await oll._ollama({"error": "boom"}, None, None)
    with _raises_code("invalid_response"):
        await oll._ollama({"message": {"content": 5}, "done": True}, None, None)
    with _raises_code("invalid_response"):
        await oll._ollama({"message": {"content": "", "thinking": 5}, "done": True}, None, None)
    with _raises_code("invalid_response"):
        await oll._ollama({"message": {"content": "", "tool_calls": [{"index": -1, "function": {"name": "a"}}]}, "done": True}, None, None)
    with _raises_code("invalid_response"):
        await oll._ollama({"message": {"content": "", "tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "a", "arguments": {}}},
            {"index": 0, "id": "c2", "function": {"name": "b", "arguments": {}}},
        ]}, "done": True}, None, None)
    with _raises_code("output_limit"):
        await oll._ollama({"message": {"content": "x"}, "done": True, "done_reason": "length"}, None, None)
    with _raises_code("invalid_response"):
        await oll._ollama({"message": {"content": "x"}, "done": False}, None, None)

    async def timeout_handler(_req):
        raise httpx.ReadTimeout("slow")

    async def http_err_handler(_req):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as c1:
        with _raises_code("network_timeout"):
            await make_provider(_profile("openai"), "k", client=c1).list_models()
    async with httpx.AsyncClient(transport=httpx.MockTransport(http_err_handler)) as c2:
        with _raises_code("network_unavailable"):
            await make_provider(_profile("openai"), "k", client=c2).list_models()

    for payload in ("[1]", '{"data":"bad"}', '{"data":[1]}', '{"data":[{"id":"bad\\x00name"}]}'):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r, p=payload: httpx.Response(200, content=p.encode("utf-8")))
        ) as cli:
            with _raises_code("invalid_response"):
                await make_provider(_profile("openai"), "k", client=cli).list_models()
