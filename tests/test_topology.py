"""Where storage and the memory models run, as `topology` reads it (#74).

Pure reads of settings: no engine, no model server, no network. The requests doctor makes are replaced by hand-made
blocks of the same shape.
"""
from __future__ import annotations

import json
import os
import string
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.cli_cmds import topology as topology_module
from cairnmind.cli_cmds.first_start import KNOWLEDGE_INVALID
from cairnmind.cli_cmds.topology import (
    CHANGE_HINT,
    EVALUATED_NOT,
    EVALUATED_NOT_CHECKED,
    EVALUATED_PASSED,
    EVALUATED_RELEASE,
    EVALUATED_UNKNOWN,
    EVALUATED_UNREACHABLE,
    FROM_VARIABLE,
    ITEM_DATABASE_HERE,
    ITEM_ENGINE_DOCKER,
    ITEM_MODELS_EMBEDDING,
    ITEM_NO_SERVICE,
    ITEM_SERVICE_VARIABLE,
    MODELS_DISABLED,
    MODELS_INVALID,
    OLLAMA_FROM_VARIABLE,
    STORAGE_CONNECT_HERE,
    STORAGE_CONNECT_REMOTE,
    STORAGE_DOCKER,
    STORAGE_PROBLEM,
    lines,
    status_words,
    topology,
)
from cairnmind.config import Settings, is_own_variable
from cairnmind.memory import integrations, memory_models
from cairnmind.pal.terminal_safe import safe_text
from cairnmind.storage import store_writer
from cairnmind.storage.database_credentials import problem as database_problem
from cairnmind.storage.database_mode import MODE_INVALID

