"""`cairnmind setup PROFILE`: where storage and the memory models run, tested before anything is saved (#74).

No engine, no database, no model server: the database test, the user creation, the writer look, the model check and
the engine start are replaced by recording fakes, and any other request fails the test. A fake that is not expected
to be called records it, so a test can say that nothing was.
"""
from __future__ import annotations

import fcntl
import json
import os
import string

import httpx
import pytest
from typer.testing import CliRunner

from conftest import plain_words
from cairnmind import cli, database_credentials, memory_models, setup_profiles, store_writer
from cairnmind import topology as topology_module
from cairnmind.config import Settings, is_own_variable
from cairnmind.database_mode import connect_only
from cairnmind.memory_openai import OpenAICompatible
from cairnmind.ollama import Ollama, OllamaUnavailable
from cairnmind.setup_profiles import (
    MESSAGES, Request, render, render_one, run, SetupRefused, TEST_STORAGE_OK)

REMOTE = {"provider": "openai_compatible", "base_url": "https://models.example/v1", "allow_remote": True}
DB = "https://db.example:2490"
MODELS = "https://models.example/v1"
RELEASE_CHAT = Settings.__dataclass_fields__["chat_model"].default
RELEASE_EMBEDDING = Settings.__dataclass_fields__["embedding_model"].default


class World:
    """What the profile reaches, replaced: every call is logged as (name, detail)."""

    def __init__(self, monkeypatch):
        self.log = []
        self.block = {"available": True, "database_available": True, "version": "26.11.1"}
        self.writer = {"role": "none"}
        self.model_errors = {}
        monkeypatch.setattr(database_credentials, "doctor_block", self.doctor_block)
        monkeypatch.setattr(database_credentials, "create_user", self.create_user)
        monkeypatch.setattr(store_writer, "inspect", self.inspect)
        monkeypatch.setattr(store_writer, "take_over_at", self.take_over_at)
        monkeypatch.setattr(memory_models, "_check", self.check)
        monkeypatch.setattr(Ollama, "model_identity", self.identity)
        monkeypatch.setattr(OpenAICompatible, "model_identity", self.identity)

    def calls(self, name):
        return [detail for what, detail in self.log if what == name]

    def doctor_block(self, candidate, **kwargs):
        self.log.append(("doctor_block", (candidate.arcade_user, candidate.arcade_database, candidate.arcade_password)))
        return dict(self.block)

    def create_user(self, candidate, admin_secret, **kwargs):
        self.log.append(("create_user", (candidate.arcade_user, candidate.arcade_database, admin_secret)))
        return {"created": True, "database_created": True}

    def inspect(self, settings):
        self.log.append(("inspect", settings.arcade_url))
        return dict(self.writer)

    def take_over_at(self, *args, **kwargs):
        self.log.append(("take_over", None))
        raise AssertionError("setup took over writing a store")

    async def check(self, settings, role):
        row = memory_models.profile(settings, role)
        self.log.append(("check", (role, getattr(settings, memory_models.MODEL_FIELDS[role]),
                                   row["base_url"] if row else settings.ollama_url)))
        if role in self.model_errors:
            raise self.model_errors[role]
        return {"structured_output": True}, f"{role}@1"

    async def identity(self, name):
        return "earlier@1"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in [key for key in os.environ if is_own_variable(key)]:
        monkeypatch.delenv(key)

    def refuse(*args, **kwargs):
        raise AssertionError("an unexpected request")
    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def make(tmp_path, **fields):
    settings = Settings.load(tmp_path / "data", create=True)
    for name, value in fields.items():
        setattr(settings, name, value)
    settings.save()
    return Settings.load(tmp_path / "data")


def config_bytes(tmp_path) -> bytes:
    return (tmp_path / "data" / "config.json").read_bytes()


def config(tmp_path) -> dict:
    return json.loads(config_bytes(tmp_path))


def private(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text + "\n")
    path.chmod(0o600)
    return str(path)


def invoke(tmp_path, *arguments):
    environment = {"COLUMNS": "500", "TERMINAL_WIDTH": "500"}
    return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "data"), *arguments], env=environment)


