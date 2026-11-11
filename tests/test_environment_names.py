"""CAIRNMIND_* variables and the data directory, with the earlier DEVBRAIN_* names still honoured (issue #40).

No engine: only the environment, the settings file and directories under a temporary home.
"""
from __future__ import annotations

import json
import os

import pytest

from cairnmind.api_runtime import _worker_environment
from cairnmind.config import (Settings, data_dir_note, default_data_dir, drop_env, env_conflicts, env_names, env_value,
                              is_own_variable, session_conflicts, session_value, set_env)
from cairnmind.mcp_server import ServiceClient

@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for key in [key for key in os.environ if is_own_variable(key)]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_the_current_spelling_wins_and_the_earlier_one_is_still_read(monkeypatch):
    assert env_names("HOME") == ("CAIRNMIND_HOME", "DEVBRAIN_HOME")
    assert env_value("ARCADE_URL") is None and env_value("ARCADE_URL", "fallback") == "fallback"
    monkeypatch.setenv("DEVBRAIN_ARCADE_URL", "http://earlier")
    assert env_value("ARCADE_URL") == "http://earlier"
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", "http://current")
    assert env_value("ARCADE_URL") == "http://current"
    # Presence decides, as it always did: a variable set to nothing still overrides.
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", "")
    assert env_value("ARCADE_URL") == ""
    assert is_own_variable("CAIRNMIND_X") and is_own_variable("DEVBRAIN_X") and not is_own_variable("PATH")


@pytest.mark.parametrize("field, name", [
    ("arcade_url", "ARCADE_URL"), ("arcade_user", "ARCADE_USER"), ("arcade_password", "ARCADE_PASSWORD"),
    ("arcade_database", "ARCADE_DATABASE"), ("java_executable", "JAVA_EXECUTABLE"), ("tmux_executable", "TMUX_EXECUTABLE")])
@pytest.mark.parametrize("spellings", [("CAIRNMIND_",), ("DEVBRAIN_",), ("CAIRNMIND_", "DEVBRAIN_")])
def test_settings_take_each_override_from_either_spelling(monkeypatch, tmp_path, field, name, spellings):
    for prefix in spellings:
        monkeypatch.setenv(prefix + name, f"value-from-{prefix}")
    assert getattr(Settings.load(tmp_path / "data"), field) == f"value-from-{spellings[0]}"


def test_the_database_name_of_an_existing_store_does_not_change(monkeypatch, tmp_path):
    before = Settings.load(tmp_path / "data").arcade_database
    assert before.startswith("devbrain_")  # the default prefix is part of every existing database name
    monkeypatch.setenv("DEVBRAIN_ARCADE_DB_PREFIX", "earlier")
    assert Settings.load(tmp_path / "data").arcade_database.startswith("earlier_")
    monkeypatch.setenv("CAIRNMIND_ARCADE_DB_PREFIX", "current")
    assert Settings.load(tmp_path / "data").arcade_database.startswith("current_")
    monkeypatch.setenv("CAIRNMIND_ARCADE_DB_PREFIX", "not valid")
    with pytest.raises(ValueError, match="Invalid ArcadeDB database prefix"):
        Settings.load(tmp_path / "data")


def test_a_new_installation_starts_under_the_current_name(clean_environment):
    assert default_data_dir() == (clean_environment / ".local/share/cairnmind").resolve()
    assert data_dir_note() is None
    assert not (clean_environment / ".local").exists()  # choosing a directory creates nothing


def test_a_directory_created_by_an_earlier_version_is_used_where_it_is(clean_environment):
    earlier = clean_environment / ".local/share/devbrain"
    earlier.mkdir(parents=True)
    assert default_data_dir() == (clean_environment / ".local/share/cairnmind").resolve()  # an empty directory is not a store
    (earlier / "arcade").mkdir()
    # A store that lost its configuration file is still the store: no second one is started beside it.
    assert default_data_dir() == earlier.resolve()
    (earlier / "arcade").rmdir()
    (earlier / "config.json").write_text(json.dumps({"token": "kept"}))
    assert default_data_dir() == earlier.resolve()
    assert Settings.load().token == "kept"
    assert not (clean_environment / ".local/share/cairnmind").exists() and data_dir_note() is None
    assert sorted(path.name for path in earlier.iterdir()) == ["config.json"]  # nothing was moved or copied


def test_two_default_directories_are_reported_and_the_earlier_one_keeps_being_used(clean_environment):
    for name in ("devbrain", "cairnmind"):
        (clean_environment / ".local/share" / name).mkdir(parents=True)
        (clean_environment / ".local/share" / name / "config.json").write_text("{}")
    assert default_data_dir() == (clean_environment / ".local/share/devbrain").resolve()
    assert "Both" in data_dir_note() and "CAIRNMIND_HOME" in data_dir_note()


