"""The knowledge agent's model servers: profiles, the OpenAI-compatible seam, doctor and evaluation records.

No model and no network: every server here is an httpx.MockTransport.
"""
from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import memory_models
from cairnmind.config import Settings
from cairnmind.memory_openai import OpenAICompatible
from cairnmind.ollama import Ollama, OllamaUnavailable


LMSTUDIO = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1234/v1"}


def settings_with(tmp_path, **values):
    return Settings(data_dir=tmp_path, **values)


# Profiles -------------------------------------------------------------------------------------------

def test_default_configuration_keeps_the_plain_local_ollama_client(tmp_path):
    settings = settings_with(tmp_path)
    client = memory_models.from_settings(settings)
    assert type(client) is Ollama
    assert memory_models.is_default(settings)
    assert memory_models.context_tokens(settings) == settings.num_ctx


@pytest.mark.parametrize("row,message", [
    ({"provider": "openai_compatible", "base_url": "http://models.example:8000/v1"}, "HTTPS"),
    ({"provider": "openai_compatible", "base_url": "https://models.example/v1"}, "--allow-remote"),
    ({"provider": "openai_compatible", "base_url": "https://user:secret@models.example/v1",
      "allow_remote": True}, "key"),
    ({"provider": "openai_compatible"}, "address"),
    ({"provider": "anthropic", "base_url": "https://api.anthropic.com/v1"}, "provider"),
    ({**LMSTUDIO, "api_key": "sk-secret"}, "unsupported"),
    ({**LMSTUDIO, "credential_ref": "sk-secret"}, "credential"),
    ({**LMSTUDIO, "context_window": 100}, "context"),
    ({**LMSTUDIO, "allow_remote": "yes"}, "allow_remote"),
])
def test_profiles_refuse_plain_http_remote_hosts_unconfirmed_remotes_and_secrets(row, message):
    with pytest.raises(ValueError, match=message):
        memory_models.normalize_memory_model(row, "chat")


def test_a_remote_server_is_accepted_only_with_https_and_explicit_consent():
    row = memory_models.normalize_memory_model(
        {"provider": "openai_compatible", "base_url": "https://models.example/v1/", "allow_remote": True,
         "credential_ref": "env:MODELS_KEY"}, "embedding")
    assert row == {"provider": "openai_compatible", "base_url": "https://models.example/v1",
                   "allow_remote": True, "credential_ref": "env:MODELS_KEY"}
    with pytest.raises(ValueError, match="chat"):
        memory_models.normalize_memory_model({**LMSTUDIO, "context_window": 32768}, "embedding")


def test_the_legacy_ollama_address_still_refuses_a_remote_host(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        Ollama(settings_with(tmp_path, ollama_url="https://models.example"))


def test_an_invalid_saved_profile_is_refused_before_any_request(tmp_path):
    settings = settings_with(tmp_path, memory_chat={"provider": "openai_compatible",
                                                    "base_url": "http://models.example/v1"})
    with pytest.raises(ValueError, match="HTTPS"):
        memory_models.from_settings(settings)


# The OpenAI-compatible client -------------------------------------------------------------------------

def chat_reply(message, finish="stop", usage=None):
    return {"id": "x", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 3}}


async def test_chat_speaks_chat_completions_and_answers_in_the_ollama_shape(tmp_path):
    settings = settings_with(tmp_path, chat_model="qwen/qwen3-8b", memory_chat=dict(LMSTUDIO))
    seen = []

    def server(request):
        seen.append(request)
        return httpx.Response(200, json=chat_reply({"role": "assistant", "content": '{"ok": true}'}))

    async with httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(server)) as http:
        client = OpenAICompatible(settings, "chat", memory_models.profile(settings, "chat"), http)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        reply = await client.chat([{"role": "user", "content": "probe"}], tools=None, format=schema,
                                  options={"num_predict": 77})
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "qwen/qwen3-8b"
    assert body["max_tokens"] == 77 and body["temperature"] == 0 and body["stream"] is False
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "cairnmind_response", "schema": schema}}
    assert not {"num_ctx", "think", "options", "format", "tools"} & set(body)
    assert "authorization" not in request.headers
    assert reply["message"] == {"role": "assistant", "content": '{"ok": true}'}
    assert reply["done_reason"] == "stop"
    assert reply["prompt_eval_count"] == 11 and reply["eval_count"] == 3


