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
import types
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from conftest import plain_words
from cairnmind import (backup_reader, cli, database_credentials, engine, engine_docker, memory_models, person_proof,
                       setup_profiles, store_writer)
from cairnmind import topology as topology_module
from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings, derived_database, is_own_variable
from cairnmind.database_mode import connect_only
from cairnmind.restore_claims import BackupClaims, restore_notice
from cairnmind.store import Store
from cairnmind.store_readonly import StoreNotReady
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

    def __init__(self, monkeypatch, config_path):
        self.log = []
        self.config_path = config_path
        self.block = {"available": True, "database_available": True, "version": "26.11.1"}
        self.writer = {"role": "none"}
        self.model_errors = {}
        self.second_check_errors = {}  # role -> raised from its second check: the one configure_role makes
        self.checked = {}
        self.passwords = []  # what each engine start was given to sign in with
        self.backup_error = self.check_error = self.restore_error = self.start_error = None
        self.proof, self.claims = "key", []  # what restore_proof answers; the claims each restore was given (#214)
        monkeypatch.setattr(person_proof, "restore_proof", lambda settings: self.proof)
        monkeypatch.setattr(Store, "open_readonly", classmethod(lambda cls, settings: self.open_readonly(settings)))
        monkeypatch.setattr(Store, "__init__", lambda store, data_dir, **kwargs: self.log.append(("Store", self.mode())))
        monkeypatch.setattr(Store, "restore", lambda store, path, claims=None: self.restore(path, claims))
        monkeypatch.setattr(backup_reader, "opened_backup", self.opened_backup)
        monkeypatch.setattr(engine, "start_command", self.start_command)
        monkeypatch.setattr(engine_docker, "choose_engine_mode", self.choose_engine_mode)
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
        self.checked[role] = self.checked.get(role, 0) + 1
        if role in self.model_errors:
            raise self.model_errors[role]
        if self.checked[role] > 1 and role in self.second_check_errors:
            raise self.second_check_errors[role]
        return {"structured_output": True}, f"{role}@1"

    def mode(self):
        """Where config.json points at this moment: the store a call would reach."""
        return "connect" if json.loads(self.config_path.read_text()).get("arcade_connect_url") else "managed"

    def open_readonly(self, settings):
        self.log.append(("open_readonly", settings.arcade_url))
        world = self

        class Reading:
            client = types.SimpleNamespace(close=lambda: None)

            def backup(self, target):
                world.log.append(("backup", (str(target), world.mode())))
                if world.backup_error:
                    raise world.backup_error
                Path(target).write_bytes(b"BACKUP")
                return Path(target)
        return Reading()

    @contextmanager
    def opened_backup(self, path, load_whole, types):
        self.log.append(("opened_backup", (str(path), self.mode())))
        if self.check_error:
            raise self.check_error
        yield None

    def restore(self, path, claims):
        self.log.append(("restore", (str(path), self.mode())))
        self.claims.append(claims)
        if self.restore_error:
            raise self.restore_error

    def choose_engine_mode(self, settings):
        self.log.append(("choose_engine_mode", self.mode()))

    def start_command(self, settings, **kwargs):
        self.passwords.append(settings.arcade_password)
        self.log.append(("start_command", (self.mode(), bool(settings.arcade_password), connect_only(settings))))
        if self.start_error:
            raise self.start_error
        return {}

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
def world(monkeypatch, tmp_path):
    return World(monkeypatch, tmp_path / "data" / "config.json")


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


def test_a_user_created_before_a_failed_test_is_named_in_the_refusal(tmp_path, world):
    make(tmp_path)
    before = config_bytes(tmp_path)
    admin = private(tmp_path, "root-password", "PRIVATE_ADMIN_MARKER")
    world.block = {"available": True, "database_available": False, "database_status": 401}
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--admin-password-file", admin, "--yes")
    refused(result, "The sign-in was refused", "Nothing was saved.",
            f"Created on ArcadeDB at {DB}: the user cairnmind, with rights on the database cairnmind only.")
    assert config_bytes(tmp_path) == before and "PRIVATE_ADMIN_MARKER" not in result.output


