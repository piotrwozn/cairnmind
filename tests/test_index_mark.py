"""A start that is building a lookup index for longer than a minute says so instead of "did not start" (#422)."""
from __future__ import annotations

from contextlib import contextmanager
import json
import string
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cairnmind import cli, index_mark, store as store_module, upgrade_mark
from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.store import Store
from cairnmind.terminal_i18n import CATALOG

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