def test_an_empty_home_variable_names_no_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("CAIRNMIND_HOME", "")
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path / "earlier-choice"))
    assert default_data_dir() == (tmp_path / "earlier-choice").resolve()
    assert env_conflicts() == []


def test_an_explicit_home_overrides_both_default_directories(monkeypatch, clean_environment, tmp_path):
    (clean_environment / ".local/share/devbrain").mkdir(parents=True)
    (clean_environment / ".local/share/devbrain/config.json").write_text("{}")
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path / "earlier-choice"))
    assert default_data_dir() == (tmp_path / "earlier-choice").resolve()
    monkeypatch.setenv("CAIRNMIND_HOME", str(tmp_path / "current-choice"))
    assert default_data_dir() == (tmp_path / "current-choice").resolve() and data_dir_note() is None
    assert env_conflicts() == ["HOME"]


def test_conflicts_are_only_spellings_that_are_both_present_and_differ(monkeypatch):
    monkeypatch.setenv("CAIRNMIND_ARCADE_URL", "http://same")
    monkeypatch.setenv("DEVBRAIN_ARCADE_URL", "http://same")
    monkeypatch.setenv("CAIRNMIND_ARCADE_USER", "only-current")
    monkeypatch.setenv("DEVBRAIN_JAVA_EXECUTABLE", "only-earlier")
    assert env_conflicts() == []
    monkeypatch.setenv("DEVBRAIN_ARCADE_USER", "another")
    assert env_conflicts() == ["ARCADE_USER"]


def test_a_session_variable_is_the_first_spelling_that_is_not_empty():
    environment = {"CAIRNMIND_SESSION_ID": "", "DEVBRAIN_SESSION_ID": "from-an-earlier-launcher"}
    # A project file forwards a listed variable as an empty string when the launcher did not set it.
    assert session_value("SESSION_ID", environment) == "from-an-earlier-launcher"
    assert session_value("SESSION_ID", {"CAIRNMIND_SESSION_ID": "current"}) == "current"
    assert session_value("SESSION_ID", {}) is None and session_value("SESSION_ID", {"DEVBRAIN_SESSION_ID": ""}) is None
    same = {"CAIRNMIND_SESSION_ID": "one", "DEVBRAIN_SESSION_ID": "one"}
    assert session_value("SESSION_ID", same) == "one"


def test_two_spellings_that_name_different_sessions_are_refused():
    mixed = {"CAIRNMIND_SESSION_ID": "inner", "DEVBRAIN_SESSION_ID": "outer"}
    with pytest.raises(ValueError, match="resume_required: CAIRNMIND_SESSION_ID and DEVBRAIN_SESSION_ID"):
        session_value("SESSION_ID", mixed)
    assert session_value("SESSION_ID", mixed, strict=False) is None


def test_setting_and_dropping_cover_both_spellings():
    environment = {"DEVBRAIN_SESSION_ID": "stale", "PATH": "/bin"}
    set_env(environment, "SESSION_ID", "fresh")
    assert environment == {"CAIRNMIND_SESSION_ID": "fresh", "DEVBRAIN_SESSION_ID": "fresh", "PATH": "/bin"}
    drop_env(environment, "SESSION_ID", "PACKET_ID")
    assert environment == {"PATH": "/bin"}


def test_the_mcp_server_binds_from_either_spelling_and_stays_unbound_when_they_disagree(monkeypatch, tmp_path):
    settings = Settings.load(tmp_path / "data")
    assert (ServiceClient(settings).session_id, ServiceClient(settings).packet_id) == (None, None)
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "session")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet")
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "")  # as forwarded by a project file of a later version
    bound = ServiceClient(settings)
    assert (bound.session_id, bound.packet_id) == ("session", "packet")
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "another-session")
    unbound = ServiceClient(settings)
    assert (unbound.session_id, unbound.packet_id) == (None, None)
    explicit = ServiceClient(settings, session_id="given", packet_id="given-packet")
    assert (explicit.session_id, explicit.packet_id) == ("given", "given-packet")


def test_workers_get_neither_spelling(monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID", "CAIRNMIND_ARCADE_PASSWORD", "DEVBRAIN_ARCADE_PASSWORD"):
        monkeypatch.setenv(name, "SECRET")
    environment = _worker_environment([])
    assert not any(is_own_variable(name) for name in environment) and "PATH" in environment


def test_doctor_words_a_session_conflict_as_refused_not_as_resolved(monkeypatch):
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "inner")
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", "outer")
    monkeypatch.setenv("CAIRNMIND_PACKET_ID", "")
    monkeypatch.setenv("DEVBRAIN_PACKET_ID", "packet")
    # Session names are never "resolved" in favour of one spelling, and an empty one is no conflict.
    assert env_conflicts() == [] and session_conflicts() == ["SESSION_ID"]