async def test_a_reasoning_effort_in_the_chat_profile_is_sent_with_every_chat_request(tmp_path):
    # Ollama's /v1 endpoint thinks by default (its native think:false has no effect there); answers
    # through it match the native path only with reasoning_effort "none".
    settings = settings_with(tmp_path, memory_chat={**LMSTUDIO, "reasoning_effort": "none"})
    bodies = []

    def server(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply({"role": "assistant", "content": "x"}))

    async with httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(server)) as http:
        await OpenAICompatible(settings, "chat", memory_models.profile(settings, "chat"), http).chat([])
    assert bodies[0]["reasoning_effort"] == "none"
    for row, message in (({**LMSTUDIO, "reasoning_effort": "max"}, "reasoning"),):
        with pytest.raises(ValueError, match=message):
            memory_models.normalize_memory_model(row, "chat")
    with pytest.raises(ValueError, match="chat"):
        memory_models.normalize_memory_model({**LMSTUDIO, "reasoning_effort": "none"}, "embedding")
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        memory_models.normalize_memory_model({"provider": "ollama", "reasoning_effort": "none"}, "chat")


async def test_tool_calls_round_trip_with_ids_and_json_arguments(tmp_path):
    settings = settings_with(tmp_path, memory_chat=dict(LMSTUDIO))
    bodies = []

    def server(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply({"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "search", "arguments": '{"query": "engine"}'}}]}, finish="tool_calls"))

    async with httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(server)) as http:
        client = OpenAICompatible(settings, "chat", memory_models.profile(settings, "chat"), http)
        tools = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
        reply = await client.chat([{"role": "user", "content": "q"}], tools=tools)
        calls = reply["message"]["tool_calls"]
        assert calls == [{"id": "call_a", "function": {"name": "search", "arguments": {"query": "engine"}}}]
        assert reply["message"]["content"] == "" and reply["done_reason"] == "tool_calls"
        # The agent appends the assistant turn and one tool message per call, in Ollama's shape.
        await client.chat([{"role": "user", "content": "q"},
                           {"role": "assistant", "content": "", "tool_calls": calls},
                           {"role": "tool", "tool_name": "search", "content": "{}"}], tools=tools)
    assert bodies[0]["tools"] == tools
    assistant, tool = bodies[1]["messages"][1:]
    assert assistant["tool_calls"] == [{"id": "call_a", "type": "function",
                                        "function": {"name": "search", "arguments": '{"query": "engine"}'}}]
    assert tool == {"role": "tool", "tool_call_id": "call_a", "content": "{}"}


async def test_a_credential_reference_is_resolved_per_request_and_never_echoed(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_TEST_MODEL_KEY", "sk-test-value")
    settings = settings_with(tmp_path, memory_chat={**LMSTUDIO, "credential_ref": "env:CAIRNMIND_TEST_MODEL_KEY"})
    seen = []

    def server(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(401, json={"error": {"message": "invalid key sk-test-value"}})

    async with httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(server)) as http:
        client = OpenAICompatible(settings, "chat", memory_models.profile(settings, "chat"), http,
                                  credential=memory_models.credential_for(settings, "chat"))
        with pytest.raises(OllamaUnavailable) as caught:
            await client.chat([{"role": "user", "content": "q"}])
    assert seen == ["Bearer sk-test-value"]
    assert "sk-test-value" not in str(caught.value) and caught.value.error_code == "http_error"
    monkeypatch.delenv("CAIRNMIND_TEST_MODEL_KEY")
    with pytest.raises(OllamaUnavailable) as missing:
        await OpenAICompatible(settings, "chat", memory_models.profile(settings, "chat"),
                               credential=memory_models.credential_for(settings, "chat")).chat([])
    assert missing.value.error_code == "credential_missing"


@pytest.mark.parametrize("status,body,code", [
    (404, {"error": {"message": "model 'secret-name' not found"}}, "model_not_found"),
    (400, {"error": {"message": "response_format json_schema is not supported"}}, "unsupported_capability"),
    (400, {"error": {"message": "This model's maximum context length is 4096 tokens"}}, "context_length"),
    (429, {"error": {"message": "rate limited"}}, "runtime_busy"),
    (200, {"choices": []}, "invalid_response"),
])
async def test_server_errors_keep_the_existing_codes_without_echoing_text(tmp_path, status, body, code):
    settings = settings_with(tmp_path, memory_chat=dict(LMSTUDIO))
    async with httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body))) as http:
        client = OpenAICompatible(settings, "chat", memory_models.profile(settings, "chat"), http)
        with pytest.raises(OllamaUnavailable) as caught:
            await client.chat([{"role": "user", "content": "SECRET_PROMPT"}])
    assert caught.value.error_code == code
    assert caught.value.endpoint == "/chat/completions"
    assert "secret-name" not in str(caught.value) and "SECRET_PROMPT" not in str(caught.value)