REMOTE = {"provider": "openai_compatible", "base_url": "https://models.example/v1", "allow_remote": True}
WRITER = {"role": "writer"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in [key for key in os.environ if is_own_variable(key)]:
        monkeypatch.delenv(key)


@pytest.fixture
def offline(monkeypatch):
    """The topology reaches neither a server nor a process: either one fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("the topology reached the machine")
    for owner, name in ((httpx, "Client"), (httpx, "AsyncClient"), (subprocess, "run"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, refuse)


def make(tmp_path, **fields):
    """Settings as a later command loads them: the fields saved to config.json, then read back."""
    settings = Settings.load(tmp_path / "data", create=True)
    for name, value in fields.items():
        setattr(settings, name, value)
    settings.save()
    return Settings.load(tmp_path / "data")


def password_file(tmp_path):
    path = tmp_path / "db-password"
    path.write_text("a-database-password\n")
    path.chmod(0o600)
    return str(path)


def remote_store(tmp_path, **fields):
    return make(tmp_path, arcade_connect_url="https://db.example:2490", arcade_user="cairnmind",
                arcade_password_file=password_file(tmp_path), **fields)


def test_everything_local_reads_as_three_lines(tmp_path, offline):
    settings = make(tmp_path)
    models = {"evaluated": False, "chat": {"identity": "c@1"}, "embedding": {"identity": "e@1"}}
    place = topology(settings, models=models, writer=WRITER)
    assert place["lines"] == [
        "Storage: ArcadeDB on this machine at http://127.0.0.1:2480, started and stopped by Cairnmind "
        "(native engine, Java 21 or newer). This installation is the one that writes to the store.",
        "Memory models: chat qwen3.5:9b and embedding qwen3-embedding:0.6b, both on this machine "
        "(Ollama at http://127.0.0.1:11434). These are the release's models, evaluated for this release.",
        "On this machine: the knowledge service at http://127.0.0.1:8765; the ArcadeDB engine (native, Java 21 or newer); "
        "Ollama at http://127.0.0.1:11434 for chat and embedding."]
    assert list(place) == ["lines", "storage", "models", "this_machine"]
    assert place["storage"]["source"] == place["models"]["chat"]["source"] == "config.json"
    assert status_words(place) == (("local", False), ("local", False))


def test_the_java_version_found_is_named_not_the_minimum(tmp_path, offline):
    """#368: JDK 22 runs the engine, so the lines say Java 22, in both languages; 21 stays Java 21."""
    from cairnmind.cli_cmds.topology import lines
    settings = make(tmp_path)
    for major in (22, 21):
        place = topology(settings, writer=WRITER, java_major=major)
        assert f"(native engine, Java {major})" in place["lines"][0]
        assert f"(native, Java {major})" in place["lines"][2] and "or newer" not in place["lines"][0]
        assert f"Java {major})" in lines(place, "pl")[0] and f"Java {major})" in lines(place, "pl")[2]
    assert "Java 21 lub nowsza" in lines(topology(settings, writer=WRITER), "pl")[0]
    assert topology(make(tmp_path, engine_mode="docker"), writer=WRITER, java_major=22)["storage"].get("java_major") is None


def test_the_engine_in_docker_is_named(tmp_path, offline):
    place = topology(make(tmp_path, engine_mode="docker"), writer=WRITER)
    assert STORAGE_DOCKER.format(url="http://127.0.0.1:2480") in place["lines"][0]
    assert ITEM_ENGINE_DOCKER in place["lines"][2] and place["storage"]["engine"] == "docker"
    assert status_words(place)[0] == ("local (Docker)", False)


def test_an_invalid_engine_mode_is_named_and_not_read_as_native(tmp_path, offline):
    place = topology(make(tmp_path, engine_mode="dokker"))
    assert MODE_INVALID.format(value="'dokker'") in place["lines"][0]
    assert place["storage"]["engine"] == "unknown" and "native engine" not in place["lines"][0]
    assert "cannot be used" in place["lines"][2]
    assert status_words(place)[0] == ("error", True)


def test_a_remote_store_and_a_reader_role(tmp_path, offline):
    reason = "installation 1a2b3c4d on host-b writes it; run the takeover to change that"
    place = topology(remote_store(tmp_path, knowledge_agent="disabled"), writer={"role": "reader", "reason": reason})
    assert STORAGE_CONNECT_REMOTE.format(url="https://db.example:2490", host="db.example") in place["lines"][0]
    assert place["lines"][0].endswith(" " + reason) and "problem" not in place["storage"]
    assert place["this_machine"] == [{"kind": "no_service"}]
    assert place["lines"][2] == "On this machine: " + ITEM_NO_SERVICE + "."
    assert place["storage"]["writer"] == {"role": "reader", "reason": reason}
    assert status_words(place)[0] == ("db.example", False)


def test_a_connect_only_database_on_this_machine_is_not_called_managed(monkeypatch, tmp_path, offline):
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", "a-database-password")
    place = topology(make(tmp_path, arcade_connect_url="http://127.0.0.1:2999"), writer=WRITER)
    assert STORAGE_CONNECT_HERE.format(url="http://127.0.0.1:2999") in place["lines"][0]
    assert ITEM_DATABASE_HERE.format(url="http://127.0.0.1:2999") in place["lines"][2]
    assert not any(item["kind"] == "engine" for item in place["this_machine"])
    assert status_words(place)[0] == ("local (connect-only)", False)


def test_a_store_that_cannot_be_reached_says_why(tmp_path, offline):
    settings = make(tmp_path, arcade_url="https://db.example", arcade_user="root")
    why = database_problem(settings)
    assert why
    place = topology(settings)
    assert STORAGE_PROBLEM.format(problem=safe_text(why, limit=400)) in place["lines"][0]  # long ones are cut
    assert place["storage"]["problem"] == why
    assert status_words(place)[0] == ("db.example", True)


def test_values_from_the_environment_name_their_variable(monkeypatch, tmp_path, offline):
    root = tmp_path / "data"
    make(tmp_path)
    plain = topology(Settings.load(root))
    assert plain["storage"]["source"] == plain["models"]["chat"]["source"] == "config.json"
    assert not any("variable" in block for block in (plain["storage"], plain["models"]["chat"]))
    assert not any("variable" in item for item in plain["this_machine"])
    assert "for this process only" not in " ".join(plain["lines"])

    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", "http://127.0.0.1:2999")
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", "a-database-password")
    monkeypatch.setenv("DEVBRAIN_OLLAMA_URL", "http://127.0.0.1:9103")
    monkeypatch.setenv("CAIRNMIND_PORT", "9101")
    place = topology(Settings.load(root))
    assert place["storage"]["source"] == "environment" and place["storage"]["variable"] == "CAIRNMIND_ARCADE_CONNECT_URL"
    assert place["models"]["chat"]["source"] == "environment"
    assert place["models"]["chat"]["variable"] == place["models"]["embedding"]["variable"] == "DEVBRAIN_OLLAMA_URL"
    assert place["this_machine"][0] == {"kind": "service", "url": "http://127.0.0.1:9101", "variable": "CAIRNMIND_PORT"}
    joined = " ".join(place["lines"])
    assert FROM_VARIABLE.format(variable="CAIRNMIND_ARCADE_CONNECT_URL") in joined
    assert OLLAMA_FROM_VARIABLE.format(variable="DEVBRAIN_OLLAMA_URL") in joined
    assert ITEM_SERVICE_VARIABLE.format(url="http://127.0.0.1:9101", variable="CAIRNMIND_PORT") in joined


def test_models_disabled_list_no_model_server(tmp_path, offline):
    place = topology(make(tmp_path, knowledge_agent="disabled"), writer=WRITER)
    assert place["lines"][1] == MODELS_DISABLED
    assert place["models"] == {"knowledge_agent": "disabled", "evaluated": None}
    assert not any(item["kind"] == "model_server" for item in place["this_machine"])
    assert "Ollama" not in place["lines"][2]
    assert status_words(place)[1] == ("off", False)


def test_remote_models_say_which_classes_leave_this_computer(tmp_path, offline):
    settings = make(tmp_path, memory_chat=REMOTE, memory_embedding=REMOTE)
    place = topology(settings)
    url = place["models"]["chat"]["url"]
    assert url.startswith("https://models.example")
    assert ("both on models.example, another computer (an OpenAI-compatible server at " + url + "), which receives "
            "questions and memory text of the classes project.") in place["lines"][1]
    assert place["models"]["chat"]["sends"] == ["project"] and not place["models"]["chat"]["on_this_machine"]
    assert not any(item["kind"] == "model_server" for item in place["this_machine"])
    assert status_words(place)[1] == ("remote", False)

    nothing = topology(make(tmp_path, memory_chat=REMOTE, memory_embedding=REMOTE, memory_remote_classes=[]))
    assert "which receives questions and no memory text" in nothing["lines"][1]
    assert nothing["models"]["chat"]["sends"] == []


def test_split_roles_are_shown_each_on_its_server(tmp_path, offline):
    place = topology(make(tmp_path, memory_chat=REMOTE))
    assert place["lines"][1].startswith("Memory models: chat qwen3.5:9b on models.example, another computer")
    assert "; embedding qwen3-embedding:0.6b on this machine (Ollama at http://127.0.0.1:11434)." in place["lines"][1]
    assert ITEM_MODELS_EMBEDDING.format(provider="Ollama", url="http://127.0.0.1:11434") in place["lines"][2]
    assert status_words(place)[1] == ("mixed", False)


@pytest.mark.parametrize("fields, models, word, sentence", [
    ({}, None, "release", EVALUATED_RELEASE),
    ({"chat_model": "other:4b"}, None, "not checked", EVALUATED_NOT_CHECKED),
    ({"chat_model": "other:4b"}, {"evaluated": True, "chat": {"identity": "a@1"}, "embedding": {"identity": "b@1"}},
     "passed", EVALUATED_PASSED),
    ({"chat_model": "other:4b"}, {"evaluated": False, "chat": {"identity": "a@1"}, "embedding": {"identity": "b@1"}},
     "not evaluated", EVALUATED_NOT),
    ({"chat_model": "other:4b"}, {"evaluated": False, "chat": {"identity": "a@1"}, "embedding": {"error": "Timeout"}},
     "unknown", EVALUATED_UNKNOWN),
    ({"chat_model": "other:4b"}, {"evaluated": False, "chat": {"reachable": False, "error": "ConnectError"},
                                  "embedding": {"identity": "b@1"}}, "unreachable", EVALUATED_UNREACHABLE)])
def test_evaluation_states(tmp_path, offline, fields, models, word, sentence):
    place = topology(make(tmp_path, **fields), models=models)
    assert place["models"]["evaluated"] == word
    assert place["lines"][1].endswith(" " + sentence)


def test_an_invalid_profile_or_knowledge_agent_is_a_named_problem(tmp_path, offline):
    place = topology(make(tmp_path, memory_chat={"provider": "x"}))
    message = "Choose a memory model provider: ollama or openai_compatible."
    assert place["models"]["problem"] == "chat: " + message and place["models"]["chat"]["problem"].endswith(message)
    assert place["lines"][1] == MODELS_INVALID.format(problem="chat: " + message)
    assert place["models"]["evaluated"] is None and "Ollama at" not in place["lines"][2]
    assert status_words(place)[1] == ("error", True)

    other = topology(make(tmp_path, memory_chat=None, knowledge_agent="maybe"))
    said = KNOWLEDGE_INVALID.format(value="'maybe'")
    assert other["models"]["knowledge_agent"] == "invalid" and other["lines"][1] == MODELS_INVALID.format(problem=said)
    assert "chat" not in other["models"] and status_words(other)[1] == ("error", True)


def test_an_ollama_url_that_is_not_an_address_is_a_named_problem(tmp_path, offline):
    place = topology(make(tmp_path, ollama_url="http://[::1"))
    assert place["models"]["problem"] == "chat: ollama_url is not a valid address."
    assert status_words(place)[1] == ("error", True)


def test_an_ollama_url_on_another_computer_is_the_refusal_not_a_server_that_receives_memory(tmp_path, offline):
    # #301 refuses such an address before any request; the topology (and the doctor summary) must not say it
    # receives questions and memory text (#287 merge).
    place = topology(make(tmp_path, ollama_url="http://10.0.0.5:11434"))
    assert place["models"]["problem"].startswith("chat: ") and "ollama_url" in place["models"]["problem"]
    assert place["lines"][1].startswith("Memory models: they cannot be used: chat: ollama_url names 10.0.0.5")
    assert "(Ollama at http://10.0.0.5" not in place["lines"][1] and "10.0.0.5" not in place["lines"][2]
    assert status_words(place)[1] == ("error", True)


def test_a_writer_row_that_the_journal_head_contradicts_is_not_called_the_writer(tmp_path, offline):
    problem = "the journal head names another installation; run cairnmind store takeover --yes"
    place = topology(make(tmp_path), writer={"role": "writer", "head_writer": "other", "problem": problem})
    assert place["lines"][0].endswith(" " + problem) and "writes to the store" not in place["lines"][0]
    assert place["storage"]["writer"]["problem"] == problem
    assert status_words(place)[0] == ("local", True)


def test_setup_names_an_ollama_url_that_is_not_an_address(tmp_path, monkeypatch):
    make(tmp_path, ollama_url="http://[::1")
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "writer"})
    result = invoke(tmp_path, "setup")
    assert result.exit_code == 0, result.output
    assert "ollama_url is not a valid address." in result.output

