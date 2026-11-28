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
from conftest import as_person
from database_relay import Relay
from test_connect_only import data_directory, repository, test_engine  # noqa: F401 - test_engine is a fixture
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.memory.confirmation import confirmation_of
from cairnmind.storage.arcade import ArcadeClient
from cairnmind.storage.database_mode import connect_only
from cairnmind.store import Store
from cairnmind.terminal import person_proof

runner = CliRunner()


def cli(data, *arguments):
    return runner.invoke(app, ["--data-dir", str(data), *arguments])


def counts(settings) -> tuple[int, dict]:
    """The sources of a database and its journal head, read through a client."""
    with closing(ArcadeClient(settings)) as client:
        head = client.query("SELECT FROM journal_state WHERE id='current'")[0]
        return client.query("SELECT count(*) AS n FROM sources")[0]["n"], head


@pytest.mark.no_schema_template
@pytest.mark.parametrize("proof", ["unavailable", "key"], ids=["without_the_key", "with_the_key"])
def test_moving_to_a_connect_only_store_and_back_with_a_backup_keeps_every_row_and_leaves_the_remote_store(
        tmp_path, test_engine, monkeypatch, proof):  # noqa: F811
    # The way back restores under the rule of `cairnmind restore` (#214): only the workspace's key keeps a
    # person's confirmation; without it the confirmation becomes the backup's claim, and the count is said.
    monkeypatch.setattr(person_proof, "restore_proof", lambda settings: proof)
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
        remote_store = Store(data, settings=remote)
        pid = remote_store.resolve_project("second")["id"]
        note = remote_store.snapshot(pid, "manual://note", "A note a person wrote", "manual")
        typed = as_person(remote_store, pid, {"kind": "decision", "content": "Typed by a person"}, [note["id"]])
        remote_rows, remote_head = counts(remote)
        assert remote_rows >= 1 and counts(local)[0] == local_before  # the local store was left as it was

        backup = tmp_path / "from-remote.cairnmind.json.gz"
        back = cli(data, "setup", "local", "--backup", str(backup), "--yes", "--json")
        assert back.exit_code == 0, (back.output, back.exception)
        assert backup.exists() and not connect_only(Settings.load(data))
        shown = json.loads(back.stdout)
        row = Store(data, settings=Settings.load(data)).query("SELECT FROM records WHERE id=:id", {"id": typed["id"]})[0]
        if proof == "key":
            assert shown["person_confirmations"] == "kept" and confirmation_of(row)["by"] == "person"
        else:
            assert (confirmation_of(row)["by"], confirmation_of(row).get("package")) == ("import", "backup")
            assert (shown["person_confirmations"], shown["confirmations_turned_into_claims"]) == (
                "claims_of_the_backup", 1)
            assert any("Restored without the person's workspace" in line and "1 confirmed record(s)" in line
                       for line in shown["done"])
        # Every row of the remote store is in the local one; the remote store is as it was.
        assert counts(Settings.load(data))[0] == remote_rows
        assert counts(remote) == (remote_rows, remote_head)
        assert len(list((data / "backups").glob("before-restore-*"))) == 1
        saved = json.loads((data / "config.json").read_text())
        assert saved["arcade_connect_url"] == "" and saved["arcade_password_file"] == ""
