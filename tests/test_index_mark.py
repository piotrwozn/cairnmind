"""A start that is building a lookup index for longer than a minute says so instead of "did not start" (#422)."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import string
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from textual.widgets import Static
from typer.testing import CliRunner

from cairnmind import cli, index_mark, process_identity, store as store_module, upgrade_mark
from cairnmind.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings
from cairnmind.store import Store
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_panels import SidebarApp

BUILDER = ["-c", "import time; time.sleep(30)", "cairnmind"]


@pytest.fixture
def children():
    started = []

    def start(*arguments):
        child = subprocess.Popen([sys.executable, *arguments])
        started.append(child)
        return child

    yield start
    for child in started:
        child.kill()
        child.wait()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_PORT", "1")  # nothing here may reach the person's own service
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("none", None))  # nor the mark of a real installer
    return Settings.load(tmp_path / "data", create=True)


def waiting_for_a_start(settings, monkeypatch, *, answers_after):
    """_wait_for_start with a service that answers once `answers_after` polls have passed; no real sleeping."""
    polls = []
    monkeypatch.setattr(cli, "START_POLLS", 3)
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=lambda seconds: polls.append(1)))
    monkeypatch.setattr(cli, "service_alive", lambda _: len(polls) >= answers_after)
    return polls


def test_a_command_past_the_ordinary_wait_says_the_store_is_prepared_once_and_goes_on_when_it_ends(
        settings, children, monkeypatch, capsys):
    builder = children(*BUILDER)
    index_mark.mark_path(settings.data_dir).write_text(f"{builder.pid}\n")
    polls = waiting_for_a_start(settings, monkeypatch, answers_after=12)
    real = index_mark.note

    def note(data_dir):  # the build ends at the 10th poll: its mark goes
        if len(polls) >= 10:
            index_mark.mark_path(data_dir).unlink(missing_ok=True)
        return real(data_dir)
    monkeypatch.setattr(index_mark, "note", note)
    assert cli._wait_for_start(settings, SimpleNamespace(poll=lambda: None), settings.data_dir / "log", 0) is True
    assert len(polls) >= 12 > 3  # past the (patched) ordinary limit
    said = capsys.readouterr().err
    assert said.count(index_mark.NOTE_RUNNING.format(pid=builder.pid)) == 1  # once, not at every poll
    assert "did not start" not in said


def test_a_build_that_is_never_over_ends_the_wait_with_the_old_sentence_once_its_mark_is_gone(
        settings, children, monkeypatch):
    builder = children(*BUILDER)
    mark = index_mark.mark_path(settings.data_dir)
    mark.write_text(f"{builder.pid}\n")
    polls = waiting_for_a_start(settings, monkeypatch, answers_after=10 ** 9)
    real = index_mark.note

    def note(data_dir):
        if len(polls) >= 6:
            mark.unlink(missing_ok=True)
        return real(data_dir)
    monkeypatch.setattr(index_mark, "note", note)
    with pytest.raises(cli.ServiceDidNotStart, match="The service did not start"):
        cli._wait_for_start(settings, SimpleNamespace(poll=lambda: None), settings.data_dir / "log", 0)
    assert 6 < len(polls) <= 6 + 3 + 1  # the ordinary wait begins anew when the mark goes


def test_a_stale_mark_is_ignored_and_removed(settings, children, monkeypatch, capsys):
    builder = children(*BUILDER)
    builder.kill()
    builder.wait()
    mark = index_mark.mark_path(settings.data_dir)
    mark.write_text(f"{builder.pid}\n")
    polls = waiting_for_a_start(settings, monkeypatch, answers_after=10 ** 9)
    with pytest.raises(cli.ServiceDidNotStart, match="The service did not start"):
        cli._wait_for_start(settings, SimpleNamespace(poll=lambda: None), settings.data_dir / "log", 0)
    assert len(polls) == 3 and not mark.exists()
    assert "being prepared" not in capsys.readouterr().err


def test_status_and_doctor_name_the_build_while_it_runs(settings, children, monkeypatch):
    builder = children(*BUILDER)
    index_mark.mark_path(settings.data_dir).write_text(f"{builder.pid}\n")
    sentence = index_mark.NOTE_RUNNING.format(pid=builder.pid)
    assert cli._service_unavailable(settings) == sentence  # what `status` says without a service
    from cairnmind import integrations
    monkeypatch.setattr(integrations, "doctor", lambda settings: {})
    result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "doctor"])
    assert sentence in [" ".join(line.split()) for line in json.loads(result.output)["environment_notes"]]


def fake_store(tmp_path, command):
    @contextmanager
    def patient(seconds):
        yield

    @contextmanager
    def transaction():
        yield SimpleNamespace(query=lambda *a, **k: [])
    return SimpleNamespace(data_dir=tmp_path, client=SimpleNamespace(
        patient=patient, command=command, transaction=transaction))


def test_the_mark_stands_while_the_indexes_are_built_and_goes_when_it_ends(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(store_module, "insert_document", lambda *a, **k: None)
    fake = fake_store(tmp_path, lambda sql, params=None: seen.append(index_mark.mark_path(tmp_path).exists()))
    Store._lookup_indexes(fake, None)
    assert seen and all(seen) and not index_mark.mark_path(tmp_path).exists()
    assert index_mark.state(tmp_path) == ("none", None)


def test_a_failed_build_removes_the_mark_and_still_prints_the_warning(tmp_path, capsys):
    def command(sql, params=None):
        raise ArcadeError("Local ArcadeDB is unavailable (ReadTimeout)")
    Store._lookup_indexes(fake_store(tmp_path, command), None)
    assert not index_mark.mark_path(tmp_path).exists()
    assert "could not be built (Local ArcadeDB is unavailable (ReadTimeout))" in capsys.readouterr().err


def test_a_store_with_nothing_to_build_writes_no_mark(tmp_path, monkeypatch):
    store = Store(tmp_path / "memory")  # the first start builds every index and records the set
    built = []
    monkeypatch.setattr(index_mark, "building", lambda data_dir: built.append(data_dir) or pytest.fail("a mark"))
    Store(tmp_path / "memory")
    assert not built and not index_mark.mark_path(store.data_dir).exists()


def test_the_sentence_has_polish_with_the_same_placeholders():
    names = lambda text: {field for _, field, _, _ in string.Formatter().parse(text) if field}  # noqa: E731
    polish = index_mark.MESSAGES[index_mark.NOTE_RUNNING]
    assert names(polish) == names(index_mark.NOTE_RUNNING) == {"pid"}
    assert CATALOG[index_mark.NOTE_RUNNING] == polish and "indeksu" in polish
    bridge = index_mark.MESSAGES[index_mark.BRIDGE_INDEX]  # what the MCP bridge adds to it (#425)
    assert names(bridge) == names(index_mark.BRIDGE_INDEX) == {"reason"} and CATALOG[index_mark.BRIDGE_INDEX] == bridge


def test_a_mark_is_held_only_by_a_process_that_is_cairnmind_itself(settings, children):
    mine = children("-c", "import time; time.sleep(30)", "cairnmind")
    other = children("-c", "import time; time.sleep(30)", "/home/x/.local/share/cairnmind/service.log")  # a pager's
    for child, kind in ((mine, "running"), (other, "stale")):
        index_mark.mark_path(settings.data_dir).write_text(f"{child.pid}\n")
        assert index_mark.state(settings.data_dir) == (kind, child.pid)


def test_a_mark_names_the_start_of_its_holder_and_a_reused_process_id_is_not_the_holder(settings, children):
    child = children(*BUILDER)
    mark = index_mark.mark_path(settings.data_dir)
    birth = process_identity.start(child.pid)
    mark.write_text(f"{child.pid}\n{birth}\n")  # the same id and the same start: the holder
    assert index_mark.state(settings.data_dir) == ("running", child.pid)
    assert index_mark.note(settings.data_dir) == index_mark.NOTE_RUNNING.format(pid=child.pid)
    mark.write_text(f"{child.pid}\n{birth}9\n")  # a living cairnmind process with that id, started at another time
    assert index_mark.state(settings.data_dir) == ("stale", child.pid)
    assert index_mark.note(settings.data_dir) is None and not mark.exists()
    mark.write_text(f"{child.pid}\n")  # a mark of an older version has no identity: the rule it had
    assert index_mark.state(settings.data_dir) == ("running", child.pid)


def test_the_builder_writes_the_start_of_its_process_beside_its_id(tmp_path):
    with index_mark.building(tmp_path):
        assert index_mark.mark_path(tmp_path).read_text().split() == [str(os.getpid()),
                                                                      process_identity.start(os.getpid())]
    assert not index_mark.mark_path(tmp_path).exists()


def test_a_real_build_held_back_is_said_and_waited_for_and_a_second_open_builds_nothing(
        tmp_path, monkeypatch, capsys):
    from test_lookup_indexes import as_0_12_0_left_it, marker
    store = Store(tmp_path / "memory")
    monkeypatch.setenv("CAIRNMIND_PORT", "1")  # nothing here may reach the person's own service
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("none", None))
    settings = Settings.load(store.data_dir, create=True)  # the data directory the store builds in
    as_0_12_0_left_it(store)
    # This process stands for the builder, which runs as `python -m cairnmind`.
    monkeypatch.setattr(upgrade_mark.process_identity, "command_line", lambda pid: [sys.executable, "-m", "cairnmind"])
    started, release, sent = threading.Event(), threading.Event(), []
    real = ArcadeClient.command

    def held(self, sql, params=None):
        sent.append((threading.current_thread().name, sql))
        if sql.startswith("CREATE INDEX IF NOT EXISTS ON nodes(kind)"):
            started.set()
            assert release.wait(60)
        return real(self, sql, params)
    monkeypatch.setattr(ArcadeClient, "command", held)
    first = threading.Thread(target=Store, args=(tmp_path / "memory",), name="first")
    second = threading.Thread(target=Store, args=(tmp_path / "memory",), name="second")
    first.start()
    assert started.wait(60)
    second.start()  # meanwhile: waits for the first one's build and builds nothing itself
    polls = []

    def sleep(seconds):
        polls.append(1)
        if len(polls) == 8:
            release.set()
        time.sleep(0.05)
    monkeypatch.setattr(cli, "START_POLLS", 3)
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(cli, "service_alive", lambda _: not first.is_alive() and not second.is_alive())
    try:
        assert cli._wait_for_start(settings, SimpleNamespace(poll=lambda: None), settings.data_dir / "log", 0) is True
    finally:
        release.set()
        first.join(60)
        second.join(60)
    assert len(polls) >= 8 > 3  # it waited past the (patched) ordinary limit, for the build
    sentence = index_mark.NOTE_RUNNING.format(pid=os.getpid())
    assert capsys.readouterr().err.count(sentence) == 1
    assert any(name == "first" and "CREATE INDEX" in sql for name, sql in sent)
    assert not [sql for name, sql in sent if name == "second" and "CREATE INDEX" in sql]
    assert marker(store) == [store_module.LOOKUP_INDEX_SET] and not index_mark.mark_path(store.data_dir).exists()


@pytest.mark.asyncio
async def test_the_workspace_says_the_build_in_polish_while_the_broker_starts_and_clears_it_after(
        tmp_path, children, monkeypatch):
    from test_terminal_panels import setup, settle
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    settings.data_dir.mkdir()
    builder = children(*BUILDER)
    index_mark.mark_path(settings.data_dir).write_text(f"{builder.pid}\n")
    release = threading.Event()
    broker.settings, broker.ensure = settings, lambda: release.wait(30)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    shown = lambda: str(app.query_one("#panel-notice", Static).render())  # noqa: E731
    async with app.run_test(size=(120, 45)) as pilot:
        for _ in range(40):  # the panel looks every 2 s
            await asyncio.sleep(0.25)
            if shown():
                break
        assert shown() == tr(index_mark.NOTE_RUNNING, "pl", pid=builder.pid) and "indeksu" in shown()
        release.set()
        await settle(app, pilot)
        assert shown() == ""
