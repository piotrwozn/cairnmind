"""tmux 3.4+ is needed only to open the terminal workspace (#273); every other command works without it.

No test runs a real tmux server, the bare `cairnmind` command, the owner's data directory or port 8765: tmux is a
stand-in script on a PATH of its own that records its calls, and the data directory sets port 1, so a connection
a test forgot to fake is refused at once.
"""
import json
import os
import stat
import sys
import types

import pytest

from cairnmind import cli, first_start, platform_hints
from cairnmind.config import Settings
from cairnmind.terminal_i18n import tr

STATES = ["missing", "3.3a", "3.5a"]
SENTENCE_TAIL = "Without it, these commands work: cairnmind serve, cairnmind run, cairnmind mcp and cairnmind brief."


@pytest.fixture
def data(tmp_path):
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"port": 1, "arcade_url": "http://127.0.0.1:1"}))
    Settings.load(directory, create=True)
    return directory


@pytest.fixture
def tmux(tmp_path, monkeypatch):
    """Put one tmux state on a PATH of its own; returns a function that installs a state and one that lists calls."""
    calls = tmp_path / "calls"

    def install(state):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        stand_in = bin_dir / "tmux"
        stand_in.unlink(missing_ok=True)
        if state != "missing":
            stand_in.write_text(f"#!/bin/sh\necho \"$*\" >> \"$TMUX_STAND_IN_CALLS\"\necho 'tmux {state}'\n")
            stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setenv("TMUX_STAND_IN_CALLS", str(calls))
        for name in ("CAIRNMIND_TMUX_EXECUTABLE", "DEVBRAIN_TMUX_EXECUTABLE", "CAIRNMIND_SESSION_ID",
                     "CAIRNMIND_PACKET_ID", "DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"):
            monkeypatch.delenv(name, raising=False)

    install.calls = lambda: calls.read_text().splitlines() if calls.exists() else []
    return install


def test_cockpit_status_words_each_state():
    status = first_start.cockpit_status
    assert status({"code": "ready", "version": "tmux 3.5a"}) is None
    assert status({"code": "tmux_missing"}) == "unavailable (tmux 3.4+ not found)"
    assert status({"code": "tmux_unsupported", "version": "tmux 3.3a\n"}) == (
        "unavailable (tmux 3.4+ not found; found tmux 3.3a)")
    assert status({"code": "tmux_unavailable"}) == "unavailable (tmux 3.4+ not found; tmux_unavailable)"
    assert status({"code": "tmux_invalid_executable", "error": "x"}) == (
        "unavailable (tmux 3.4+ not found; tmux_invalid_executable)")
    assert status({}) == "unavailable (tmux 3.4+ not found; unknown)"


@pytest.mark.parametrize("system, hint", [("wsl", "Install tmux >= 3.4 in Ubuntu: sudo apt install tmux."),
                                          ("darwin", "Install tmux >= 3.4, for example with: brew install tmux.")])
def test_workspace_refusal_names_the_need_the_version_the_hint_and_what_works(monkeypatch, system, hint):
    monkeypatch.setattr(platform_hints, "host_system", lambda *a, **k: system)
    refusal = first_start.workspace_refusal
    need = "The terminal workspace needs tmux 3.4 or newer."
    assert refusal({"available": False, "code": "tmux_missing"}) == f"{need} tmux was not found. {hint} {SENTENCE_TAIL}"
    assert refusal({"available": False, "code": "tmux_unsupported", "version": "tmux 3.3a"}) == (
        f"{need} Found tmux 3.3a. {hint} {SENTENCE_TAIL}")
    assert refusal({"available": False, "code": "tmux_unavailable"}) == (
        f"{need} tmux did not report its version. {hint} {SENTENCE_TAIL}")
    escaped = refusal({"available": False, "code": "tmux_unsupported", "version": "tmux 3.3\x1b[31m"})
    assert "\x1b" not in escaped and "\\x1b" in escaped


class Reached(Exception):
    pass


def _terminal(monkeypatch, data, tail):
    interactive = types.SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(sys, "stdin", interactive)
    monkeypatch.setattr(sys, "stdout", interactive)
    monkeypatch.setattr(sys, "argv", ["cairnmind", "--data-dir", str(data), *tail])


