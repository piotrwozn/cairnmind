"""The change of where storage and models run, carried out for the workspace (#339).

No engine, no tmux, no Docker: the profiles' own World of test_setup_profiles stands in for the stores, the models and
the engine, and a fake stands in for the knowledge service. Every test runs unchanged on macOS: no test here reads
procfs but through process_identity, and no tmux is started.
"""
from __future__ import annotations

import json
import os
import string
import subprocess
import sys
import time
import types

import httpx
import pytest

from cairnmind import (cli, project_removal, service_version, setup_change, setup_mark, setup_profiles, store_writer,
                       upgrade_mark)
from cairnmind.config import Settings, is_own_variable
from cairnmind.setup_profiles import Request, render_one, run
from cairnmind.store import Store
from cairnmind.terminal_i18n import CATALOG, tr
from test_setup_profiles import (DB, MODELS, World, config, config_bytes, config_without_password,
                                 connect_only_installation, make, private)

PASSWORD = "PRIVATE_DB_MARKER"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in [key for key in os.environ if is_own_variable(key)]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("none", None))


class Rig:
    """The knowledge service, the stores and the models, replaced; every call is logged."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.data = tmp_path, tmp_path / "data"
        self.world = World(monkeypatch, self.data / "config.json")
        self.role = {"role": "writer"}        # what this installation is to the store it uses now
        self.world.writer = {"role": "writer"}  # and to the store a profile moves to
        self.alive = True                     # the fake service
        self.shutdowns, self.starts = [], []
        self.stop_answer, self.ignores_stop = {"stopping": True}, False
        self.start_error = None               # a function of the config.json bytes at the start, or None
        self.start_signal = None              # "gone": a signal ends the start and the process it spawned; "stays": not
        self.jobs, self.projects = [], []
        self.mark_during = []
        monkeypatch.setattr(cli, "service_alive", lambda settings: self.alive)
        monkeypatch.setattr(cli, "ensure_service", self.ensure)
        monkeypatch.setattr(service_version, "refusal", lambda settings: None)
        monkeypatch.setattr(setup_change, "httpx", types.SimpleNamespace(
            HTTPError=httpx.HTTPError, Client=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(self.shutdown))))
        monkeypatch.setattr(setup_change, "STOP_SECONDS", 0.3)
        monkeypatch.setattr(store_writer, "inspect", self.inspect)
        inner = Store.open_readonly
        rig = self

        class Rows:
            def __init__(self, reading):
                self.reading, self.client = reading, reading.client

            def query(self, sql, params=None):
                return list(rig.jobs if "FROM jobs" in sql else rig.projects)

            def backup(self, target):
                return self.reading.backup(target)
        monkeypatch.setattr(Store, "open_readonly", classmethod(lambda cls, settings: Rows(inner(settings))))

    def inspect(self, settings):
        now = json.loads((self.data / "config.json").read_text()).get("arcade_connect_url", "")
        return dict(self.role if (settings.arcade_connect_url or "") == now else self.world.writer)

    def shutdown(self, request):
        self.shutdowns.append(request.headers["authorization"])
        if not self.ignores_stop and self.stop_answer.get("stopping"):
            self.alive = False
        return httpx.Response(200, json=self.stop_answer)

    def ensure(self, settings, **kwargs):
        bytes_now = (self.data / "config.json").read_bytes()
        self.starts.append((bytes_now, kwargs, setup_mark.refusal(self.data), setup_mark.mark_path(self.data).exists()))
        if self.start_signal and json.loads(bytes_now).get("arcade_connect_url") == DB:
            self.alive = self.ignores_stop = self.start_signal == "stays"  # a spawned service that did (not) end
            raise KeyboardInterrupt
        if self.start_error and self.start_error(bytes_now):
            raise cli.ServiceDidNotStart("The service stopped at start: it did not like it.")
        self.alive = True
        return True

    def request(self, **fields):
        fields = {"url": DB, "user": "cairnmind", "database": "cairnmind",
                  "password_file": private(self.tmp, "db-password", PASSWORD), **fields}
        return Request("remote-db", **fields)

    def agreed(self, request):
        return setup_change.agreed_plan(run(Settings.load(self.data), request, yes=False, say=lambda text: None))

    def go(self, request, expected=None, language="en"):
        """carry_out; (the final state, the last lines)."""
        reports = []
        state = setup_change.carry_out(self.data, request, expected or self.agreed(request), language,
                                       lambda state, lines: reports.append((state, list(lines))))
        assert reports and reports[-1][0] == state
        return state, "\n".join(reports[-1][1])

    def untouched(self, before):
        """The installation as the change found it: its settings, no mark, no copy, a service that runs."""
        assert config_bytes(self.tmp) == before or config_without_password(self.tmp) == before
        assert not (self.data / setup_mark.MARK).exists() and not (self.data / setup_mark.BEFORE).exists()
        assert self.alive


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def said(text):
    return " ".join(text.split())


def registry(data, rows):
    directory = data / "runtime"
    directory.mkdir(exist_ok=True)
    path = directory / "runs.json"
    path.write_text(json.dumps({"version": 1, "runs": rows}))
    path.chmod(0o600)


def row(client, lifecycle, project="p1"):
    return {"id": "r-" + client, "client": client, "lifecycle": lifecycle, "project_id": project,
            "tmux_session": "s-" + client, "generation": 1}


# What refuses the change, by name ---------------------------------------------------------------------------------------

def test_a_live_session_refuses_the_change_by_name_and_nothing_stops(rig, monkeypatch):
    from cairnmind import client_runtime
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    registry(rig.data, [row("claude", "running"), row("codex", "exited")])
    rig.projects = [{"id": "p1", "name": "demo"}]
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: "tmux")
    monkeypatch.setattr(client_runtime.NativeRuntime, "_pane", lambda self, row: {"pane": "%1", "pid": 1, "dead": False})
    state, text = rig.go(rig.request(), expected={"plan": [], "writer": []})
    assert state == "refused" and "A session runs in the workspace (demo: claude running)" in said(text)
    assert rig.shutdowns == [] and rig.starts == [] and config_bytes(rig.tmp) == before
    rig.untouched(before)


def test_a_live_row_without_tmux_refuses(rig, monkeypatch):
    from cairnmind import client_runtime
    make(rig.tmp)
    registry(rig.data, [row("claude", "running")])
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: None)
    state, text = rig.go(rig.request(), expected={"plan": [], "writer": []})
    assert state == "refused" and "Whether a session runs in the workspace could not be read (tmux cannot be found)" in text
    assert rig.shutdowns == []


def test_a_job_in_work_refuses_the_change_by_name(rig):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    rig.jobs = [{"project_id": "p1", "kind": "ingest"}, {"project_id": "p1", "kind": "embed"}]
    rig.projects = [{"id": "p1", "name": "demo"}]
    state, text = rig.go(rig.request(), expected={"plan": [], "writer": []})
    assert state == "refused" and "Jobs are in work now (demo (embed, ingest))" in said(text)
    assert rig.shutdowns == []
    rig.untouched(before)


def test_a_store_that_cannot_be_read_for_jobs_refuses(rig, monkeypatch):
    make(rig.tmp)

    def unreadable(cls, settings):
        raise ValueError("no schema")
    monkeypatch.setattr(Store, "open_readonly", classmethod(unreadable))
    item = setup_change.refusal(Settings.load(rig.data))
    assert item[0] == setup_change.JOBS_UNKNOWN and "no schema" in item[1]["reason"]


@pytest.mark.parametrize("role, template", [
    ({"role": "reader", "reason": "Another installation writes it."}, setup_change.READER_HERE),
    ({"role": "lost", "reason": "It was taken over."}, setup_change.READER_HERE),
    ({"role": "unknown", "reason": "The store cannot be read."}, setup_change.WRITER_UNKNOWN),
    ({"role": "none"}, setup_change.WRITER_UNKNOWN),
    ({"role": "writer", "problem": "the journal head disagrees"}, setup_change.WRITER_UNKNOWN)])
def test_a_reader_and_an_unknown_writer_refuse(rig, role, template):
    make(rig.tmp)
    rig.role = role
    state, text = rig.go(rig.request(), expected={"plan": [], "writer": []})
    assert state == "refused" and rig.shutdowns == []
    assert setup_change.refusal(Settings.load(rig.data))[0] == template


def test_a_variable_and_a_coding_session_refuse(rig, monkeypatch):
    make(rig.tmp)
    monkeypatch.setenv("CAIRNMIND_ARCADE_USER", "x")
    assert setup_change.refusal(Settings.load(rig.data))[0] == setup_profiles.VARIABLE_SET
    monkeypatch.delenv("CAIRNMIND_ARCADE_USER")
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "s-1")
    assert setup_change.refusal(Settings.load(rig.data)) == (setup_profiles.IN_SESSION, {})
    state, _ = rig.go(rig.request(), expected={"plan": [], "writer": []})
    assert state == "refused" and rig.shutdowns == []


def test_another_change_an_upgrade_and_a_service_of_another_version_refuse(rig, monkeypatch):
    make(rig.tmp)
    mark = setup_mark.mark_path(rig.data)
    mark.write_text("garbage")
    assert setup_change.refusal(Settings.load(rig.data))[0] == setup_mark.NOTE_UNREADABLE
    mark.unlink()
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("running", 4242))
    assert setup_change.refusal(Settings.load(rig.data)) == (upgrade_mark.NOTE_RUNNING, {"pid": 4242})
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("unreadable", None))
    assert setup_change.refusal(Settings.load(rig.data)) == (upgrade_mark.NOTE_UNREADABLE, {})
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("none", None))
    other = service_version.Sentence(service_version.OTHER_VERSION, service="0.1", client="0.2", command="cairnmind stop")
    monkeypatch.setattr(service_version, "refusal", lambda settings: other)
    assert setup_change.refusal(Settings.load(rig.data)) == (other.template, other.values)
    assert rig.shutdowns == []


def test_a_target_written_by_another_installation_is_refused_before_anything_stops(rig):
    make(rig.tmp)
    rig.world.writer = {"role": "reader", "reason": "Another installation writes it."}
    request = rig.request()
    result = run(Settings.load(rig.data), request, yes=False, say=lambda text: None)
    item = setup_change.target_refusal(result, request)
    assert item == (setup_change.READER_TARGET, {"url": DB, "profile": "remote-db"})
    assert "Another installation writes the store at" in render_one(item) and "remote-db" in render_one(item)
    rig.world.writer = {"role": "writer"}
    assert setup_change.target_refusal(run(Settings.load(rig.data), request, yes=False, say=lambda t: None), request) is None
    # A writer that changed after the agreement is refused by the plan check, with nothing changed.
    before = config_bytes(rig.tmp)
    expected = rig.agreed(request)
    rig.world.writer = {"role": "reader", "reason": "Another installation writes it."}
    state, text = rig.go(request, expected=expected)
    assert state == "refused" and "no longer what you agreed to" in said(text)
    rig.untouched(before)


# The change ---------------------------------------------------------------------------------------------------------------

def test_a_change_stops_the_service_changes_the_settings_and_starts_it_with_the_mark_in_place(rig):
    make(rig.tmp)
    before = config(rig.tmp)
    request = rig.request()
    state, text = rig.go(request)
    assert state == "changed", text
    assert rig.shutdowns == [f"Bearer {Settings.load(rig.data).token}"]
    assert len(rig.starts) == 1 and rig.starts[0][1] == {"background_inference": False}
    assert json.loads(rig.starts[0][0])["arcade_connect_url"] == DB  # started on the new settings
    assert rig.starts[0][2] is None and rig.starts[0][3] is True  # the mark was held: its own start is not refused
    after = config(rig.tmp)
    assert {name for name in after if after[name] != before.get(name)} == {
        "arcade_connect_url", "arcade_user", "arcade_database", "arcade_password_file"}
    assert "Stopping the knowledge service" in text and "The knowledge service stopped." in text
    assert "Storage: ArcadeDB at" in text and text.rstrip().endswith("the panels reconnect by themselves.")
    assert "Next:" not in text  # setup's own next steps name `init` and a start already made
    assert not (rig.data / setup_mark.MARK).exists() and not (rig.data / setup_mark.BEFORE).exists() and rig.alive


FAILURES = {
    "declined": "The knowledge service answered but did not stop",
    "timeout": "did not stop within 30 seconds",
    "run_after_stop": "A session runs in the workspace",
    "test_after_stop": "cannot be used",
    "plan_changed": "no longer what you agreed to",
    "model_save": "stopped at saving the chat model",
    "restore": "stopped at the restore",
    "start_new": "did not start with the new settings",
}


@pytest.mark.parametrize("failure", list(FAILURES))
def test_a_failure_at_each_step_leaves_a_running_service_on_the_old_settings_and_no_mark(rig, monkeypatch, failure):
    if failure == "restore":
        connect_only_installation(rig.tmp)
        request = Request("local", backup=str(rig.tmp / "kept.cairnmind.json.gz"))
    elif failure == "model_save":
        make(rig.tmp, knowledge_agent="enabled", chat_model="gpt-x",
             memory_chat={"provider": "openai_compatible", "base_url": MODELS, "allow_remote": True})
        request = rig.request()
    else:
        make(rig.tmp)
        request = rig.request()
    before = config_bytes(rig.tmp)
    expected = rig.agreed(request)
    if failure == "declined":
        rig.stop_answer = {"stopping": False}
    elif failure == "timeout":
        rig.ignores_stop = True
        clock = iter(range(0, 10_000, 10))
        monkeypatch.setattr(setup_change, "STOP_SECONDS", 30)
        monkeypatch.setattr(setup_change, "time", types.SimpleNamespace(
            monotonic=lambda: next(clock), sleep=lambda seconds: None, time=time.time))
    elif failure == "run_after_stop":
        calls = []
        monkeypatch.setattr(project_removal, "live_runs", lambda settings, project_id=None: (
            calls.append(1) or ([] if len(calls) == 1 else [row("claude", "starting")])))
    elif failure == "test_after_stop":
        rig.world.block = {"available": False, "error": "refused", "database_available": False}
    elif failure == "plan_changed":
        expected = {"plan": ["something else"], "writer": expected["writer"]}
    elif failure == "model_save":
        rig.world.checked.clear()  # the dry run's test is not counted: the failing one is the check of the save
        rig.world.second_check_errors["chat"] = ValueError("the server changed its mind")
    elif failure == "restore":
        rig.world.restore_error = setup_profiles.ArcadeError("cut")
    elif failure == "start_new":
        rig.start_error = lambda data: json.loads(data).get("arcade_connect_url") == DB
    state, text = rig.go(request, expected=expected)
    assert FAILURES[failure] in said(text), text
    assert state == ("failed" if failure in ("model_save", "restore", "start_new") else "refused")
    assert "The knowledge service runs again with the settings from before the change." in text
    assert rig.starts  # the service was brought back, on the settings config.json holds
    assert config_bytes(rig.tmp) == before or config_without_password(rig.tmp) == before
    rig.untouched(before)
    assert not any(json.loads(bytes_now).get("arcade_connect_url") == DB for bytes_now, *_ in rig.starts[-1:]) \
        or failure == "restore"


def test_when_the_service_cannot_start_again_the_outcome_names_the_command(rig):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    request = rig.request()
    expected = {"plan": ["something else"], "writer": []}
    rig.start_error = lambda data: True
    state, text = rig.go(request, expected=expected)
    command = f"cairnmind --data-dir {rig.data} serve"
    assert state == "service_down" and "could not be started again" in text and f"`{command}`" in said(text)
    assert config_bytes(rig.tmp) == before and not (rig.data / setup_mark.MARK).exists()
    assert not (rig.data / setup_mark.BEFORE).exists()  # the settings are the old ones, so no copy is needed
    assert setup_change.EXIT[state] == 3


def test_a_configuration_that_cannot_be_put_back_keeps_the_copy_and_starts_nothing(rig, monkeypatch):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    request = rig.request()
    rig.start_error = lambda data: json.loads(data).get("arcade_connect_url") == DB
    expected = rig.agreed(request)

    def cannot(path, data):
        raise OSError("read-only file system")
    monkeypatch.setattr(setup_profiles, "_put_back", cannot)
    state, text = rig.go(request, expected=expected)
    copy = rig.data / setup_mark.BEFORE
    assert state == "service_down" and "could NOT be put back (read-only file system)" in text
    assert f"The settings from before the change are kept in {copy}" in text
    assert copy.read_bytes() == before and len(rig.starts) == 1  # nothing was started on a configuration nobody checked
    assert not (rig.data / setup_mark.MARK).exists()
    # The mark is gone, but the copy blocks every automatic start and doctor counts it, so the panels that reconnect
    # as the dialog closes cannot start the service on settings nobody checked.
    assert str(setup_mark.refusal(rig.data)) == setup_mark.NOTE_COPY.format(copy=copy)
    assert setup_mark.unchecked(rig.data) is not None


@pytest.mark.parametrize("what", ["gone", "stays"])
def test_a_signal_during_the_start_on_the_new_settings_leaves_no_service_on_settings_that_are_not_config_json(
        rig, monkeypatch, what):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    request = rig.request()
    expected = rig.agreed(request)
    rig.start_signal = what
    monkeypatch.setattr(setup_change, "_signal", [15])
    state, text = rig.go(request, expected=expected)
    copy = rig.data / setup_mark.BEFORE
    assert len(rig.starts) == 1 and json.loads(rig.starts[0][0])["arcade_connect_url"] == DB
    if what == "gone":
        # The spawned service is gone: config.json is put back, the outcome says so and says what was not started.
        assert state == "interrupted" and "config.json was put back as it was" in said(text) and "interrupted (signal 15)" in said(text)
        assert config_bytes(rig.tmp) == before and not copy.exists() and not rig.alive
        assert not (rig.data / setup_mark.MARK).exists()
    else:
        # The spawned service could not be ended: config.json is not put back under it, and the copy is kept.
        assert state == "service_down" and "could NOT be put back" in said(text) and f"kept in {copy}" in said(text)
        assert json.loads((rig.data / "config.json").read_bytes())["arcade_connect_url"] == DB and copy.read_bytes() == before
        assert setup_mark.refusal(rig.data) is not None  # nothing else starts a service meanwhile


def test_an_interrupt_that_comes_where_nothing_can_be_put_back_keeps_the_copy_and_says_so(rig, monkeypatch):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    request = rig.request()
    rig.start_error = lambda data: json.loads(data).get("arcade_connect_url") == DB
    expected = rig.agreed(request)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt  # in the put-back itself
    monkeypatch.setattr(setup_profiles, "put_back", interrupted)
    state, text = rig.go(request, expected=expected)
    copy = rig.data / setup_mark.BEFORE
    assert state == "interrupted" and f"kept in {copy}" in said(text)
    assert copy.read_bytes() == before and not (rig.data / setup_mark.MARK).exists()
    assert setup_mark.refusal(rig.data) is not None  # config.json is new and unchecked: nothing starts the service


def test_an_error_after_the_apply_is_put_back_and_said_as_what_it_is(rig, monkeypatch):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    request = rig.request()
    expected = rig.agreed(request)
    monkeypatch.setattr(setup_profiles.topology, "topology", lambda settings: (_ for _ in ()).throw(RuntimeError("boom")))
    state, text = rig.go(request, expected=expected)
    assert state == "failed" and "boom" in text and "put back" in said(text) and "Nothing was changed" not in text
    assert "The knowledge service runs again with the settings from before the change." in text
    rig.untouched(before)  # config.json as it was, a service on it, no mark, no copy
    assert json.loads(rig.starts[-1][0]).get("arcade_connect_url") != DB


def test_the_service_lock_is_waited_for_and_held(rig, monkeypatch):
    """_stop does not call the service gone while its lock is held; _apply never changes anything under a service
    that started again (a `cairnmind serve` in a terminal does not read the mark, so the lock is the only guard)."""
    import fcntl
    make(rig.tmp)
    settings = Settings.load(rig.data)
    rig.alive = False
    other = (rig.data / "service.lock").open("a")
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert setup_change._stop(settings) == (setup_change.STOP_TIMEOUT, {"seconds": setup_change.STOP_SECONDS})
        before = config_bytes(rig.tmp)
        assert setup_change._apply(settings, rig.request(), {"plan": [], "writer": []}, None) == (
            "refused", (setup_change.SERVICE_BACK, {}))
        assert config_bytes(rig.tmp) == before
    finally:
        other.close()
    assert setup_change._stop(settings) is None


def test_a_signal_puts_the_configuration_back_and_starts_nothing(rig, monkeypatch):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    request = rig.request()
    expected = rig.agreed(request)
    saved = setup_profiles.save_storage

    def interrupted(data_dir, fields):
        saved(data_dir, fields)
        raise KeyboardInterrupt  # what the helper's signal handler raises, in the middle of the change
    monkeypatch.setattr(setup_profiles, "save_storage", interrupted)
    monkeypatch.setattr(setup_change, "_signal", [15])
    state, text = rig.go(request, expected=expected)
    assert state == "interrupted" and "interrupted (signal 15)" in said(text) and setup_change.EXIT[state] == 130
    assert config_bytes(rig.tmp) == before and rig.starts == [] and not rig.alive  # not started: it was asked to end
    assert not (rig.data / setup_mark.MARK).exists() and not (rig.data / setup_mark.BEFORE).exists()


def test_an_unexpected_error_before_the_change_changes_nothing_and_the_service_comes_back(rig, monkeypatch):
    make(rig.tmp)
    before = config_bytes(rig.tmp)
    monkeypatch.setattr(setup_profiles, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    state, text = rig.go(rig.request(), expected={"plan": [], "writer": []})
    assert state == "refused" and "The change could not be made: boom config.json was not changed." in said(text)
    rig.untouched(before)


def test_a_second_change_is_refused_by_its_lock(rig):
    make(rig.tmp)
    with setup_mark.held(rig.data):
        state, text = rig.go(rig.request(), expected={"plan": [], "writer": []})
    assert state == "refused" and "Another change of where storage and models run is in progress" in text


def test_the_plan_check_allows_a_user_created_by_the_change_and_never_another_writer():
    keep, new, unread, other = (setup_profiles.WRITER_KEEP, setup_profiles.WRITER_NEW, setup_profiles.WRITER_NOT_READ,
                                setup_profiles.WRITER_OTHER)
    same = setup_change._writer_same
    assert same([unread], [new]) and same([unread], [keep]) and same([keep], [keep])
    assert not same([new], [keep]) and not same([keep], [other]) and not same([unread], [other])
    assert not same([unread, setup_profiles.VERBATIM], [new])


# The helper's input ---------------------------------------------------------------------------------------------------

def payload(**changes):
    value = {"request": {"profile": "remote-db", "url": DB, "user": "u", "database": "d", "password_file": "/x"},
             "expected": {"plan": ["a"], "writer": ["b"]}, "language": "en"}
    value.update(changes)
    return json.dumps(value).encode()


def test_the_request_on_standard_input_is_validated():
    request, expected, language = setup_change._parsed(payload())
    assert request.profile == "remote-db" and request.url == DB and expected == {"plan": ["a"], "writer": ["b"]}
    assert language == "en"
    bad = [b"not json", b"[]", payload(language="de"), payload(extra=1),
           payload(request={"profile": "elsewhere"}), payload(request={"profile": "local", "nope": "x"}),
           payload(request={"profile": "local", "allow_remote": "yes"}), payload(request={"profile": "local", "backup": 5}),
           payload(request={"profile": "local", "backup": "a\0b"}), payload(request={"profile": "local", "backup": "x" * 4097}),
           payload(request={"profile": None}), payload(expected={"plan": "a", "writer": []}),
           payload(expected={"plan": ["a"] * 201, "writer": []}), payload(expected={"plan": [5], "writer": []}),
           payload(expected={"plan": []}),
           # the rules of the command line, which `run` does not apply itself
           payload(request={"profile": "local", "backup": "/x", "empty": True}),
           payload(request={"profile": "remote-db", "url": DB, "user": "u", "database": "d", "password_file": "/x",
                            "chat_model": "m"}),
           payload(request={"profile": "remote-db", "url": DB}), payload(request={"profile": "local", "url": DB})]
    for raw in bad:
        with pytest.raises(ValueError):
            setup_change._parsed(raw)


def test_progress_is_read_only_from_the_helper_that_wrote_it(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    assert setup_change.read_progress(tmp_path, 7) is None
    path = tmp_path / setup_change.PROGRESS
    path.write_text(json.dumps({"pid": 7, "state": "running", "language": "pl", "lines": ["a"]}))
    assert setup_change.read_progress(tmp_path, 7)["lines"] == ["a"] and setup_change.read_progress(tmp_path, 8) is None
    for broken in ({"pid": 7, "state": "done", "language": "pl", "lines": []},
                   {"pid": 7, "state": "running", "language": "de", "lines": []},
                   {"pid": 7, "state": "running", "language": "pl", "lines": [1]}, [], "text"):
        path.write_text(json.dumps(broken))
        assert setup_change.read_progress(tmp_path, 7) is None
    path.write_text("{")
    assert setup_change.read_progress(tmp_path, 7) is None


def test_no_secret_reaches_the_command_line_the_input_or_the_progress(rig, monkeypatch):
    make(rig.tmp)
    admin = private(rig.tmp, "admin-password", "ADMIN_MARKER")
    request = rig.request(admin_password_file=admin)
    settings = Settings.load(rig.data)
    captured = {}

    class Process:
        stdin = types.SimpleNamespace(write=lambda data: captured.setdefault("input", data), close=lambda: None)
    monkeypatch.setattr(setup_change.subprocess, "Popen",
                        lambda command, **kwargs: captured.update(command=command, kwargs=kwargs) or Process())
    setup_change.begin(settings, request, rig.agreed(request), "en")
    state, text = rig.go(request)
    assert state == "changed", text
    log, progress = rig.data / setup_change.LOG, rig.data / setup_change.PROGRESS
    for marker in (PASSWORD, "ADMIN_MARKER"):
        assert marker not in " ".join(captured["command"]) and marker.encode() not in captured["input"]
        assert marker not in text and marker not in "".join(p.read_text() for p in (log, progress) if p.exists())
        assert marker not in json.dumps(config(rig.tmp))
    assert json.loads(captured["input"])["request"]["password_file"].endswith("db-password")  # a path, not a password


def test_begin_starts_a_detached_helper_with_a_clean_environment(rig, monkeypatch):
    make(rig.tmp)
    for name, value in (("TMUX", "/tmp/tmux-1/default,1,0"), ("TMUX_PANE", "%1"), ("CAIRNMIND_SESSION_ID", "s"),
                        ("CAIRNMIND_CLIENT", "claude"), ("CAIRNMIND_ARCADE_URL_NOT_THIS", "kept")):
        monkeypatch.setenv(name, value)
    seen = {}
    written = []

    class Process:
        stdin = types.SimpleNamespace(write=written.append, close=lambda: written.append("closed"))
    monkeypatch.setattr(setup_change.subprocess, "Popen",
                        lambda command, **kwargs: seen.update(command=command, kwargs=kwargs) or Process())
    request = rig.request()
    setup_change.begin(Settings.load(rig.data), request, {"plan": ["a"], "writer": []}, "pl")
    assert seen["command"] == [sys.executable, "-I", "-m", "cairnmind.setup_change", "--data-dir", str(rig.data)]
    kwargs = seen["kwargs"]
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.PIPE and kwargs["cwd"] == rig.data
    assert "TMUX" not in kwargs["env"] and "TMUX_PANE" not in kwargs["env"]
    assert "CAIRNMIND_SESSION_ID" not in kwargs["env"] and "CAIRNMIND_CLIENT" not in kwargs["env"]
    assert kwargs["env"]["CAIRNMIND_ARCADE_URL_NOT_THIS"] == "kept"
    assert (rig.data / setup_change.LOG).stat().st_mode & 0o777 == 0o600
    assert json.loads(written[0])["language"] == "pl" and written[1] == "closed"


def test_the_helper_reports_a_refusal_through_its_progress_file(tmp_path):
    """A real helper process: an unknown profile on standard input is refused and nothing starts."""
    data = tmp_path / "data"
    Settings.load(data, create=True)
    environment = {key: value for key, value in os.environ.items() if not is_own_variable(key)}
    done = subprocess.run([sys.executable, "-I", "-m", "cairnmind.setup_change", "--data-dir", str(data)],
                          input=payload(request={"profile": "elsewhere"}), capture_output=True, timeout=60,
                          env=environment, cwd=tmp_path)
    assert done.returncode == 2, done.stderr
    found = json.loads((data / setup_change.PROGRESS).read_text())
    assert found["state"] == "refused" and found["pid"] > 0
    assert found["lines"] == [setup_change.REQUEST_INVALID.format(reason="request")]
    assert not (data / setup_mark.MARK).exists() and not (data / "service.log").exists()
    missing = subprocess.run([sys.executable, "-I", "-m", "cairnmind.setup_change", "--data-dir", str(tmp_path / "none")],
                             input=b"", capture_output=True, timeout=60, env=environment, cwd=tmp_path)
    assert missing.returncode == 2 and b"no Cairnmind configuration" in missing.stderr


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP", "SIGINT"])
def test_a_signal_sent_to_a_real_helper_ends_it_through_the_handlers_with_status_130(tmp_path, name):
    """What closing the terminal (SIGHUP), `kill` (SIGTERM) and Ctrl-C (SIGINT) do to the helper: the handlers turn them
    into KeyboardInterrupt, so the change ends in its own words with the mark removed; without them the process would
    die on the signal. The change itself is replaced by a wait (nothing is stopped or started, no engine, no service)."""
    import signal
    data = tmp_path / "data"
    Settings.load(data, create=True)
    before = (data / "config.json").read_bytes()
    code = ("import sys, time\n"
            "from cairnmind import setup_change, setup_profiles\n"
            "setup_change.refusal = lambda settings: None\n"
            "setup_change._stop = lambda settings: None\n"
            "setup_change._runs = lambda settings: None\n"
            "def waits(*args, **kwargs):\n"
            "    print('ready', flush=True)\n"
            "    time.sleep(60)\n"
            "setup_profiles.run = waits\n"
            "sys.exit(setup_change.main(['--data-dir', sys.argv[1]]))\n")
    environment = {key: value for key, value in os.environ.items() if not is_own_variable(key)}
    helper = subprocess.Popen([sys.executable, "-c", code, str(data)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              env=environment, cwd=tmp_path)
    try:
        helper.stdin.write(payload(request={"profile": "local"}, expected={"plan": [], "writer": []}))
        helper.stdin.close()
        assert helper.stdout.readline().strip() == b"ready"  # inside the change: the mark and the copy are in place
        assert (data / setup_mark.MARK).exists() and (data / setup_mark.BEFORE).exists()
        helper.send_signal(getattr(signal, name))
        assert helper.wait(timeout=30) == 130
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait()
    found = json.loads((data / setup_change.PROGRESS).read_text())
    assert found["state"] == "interrupted" and found["pid"] == helper.pid
    assert f"signal {getattr(signal, name).value}" in " ".join(found["lines"])
    assert (data / "config.json").read_bytes() == before
    assert not (data / setup_mark.MARK).exists() and not (data / setup_mark.BEFORE).exists()


# Sentences ----------------------------------------------------------------------------------------------------------------

def test_progress_sentences_are_said_in_polish():
    from cairnmind import engine_docker
    samples = {setup_profiles.BACKING_UP: {"url": "https://db.example", "path": "/b.json.gz"},
               setup_profiles.ENGINE_STARTING: {}, setup_profiles.RESTORING: {"path": "/b.json.gz"},
               engine_docker.CHOSE_DOCKER: {"config": "/d/config.json"},
               engine_docker.CHOSE_NATIVE: {"java": "/usr/bin/java", "config": "/d/config.json"},
               engine_docker.KEPT_NATIVE: {"config": "/c"}}
    assert set(samples) == set(setup_change.SAY_TEMPLATES)
    for template, values in samples.items():
        english = template.format(**values)
        assert setup_change.said_in(english, "en") == english
        polish = setup_change.said_in(english, "pl")
        assert polish == tr(template, "pl", **values) and polish != english
    assert setup_change.said_in("Something setup said that is not listed.", "pl") == "Something setup said that is not listed."


def test_every_new_sentence_has_polish_with_the_same_placeholders():
    names = {name for name, value in vars(setup_change).items() if isinstance(value, str) and name.isupper() and " " in value}
    sentences = {getattr(setup_change, name) for name in names}
    assert len(sentences) >= 25 and sentences == set(setup_change.MESSAGES)
    for english in sentences:
        def holes(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}
        polish = setup_change.MESSAGES[english]
        assert polish != english and holes(polish) == holes(english), english
        assert CATALOG[english] == polish


def test_the_whole_outcome_reads_in_polish(rig):
    make(rig.tmp)
    state, text = rig.go(rig.request(), language="pl")
    assert state == "changed" and "Zatrzymywanie usługi wiedzy" in text and "Usługa wiedzy zatrzymana." in text
    assert "Zmiana jest wprowadzona" in text
    for english in ("Stopping the", "The knowledge service stopped", "The change is made", "Tested:"):
        assert english not in text