def db_options(tmp_path, password="PRIVATE_DB_MARKER"):
    return ["--url", DB, "--user", "cairnmind", "--database", "cairnmind",
            "--password-file", private(tmp_path, "db-password", password)]


def model_options(consent=True):
    return ["--models-url", MODELS, "--provider", "openai_compatible", "--chat-model", "gpt-x",
            "--embedding-model", "emb-y", "--credential-ref", "env:MODELS_KEY", *(["--allow-remote"] if consent else [])]


def said(result):
    return plain_words(result.output)


def refused(result, *words):
    assert result.exit_code == 2, result.output
    for word in words:
        assert word in said(result), (word, result.output)


# Refusals before anything is tested ------------------------------------------------------------------------------

def test_a_profile_is_refused_while_the_service_runs(tmp_path, world, monkeypatch):
    make(tmp_path)
    before = config_bytes(tmp_path)
    with (tmp_path / "data" / "service.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        locked = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    refused(locked, "The knowledge service of this data directory is running; run `cairnmind stop` first")
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    answering = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path))
    refused(answering, "run `cairnmind stop` first")
    assert config_bytes(tmp_path) == before and world.log == []


def test_a_coding_session_cannot_change_where_storage_runs(tmp_path, world, monkeypatch):
    make(tmp_path)
    before = config_bytes(tmp_path)
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "session-1")
    for extra in ([], ["--yes"]):
        refused(invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), *extra),
                "A coding session cannot change where storage and models run")
    monkeypatch.setattr(topology_module, "probe", lambda settings: topology_module.topology(settings))
    assert invoke(tmp_path, "setup").exit_code == 0  # showing is allowed
    assert config_bytes(tmp_path) == before and world.log == []


@pytest.mark.parametrize("variable,value", [
    ("CAIRNMIND_ARCADE_USER", "someone"), ("DEVBRAIN_ARCADE_DATABASE", "elsewhere"),
    ("CAIRNMIND_ARCADE_PASSWORD", "from-the-shell"), ("CAIRNMIND_OLLAMA_URL", "http://127.0.0.1:11434")])
def test_a_variable_that_would_replace_a_saved_setting_is_named(tmp_path, world, monkeypatch, variable, value):
    make(tmp_path)
    before = config_bytes(tmp_path)
    monkeypatch.setenv(variable, value)
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    refused(result, f"{variable} is set in this shell and would replace what setup saves")
    assert config_bytes(tmp_path) == before and world.log == []


@pytest.mark.parametrize("arguments,sentence", [
    (["local", "--url", DB], "--url does not go with `cairnmind setup local`."),
    (["remote-db", "--backup", "F"], "--backup does not go with `cairnmind setup remote-db`."),
    (["remote-db", "--chat-model", "m"], "`cairnmind setup remote-db` runs the memory models on this computer"),
    (["remote-db", "--url", DB], "needs these options: --url, --user, --database, --password-file."),
    (["remote", "--url", DB, "--user", "u", "--database", "d", "--password-file", "/x"], "--models-url"),
    (["local", "--backup", "F", "--empty"], "Choose one of --backup and --empty."),
    (["--url", DB], "--url goes with a profile"),
    (["elsewhere"], "Unknown profile elsewhere: choose local, remote-db or remote.")])
def test_options_that_do_not_fit_the_profile_are_refused(tmp_path, world, arguments, sentence):
    make(tmp_path)
    before = config_bytes(tmp_path)
    refused(invoke(tmp_path, "setup", *arguments), sentence)
    assert config_bytes(tmp_path) == before and world.log == []


# remote-db ---------------------------------------------------------------------------------------------------------

def test_remote_db_without_yes_is_tested_and_shown_and_changes_nothing(tmp_path, world):
    make(tmp_path)
    before = config_bytes(tmp_path)
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path))
    assert result.exit_code == 1, result.output
    for words in (f"Tested: ArcadeDB at {DB} answers with version 26.11.1, and the user cairnmind signs in",
                  f"Storage: from the engine on this machine to ArcadeDB at {DB}, which this installation only connects to",
                  f"Writer: no installation writes the store at {DB} yet",
                  "Left as it is: the store on this machine, in", "Nothing was changed. To make these changes"):
        assert words in said(result), (words, result.output)
    assert config_bytes(tmp_path) == before and world.calls("create_user") == []


