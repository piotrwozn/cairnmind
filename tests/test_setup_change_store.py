"""The change of setup of the workspace against a real engine and a real knowledge service (#339).

The real test engine stands behind a relay as the "remote" server, as in test_setup_profiles_store.py. The helper's
function (setup_change.carry_out) runs in this process, which stands for the helper; the knowledge service it stops
and starts is a real process. No tmux and no Docker. The models are off (knowledge_agent disabled), so no model server
is needed: the move of the models to another computer is covered with fakes in test_setup_change.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cairnmind import cli as cairnmind_cli
from cairnmind import person_proof, setup_change, setup_mark, setup_profiles, store_writer, upgrade_mark
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.confirmation import confirmation_of
from cairnmind.database_mode import connect_only
from cairnmind.store import Store
from conftest import as_person
from database_relay import Relay
from test_connect_only import data_directory, repository, test_engine  # noqa: F401 - test_engine is a fixture

runner = CliRunner()


def cli(data, *arguments):
    return runner.invoke(app, ["--data-dir", str(data), *arguments])


def counts(settings) -> tuple[int, dict]:
    with closing(ArcadeClient(settings)) as client:
        head = client.query("SELECT FROM journal_state WHERE id='current'")[0]
        return client.query("SELECT count(*) AS n FROM sources")[0]["n"], head


def alive(settings) -> bool:
    return cairnmind_cli.service_alive(settings)


def service_pids(settings) -> list[int]:
    """The process ids of the knowledge service of this data directory (ps: the same on Linux and macOS)."""
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    return [int(line.split(None, 1)[0]) for line in listing.splitlines()
            if f"--data-dir {settings.data_dir} serve" in line]


def service_pid(settings) -> int:
    found = service_pids(settings)
    assert len(found) == 1, found
    return found[0]


@pytest.fixture
def installation(tmp_path, test_engine, monkeypatch):  # noqa: F811
    """A local installation with one project and a running knowledge service; the service is stopped at the end."""
    port, secret = test_engine
    prefix = os.environ["CAIRNMIND_ARCADE_DB_PREFIX"]
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("none", None))  # the machine's own upgrade is not this test's
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    data = data_directory(tmp_path, arcade_url=f"http://127.0.0.1:{port}", arcade_password=secret,
                          knowledge_agent="disabled")
    assert cli(data, "init").exit_code == 0
    assert cli(data, "project", "add", "first", str(repository(tmp_path))).exit_code == 0
    local = Settings.load(data)
    password_file = tmp_path / "root-password"
    password_file.write_text(secret + "\n")
    os.chmod(password_file, 0o600)
    names = [f"{prefix}_remote", f"{prefix}_remote2"]
    for name in names:
        with closing(ArcadeClient(replace(local, arcade_database=name))) as client:
            client.ensure_database()
    cairnmind_cli.ensure_service(local, background_inference=False)
    state = SimpleNamespace(data=data, port=port, local=local, names=names, password_file=password_file,
                            tmp=tmp_path, relay=None)

    def remote_db(database=None, url=None):
        return setup_profiles.Request("remote-db", url=url or state.relay.url, user="root",
                                      database=database or names[0], password_file=str(password_file))

    def agreed(request):
        return setup_change.agreed_plan(setup_profiles.run(Settings.load(data), request, yes=False,
                                                           say=lambda text: None))

    def change(request, expected=None):
        reports = []
        outcome = setup_change.carry_out(data, request, expected or agreed(request), "en",
                                         lambda state_, lines, other: reports.append((state_, list(lines))))
        return outcome, "\n".join(reports[-1][1])

    def stop():
        settings = Settings.load(data)
        if alive(settings):
            cli(data, "stop")
            for _ in range(100):  # until the process has gone too, not only its answer
                if not alive(settings) and not service_pids(settings):
                    break
                time.sleep(0.1)

    state.remote_db, state.agreed, state.change, state.stop = remote_db, agreed, change, stop
    yield state
    stop()  # a test that failed before its own stop


def settled(data, *, remote: bool):
    """A change that finished: the settings it names, a service that answers, no mark, no copy."""
    settings = Settings.load(data)
    assert connect_only(settings) is remote
    assert alive(settings)
    assert not (data / setup_mark.MARK).exists() and not (data / setup_mark.BEFORE).exists()
    assert cli(data, "status").exit_code == 0
    return settings


@pytest.mark.no_schema_template
@pytest.mark.parametrize("proof", ["unavailable", "key"], ids=["without_the_key", "with_the_key"])
def test_local_to_remote_db_and_back_with_a_backup(installation, monkeypatch, proof):
    ns = installation
    monkeypatch.setattr(person_proof, "restore_proof", lambda settings: proof)
    local_before = counts(ns.local)[0]
    first_pid = service_pid(ns.local)
    with Relay(ns.port) as relay:
        ns.relay = relay
        state, text = ns.change(ns.remote_db())
        assert state == "changed", text
        remote = settled(ns.data, remote=True)
        assert service_pid(remote) != first_pid and "runs again with the new settings" in text
        saved = json.loads((ns.data / "config.json").read_text())
        assert saved["arcade_connect_url"] == relay.url and saved["arcade_database"] == ns.names[0]
        second = ns.tmp / "second"
        second.mkdir()
        (second / "other.py").write_text("OTHER = 1\n\n\ndef other():\n    return OTHER\n")
        remote_store = Store(ns.data, settings=remote)
        assert cli(ns.data, "project", "add", "second", str(second)).exit_code == 0
        pid = remote_store.resolve_project("second")["id"]
        note = remote_store.snapshot(pid, "manual://note", "A note a person wrote", "manual")
        typed = as_person(remote_store, pid, {"kind": "decision", "content": "Typed by a person"}, [note["id"]])
        remote_rows, remote_head = counts(remote)
        assert remote_rows >= 1 and counts(ns.local)[0] == local_before  # the local store was left as it was

        backup = ns.tmp / "from-remote.cairnmind.json.gz"
        back = setup_profiles.Request("local", backup=str(backup))
        state, text = ns.change(back)
        assert state == "changed", text
        local = settled(ns.data, remote=False)
        assert backup.exists() and "Restored" in text
        row = Store(ns.data, settings=local).query("SELECT FROM records WHERE id=:id", {"id": typed["id"]})[0]
        if proof == "key":
            assert confirmation_of(row)["by"] == "person"
        else:
            assert (confirmation_of(row)["by"], confirmation_of(row).get("package")) == ("import", "backup")
        assert counts(local)[0] == remote_rows and counts(remote) == (remote_rows, remote_head)
        assert len(list((ns.data / "backups").glob("before-restore-*"))) == 1
    ns.stop()


@pytest.mark.no_schema_template
def test_remote_db_back_to_local_with_empty(installation):
    ns = installation
    local_before = counts(ns.local)[0]
    with Relay(ns.port) as relay:
        ns.relay = relay
        assert ns.change(ns.remote_db())[0] == "changed"
        state, text = ns.change(setup_profiles.Request("local", empty=True))
        assert state == "changed", text
        local = settled(ns.data, remote=False)
        assert counts(local)[0] == local_before  # the store on this machine as it was left, with nothing from the other
        assert "Storage: ArcadeDB on this machine" in text
    ns.stop()


@pytest.mark.no_schema_template
def test_remote_db_to_another_database_of_the_same_server(installation):
    ns = installation
    with Relay(ns.port) as relay:
        ns.relay = relay
        assert ns.change(ns.remote_db())[0] == "changed"
        state, text = ns.change(ns.remote_db(ns.names[1]))
        assert state == "changed", text
        remote = settled(ns.data, remote=True)
        assert remote.arcade_database == ns.names[1]
        assert "Saved in" in text
    ns.stop()


@pytest.mark.no_schema_template
def test_a_restore_that_fails_leaves_the_service_running_on_the_old_settings(installation, monkeypatch):
    ns = installation
    with Relay(ns.port) as relay:
        ns.relay = relay
        assert ns.change(ns.remote_db())[0] == "changed"
        before = Settings.load(ns.data)
        pid = service_pid(before)
        backup = ns.tmp / "kept.cairnmind.json.gz"
        request = setup_profiles.Request("local", backup=str(backup))
        expected = ns.agreed(request)

        def cut(store, path, claims=None):
            raise ArcadeError("cut")
        monkeypatch.setattr(Store, "restore", cut)
        state, text = ns.change(request, expected)
        assert state == "failed" and "stopped at the restore" in text and f"The backup {backup} is kept." in text
        assert "runs again with the settings from before the change" in text
        after = settled(ns.data, remote=True)  # still connect-only, on the same store
        assert (after.arcade_connect_url, after.arcade_database) == (before.arcade_connect_url, before.arcade_database)
        assert service_pid(after) != pid and backup.exists()
    ns.stop()


@pytest.mark.no_schema_template
def test_a_store_another_installation_writes_is_refused_and_the_service_keeps_running(installation, monkeypatch):
    ns = installation
    pid = service_pid(ns.local)
    other = data_directory(ns.tmp, "other", arcade_connect_url="", knowledge_agent="disabled")
    with Relay(ns.port) as relay:
        ns.relay = relay
        values = {"arcade_connect_url": relay.url, "arcade_user": "root", "arcade_database": ns.names[0],
                  "arcade_password_file": str(ns.password_file)}
        (other / "config.json").write_text(json.dumps({**json.loads((other / "config.json").read_text()), **values}))
        original = store_writer.machine_identity
        monkeypatch.setattr(store_writer, "machine_identity", lambda: "machine-id:the-other-machine")
        Store(other, settings=Settings.load(other))  # the other installation becomes the writer of that store
        monkeypatch.setattr(store_writer, "machine_identity", original)
        request = ns.remote_db()
        result = setup_profiles.run(Settings.load(ns.data), request, yes=False, say=lambda text: None)
        item = setup_change.target_refusal(result, request)
        assert item is not None and item[0] == setup_change.READER_TARGET
        state, text = ns.change(request, setup_change.agreed_plan(result))
        assert state == "refused" and "no longer what you agreed to" in text
        settings = settled(ns.data, remote=False)
        assert service_pid(settings) != pid  # it was stopped for the change and started again, on the old settings
    ns.stop()