def test_the_plan_of_the_walk_through_does_not_call_one_store_changed_and_left_alone(tmp_path, world):
    """A connect-only installation with no password yet, set up for the same address (docs/DEPLOYMENT.md, step 2)."""
    make(tmp_path, arcade_connect_url=DB)
    admin = private(tmp_path, "root-password", "PRIVATE_ADMIN_MARKER")
    world.block = {"available": True, "database_available": False, "database_status": 401}
    dry = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--admin-password-file", admin)
    assert dry.exit_code == 1 and "Storage: from ArcadeDB at" in said(dry)
    assert "(database " in said(dry) and "Left as it is: the store at" not in said(dry), dry.output


def test_the_same_store_with_another_user_is_not_left_behind(tmp_path, world):
    make(tmp_path, arcade_connect_url=DB, arcade_user="other", arcade_database="cairnmind",
         arcade_password_file=private(tmp_path, "old-password", "OLD"))
    dry = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path))
    assert dry.exit_code == 1 and "Left as it is: the store at" not in said(dry), dry.output
    moved = make(tmp_path, arcade_database="elsewhere")
    again = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path))
    assert f"Left as it is: the store at {DB} (database elsewhere)" in said(again), again.output
    assert moved.arcade_database == "elsewhere"


def test_moving_out_of_a_store_whose_database_has_another_name_names_it(tmp_path, world):
    make(tmp_path, arcade_database="earlier_name")
    dry = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path))
    assert "The store there holds the database earlier_name, not" in said(dry), dry.output


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


def test_remote_does_not_advise_an_option_it_cannot_set_when_the_test_call_is_cut_at_the_length_limit(tmp_path, world):
    make(tmp_path)
    world.model_errors["chat"] = ValueError(memory_models.LENGTH_CUT_EFFORT)
    result = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *model_options(), "--yes")
    assert result.exit_code != 0 and "length limit" in said(result) and "--reasoning-effort" not in said(result), result.output


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
    world.model_errors["chat"] = OllamaUnavailable("down")
    refused(invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes"), "down")
    assert config(tmp_path)["knowledge_agent"] == "enabled"


def test_a_remote_ollama_url_is_refused_and_never_asked(tmp_path, world):
    make(tmp_path, knowledge_agent="enabled", ollama_url="http://models.lan:11434")
    before = config_bytes(tmp_path)
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    refused(result, "ollama_url names models.lan, a server on another computer")
    assert config_bytes(tmp_path) == before and world.log == []


def test_a_remote_ollama_url_with_a_role_on_another_computer_names_the_url_not_a_pull(tmp_path, world):
    make(tmp_path, knowledge_agent="enabled", memory_chat=dict(REMOTE), chat_model="gpt-x",
         ollama_url="http://models.lan:11434")
    before = config_bytes(tmp_path)
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    refused(result, "ollama_url names models.lan, a server on another computer")
    assert "ollama pull" not in said(result) and config_bytes(tmp_path) == before and world.log == []


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


# local, from a store this installation only connects to --------------------------------------------------------------

def connect_only_installation(tmp_path, **fields):
    """An installation that was connect-only from its start: no password of its own in config.json."""
    make(tmp_path, arcade_connect_url=DB, arcade_user="cairnmind", arcade_database="cairnmind",
         arcade_password_file=private(tmp_path, "db-password", "PRIVATE_DB_MARKER"), **fields)
    stored = config(tmp_path)
    stored.pop("arcade_password", None)
    (tmp_path / "data" / "config.json").write_text(json.dumps(stored, indent=2))
    return stored


def here_options(chat=RELEASE_CHAT, provider="ollama"):
    return ["--models-url", "http://127.0.0.1:11434", "--provider", provider, "--chat-model", chat,
            "--embedding-model", RELEASE_EMBEDDING]


def test_remote_with_the_models_and_place_the_installation_already_uses_changes_nothing(tmp_path, world):
    connect_only_installation(tmp_path, knowledge_agent="enabled")
    before = config_bytes(tmp_path)
    result = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options(), "--yes")
    assert result.exit_code == 0 and "nothing to change" in said(result), result.output
    assert "model: from" not in said(result)
    assert config_bytes(tmp_path) == before and world.log == []