def test_remote_db_saves_the_path_of_the_password_file_and_never_the_password(tmp_path, world):
    before_settings = make(tmp_path)
    options = db_options(tmp_path)
    preview = invoke(tmp_path, "setup", "remote-db", *options, "--json")
    assert preview.exit_code == 1 and "PRIVATE_DB_MARKER" not in preview.output
    before = config(tmp_path)
    result = invoke(tmp_path, "setup", "remote-db", *options, "--yes")
    assert result.exit_code == 0, result.output
    after = config(tmp_path)
    changed = {name for name in after if after[name] != before.get(name)}
    assert changed == {"arcade_connect_url", "arcade_user", "arcade_database", "arcade_password_file"}
    assert after["arcade_connect_url"] == DB and after["arcade_password_file"] == options[7]
    assert after["arcade_ca_file"] == "" and after["arcade_url"] == before["arcade_url"] == before_settings.arcade_url
    assert "PRIVATE_DB_MARKER" not in json.dumps(after) and "PRIVATE_DB_MARKER" not in result.output
    loaded = Settings.load(tmp_path / "data")
    assert connect_only(loaded) and loaded._origin["password"] == "file"
    first = topology_module.topology(loaded)["lines"][0]
    assert first in result.output and "Saved in" in result.output and "`cairnmind init` opens the new store" in result.output
    assert world.calls("doctor_block") == [("cairnmind", "cairnmind", "PRIVATE_DB_MARKER")] * 2


@pytest.mark.parametrize("how,words", [
    ("mode", "which must be a regular file of this user with mode 0600"),
    ("link", "a symbolic link"), ("missing", "which does not exist")])
def test_a_password_file_that_is_not_private_is_refused_before_any_request(tmp_path, world, how, words):
    make(tmp_path)
    before = config_bytes(tmp_path)
    path = private(tmp_path, "db-password", "PRIVATE_DB_MARKER")
    if how == "mode":
        os.chmod(path, 0o644)
    elif how == "link":
        os.symlink(path, tmp_path / "linked")
        path = str(tmp_path / "linked")
    else:
        path = str(tmp_path / "none")
    result = invoke(tmp_path, "setup", "remote-db", "--url", DB, "--user", "cairnmind", "--database", "cairnmind",
                    "--password-file", path, "--yes")
    refused(result, f"The store at {DB} cannot be used:", words, "Nothing was saved.")
    assert config_bytes(tmp_path) == before and world.log == [] and "PRIVATE_DB_MARKER" not in result.output


def test_plain_http_to_another_host_and_root_there_are_refused_before_any_request(tmp_path, world):
    make(tmp_path)
    before = config_bytes(tmp_path)
    password = db_options(tmp_path)[7]
    plain = invoke(tmp_path, "setup", "remote-db", "--url", "http://db.example:2480", "--user", "cairnmind",
                   "--database", "cairnmind", "--password-file", password, "--yes")
    refused(plain, "must be reached with https://")
    root = invoke(tmp_path, "setup", "remote-db", "--url", DB, "--user", "root", "--database", "cairnmind",
                  "--password-file", password, "--yes")
    refused(root, "ArcadeDB user root is refused for db.example")
    embedded = invoke(tmp_path, "setup", "remote-db", "--url", "https://someone:PRIVATE_URL_MARKER@db.example",
                      "--user", "cairnmind", "--database", "cairnmind", "--password-file", password, "--yes")
    refused(embedded, "The address must start with http:// or https://")
    assert "PRIVATE_URL_MARKER" not in embedded.output
    assert config_bytes(tmp_path) == before and world.log == []


