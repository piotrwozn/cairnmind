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


def test_existing_unavailable_exception_constructor_remains_compatible():
    error = OllamaUnavailable("Model is unavailable")
    assert str(error) == "Model is unavailable"
    assert error.status_code is None and error.endpoint is None and error.error_code is None


async def test_diagnostic_endpoint_omits_query_strings_and_unrecognized_paths(tmp_path):
    settings = Settings(data_dir=tmp_path)
    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "unrecognized failure"}))) as client:
        with pytest.raises(OllamaUnavailable) as caught:
            await Ollama(settings, client)._request("GET", "/api/private?token=SECRET_TOKEN")
    assert caught.value.endpoint == "/api/unknown"
    assert "private" not in str(caught.value) and "SECRET_TOKEN" not in str(caught.value)