def test_remote_with_one_role_or_the_provider_different_is_still_a_change(tmp_path, world):
    connect_only_installation(tmp_path, knowledge_agent="enabled")
    dry = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options(chat="other:4b"))
    assert "The chat model: from" in said(dry) and "The embedding model: from" not in said(dry), dry.output
    other = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options(provider="openai_compatible"))
    assert "The chat model: from" in said(other) and "The embedding model: from" in said(other), other.output


def test_the_release_models_are_not_told_they_are_unevaluated_and_the_topology_agrees(tmp_path, world):
    make(tmp_path, knowledge_agent="disabled")
    result = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options(), "--yes")
    assert result.exit_code == 0, result.output
    assert "labelled unevaluated" not in said(result)
    assert "evaluated for this release" in said(result)
    other = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options(chat="other:4b"), "--yes")
    assert "labelled unevaluated" in said(other) and "evaluated for this release" not in said(other), other.output


def test_with_the_agent_off_models_that_change_nothing_are_tested_but_not_planned_as_a_change(tmp_path, world):
    connect_only_installation(tmp_path, knowledge_agent="disabled")
    dry = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options())
    assert "model: from" not in said(dry) and "knowledge agent" in said(dry), dry.output
    assert len(world.calls("check")) == 2  # both are still tested before the agent is turned on
    done = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options(), "--yes")
    assert done.exit_code == 0 and config(tmp_path)["knowledge_agent"] == "enabled", done.output


def test_the_evaluation_step_is_left_out_when_the_second_saved_role_completes_the_release_pair(tmp_path, world):
    connect_only_installation(tmp_path, knowledge_agent="enabled", memory_embedding=dict(REMOTE), chat_model="other:4b")
    result = invoke(tmp_path, "setup", "remote", *db_options(tmp_path), *here_options(), "--yes")
    assert result.exit_code == 0, result.output
    assert "labelled unevaluated" not in said(result), result.output


def test_going_back_without_a_choice_is_refused_and_names_both(tmp_path, world):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)
    result = invoke(tmp_path, "setup", "local", "--yes")
    refused(result, f"needs a choice about the data of the store at {DB}", "--backup FILE", "--empty",
            f"The store at {DB} is left as it is either way.")
    assert config_bytes(tmp_path) == before and world.log == []


def test_back_to_local_with_empty_saves_the_managed_settings_and_starts_the_engine(tmp_path, world):
    before = connect_only_installation(tmp_path)
    data = Settings.load(tmp_path / "data").data_dir
    derived = derived_database(data)
    world.writer = {"role": "writer"}
    dry = invoke(tmp_path, "setup", "local", "--empty")
    assert dry.exit_code == 1 and world.calls("start_command") == []
    for words in (f"Storage: from ArcadeDB at {DB} to the engine on this machine at {before['arcade_url']}, with the "
                  f"database {derived}.", "The engine on this machine will be started", f"has no database {derived}",
                  f"Left as it is: the store at {DB} (database cairnmind); nothing in it is changed or deleted.",
                  "This installation stays named as its writer", "Nothing was changed. To make these changes"):
        assert words in said(dry), (words, dry.output)
    (data / "arcade" / "databases" / derived).mkdir(parents=True)
    assert f"still holds the database {derived} from before" in said(invoke(tmp_path, "setup", "local", "--empty"))
    result = invoke(tmp_path, "setup", "local", "--empty", "--yes")
    assert result.exit_code == 0, result.output
    after = config(tmp_path)
    assert (after["arcade_connect_url"], after["arcade_user"], after["arcade_password_file"],
            after["arcade_ca_file"], after["arcade_database"]) == ("", "root", "", "", derived)
    assert after["arcade_url"] == before["arcade_url"] and after["arcade_password"]
    assert "PRIVATE_DB_MARKER" not in json.dumps(after) + result.output
    assert world.calls("start_command") == [("managed", True, False)]
    assert world.calls("Store") == [] and world.calls("backup") == [] and world.calls("restore") == []
    assert "The engine on this machine answers (native, Java 21)." in said(result)
    assert "`cairnmind init` opens the store on this machine" in said(result)
    assert "Storage: ArcadeDB on this machine at" in result.output


