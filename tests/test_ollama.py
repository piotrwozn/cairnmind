"""Transport diagnostics retain actionable categories without disclosing payloads."""
from __future__ import annotations

import json

import httpx
import pytest

from cairnmind.config import Settings
from cairnmind.ollama import Ollama, OllamaUnavailable


@pytest.mark.parametrize("status,message,category", [
    (400, "model does not support tools", "unsupported_capability"),
    (400, "request validation failed", "invalid_request"),
    (404, "model 'private-model-name' not found", "model_not_found"),
    (500, "llama runner process has terminated: exit status 2", "runner_failed"),
    (500, "CUDA error: out of memory", "memory_exhausted"),
    (500, "model requires more system memory (10.5 GiB) than is available (9.0 GiB)", "memory_exhausted"),
    (500, "input length exceeds the context length", "context_length"),
    (503, "please wait", "runtime_busy"),
    (500, "unrecognized server failure", "http_error"),
    (302, "redirect", "http_error"),
    (200, "model failed to load model", "runner_failed"),
])
async def test_http_and_json_errors_have_safe_structured_diagnostics(tmp_path, status, message, category):
    settings = Settings(data_dir=tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": message})

    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client).chat([{"role": "user", "content": "private prompt"}])
    error = caught.value
    assert error.status_code == status
    assert error.endpoint == "/api/chat"
    assert error.error_code == category
    assert f"HTTP {status}" in str(error)
    assert "private-model-name" not in str(error)
    assert len(str(error)) < 240
    assert len(requests) == 1  # Failed attempts are never silently retried.


@pytest.mark.parametrize("status,body", [
    (500, {"error": "prompt: SECRET_PROMPT; password=SECRET_PASSWORD; Authorization: Bearer SECRET_TOKEN; "
                    "api_key=sk-live-secret; https://user:secret@private.example/path?token=SECRET_TOKEN"}),
    (200, {"error": "SECRET_PROMPT SECRET_PASSWORD SECRET_TOKEN"}),
    (500, {"error": "out of memory SECRET_PROMPT SECRET_PASSWORD SECRET_TOKEN"}),
    (500, {"error": "SECRET_PROMPT" * 1000}),
])
async def test_server_error_never_echoes_prompts_credentials_urls_or_long_content(tmp_path, status, body):
    settings = Settings(data_dir=tmp_path, token="SECRET_TOKEN", arcade_password="SECRET_PASSWORD")
    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body))) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client).chat([{"role": "user", "content": "SECRET_PROMPT"}])
    serialized = json.dumps({"message": str(caught.value), **caught.value.__dict__})
    for private in ("SECRET_PROMPT", "SECRET_PASSWORD", "SECRET_TOKEN", "sk-live-secret", "private.example"):
        assert private not in serialized
    assert len(serialized) < 500


@pytest.mark.parametrize("status,body", [(500, "<html>SECRET_PROMPT</html>"),
                                        (200, "<html>SECRET_PROMPT</html>"), (200, "[]")])
async def test_invalid_json_html_and_wrong_shapes_are_not_echoed(tmp_path, status, body):
    settings = Settings(data_dir=tmp_path)
    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=body))) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client).embed(["private input"])
    assert caught.value.status_code == status
    assert caught.value.endpoint == "/api/embed"
    assert caught.value.error_code == ("http_error" if status == 500 else "invalid_response")
    assert "SECRET_PROMPT" not in str(caught.value)


@pytest.mark.parametrize("exception,category", [(httpx.ReadTimeout, "read_timeout"),
                                               (httpx.ConnectError, "connect_error"),
                                               (httpx.ConnectTimeout, "request_timeout"),
                                               (httpx.RemoteProtocolError, "request_error")])
async def test_transport_failures_are_distinguishable_without_private_exception_text(tmp_path, exception, category):
    settings = Settings(data_dir=tmp_path)
    calls = []

    def respond(request):
        calls.append(request)
        raise exception("SECRET_PROMPT https://user:SECRET_PASSWORD@private.example", request=request)

    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client).models()
    error = caught.value
    assert error.status_code is None and error.endpoint == "/api/tags" and error.error_code == category
    assert exception.__name__ in str(error)
    assert "SECRET" not in str(error) and "private.example" not in str(error)
    assert error.__cause__ is None
    assert len(calls) == 1


@pytest.mark.parametrize("url,word", [("http://127.0.0.1:11434", "local"), ("https://lan.example:11434", None)])
async def test_only_a_server_on_this_machine_is_called_local(tmp_path, url, word):
    def respond(request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(base_url=url, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(Settings(data_dir=tmp_path), client, base_url=url).models()
    assert ("local" in str(caught.value)) == (word == "local"), str(caught.value)


def test_existing_unavailable_exception_constructor_remains_compatible():
    error = OllamaUnavailable("Model is unavailable")
    assert str(error) == "Model is unavailable"
    assert error.status_code is None and error.endpoint is None and error.error_code is None


# What Ollama with an MLX build answers on Apple silicon to any request with `format` (#231).
MLX_REFUSAL = {"error": "structured output is unavailable"}
OK_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
             "additionalProperties": False}


def _no_structured_output(settings, *, reply='{"ok": true}', status=501, body=MLX_REFUSAL):
    """A fake Ollama that refuses `format` and answers the same request without it."""
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(("chat", payload))
        if "format" in payload or "response_format" in payload:
            return httpx.Response(status, json=body)
        message = {"role": "assistant", "content": reply}
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"message": message})
    return respond, requests