def test_the_topology_reads_only(tmp_path, offline):
    settings = remote_store(tmp_path, memory_chat=REMOTE)
    config = (tmp_path / "data" / "config.json").read_bytes()
    listing = sorted(os.listdir(tmp_path)) + sorted(os.listdir(tmp_path / "data"))
    topology(settings, models={}, writer={"role": "none", "reason": "nothing yet"})
    lines(topology(settings), "pl")
    assert (tmp_path / "data" / "config.json").read_bytes() == config
    assert sorted(os.listdir(tmp_path)) + sorted(os.listdir(tmp_path / "data")) == listing


def test_no_secret_reaches_the_topology(monkeypatch, tmp_path, offline):
    monkeypatch.setenv("CAIRNMIND_ARCADE_PASSWORD", "PRIVATE_DB_MARKER")
    monkeypatch.setenv("MODEL_KEY", "PRIVATE_KEY_MARKER")
    settings = make(tmp_path, ollama_url="http://user:PRIVATE_URL_MARKER@127.0.0.1:11434",
                    memory_chat=dict(REMOTE, credential_ref="env:MODEL_KEY"))
    place = topology(settings, writer=WRITER)
    for text in (json.dumps(place), *lines(place, "pl"), *place["lines"]):
        assert not any(marker in text for marker in ("PRIVATE_DB_MARKER", "PRIVATE_URL_MARKER", "PRIVATE_KEY_MARKER"))
    assert place["models"]["embedding"]["url"] == "http://127.0.0.1:11434" and "credential_ref" not in json.dumps(place)