def test_back_to_local_with_a_backup_runs_backup_switch_engine_restore_in_that_order(tmp_path, world, monkeypatch):
    connect_only_installation(tmp_path)
    target = tmp_path / "remote-store.cairnmind.json.gz"
    original = setup_profiles.save_storage

    def watched(data_dir, fields):
        world.log.append(("save_storage", world.mode()))
        original(data_dir, fields)
    monkeypatch.setattr(setup_profiles, "save_storage", watched)
    result = invoke(tmp_path, "setup", "local", "--backup", str(target), "--yes")
    assert result.exit_code == 0, result.output
    assert world.log == [
        ("open_readonly", DB), ("inspect", DB), ("open_readonly", DB), ("backup", (str(target), "connect")), ("opened_backup", (str(target), "connect")),
        ("save_storage", "connect"), ("choose_engine_mode", "managed"),
        ("start_command", ("managed", True, False)), ("Store", "managed"), ("restore", (str(target), "managed"))]
    assert target.read_bytes() == b"BACKUP" and config(tmp_path)["arcade_connect_url"] == ""
    for words in (f"Backup written and checked: {target}.", f"Restored {target} into the store on this machine",
                  "Next: start Cairnmind as usual."):
        assert words in said(result), (words, result.output)


def test_a_failed_restore_puts_the_configuration_back_and_names_the_backup(tmp_path, world):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)
    target = tmp_path / "kept.cairnmind.json.gz"
    world.restore_error = ArcadeError("cut")
    result = invoke(tmp_path, "setup", "local", "--backup", str(target), "--yes")
    assert result.exit_code == 1 and isinstance(result.exception, setup_profiles.SetupFailed)
    message = str(result.exception)
    assert (f"The change stopped at the restore: cut config.json was put back as it was. The backup {target} is kept. "
            "The engine on this machine was started and is left running") in message
    assert config_without_password(tmp_path) == before and target.exists()  # the engine's password stays, see below
    assert "stays in config.json (arcade_password)" in message and "PRIVATE_DB_MARKER" not in message
    assert world.calls("take_over") == [] and all(what != "create_user" for what, _ in world.log)


def config_without_password(tmp_path) -> bytes:
    """config.json as it is, minus the managed password a started engine keeps (the only difference a put-back leaves)."""
    values = config(tmp_path)
    values.pop("arcade_password", None)
    return json.dumps(values, indent=2).encode()


def test_a_retry_after_a_failed_restore_starts_the_engine_with_the_same_password(tmp_path, world):
    stored = connect_only_installation(tmp_path)
    world.restore_error = ArcadeError("cut")
    first = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "one.cairnmind.json.gz"), "--yes")
    assert first.exit_code == 1
    kept = config(tmp_path)["arcade_password"]
    assert kept and config(tmp_path)["arcade_connect_url"] == stored["arcade_connect_url"]  # still connect-only
    world.restore_error = None
    second = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "two.cairnmind.json.gz"), "--yes")
    assert second.exit_code == 0, second.output
    assert world.passwords == [kept, kept] and config(tmp_path)["arcade_password"] == kept


@pytest.mark.parametrize("failure", [OSError("disk full"), ValueError("Expecting value: line 1 column 1 (char 0)")])
@pytest.mark.parametrize("earlier_store", [False, True])
def test_a_password_that_cannot_be_kept_is_said_and_the_original_error_stays(tmp_path, world, monkeypatch, failure,
                                                                             earlier_store):
    connect_only_installation(tmp_path)
    if earlier_store:
        (tmp_path / "data" / "arcade").mkdir()
    before = config_bytes(tmp_path)
    world.restore_error = ArcadeError("cut")

    def cannot(path, password):
        raise failure
    monkeypatch.setattr(setup_profiles, "_keep_password", cannot)
    result = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "k.cairnmind.json.gz"), "--yes")
    assert isinstance(result.exception, setup_profiles.SetupFailed)
    message = str(result.exception)
    assert "The change stopped at the restore: cut config.json was put back as it was." in message
    assert "could NOT be put back" not in message and "could not be kept in config.json (arcade_password)" in message
    assert str(failure) in message and config_bytes(tmp_path) == before
    if earlier_store:
        assert "remove nothing" in message and "just created" not in message
    else:
        assert "just created its `arcade` folder" in message and "remove nothing" not in message
    template = setup_profiles.PASSWORD_NOT_KEPT_OLD if earlier_store else setup_profiles.PASSWORD_NOT_KEPT_NEW
    assert "nie udało się zachować" in setup_profiles.render_one((template, {"reason": "x"}), "pl")