def test_a_store_that_fails_its_test_changes_nothing(tmp_path, world):
    make(tmp_path)
    before = config_bytes(tmp_path)
    world.block = {"available": True, "database_available": False, "database_status": 401}
    refused(invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes"),
            "The sign-in was refused", "did not accept the user cairnmind", "Nothing was saved.")
    world.block = {"available": True, "error": "ArcadeDB at https://db.example:2490 runs version 1.0.0, which "
                                               "this release of Cairnmind does not support."}
    refused(invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes"), "runs version 1.0.0")
    world.block = {"available": True, "database_available": False, "database_status": 500}
    refused(invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes"), "The test query failed (status 500).")
    assert config_bytes(tmp_path) == before and world.calls("create_user") == []


def test_the_admin_password_file_creates_the_user_only_with_yes(tmp_path, world):
    make(tmp_path)
    before = config_bytes(tmp_path)
    admin = private(tmp_path, "root-password", "PRIVATE_ADMIN_MARKER")
    weak = private(tmp_path, "weak-password", "PRIVATE_ADMIN_MARKER")
    os.chmod(weak, 0o644)
    refused(invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--admin-password-file", weak),
            "--admin-password-file names")
    assert world.log == []
    world.block = {"available": True, "database_available": False, "database_status": 401}
    preview = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--admin-password-file", admin)
    assert preview.exit_code == 1 and "Not tested yet: the user cairnmind cannot sign in" in said(preview)
    assert world.calls("create_user") == [] and config_bytes(tmp_path) == before
    world.block = {"available": True, "database_available": True, "version": "26.11.1"}
    applied = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--admin-password-file", admin, "--yes")
    assert applied.exit_code == 0, applied.output
    assert world.calls("create_user") == [("cairnmind", "cairnmind", "PRIVATE_ADMIN_MARKER")]
    assert [what for what, _ in world.log if what in ("create_user", "doctor_block")][-2:] == ["create_user", "doctor_block"]
    assert f"Created on ArcadeDB at {DB}: the user cairnmind" in said(applied)
    assert "PRIVATE_ADMIN_MARKER" not in json.dumps(config(tmp_path)) + applied.output


def test_a_store_with_another_writer_makes_this_installation_a_reader_and_nothing_takes_over(tmp_path, world):
    make(tmp_path)
    world.writer = {"role": "reader", "reason": "Installation abc writes it. Run `cairnmind store takeover --yes`."}
    dry = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path))
    assert dry.exit_code == 1
    assert "another installation writes the store" in said(dry) and "Run `cairnmind store takeover --yes`." in said(dry)
    applied = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    assert applied.exit_code == 0 and config(tmp_path)["arcade_connect_url"] == DB
    assert world.calls("take_over") == []


# remote ------------------------------------------------------------------------------------------------------------

def test_remote_tests_both_models_before_the_store_and_saves_nothing_when_one_fails(tmp_path, world):
    make(tmp_path)
    before = config_bytes(tmp_path)
    world.model_errors["embedding"] = ValueError("no vector")
    result = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *model_options(), "--yes")
    refused(result, f"The embedding model emb-y at {MODELS} cannot be used: no vector")
    assert [detail[0] for detail in world.calls("check")] == ["chat", "embedding"]
    assert world.calls("doctor_block") == [] and world.calls("create_user") == []
    assert config_bytes(tmp_path) == before


def test_remote_needs_the_consent_and_https_that_models_set_needs(tmp_path, world):
    make(tmp_path)
    before = config_bytes(tmp_path)
    refused(invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *model_options(consent=False), "--yes"),
            "A model server on another computer receives memory content", "Confirm that with --allow-remote.")
    options = model_options()
    options[1] = "http://models.example"
    refused(invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *options, "--yes"),
            "Plain HTTP is allowed only for localhost; use HTTPS.")
    assert config_bytes(tmp_path) == before and world.log == []