async def test_a_server_without_structured_output_is_asked_again_with_the_schema_in_the_prompt(tmp_path):
    from cairnmind import structured_output
    settings = Settings(data_dir=tmp_path)
    respond, requests = _no_structured_output(settings)
    messages = [{"role": "system", "content": "Reply with JSON only."}, {"role": "user", "content": "Return ok."}]
    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(respond)) as client:
        ollama = Ollama(settings, client)
        reply = await ollama.chat(messages, format=dict(OK_SCHEMA))
        assert json.loads(reply["message"]["content"]) == {"ok": True}
        assert reply["structured_output"] == "prompted"
        chats = [payload for kind, payload in requests if kind == "chat"]
        assert len(chats) == 2 and "format" in chats[0] and "format" not in chats[1]
        assert json.dumps(OK_SCHEMA, sort_keys=True) in chats[1]["messages"][0]["content"]
        assert chats[1]["messages"][1:] == messages[1:]
        assert messages[0]["content"] == "Reply with JSON only."  # the caller's list is not changed
        # Nothing is cached: the next request tries `format` again.
        requests.clear()
        await ollama.chat(messages, format=dict(OK_SCHEMA))
        assert ["format" in payload for _, payload in requests] == [True, False]
    note = structured_output.doctor_note(tmp_path, settings.ollama_url, settings.chat_model)
    assert note["refused"] is True and note["text"] == "structured output: unavailable on this server"


async def test_a_fenced_json_reply_without_format_is_unwrapped_and_nothing_else_is_changed(tmp_path):
    settings = Settings(data_dir=tmp_path)
    for reply, content in (('```json\n{"ok": true}\n```', '{"ok": true}'), ("not JSON", "not JSON")):
        respond, _ = _no_structured_output(settings, reply=reply)
        async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(respond)) as client:
            result = await Ollama(settings, client).chat([{"role": "user", "content": "x"}], format=dict(OK_SCHEMA))
        assert result["message"]["content"] == content  # the caller validates it, as with `format`


@pytest.mark.parametrize("status,body,code", [
    (501, {"error": "not implemented"}, "http_error"),
    (500, MLX_REFUSAL, "http_error"),
    (400, {"error": "invalid format"}, "invalid_request"),
])
async def test_other_refusals_of_a_structured_request_are_still_unavailable(tmp_path, status, body, code):
    from cairnmind import structured_output
    settings = Settings(data_dir=tmp_path)
    respond, requests = _no_structured_output(settings, status=status, body=body)
    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client).chat([{"role": "user", "content": "x"}], format=dict(OK_SCHEMA))
    assert caught.value.error_code == code
    assert [kind for kind, _ in requests] == ["chat"]  # no retry without format
    assert structured_output.doctor_note(tmp_path, settings.ollama_url, settings.chat_model) is None


async def test_the_refusal_of_a_request_without_format_is_not_retried(tmp_path):
    settings = Settings(data_dir=tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(501, json=MLX_REFUSAL)
    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client).chat([{"role": "user", "content": "x"}])
    assert caught.value.error_code == "structured_output_unavailable" and len(requests) == 1


async def test_an_openai_compatible_server_without_structured_output_gets_the_schema_in_the_prompt(tmp_path):
    from cairnmind.memory_openai import OpenAICompatible
    settings = Settings(data_dir=tmp_path)
    respond, requests = _no_structured_output(settings, body={"error": {"message": "Structured output is unavailable"}})
    profile = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:8080/v1"}
    async with httpx.AsyncClient(base_url=profile["base_url"], transport=httpx.MockTransport(respond)) as client:
        server = OpenAICompatible(settings, "chat", profile, client)
        reply = await server.chat([{"role": "user", "content": "x"}], format=dict(OK_SCHEMA))
    assert json.loads(reply["message"]["content"]) == {"ok": True}
    chats = [payload for kind, payload in requests if kind == "chat"]
    assert "response_format" in chats[0] and "response_format" not in chats[1]
    assert chats[1]["messages"][0]["role"] == "system"


async def test_diagnostic_endpoint_omits_query_strings_and_unrecognized_paths(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "unrecognized failure"}))) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client)._request("GET", "/api/private?token=SECRET_TOKEN")
    assert caught.value.endpoint == "/api/unknown"
    assert "private" not in str(caught.value) and "SECRET_TOKEN" not in str(caught.value)