def test_a_retry_after_an_engine_start_that_failed_late_uses_the_same_password(tmp_path, world):
    stored = connect_only_installation(tmp_path)
    world.start_error = ArcadeError("ArcadeDB did not become ready")
    first = invoke(tmp_path, "setup", "local", "--empty", "--yes")
    assert first.exit_code == 1
    message = str(first.exception)
    kept = config(tmp_path)["arcade_password"]
    assert kept and config(tmp_path)["arcade_connect_url"] == stored["arcade_connect_url"]  # still connect-only
    assert "did not finish starting" in message and "config.json was put back as it was." in message
    assert kept not in message and "left running" not in message
    world.start_error = None
    second = invoke(tmp_path, "setup", "local", "--empty", "--yes")
    assert second.exit_code == 0, second.output
    assert world.passwords == [kept, kept]


def test_a_restore_that_finished_is_said_when_a_later_step_fails(tmp_path, world):
    connect_only_installation(tmp_path, knowledge_agent="enabled", memory_chat=dict(REMOTE), chat_model="gpt-x")
    world.second_check_errors["chat"] = ValueError("the server changed its mind")
    target = tmp_path / "done.cairnmind.json.gz"
    result = invoke(tmp_path, "setup", "local", "--backup", str(target), "--yes")
    assert result.exit_code == 1
    message = str(result.exception)
    assert f"The restore had already finished: the store on this machine holds the data of {target}" in message
    assert "safety backup in" in message and "config.json was put back as it was." in message


def test_the_report_of_confirmations_before_the_rule_is_said_after_the_restore(tmp_path, world, monkeypatch):
    connect_only_installation(tmp_path)
    monkeypatch.setattr(Store, "confirmations_before_rule", {"marked": 2, "before_rule": 1, "at": "x"})
    result = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "c.cairnmind.json.gz"), "--yes")
    assert result.exit_code == 0, result.output
    assert "The restore marked 2 records as confirmed by a person and left 1 before the rule" in said(result)


@pytest.mark.parametrize("agent, words", [
    ("enabled", "3 embedding job(s) are pending again and the service rebuilds the vectors"),
    ("disabled", "3 embedding job(s) are pending again, and they wait, as every embedding job does without models")])
def test_the_rebuild_of_vectors_is_said_after_the_restore(tmp_path, world, monkeypatch, agent, words):
    connect_only_installation(tmp_path, knowledge_agent=agent)
    monkeypatch.setattr(Store, "embed_jobs_requeued", 3)  # what Store.restore sets when the backup brought no vectors (#187)
    result = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "v.cairnmind.json.gz"), "--yes")
    assert result.exit_code == 0, result.output
    assert words in said(result)


def test_an_interrupt_names_the_kept_backup_and_the_engine_before_it_goes_on(tmp_path, world):
    settings = connect_only_settings(tmp_path)
    world.restore_error = KeyboardInterrupt()
    target = tmp_path / "ctrl-c.cairnmind.json.gz"
    said_lines = []
    with pytest.raises(KeyboardInterrupt):
        run(settings, Request("local", backup=str(target)), yes=True, say=said_lines.append)
    assert f"The backup {target} is kept." in said_lines[-1] and "left running" in said_lines[-1]
    assert "config.json was put back as it was." in said_lines[-1]
    assert config(tmp_path)["arcade_connect_url"] == DB


def connect_only_settings(tmp_path):
    connect_only_installation(tmp_path)
    return Settings.load(tmp_path / "data")