def test_remote_saves_through_configure_role_and_turns_the_agent_on(tmp_path, world, monkeypatch):
    make(tmp_path, knowledge_agent="disabled", ollama_url="http://127.0.0.1:11555")
    configured = []
    original = memory_models.configure_role

    async def spy(settings, role, **kwargs):
        configured.append((role, kwargs))
        return await original(settings, role, **kwargs)
    monkeypatch.setattr(memory_models, "configure_role", spy)
    dry = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *model_options())
    assert dry.exit_code == 1 and configured == []
    for words in ("will receive your questions and the memory text of the classes project (memory_remote_classes)",
                  "The knowledge agent will be turned on", "To rebuild: the vectors, because the embedding model changes"):
        assert words in said(dry), (words, dry.output)
    result = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *model_options(), "--yes")
    assert result.exit_code == 0, result.output
    assert [role for role, _ in configured] == ["chat", "embedding"]
    saved = config(tmp_path)
    for role, field, model in (("chat", "memory_chat", "gpt-x"), ("embedding", "memory_embedding", "emb-y")):
        assert saved[field] == {**REMOTE, "credential_ref": "env:MODELS_KEY"} and saved[f"{role}_model"] == model
    assert saved["knowledge_agent"] == "enabled" and saved["ollama_url"] == "http://127.0.0.1:11555"
    assert saved["arcade_connect_url"] == DB
    assert "another computer (an OpenAI-compatible server at https://models.example/v1)" in said(result)


def test_remote_db_brings_remote_models_back_to_the_release_models_here(tmp_path, world):
    make(tmp_path, knowledge_agent="enabled", memory_chat=dict(REMOTE), memory_embedding=dict(REMOTE),
         chat_model="gpt-x", embedding_model="emb-y")
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    assert result.exit_code == 0, result.output
    saved = config(tmp_path)
    assert saved["memory_chat"] is None and saved["memory_embedding"] is None
    assert saved["chat_model"] == RELEASE_CHAT and saved["embedding_model"] == RELEASE_EMBEDDING
    assert f"Left as it is: the model server at {MODELS}; nothing is sent to it any more." in said(result)
    assert "To rebuild: the vectors" in said(result)


