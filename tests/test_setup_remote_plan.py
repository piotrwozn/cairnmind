"""`setup remote-db` with the administrator's file and without --yes, on the real engine (#365).

The plan must be printed whether or not the user exists yet, and the server must hold nothing new afterwards.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import os
import uuid

import pytest
from typer.testing import CliRunner

from conftest import plain_words
from cairnmind import cli, database_credentials
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings, is_own_variable

USER_SECRET = "User-password-for-the-plan-1"


def private(path, text):
    path.write_text(text + "\n")
    os.chmod(path, 0o600)
    return str(path)


@pytest.fixture
def server(tmp_path, monkeypatch, arcade_test_engine):
    root = arcade_test_engine[0]
    root_secret = os.environ["CAIRNMIND_ARCADE_PASSWORD"]
    for key in [key for key in os.environ if is_own_variable(key)]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    suffix = uuid.uuid4().hex[:10]
    names = {"user": "cm_" + suffix, "database": "cm_plan_" + suffix}
    Settings.load(tmp_path / "data", create=True).save()
    try:
        yield root, root_secret, names, tmp_path
    finally:
        with closing(ArcadeClient(replace(root, arcade_password=root_secret))) as client:
            for command in (f"drop user {names['user']}", f"drop database {names['database']}"):
                try:
                    client.server_command(command)
                except ArcadeError:
                    pass


def dry_run(server, admin_secret=None, password=USER_SECRET):
    root, root_secret, names, tmp_path = server
    admin = private(tmp_path / "admin", admin_secret or root_secret)
    arguments = ["--data-dir", str(tmp_path / "data"), "setup", "remote-db", "--url", root.arcade_url,
                 "--user", names["user"], "--database", names["database"],
                 "--password-file", private(tmp_path / "db-password", password), "--admin-password-file", admin]
    return CliRunner().invoke(cli.app, arguments, env={"COLUMNS": "500", "TERMINAL_WIDTH": "500"})


def make_user(server, database=None):
    root, root_secret, names, _ = server
    with closing(ArcadeClient(replace(root, arcade_password=root_secret))) as client:
        client.server_command("create user " + json.dumps(
            {"name": names["user"], "password": USER_SECRET, "databases": {database or names["database"]: ["admin"]}}))


def make_database(server):
    root, root_secret, names, _ = server
    with closing(ArcadeClient(replace(root, arcade_password=root_secret, arcade_database=names["database"]))) as client:
        client.ensure_database()


def database_exists(server) -> bool:
    """Whether the server holds the database, asked of the server itself as the administrator."""
    root, root_secret, names, _ = server
    with closing(ArcadeClient(replace(root, arcade_password=root_secret, arcade_database=names["database"]))) as client:
        return client.database_exists()


def test_a_user_and_database_that_do_not_exist_yet_get_a_plan_and_nothing_is_created(server):
    root, root_secret, names, tmp_path = server
    before = (tmp_path / "data" / "config.json").read_bytes()
    result = dry_run(server)
    words = plain_words(result.output)
    assert result.exit_code == 1, result.output
    assert "answers with version" in words and "accepts the administrator; nothing was created" in words
    assert "creates the database and, if it does not exist yet, the user" in words
    assert (tmp_path / "data" / "config.json").read_bytes() == before
    assert root_secret not in result.output and USER_SECRET not in result.output
    assert database_exists(server) is False
    # The server holds no new user: creating it now is the first time.
    candidate = Settings.load(tmp_path / "data")
    candidate = replace(candidate, arcade_connect_url=root.arcade_url, arcade_url=root.arcade_url,
                        arcade_user=names["user"], arcade_database=names["database"], arcade_password=USER_SECRET)
    candidate._origin = {"password": "file"}
    made = database_credentials.create_user(candidate, root_secret)
    assert made["created"] is True and made["database_created"] is True


def test_an_existing_user_and_a_missing_database_are_planned_as_a_database_only(server):
    root, root_secret, names, tmp_path = server
    make_user(server, database=names["database"] + "_other")
    before = (tmp_path / "data" / "config.json").read_bytes()
    result = dry_run(server)
    words = plain_words(result.output)
    assert result.exit_code == 1, result.output
    assert f"the user {names['user']} exists, but the database {names['database']} does not" in words
    assert "creates the database; the user is used only if it has rights on it" in words
    assert (tmp_path / "data" / "config.json").read_bytes() == before
    assert database_exists(server) is False


def test_both_existing_with_the_right_password_are_tested_as_they_are(server):
    root, root_secret, names, tmp_path = server
    make_user(server)
    make_database(server)
    result = dry_run(server)
    words = plain_words(result.output)
    assert result.exit_code == 1, result.output
    assert f"the user {names['user']} signs in to the database {names['database']}" in words
    assert "Not tested yet" not in words


def test_a_user_with_another_password_is_never_offered_a_new_one(server):
    root, root_secret, names, tmp_path = server
    make_user(server)
    make_database(server)
    before = (tmp_path / "data" / "config.json").read_bytes()
    result = dry_run(server, password="Another-password-for-it-1")
    words = plain_words(result.output)
    assert result.exit_code == 1, result.output
    assert "creates the user, if it does not exist yet" in words
    assert "A user that exists with another password is never changed" in words
    assert (tmp_path / "data" / "config.json").read_bytes() == before
    # The old password still signs in: nothing was overwritten.
    stored = replace(root, arcade_user=names["user"], arcade_password=USER_SECRET, arcade_database=names["database"])
    with closing(ArcadeClient(stored)) as client:
        assert client.query("SELECT 1 AS ok")


def test_a_user_of_another_database_is_planned_as_refused_and_nothing_is_created(server):
    root, root_secret, names, tmp_path = server
    make_user(server, database=names["database"] + "_other")
    make_database(server)
    result = dry_run(server)
    words = plain_words(result.output)
    assert result.exit_code == 1, result.output
    assert f"the user {names['user']} exists, but it has no rights on the database {names['database']}" in words
    assert "each database gets its own user" in words


def test_a_wrong_administrator_password_is_refused_naming_the_administrator(server):
    root, root_secret, names, tmp_path = server
    result = dry_run(server, admin_secret="Wrong-administrator-password-1")
    words = plain_words(result.output)
    assert result.exit_code == 2, result.output
    assert "The administrator was refused" in words and "did not accept root" in words
    assert f"did not accept the user {names['user']}" not in words
    assert "Wrong-administrator-password-1" not in result.output
    assert database_exists(server) is False
