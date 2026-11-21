"""An ollama_url on another computer sends nothing (#288): a role without a profile needs the consent a profile
records, so the address is refused, from config.json and from the environment alike, before any request.

Every client of the process is a recording httpx.MockTransport; the tests fail when one request names another host.
"""
import json

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli, integrations, knowledge_mode, memory_models
from cairnmind.config import Settings
from cairnmind.knowledge_mode import missing_models as REAL_PROBE
from cairnmind.memory_models import RemoteServerRefused
from cairnmind.terminal_i18n import tr

REMOTE = "http://models.lan:11434"
TAGS = {"models": [{"name": "qwen3.5:9b", "digest": "a" * 64}, {"name": "qwen3-embedding:0.6b", "digest": "b" * 64}]}


@pytest.fixture
def seen(monkeypatch):
    """Every request any httpx client of the process makes, as (host, path)."""
    calls = []

    def respond(request):
        calls.append((request.url.host, request.url.path))
        return httpx.Response(200, json=TAGS)
    for name in ("Client", "AsyncClient"):
        real = getattr(httpx, name)
        monkeypatch.setattr(httpx, name, lambda real=real, **kw: real(transport=httpx.MockTransport(respond), **kw))
    return calls


def remote_settings(tmp_path, monkeypatch, source):
    if source == "environment":
        monkeypatch.setenv("CAIRNMIND_OLLAMA_URL", REMOTE)
        return Settings.load(tmp_path / "data")
    return Settings(data_dir=tmp_path, ollama_url=REMOTE)


def sentence_for(source):
    return "ollama_url (CAIRNMIND_OLLAMA_URL)" if source == "environment" else "ollama_url names"


SOURCES = ["config.json", "environment"]


@pytest.mark.parametrize("source", SOURCES)
def test_doctor_probes_send_nothing_to_the_other_computer_and_name_the_setting(tmp_path, monkeypatch, seen, source):
    settings = remote_settings(tmp_path, monkeypatch, source)
    monkeypatch.setattr(integrations, "_probe_cli", lambda *a: {"installed": False})
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)
    result = integrations.doctor(settings)
    assert all(host != "models.lan" for host, _ in seen), seen
    for found in (result["ollama"]["error"], result["memory_models"]["chat"]["error"],
                  result["memory_models"]["embedding"]["error"]):
        assert sentence_for(source) in found and "--allow-remote" in found
    assert result["memory_models"]["chat"]["reachable"] is False


@pytest.mark.parametrize("source", SOURCES)
def test_init_probe_refuses_instead_of_choosing_deterministic_only_mode(tmp_path, monkeypatch, seen, source):
    settings = remote_settings(tmp_path, monkeypatch, source)
    with pytest.raises(ValueError, match="--allow-remote"):
        REAL_PROBE(settings)
    assert not seen


@pytest.mark.parametrize("source", SOURCES)
def test_the_clients_of_the_knowledge_agent_refuse_the_address(tmp_path, monkeypatch, seen, source):
    settings = remote_settings(tmp_path, monkeypatch, source)
    with pytest.raises(RemoteServerRefused) as refused:
        memory_models.from_settings(settings)  # ask, extraction, embedding, eval, the start check
    assert sentence_for(source) in str(refused.value)
    settings.memory_chat = {"provider": "ollama", "base_url": "http://127.0.0.1:11434"}
    with pytest.raises(RemoteServerRefused, match="--allow-remote"):  # the embedding role has no profile
        memory_models.from_settings(settings)
    for call in (memory_models.setup_tag, memory_models.destinations):
        with pytest.raises(RemoteServerRefused):
            call(settings)
    assert not seen


def test_init_stores_nothing_and_asks_nobody_when_the_address_is_remote(tmp_path, monkeypatch, seen):
    from cairnmind import database_mode, engine_docker
    from test_knowledge_mode import FakeStore
    monkeypatch.setattr(engine_docker, "choose_engine_mode", lambda settings: None)
    monkeypatch.setattr(database_mode, "connect_only", lambda settings: False)
    monkeypatch.setattr(database_mode, "engine_mode", lambda settings: "native")
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore())
    monkeypatch.setattr(knowledge_mode, "missing_models", REAL_PROBE)
    data = tmp_path / "memory"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"port": 1, "knowledge_agent": "", "ollama_url": REMOTE}))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "init"])
    assert isinstance(result.exception, ValueError) and "--allow-remote" in str(result.exception)
    assert Settings.load(data).knowledge_agent == "" and not seen


@pytest.mark.parametrize("command", [["models", "pull"], ["models", "list"]])
def test_models_pull_and_list_send_nothing_to_the_other_computer(tmp_path, monkeypatch, seen, command):
    data = tmp_path / "memory"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"port": 1, "knowledge_agent": "enabled", "ollama_url": REMOTE}))
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: pytest.fail("a store was opened"))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(data), *command])
    assert result.exit_code != 0 and not seen
    assert "--allow-remote" in " ".join((result.output + str(result.exception)).split())


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:9", "http://[::1]:1234"])
def test_a_loopback_address_and_every_profile_behave_as_before(tmp_path, url):
    settings = Settings(data_dir=tmp_path, ollama_url=url)
    assert type(memory_models.from_settings(settings)) is memory_models.Ollama
    assert memory_models.server(settings, "chat") == {"provider": "ollama", "base_url": url}
    remote = {"provider": "openai_compatible", "base_url": "https://models.example/v1", "allow_remote": True}
    profiled = Settings(data_dir=tmp_path, ollama_url=REMOTE, memory_chat=remote, memory_embedding=remote)
    assert memory_models.server(profiled, "chat")["base_url"] == "https://models.example/v1"  # the profile decides


def test_the_refusal_has_a_polish_sentence():
    assert tr(memory_models.REMOTE_OLLAMA, "pl") == memory_models.MESSAGES[memory_models.REMOTE_OLLAMA]
    assert memory_models.MESSAGES[memory_models.REMOTE_OLLAMA] != memory_models.REMOTE_OLLAMA