def test_an_ollama_on_this_computer_without_the_model_is_named(tmp_path, world):
    make(tmp_path, knowledge_agent="enabled", memory_chat=dict(REMOTE), chat_model="gpt-x")
    before = config_bytes(tmp_path)
    world.model_errors["chat"] = ValueError("model not found")
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    refused(result, "`cairnmind setup remote-db` runs the chat model on this computer",
            f"`ollama pull {RELEASE_CHAT}`", "model not found", "Nothing was saved.")
    assert config_bytes(tmp_path) == before and world.calls("doctor_block") == []
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    world.model_errors["chat"] = OllamaUnavailable("down")
    refused(invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes"), "down")
    assert config(tmp_path)["knowledge_agent"] == "enabled"


def test_a_remote_ollama_url_is_refused_and_never_asked(tmp_path, world):
    make(tmp_path, knowledge_agent="enabled", ollama_url="http://models.lan:11434")
    before = config_bytes(tmp_path)
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    refused(result, "ollama_url names models.lan, a server on another computer")
    assert config_bytes(tmp_path) == before and world.log == []


def test_models_that_are_off_stay_off(tmp_path, world):
    make(tmp_path, knowledge_agent="disabled")
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    assert result.exit_code == 0, result.output
    assert world.calls("check") == [] and config(tmp_path)["knowledge_agent"] == "disabled"
    assert "Memory models: none in use" in said(result)


def test_local_on_an_installation_that_is_local_has_nothing_to_change(tmp_path, world):
    make(tmp_path, knowledge_agent="enabled")
    before = config_bytes(tmp_path)
    result = invoke(tmp_path, "setup", "local")
    assert result.exit_code == 0, result.output
    assert "already run as `cairnmind setup local` says; nothing to change." in said(result)
    refused(invoke(tmp_path, "setup", "local", "--empty"), "Storage already runs on this machine")
    assert config_bytes(tmp_path) == before and world.log == []


def test_setting_up_the_same_profile_again_changes_nothing(tmp_path, world):
    make(tmp_path)
    options = db_options(tmp_path)
    assert invoke(tmp_path, "setup", "remote-db", *options, "--yes").exit_code == 0
    world.log.clear()
    before = config_bytes(tmp_path)
    again = invoke(tmp_path, "setup", "remote-db", *options, "--yes")
    assert again.exit_code == 0 and "nothing to change" in said(again)
    assert config_bytes(tmp_path) == before and world.log == []


# Put back, sentences -----------------------------------------------------------------------------------------------

def test_a_step_that_fails_puts_the_configuration_back(tmp_path, world, monkeypatch):
    make(tmp_path, knowledge_agent="disabled")
    before = config_bytes(tmp_path)

    def broken(data_dir, fields):
        raise OSError("disk full")
    monkeypatch.setattr(setup_profiles, "save_storage", broken)
    with pytest.raises(setup_profiles.SetupFailed) as failure:
        run(Settings.load(tmp_path / "data"), Request("remote", url=DB, user="cairnmind", database="cairnmind",
                                                      password_file=db_options(tmp_path)[7], models_url=MODELS,
                                                      provider="openai_compatible", chat_model="gpt-x",
                                                      embedding_model="emb-y", allow_remote=True),
            yes=True, say=lambda text: None)
    assert "stopped at saving the storage settings: disk full config.json was put back as it was." in str(failure.value)
    assert config_bytes(tmp_path) == before  # the models and the agent were saved first, and are back too


def test_a_configuration_that_cannot_be_put_back_says_so(tmp_path, world, monkeypatch):
    make(tmp_path)

    def broken(data_dir, fields):
        raise ValueError("write failed")

    def cannot(path, data):
        raise OSError("read-only file system")
    monkeypatch.setattr(setup_profiles, "save_storage", broken)
    monkeypatch.setattr(setup_profiles, "_put_back", cannot)
    with pytest.raises(setup_profiles.SetupFailed, match="could NOT be put back \\(read-only file system\\)"):
        run(Settings.load(tmp_path / "data"), Request("remote-db", url=DB, user="cairnmind", database="cairnmind",
                                                      password_file=db_options(tmp_path)[7]),
            yes=True, say=lambda text: None)


def test_every_setup_sentence_has_polish_with_the_same_placeholders():
    from cairnmind.terminal_i18n import tr
    names = {name for name, value in vars(setup_profiles).items()
             if isinstance(value, str) and name.isupper() and " " in value}
    sentences = {getattr(setup_profiles, name) for name in names}
    assert len(names) >= 70 and sentences == set(MESSAGES) and setup_profiles.VERBATIM not in MESSAGES
    for english in sentences:
        polish = MESSAGES[english]

        def holes(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}
        assert polish != english and holes(polish) == holes(english), english
        assert tr(english, "pl") == polish and tr(english, "en") == english


def test_a_plan_in_polish_leaves_no_english_sentence(tmp_path, world):
    settings = make(tmp_path, knowledge_agent="disabled")
    result = run(settings, Request("remote", url=DB, user="cairnmind", database="cairnmind",
                                   password_file=db_options(tmp_path)[7], models_url=MODELS,
                                   provider="openai_compatible", chat_model="gpt-x", embedding_model="emb-y",
                                   allow_remote=True), yes=False, say=lambda text: None)
    english, polish = "\n".join(render(result)), "\n".join(render(result, "pl"))
    assert "Tested:" in english and "Sprawdzono:" in polish and "Nic nie zostało zmienione." in polish
    for word in ("Tested", "Storage:", "Left as it is", "Nothing was changed", "will receive", "To rebuild", "Writer:"):
        assert word not in polish, word
    assert render_one((TEST_STORAGE_OK, {"url": "u", "version": "v", "user": "x", "database": "d"}), "pl").startswith("Sprawdzono")
    with pytest.raises(SetupRefused) as refusal:
        run(settings, Request("remote-db", url="https://a:b@db.example", user="u", database="d", password_file="/x"),
            yes=False, say=lambda text: None)
    assert refusal.value.template and "Nie można użyć bazy" in render_one(
        (refusal.value.template, refusal.value.values), "pl")


def test_setup_shows_how_to_change_with_the_profiles(tmp_path, world, monkeypatch):
    make(tmp_path)
    monkeypatch.setattr(topology_module, "probe", lambda settings: topology_module.topology(settings))
    result = invoke(tmp_path, "setup")
    assert result.exit_code == 0, result.output
    assert result.output.rstrip("\n").endswith(topology_module.CHANGE_HINT)
    assert "cairnmind setup local" in topology_module.CHANGE_HINT and "cairnmind setup remote" in topology_module.CHANGE_HINT