async def test_embeddings_are_ordered_by_index_and_validated(tmp_path):
    settings = settings_with(tmp_path, embedding_model="text-embed", memory_embedding=dict(LMSTUDIO))
    replies = [{"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]},
               {"data": [{"index": 0, "embedding": [0.0, 0.0]}]},
               {"data": [{"index": 0, "embedding": [1.0]}]}]
    bodies = []

    def server(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))

    async with httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(server)) as http:
        client = OpenAICompatible(settings, "embedding", memory_models.profile(settings, "embedding"), http)
        assert await client.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
        with pytest.raises(OllamaUnavailable, match="zero vector"):
            await client.embed(["a"])
        with pytest.raises(OllamaUnavailable, match="number of vectors"):
            await client.embed(["a", "b"])
        assert await client.embed([]) == []
    assert bodies[0] == {"model": "text-embed", "input": ["a", "b"], "encoding_format": "float"}


async def test_identity_names_the_server_without_its_address_and_requires_a_listed_model(tmp_path):
    settings = settings_with(tmp_path, embedding_model="text-embed", memory_embedding=dict(LMSTUDIO))

    async with httpx.AsyncClient(base_url=LMSTUDIO["base_url"], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": "text-embed"}]}))) as http:
        client = OpenAICompatible(settings, "embedding", memory_models.profile(settings, "embedding"), http)
        identity = await client.model_identity("text-embed")
        with pytest.raises(OllamaUnavailable) as missing:
            await client.model_identity("other")
    assert identity.startswith("text-embed@openai_compatible:") and "127.0.0.1" not in identity
    assert missing.value.error_code == "model_not_found"
    other = settings_with(tmp_path, embedding_model="text-embed",
                          memory_embedding={**LMSTUDIO, "base_url": "http://127.0.0.1:8000/v1"})
    assert memory_models.server_identity(other, "embedding") != memory_models.server_identity(settings, "embedding")


# Two roles, two servers -----------------------------------------------------------------------------

class Fake:
    def __init__(self, name):
        self.name, self.calls = name, []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append("chat")
        return {"message": {"role": "assistant", "content": self.name}}

    async def embed(self, texts):
        self.calls.append("embed")
        return [[1.0] for _ in texts]

    async def models(self):
        return {"models": [{"name": self.name}]}

    async def model_identity(self, name):
        return f"{name}@{self.name}"

    async def aclose(self):
        self.calls.append("close")


async def test_the_facade_sends_chat_and_embeddings_to_their_own_servers(tmp_path):
    settings = settings_with(tmp_path, chat_model="c", embedding_model="e", memory_chat=dict(LMSTUDIO))
    chat, embedding = Fake("chat-server"), Fake("embedding-server")
    models = memory_models.MemoryModels(settings, chat, embedding)
    assert (await models.chat([]))["message"]["content"] == "chat-server"
    assert await models.embed(["x"]) == [[1.0]]
    assert await models.model_identity("e") == "e@embedding-server"
    assert await models.model_identity("c") == "c@chat-server"
    assert await models.identities() == {"chat": "c@chat-server", "embedding": "e@embedding-server"}
    assert [m["name"] for m in (await models.models())["models"]] == ["chat-server", "embedding-server"]
    await models.aclose()
    assert chat.calls == ["chat", "close"] and embedding.calls == ["embed", "close"]


def test_from_settings_builds_one_client_per_role(tmp_path):
    settings = settings_with(tmp_path, memory_embedding=dict(LMSTUDIO))
    models = memory_models.from_settings(settings)
    assert isinstance(models, memory_models.MemoryModels)
    assert type(models.chat_client) is Ollama and type(models.embedding_client) is OpenAICompatible
    chat = settings_with(tmp_path, memory_chat={**LMSTUDIO, "context_window": 32768})
    assert memory_models.context_tokens(chat) == 32768


# Evaluation records, answers and doctor --------------------------------------------------------------

def test_a_pair_is_evaluated_only_after_recall_and_memory_pass_for_exactly_that_pair(tmp_path):
    settings = settings_with(tmp_path)
    pair = {"chat": "c@1", "embedding": "e@1"}
    assert memory_models.evaluation_status(settings, pair) == {"evaluated": False, "passed": []}
    memory_models.record_evaluation(settings, "recall", pair)
    assert memory_models.evaluation_status(settings, pair) == {"evaluated": False, "passed": ["recall"]}
    memory_models.record_evaluation(settings, "memory", pair)
    assert memory_models.evaluation_status(settings, pair)["evaluated"] is True
    assert memory_models.evaluation_status(settings, {**pair, "embedding": "e@2"})["evaluated"] is False
    (tmp_path / "models.evaluated.json").write_text("{not json")
    assert memory_models.evaluation_status(settings, pair) == {"evaluated": False, "passed": []}


async def test_answers_are_labelled_only_when_the_models_differ_from_the_release(tmp_path):
    assert await memory_models.answer_label(settings_with(tmp_path), Fake("x")) is None
    settings = settings_with(tmp_path, chat_model="c", embedding_model="e", memory_chat=dict(LMSTUDIO))
    models = memory_models.MemoryModels(settings, Fake("s"), Fake("s"))
    assert await memory_models.answer_label(settings, models) == "unevaluated"
    for kind in ("recall", "memory"):
        memory_models.record_evaluation(settings, kind, await models.identities())
    assert await memory_models.answer_label(settings, models) == "evaluated"


def test_doctor_says_where_memory_content_goes_and_never_shows_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_TEST_MODEL_KEY", "sk-doctor-secret")
    settings = settings_with(tmp_path, chat_model="remote-chat", memory_chat={
        "provider": "openai_compatible", "base_url": "https://models.example/v1", "allow_remote": True,
        "credential_ref": "env:CAIRNMIND_TEST_MODEL_KEY"})

    def server(request):
        if request.url.host == "models.example":
            assert request.headers["authorization"] == "Bearer sk-doctor-secret"
            return httpx.Response(200, json={"data": [{"id": "remote-chat"}]})
        return httpx.Response(200, json={"models": [{"name": settings.embedding_model, "digest": "abc"}]})

    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        result = memory_models.doctor_models(settings, http)
    chat, embedding = result["chat"], result["embedding"]
    assert chat["provider"] == "openai_compatible" and chat["model"] == "remote-chat"
    assert chat["destination"] == "remote" and "models.example" in chat["content_sent_to"]
    assert chat["reachable"] and chat["model_available"]
    assert chat["credential"] == {"configured": True, "storage": "environment", "code": "configured_unchecked"}
    assert embedding["provider"] == "ollama" and embedding["destination"] == "this computer"
    assert embedding["identity"] == f"{settings.embedding_model}@abc"
    assert result["evaluated"] is False and result["evaluations_passed"] == []
    assert "sk-doctor-secret" not in json.dumps(result)


# The CLI ------------------------------------------------------------------------------------------------

def mock_async_client(monkeypatch, server):
    real = httpx.AsyncClient
    monkeypatch.setattr("cairnmind.ollama.httpx.AsyncClient",
                        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(server)))