def test_a_backup_that_fails_or_fails_its_check_changes_nothing(tmp_path, world):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)
    first = tmp_path / "first.cairnmind.json.gz"
    world.backup_error = StoreNotReady("x")
    failed = invoke(tmp_path, "setup", "local", "--backup", str(first), "--yes")
    assert isinstance(failed.exception, setup_profiles.SetupFailed)
    assert f"The backup of the store at {DB} failed: x Nothing was changed." in str(failed.exception)
    world.backup_error, world.check_error = None, ValueError("Backup checksum mismatch")
    second = tmp_path / "second.cairnmind.json.gz"
    checked = invoke(tmp_path, "setup", "local", "--backup", str(second), "--yes")
    assert (f"The backup {second} did not pass its check: Backup checksum mismatch The file is kept; nothing was "
            "changed.") in str(checked.exception)
    assert second.exists() and config_bytes(tmp_path) == before
    assert world.calls("start_command") == [] and world.calls("restore") == []


def test_an_existing_backup_file_is_never_overwritten(tmp_path, world):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)
    target = tmp_path / "mine.cairnmind.json.gz"
    target.write_bytes(b"A PERSON'S EARLIER BACKUP")
    refused(invoke(tmp_path, "setup", "local", "--backup", str(target), "--yes"), f"{target} already exists")
    refused(invoke(tmp_path, "setup", "local", "--backup", str(tmp_path)), "already exists")
    assert target.read_bytes() == b"A PERSON'S EARLIER BACKUP" and config_bytes(tmp_path) == before
    assert world.log == []


def test_an_engine_that_cannot_start_puts_the_configuration_back(tmp_path, world):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)
    world.start_error = ArcadeError("Docker is installed but its daemon does not answer.")
    result = invoke(tmp_path, "setup", "local", "--empty", "--yes")
    assert result.exit_code == 1
    assert ("The change stopped at starting the engine on this machine: Docker is installed but its daemon does not "
            "answer. config.json was put back as it was.") in str(result.exception)
    assert config_without_password(tmp_path) == before and "left running" not in str(result.exception)
    assert config(tmp_path)["arcade_password"] and "may already have taken the password" in str(result.exception)


def test_the_dry_run_of_local_starts_and_writes_nothing(tmp_path, world):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)
    target = tmp_path / "plan.cairnmind.json.gz"
    result = invoke(tmp_path, "setup", "local", "--backup", str(target))
    assert result.exit_code == 1, result.output
    assert [what for what, _ in world.log] == ["open_readonly", "inspect"]
    assert not target.exists() and config_bytes(tmp_path) == before
    for words in (f"Tested: the store at {DB} can be read for the backup.", "The backup: the store at",
                  "The backup carries no vectors: after the restore the service rebuilds them",  # before --yes (#187)
                  "Nothing was changed. To make these changes"):
        assert words in said(result), (words, result.output)


@pytest.mark.parametrize("proof", ["key", "unavailable", "no_key"])
def test_the_restore_of_setup_goes_through_the_rule_of_cairnmind_restore(tmp_path, world, proof):
    connect_only_installation(tmp_path)
    world.proof = proof
    plan = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "plan.cairnmind.json.gz"))
    expected = setup_profiles.RESTORE_WILL_KEEP if proof == "key" else setup_profiles.RESTORE_WILL_CLAIM
    assert plain_words(expected) in said(plan), plan.output
    assert MESSAGES[expected] in setup_profiles.render(plan_of(tmp_path, world), "pl")
    result = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "b.cairnmind.json.gz"), "--yes", "--json")
    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    if proof == "key":
        assert world.claims == [None] and shown["person_confirmations"] == "kept"
        assert setup_profiles.RESTORE_KEPT in shown["done"]
    else:
        assert isinstance(world.claims[0], BackupClaims) and world.claims[0].where == "b.cairnmind.json.gz"
        assert (shown["person_confirmations"], shown["confirmations_turned_into_claims"],
                shown["record_kinds_withdrawn"]) == ("claims_of_the_backup", 0, 0)
        assert restore_notice(proof, world.claims[0]) in shown["done"]


def plan_of(tmp_path, world):
    """The plan of `setup local --backup` as run() returns it, for rendering in another language."""
    return run(Settings.load(tmp_path / "data"), Request("local", backup=str(tmp_path / "pl.cairnmind.json.gz")),
               yes=False, say=lambda text: None)


