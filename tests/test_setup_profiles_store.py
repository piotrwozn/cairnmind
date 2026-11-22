"""`cairnmind setup` against a real engine: the move to a store this installation only connects to, and the way back (#74).

The real test engine stands behind a relay as the "remote" server (loopback http, which connect-only allows for a
user such as root); nothing is replaced. The rest of the profiles is in test_setup_profiles.py.
"""
from __future__ import annotations

import json
import os
from contextlib import closing
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from cairnmind.arcade import ArcadeClient
from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.database_mode import connect_only
from database_relay import Relay
from test_connect_only import data_directory, repository, test_engine  # noqa: F401 - test_engine is a fixture

runner = CliRunner()


def cli(data, *arguments):
    return runner.invoke(app, ["--data-dir", str(data), *arguments])


def counts(settings) -> tuple[int, dict]:
    """The sources of a database and its journal head, read through a client."""
    with closing(ArcadeClient(settings)) as client:
        head = client.query("SELECT FROM journal_state WHERE id='current'")[0]
        return client.query("SELECT count(*) AS n FROM sources")[0]["n"], head


@pytest.mark.no_schema_template
def test_moving_to_a_connect_only_store_and_back_with_a_backup_keeps_every_row_and_leaves_the_remote_store(
        tmp_path, test_engine):  # noqa: F811
    port, secret = test_engine
    prefix = os.environ["CAIRNMIND_ARCADE_DB_PREFIX"]
    data = data_directory(tmp_path, arcade_url=f"http://127.0.0.1:{port}", arcade_password=secret,
                          knowledge_agent="disabled")
    assert cli(data, "init").exit_code == 0
    first = repository(tmp_path)
    assert cli(data, "project", "add", "first", str(first)).exit_code == 0
    local = Settings.load(data)
    local_before = counts(local)[0]

    remote_name = f"{prefix}_remote"
    engine_root = replace(local, arcade_database=remote_name)
    with closing(ArcadeClient(engine_root)) as client:
        client.ensure_database()
    password_file = tmp_path / "root-password"
    password_file.write_text(secret + "\n")
    os.chmod(password_file, 0o600)
    with Relay(port) as relay:
        moved = cli(data, "setup", "remote-db", "--url", relay.url, "--user", "root", "--database", remote_name,
                    "--password-file", str(password_file), "--yes")
        assert moved.exit_code == 0, (moved.output, moved.exception)
        assert connect_only(Settings.load(data))
        assert cli(data, "init").exit_code == 0
        second = tmp_path / "second"
        second.mkdir()
        (second / "other.py").write_text("OTHER = 1\n\n\ndef other():\n    return OTHER\n")
        assert cli(data, "project", "add", "second", str(second)).exit_code == 0
        remote = Settings.load(data)
        remote_rows, remote_head = counts(remote)
        assert remote_rows >= 1 and counts(local)[0] == local_before  # the local store was left as it was

        backup = tmp_path / "from-remote.cairnmind.json.gz"
        back = cli(data, "setup", "local", "--backup", str(backup), "--yes")
        assert back.exit_code == 0, (back.output, back.exception)
        assert backup.exists() and not connect_only(Settings.load(data))
        # Every row of the remote store is in the local one; the remote store is as it was.
        assert counts(Settings.load(data))[0] == remote_rows
        assert counts(remote) == (remote_rows, remote_head)
        assert len(list((data / "backups").glob("before-restore-*"))) == 1
        saved = json.loads((data / "config.json").read_text())
        assert saved["arcade_connect_url"] == "" and saved["arcade_password_file"] == ""