def test_values_are_shown_literally(tmp_path, offline):
    place = topology(make(tmp_path, chat_model="m\x1b[31m‮"), writer=WRITER)
    for line in place["lines"]:
        assert "\x1b" not in line and "‮" not in line
    assert "m\\x1b[31m\\u202e" in place["lines"][1]


def test_every_topology_sentence_has_polish_with_the_same_placeholders():
    from cairnmind.terminal.terminal_i18n import tr
    names = {name for name, value in vars(topology_module).items() if isinstance(value, str) and (
        (name.isupper() and " " in value) or name.startswith(("WORD_", "STATUS_")))}
    sentences = {getattr(topology_module, name) for name in names}
    assert len(names) >= 40 and sentences == set(topology_module.MESSAGES)
    for english in sentences:
        polish = topology_module.MESSAGES[english]

        def holes(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}
        assert polish != english and holes(polish) == holes(english), english
        assert tr(english, "pl") == polish and tr(english, "en") == english


def invoke(tmp_path, *arguments):
    return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "data"), *arguments])


@pytest.fixture
def requests_replaced(monkeypatch):
    """What `setup` and `doctor` ask of model servers and of the store, as hand-made answers."""
    found = {"evaluated": True, "chat": {"identity": "custom@1"}, "embedding": {"identity": "emb@1"}}
    monkeypatch.setattr(memory_models, "doctor_models", lambda settings, http: found)
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "writer"})
    return found