def test_a_model_save_that_fails_after_the_storage_switch_puts_everything_back(tmp_path, world):
    connect_only_installation(tmp_path, knowledge_agent="enabled", memory_chat=dict(REMOTE), chat_model="gpt-x")
    before = config_bytes(tmp_path)
    world.second_check_errors["chat"] = ValueError("the server changed its mind")
    result = invoke(tmp_path, "setup", "local", "--empty", "--yes")
    assert result.exit_code == 1 and "the server changed its mind" in str(result.exception)
    assert "stopped at saving the chat model" in str(result.exception) and "left running" in str(result.exception)
    assert config_without_password(tmp_path) == before
    assert world.calls("start_command") == [("managed", True, False)]


def test_a_store_that_cannot_be_read_for_the_backup_changes_nothing(tmp_path, world, monkeypatch):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)

    def unreadable(cls, settings):
        raise StoreNotReady("The Cairnmind database has no schema yet; create it with `cairnmind init`.")
    monkeypatch.setattr(Store, "open_readonly", classmethod(unreadable))
    result = invoke(tmp_path, "setup", "local", "--backup", str(tmp_path / "b.cairnmind.json.gz"))
    refused(result, f"The store at {DB} cannot be read for the backup:", "has no schema yet")
    assert config_bytes(tmp_path) == before


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


def test_an_applied_result_and_a_refusal_that_carry_another_modules_sentence_render_in_polish(tmp_path, world):
    settings = make(tmp_path, knowledge_agent="disabled")
    result = run(settings, Request("remote", url=DB, user="cairnmind", database="cairnmind",
                                   password_file=db_options(tmp_path)[7], models_url=MODELS,
                                   provider="openai_compatible", chat_model="gpt-x", embedding_model="emb-y",
                                   allow_remote=True), yes=True, say=lambda text: None)
    assert any(item[0] == setup_profiles.VERBATIM for key in ("done", "next") for item in result[key])
    assert "Zapisano w" in "\n".join(render(result, "pl"))
    make(tmp_path / "other", knowledge_agent="enabled", ollama_url="http://models.lan:11434")
    with pytest.raises(SetupRefused) as refusal:
        run(Settings.load(tmp_path / "other" / "data"), Request("remote-db", url=DB, user="u", database="d",
                                                                 password_file=db_options(tmp_path)[7]),
            yes=False, say=lambda text: None)
    assert "models.lan" in render_one((refusal.value.template, refusal.value.values), "pl")


def test_an_invalid_database_prefix_is_refused_going_back(tmp_path, world, monkeypatch):
    connect_only_installation(tmp_path)
    before = config_bytes(tmp_path)
    monkeypatch.setenv("CAIRNMIND_ARCADE_DB_PREFIX", "not valid")
    refused(invoke(tmp_path, "setup", "local", "--empty", "--yes"),
            "CAIRNMIND_ARCADE_DB_PREFIX is not a valid database prefix", "Nothing was changed.")
    assert config_bytes(tmp_path) == before and world.log == []


def test_the_plan_names_native_for_a_directory_that_holds_an_engine(tmp_path, world):
    connect_only_installation(tmp_path, engine_mode="")
    (tmp_path / "data" / "arcade").mkdir()
    plan = said(invoke(tmp_path, "setup", "local", "--empty"))
    assert "will be started (native, Java 21)" in plan and "as `cairnmind init` chooses" not in plan


def test_a_refusal_after_the_generic_rule_saved_the_address_does_not_say_nothing_changed_alone(
        tmp_path, world, monkeypatch):
    make(tmp_path)
    monkeypatch.setenv("CAIRNMIND_ARCADE_CONNECT_URL", "https://db.example:2490")
    result = invoke(tmp_path, "setup", "remote-db", *db_options(tmp_path), "--yes")
    refused(result, "CAIRNMIND_ARCADE_CONNECT_URL is set in this shell", "was saved in config.json first")
    assert config(tmp_path)["arcade_connect_url"] == "https://db.example:2490" and world.log == []


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
