"""The service port, the cockpit port and the model server address come from the environment (#267).

An environment value is used by that process only: config.json keeps what it had, as for the database address.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.config import Settings, is_own_variable

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in [key for key in os.environ if is_own_variable(key)]:
        monkeypatch.delenv(key)


def test_a_second_data_directory_takes_its_ports_and_model_server_from_the_environment(monkeypatch, tmp_path):
    assert Settings.load(tmp_path / "second").port == 8765
    monkeypatch.setenv("CAIRNMIND_PORT", "9101")
    monkeypatch.setenv("DEVBRAIN_COCKPIT_PORT", "9102")
    monkeypatch.setenv("CAIRNMIND_OLLAMA_URL", "http://127.0.0.1:9103")
    created = Settings.load(tmp_path / "second", create=True)
    assert (created.port, created.cockpit_port, created.ollama_url) == (9101, 9102, "http://127.0.0.1:9103")
    stored = json.loads((tmp_path / "second" / "config.json").read_text())
    assert not {"port", "cockpit_port", "ollama_url"} & stored.keys()  # used by this process, never saved


def test_without_the_variables_config_json_decides_and_a_variable_beats_it_for_one_process(monkeypatch, tmp_path):
    root = tmp_path / "first"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"port": 9201, "cockpit_port": 9202, "ollama_url": "http://elsewhere"}))
    monkeypatch.setenv("CAIRNMIND_PORT", "")  # an empty variable names nothing
    monkeypatch.setenv("CAIRNMIND_OLLAMA_URL", "")
    loaded = Settings.load(root)
    assert (loaded.port, loaded.cockpit_port, loaded.ollama_url) == (9201, 9202, "http://elsewhere")
    monkeypatch.setenv("CAIRNMIND_PORT", "9301")
    loaded = Settings.load(root)
    assert loaded.port == 9301
    loaded.save()
    assert json.loads((root / "config.json").read_text())["port"] == 9201


@pytest.mark.parametrize("value", ["abc", "0", "70000", "80.5"])
def test_a_port_that_is_not_a_port_is_refused_by_name(monkeypatch, tmp_path, value):
    monkeypatch.setenv("CAIRNMIND_COCKPIT_PORT", value)
    with pytest.raises(ValueError, match="CAIRNMIND_COCKPIT_PORT must be a port number"):
        Settings.load(tmp_path / "data")


def test_a_start_that_cannot_bind_names_the_port_and_the_setting(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "second", create=True)

    def stopped(command, stdout=None, **kwargs):
        stdout.write(b"ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8765): "
                     b"address already in use\nINFO: Application shutdown complete.\n")
        stdout.flush()
        return SimpleNamespace(poll=lambda: 1)

    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.subprocess, "Popen", stopped)
    with pytest.raises(cli.ServiceDidNotStart) as raised:
        cli.ensure_service(settings)
    said = str(raised.value)
    assert "port 8765 is already in use" in said and "another data directory" in said
    assert f"{settings.data_dir}/config.json" in said and "CAIRNMIND_PORT" in said


def test_the_spelling_that_is_set_is_the_one_read_and_named(monkeypatch, tmp_path):
    monkeypatch.setenv("CAIRNMIND_PORT", "")
    monkeypatch.setenv("DEVBRAIN_PORT", "9101")
    assert Settings.load(tmp_path / "data").port == 9101  # an empty current spelling does not hide the earlier one
    monkeypatch.setenv("DEVBRAIN_PORT", "abc")
    with pytest.raises(ValueError, match="DEVBRAIN_PORT must be a port number"):
        Settings.load(tmp_path / "data")


def test_doctor_names_a_port_and_a_model_server_taken_from_the_environment(monkeypatch, tmp_path):
    from cairnmind import integrations
    Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(integrations, "doctor", lambda settings: {})
    monkeypatch.setenv("CAIRNMIND_PORT", "9101")
    monkeypatch.setenv("DEVBRAIN_OLLAMA_URL", "http://127.0.0.1:9103")
    report = json.loads(runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "doctor"]).output)
    assert report["service_url"] == "http://127.0.0.1:9101"
    notes = report["environment_notes"]
    assert any("CAIRNMIND_PORT" in note and "9101" in note and "8765" in note and "config.json" in note for note in notes), notes
    assert any("DEVBRAIN_OLLAMA_URL" in note and "9103" in note for note in notes), notes
    assert not any("COCKPIT" in note for note in notes)


def test_doctor_names_the_java_version_it_read_in_the_storage_line(monkeypatch, tmp_path):
    """#368: JDK 22 runs the engine: the topology lines say Java 22, not Java 21; no major read says the minimum."""
    from cairnmind import integrations
    Settings.load(tmp_path / "data", create=True)
    for java, said in (({"major": 22, "compatible": True}, "(native engine, Java 22)"),
                       ({"major": 21, "compatible": True}, "(native engine, Java 21)"),
                       ({"major": 17, "compatible": False}, "(native engine, Java 21 or newer)"),
                       ({"major": None}, "(native engine, Java 21 or newer)")):
        monkeypatch.setattr(integrations, "doctor", lambda settings, java=java: {"java": java})
        report = json.loads(runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "doctor"]).output)
        assert said in report["topology"]["lines"][0] and report["java"]["major"] == java["major"]


async def test_back_to_ollama_says_when_the_address_checked_came_from_the_environment(monkeypatch, tmp_path):
    from cairnmind import memory_models

    async def check(settings, role):
        return {"structured_output": True}, "c@1"
    monkeypatch.setattr(memory_models, "_check", check)
    monkeypatch.setenv("CAIRNMIND_OLLAMA_URL", "http://127.0.0.1:9103")
    settings = Settings.load(tmp_path / "data", create=True)
    report = await memory_models.configure_role(settings, "chat", provider="ollama", model="other:4b")
    assert any("http://127.0.0.1:9103" in step and "environment" in step for step in report["next"])
    monkeypatch.delenv("CAIRNMIND_OLLAMA_URL")
    plain = await memory_models.configure_role(Settings.load(tmp_path / "data"), "chat", provider="ollama", model="other:4b")
    assert not any("environment" in step for step in plain["next"])


def test_a_service_of_another_data_directory_is_named_and_the_token_is_not_printed(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "second", create=True)

    class Foreign:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kwargs):
            return httpx.Response(401, json={"detail": "Invalid local service token"}, request=httpx.Request(method, url))

    monkeypatch.setattr(cli.httpx, "Client", Foreign)
    result = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "status"])
    assert result.exit_code == 2 and "most likely belongs to another data directory" in result.output
    assert "port" in result.output and settings.token not in result.output
    # a command that started or matched the service by instance id does not claim it is another directory's
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    again = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "stop"])
    assert "another data directory" not in again.output and "Invalid local service token" in again.output