def lmstudio_server(request):
    if request.url.path == "/v1/chat/completions":
        return httpx.Response(200, json=chat_reply({"role": "assistant", "content": '{"ok": true}'}))
    if request.url.path == "/v1/embeddings":
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.5, 0.1]}]})
    if request.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "qwen/qwen3-8b"}, {"id": "text-embed"}]})
    return httpx.Response(404, json={"error": "not found"})


def test_models_set_checks_the_server_before_saving_and_names_the_rebuild(tmp_path, monkeypatch):
    from cairnmind import cli
    mock_async_client(monkeypatch, lmstudio_server)
    data = tmp_path / "data"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--data-dir", str(data), "models", "set", "--role", "embedding",
                                     "--provider", "openai_compatible", "--base-url", "http://127.0.0.1:1234/v1",
                                     "--model", "text-embed"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["checked"] == {"embedding_dimension": 3}
    assert report["embedding_changed"] is True and "rebuild-vectors" in " ".join(report["next"])
    saved = Settings.load(data)
    assert saved.embedding_model == "text-embed"
    assert saved.memory_embedding == {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1234/v1"}
    assert saved.memory_chat is None and saved.chat_model == Settings(data_dir=data).chat_model

    chat = runner.invoke(cli.app, ["--data-dir", str(data), "models", "set", "--role", "chat",
                                   "--provider", "openai_compatible", "--base-url", "http://127.0.0.1:1234/v1",
                                   "--model", "qwen/qwen3-8b", "--context-window", "32768"])
    assert chat.exit_code == 0, chat.output
    assert json.loads(chat.output)["checked"] == {"structured_output": True}
    assert Settings.load(data).memory_chat["context_window"] == 32768


def test_models_set_saves_nothing_when_the_check_fails(tmp_path, monkeypatch):
    from cairnmind import cli
    mock_async_client(monkeypatch, lambda request: httpx.Response(200, json=(
        {"data": [{"id": "m"}]} if request.url.path == "/v1/models"
        else chat_reply({"role": "assistant", "content": "not json"}))))
    data = tmp_path / "data"
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "models", "set", "--role", "chat",
                                          "--provider", "openai_compatible",
                                          "--base-url", "http://127.0.0.1:1234/v1", "--model", "m"])
    assert result.exit_code != 0
    assert "structured" in result.output
    assert Settings.load(data).memory_chat is None


def test_models_set_refuses_a_remote_server_without_consent(tmp_path):
    from cairnmind import cli
    data = tmp_path / "data"
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "models", "set", "--role", "chat",
                                          "--provider", "openai_compatible",
                                          "--base-url", "https://models.example/v1", "--model", "m"])
    assert result.exit_code != 0 and "--allow-remote" in result.output
    assert Settings.load(data).memory_chat is None