@pytest.mark.parametrize("tail", [[], ["tui"], ["workspace", "takeover"]])
@pytest.mark.parametrize("state", ["missing", "3.3a"])
def test_opening_the_workspace_without_tmux_prints_one_message(monkeypatch, capfd, data, tmux, state, tail):
    tmux(state)
    _terminal(monkeypatch, data, tail)

    def broker(*args, **kwargs):
        raise AssertionError("the broker must not start without tmux 3.4+")
    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient", broker)
    with pytest.raises(SystemExit) as caught:
        cli.entrypoint()
    assert caught.value.code == 1
    found = {"available": False, "code": "tmux_missing"} if state == "missing" else {
        "available": False, "code": "tmux_unsupported", "version": f"tmux {state}"}
    assert capfd.readouterr().err == "Cairnmind: " + first_start.workspace_refusal(found) + "\n"
    assert tmux.calls() in ([], ["-V"])  # asked its version, never started a server


def test_with_tmux_3_4_the_workspace_goes_past_the_check(monkeypatch, data, tmux):
    tmux("3.5a")
    _terminal(monkeypatch, data, [])

    def broker(*args, **kwargs):
        raise Reached
    monkeypatch.setattr("cairnmind.terminal_client.BrokerClient", broker)
    with pytest.raises(Reached):
        cli.entrypoint()


def test_every_new_sentence_has_polish_in_the_catalog():
    for english, polish in first_start.MESSAGES.items():
        assert english != polish
        assert tr(english, "pl") == polish
        assert tr(english, "en") == english


def _quiet_doctor(monkeypatch):
    """Doctor with no client, engine, model server or store to ask: only tmux differs between the runs."""
    import httpx
    from cairnmind import api_runtime, database_credentials, engine, integrations, store_writer
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(integrations, "_probe_cli", lambda name: {"installed": False})
    monkeypatch.setattr(database_credentials, "doctor_block", lambda settings: {"available": False})
    monkeypatch.setattr(engine, "resolve_java", lambda settings: None)
    monkeypatch.setattr(store_writer, "inspect", lambda settings: {"role": "writer"})
    monkeypatch.setattr(api_runtime, "coding_profiles_status", lambda settings: [])

    def unreadable(settings):
        raise ValueError("no store in this test")
    monkeypatch.setattr(cli, "store_for_read", unreadable)
    real = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kw))


def test_doctor_reports_the_cockpit_as_a_warning_and_exits_0(monkeypatch, data, tmux):
    from typer.testing import CliRunner
    _quiet_doctor(monkeypatch)
    reports = {}
    for state in STATES:
        tmux(state)
        result = CliRunner().invoke(cli.app, ["--data-dir", str(data), "doctor"])
        assert result.exit_code == 0, result.output
        reports[state] = json.loads(result.output)
    assert reports["missing"]["cockpit"] == "unavailable (tmux 3.4+ not found)"
    assert reports["3.3a"]["cockpit"] == "unavailable (tmux 3.4+ not found; found tmux 3.3a)"
    assert "cockpit" not in reports["3.5a"] and reports["3.5a"]["tmux"]["code"] == "ready"
    stripped = [{key: value for key, value in report.items() if key not in ("tmux", "cockpit")}
                for report in reports.values()]
    assert stripped[0] == stripped[1] == stripped[2]  # nothing else turns unhealthy


def test_the_installer_view_of_doctor_says_cockpit_unavailable(monkeypatch):
    from cairnmind import doctor_attention
    monkeypatch.setattr(platform_hints, "host_system", lambda *a, **k: "wsl")
    healthy = {"python": "3.12.3", "java": {"on_path": True, "executable": "/usr/bin/java", "compatible": True},
               "arcadedb": {"available": True, "database_available": True},
               "ollama": {"available": True, "chat_model_installed": True, "embedding_model_installed": True},
               "memory_models": {"chat": {"reachable": True}}, "service": {"available": False},
               "environment_notes": [], "data_dir": "/d", "store_writer": {"role": "writer"}}

    def lines(tmux):
        report = {**healthy, "tmux": tmux}
        assert doctor_attention.attention(report) == []  # #287: the workspace is optional, not a fault
        summary = doctor_attention.summary(report)
        assert first_start.NOTHING_MISSING in summary
        return [line for line in summary[summary.index(first_start.OPTIONAL_OFF):] if "workspace" in line]
    assert lines({"code": "tmux_missing"}) == [
        "  - Terminal workspace: unavailable (tmux 3.4+ not found). Install tmux >= 3.4 in Ubuntu: sudo apt install "
        "tmux. Every other command works without it."]
    assert lines({"code": "tmux_unsupported", "version": "tmux 3.3a"})[0].startswith(
        "  - Terminal workspace: unavailable (tmux 3.4+ not found; found tmux 3.3a).")


# --- every other command works without tmux (no gate on it exists; these keep one from appearing) -----------------

