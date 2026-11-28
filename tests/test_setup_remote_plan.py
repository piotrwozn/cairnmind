"""`setup remote-db` with the administrator's file and without --yes, on the real engine (#365).

The plan must be printed whether or not the user exists yet, and the server must hold nothing new afterwards.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import closing, suppress
from dataclasses import replace

import pytest
from conftest import plain_words
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.config import Settings, is_own_variable
from cairnmind.storage import database_credentials
from cairnmind.storage.arcade import ArcadeClient, ArcadeError

USER_SECRET = "User-password-for-the-plan-1"


def private(path, text):
    path.write_text(text + "\n")
    os.chmod(path, 0o600)
    return str(path)


@pytest.fixture
def server(tmp_path, monkeypatch, arcade_test_engine):
    root, prefix = arcade_test_engine[0], arcade_test_engine[1]
    root_secret = os.environ["CAIRNMIND_ARCADE_PASSWORD"]
    for key in [key for key in os.environ if is_own_variable(key)]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    suffix = uuid.uuid4().hex[:10]
    # The run's prefix: on a server the databases of this run are the ones the fixtures create as root (#360).
    names = {"user": "cm_" + suffix, "database": f"{prefix}_plan_{suffix}"}
    Settings.load(tmp_path / "data", create=True).save()
    try:
        yield root, root_secret, names, tmp_path
    finally:
        with closing(ArcadeClient(replace(root, arcade_password=root_secret), administrator=True)) as client:
            for command in (f"drop user {names['user']}", f"drop database {names['database']}"):
                with suppress(ArcadeError):
                    client.server_command(command)


def dry_run(server, admin_secret=None, password=USER_SECRET, *more):
    root, root_secret, names, tmp_path = server
    admin = private(tmp_path / "admin", admin_secret or root_secret)
    arguments = ["--data-dir", str(tmp_path / "data"), "setup", "remote-db", "--url", root.arcade_url,
                 "--user", names["user"], "--database", names["database"],
                 "--password-file", private(tmp_path / "db-password", password), "--admin-password-file", admin, *more]
    return CliRunner().invoke(cli.app, arguments, env={"COLUMNS": "500", "TERMINAL_WIDTH": "500"})


def make_user(server, database=None):
    root, root_secret, names, _ = server
    with closing(ArcadeClient(replace(root, arcade_password=root_secret), administrator=True)) as client:
        client.server_command("create user " + json.dumps(
            {"name": names["user"], "password": USER_SECRET, "databases": {database or names["database"]: ["admin"]}}))


def make_database(server):
    root, root_secret, names, _ = server
    with closing(ArcadeClient(replace(root, arcade_password=root_secret, arcade_database=names["database"]),
                              administrator=True)) as client:
        client.ensure_database()


def database_exists(server) -> bool:
    """Whether the server holds the database, asked of the server itself as the administrator."""
    root, root_secret, names, _ = server
    with closing(ArcadeClient(replace(root, arcade_password=root_secret, arcade_database=names["database"]),
                              administrator=True)) as client:
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
    assert "create them once" not in words  # the plan has said what --yes creates: no advice to run db create-user
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


def test_a_user_with_rights_on_a_database_that_is_not_there_yet_is_planned_and_then_gets_only_the_database(server):
    root, root_secret, names, tmp_path = server
    make_user(server)  # rights on the named database, which does not exist: the server answers the user 404
    before = (tmp_path / "data" / "config.json").read_bytes()
    result = dry_run(server)
    words = plain_words(result.output)
    assert result.exit_code == 1, result.output
    assert f"the user {names['user']} exists, but the database {names['database']} does not" in words
    assert (tmp_path / "data" / "config.json").read_bytes() == before
    assert database_exists(server) is False
    done = dry_run(server, None, USER_SECRET, "--yes")
    said = plain_words(done.output)
    assert done.exit_code == 0, done.output
    assert f"Created on ArcadeDB at {root.arcade_url}: the database {names['database']}." in said
    assert "the user " + names["user"] + ", with rights" not in said  # the user was not created by this run
    assert database_exists(server) is True


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


def test_a_user_request_that_fails_without_a_status_is_not_reported_as_signing_in(server, monkeypatch):
    root, root_secret, names, tmp_path = server
    make_user(server)
    make_database(server)
    original = ArcadeClient.query

    def query(self, *args, **kwargs):
        if self.settings.arcade_user == names["user"]:
            raise ArcadeError("ArcadeDB at the server is unavailable (ReadTimeout)")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ArcadeClient, "query", query)
    result = dry_run(server)
    words = plain_words(result.output)
    assert result.exit_code == 2, result.output
    assert "unavailable (ReadTimeout)" in words and "signs in to the database" not in words


def test_the_plan_refuses_a_password_that_a_run_with_yes_refuses(server):
    root, root_secret, names, tmp_path = server
    candidate = replace(Settings.load(tmp_path / "data"), arcade_connect_url=root.arcade_url, arcade_url=root.arcade_url,
                        arcade_user=names["user"], arcade_database=names["database"], arcade_password="has a space")
    candidate._origin = {"password": "file"}
    with pytest.raises(ValueError, match="without spaces"):
        database_credentials.plan_user(candidate, root_secret)
    with pytest.raises(ValueError, match="without spaces"):
        database_credentials.create_user(candidate, root_secret)


def test_a_wrong_administrator_password_is_refused_naming_the_administrator(server):
    root, root_secret, names, tmp_path = server
    result = dry_run(server, admin_secret="Wrong-administrator-password-1")
    words = plain_words(result.output)
    assert result.exit_code == 2, result.output
    assert "The administrator was refused" in words and "did not accept root" in words
    assert f"did not accept the user {names['user']}" not in words
    assert "Wrong-administrator-password-1" not in result.output
    assert database_exists(server) is False