def test_setup_shows_three_lines_and_how_to_change_them(tmp_path, requests_replaced):
    make(tmp_path, chat_model="custom:1")
    result = invoke(tmp_path, "setup")
    assert result.exit_code == 0, result.output
    expected = topology(Settings.load(tmp_path / "data"), models=requests_replaced, writer=WRITER)["lines"]
    assert result.output == "\n".join([*expected, "", CHANGE_HINT]) + "\n"
    assert expected[1].endswith(" " + EVALUATED_PASSED) and expected[0].endswith("This installation is the one that writes to the store.")
    assert "cairnmind engine start --docker" in CHANGE_HINT and "cairnmind models set" in CHANGE_HINT


def test_setup_json_and_changes_nothing(tmp_path, requests_replaced):
    make(tmp_path)
    config = (tmp_path / "data" / "config.json").read_bytes()
    first = invoke(tmp_path, "setup", "--json")
    second = invoke(tmp_path, "setup")
    assert first.exit_code == second.exit_code == 0, first.output
    place = json.loads(first.output)
    assert list(place) == ["lines", "storage", "models", "this_machine"] and place["storage"]["writer"] == WRITER
    assert place["models"]["evaluated"] == "release" and "\n".join(place["lines"]) in second.output
    assert (tmp_path / "data" / "config.json").read_bytes() == config


def test_doctor_starts_with_the_topology_of_what_it_found(monkeypatch, tmp_path):
    make(tmp_path, chat_model="custom:1")
    found = {"evaluated": True, "chat": {"identity": "custom@1"}, "embedding": {"identity": "emb@1"}}
    monkeypatch.setattr(integrations, "doctor", lambda settings: {"memory_models": found})
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "reader", "reason": "Another one writes."})

    def unreadable(settings):
        raise RuntimeError("no store here")
    monkeypatch.setattr(cli, "store_for_read", unreadable)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    result = invoke(tmp_path, "doctor")
    assert result.exit_code == 0, result.output  # a designed reader misses nothing (#373; #287 counted it as missing)
    report = json.loads(result.output)
    assert list(report)[0] == "topology" and report["store_writer"]["role"] == "reader"
    shown = report["topology"]["lines"]
    assert shown[0].endswith("Another one writes.") and shown[1].endswith(" " + EVALUATED_PASSED)
    assert shown[2].startswith("On this machine: " + ITEM_NO_SERVICE + "; the ArcadeDB engine (native, Java 21 or newer)")


def test_lines_in_polish(tmp_path, offline):
    reason = "REASON-OF-THE-STORE"
    place = topology(remote_store(tmp_path, memory_chat=REMOTE, memory_embedding=REMOTE),
                     writer={"role": "reader", "reason": reason})
    polish = lines(place, "pl")
    assert [line.split(":")[0] for line in polish] == ["Baza", "Modele pamięci", "Na tym komputerze"]
    for fragment in ("Storage", "Memory models", "On this machine", "only connects", "another computer", "receives",
                     "knowledge service", "Evaluated", "evaluated"):
        assert fragment not in " ".join(polish), fragment
    assert reason in polish[0] and "innym komputerze" in polish[1]
    assert polish[1].endswith("Tutaj nie sprawdzono, czy zostały ocenione; sprawdza to cairnmind doctor.")
    assert polish[2] == "Na tym komputerze: brak usługi wiedzy, bo ta instalacja tylko czyta bazę."