class FakeStore:
    """The part of the store these commands touch; the fast tier has no database."""
    not_writer = None
    confirmations_before_rule = None

    def __init__(self, root):
        self.root = root

    def resolve_project(self, project):
        return {"id": "p1", "name": "demo", "root": str(self.root)}

    def transaction(self):
        from contextlib import nullcontext
        return nullcontext()

    def read_consistently(self, read, kinds):
        return read()[1]

    def query(self, *args, **kwargs):
        return []


def _invoke(data, *args):
    from typer.testing import CliRunner
    return CliRunner().invoke(cli.app, ["--data-dir", str(data), *args])


@pytest.mark.parametrize("state", STATES)
def test_init_works_without_tmux(monkeypatch, tmp_path, data, tmux, state):
    from cairnmind import database_mode, engine_docker
    tmux(state)
    monkeypatch.setattr(engine_docker, "choose_engine_mode", lambda settings: None)
    monkeypatch.setattr(database_mode, "connect_only", lambda settings: False)
    monkeypatch.setattr(database_mode, "engine_mode", lambda settings: "native")
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore(tmp_path))
    result = _invoke(data, "init")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data_dir"] == str(data)
    assert tmux.calls() == []


@pytest.mark.parametrize("state", STATES)
def test_serve_starts_without_tmux(monkeypatch, data, tmux, state):
    import uvicorn
    from cairnmind import service
    tmux(state)
    monkeypatch.setattr(service, "create_app", lambda *a, **k: types.SimpleNamespace(state=types.SimpleNamespace()))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: None)
    result = _invoke(data, "serve")
    assert result.exit_code == 0, result.output
    assert tmux.calls() == []


@pytest.mark.parametrize("state", STATES)
def test_run_starts_the_client_in_this_terminal_without_tmux(monkeypatch, tmp_path, data, tmux, state):
    from cairnmind import ingest, integrations, store
    tmux(state)
    monkeypatch.setattr(cli, "_ensure_service_or_refuse", lambda settings: None)
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore(tmp_path))
    launch = {"command": [sys.executable, "-c", "raise SystemExit(7)"], "cwd": str(tmp_path),
              "env": {"PATH": os.environ["PATH"]}}
    monkeypatch.setattr(integrations, "prepare_client_launch", lambda *a, **k: launch)
    monkeypatch.setattr(store, "Store", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a, **k: None)
    result = _invoke(data, "run", "demo", "codex")
    assert result.exit_code == 7, result.output  # the client ran here and its own exit code came back
    assert tmux.calls() == []


@pytest.mark.parametrize("state", STATES)
def test_mcp_works_without_tmux(monkeypatch, tmp_path, data, tmux, state):
    from cairnmind import mcp_server
    tmux(state)
    started = []
    monkeypatch.setattr(mcp_server, "run", lambda settings, external=None: started.append(external))
    result = _invoke(data, "mcp", "--client", "external", "--project", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert started == [{"project": str(tmp_path), "worktree": None}]
    assert tmux.calls() == []


@pytest.mark.parametrize("state", STATES)
def test_brief_works_without_tmux(monkeypatch, tmp_path, data, tmux, state):
    from cairnmind import brief
    tmux(state)
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore(tmp_path))
    monkeypatch.setattr(brief, "build_brief", lambda *a, **k: {"text": "Where work stopped."})
    result = _invoke(data, "brief", "demo")
    assert result.exit_code == 0 and result.output.strip() == "Where work stopped.", result.output
    assert tmux.calls() == []


@pytest.mark.parametrize("state", STATES)
def test_check_run_works_without_tmux(monkeypatch, tmp_path, data, tmux, state):
    from cairnmind import verification
    tmux(state)
    monkeypatch.setattr(cli, "store_for", lambda settings, **kw: FakeStore(tmp_path))
    monkeypatch.setattr(verification, "run_check", lambda *a, **k: {"passed": True})
    result = _invoke(data, "check", "run", "demo", "unit", "--task", "t1")
    assert result.exit_code == 0, result.output
    assert tmux.calls() == []


@pytest.mark.parametrize("state", STATES)
def test_impact_works_without_tmux(monkeypatch, tmp_path, data, tmux, state):
    from cairnmind import impact
    tmux(state)
    monkeypatch.setattr(cli, "store_for_read", lambda settings: FakeStore(tmp_path))
    monkeypatch.setattr(impact, "build_impact", lambda *a, **k: {"text": "No records are linked."})
    result = _invoke(data, "impact", "demo", "--since", "HEAD")
    assert result.exit_code == 0 and result.output.strip() == "No records are linked.", result.output
    assert tmux.calls() == []
