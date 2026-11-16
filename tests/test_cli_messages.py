"""What the command line says when it refuses, fails or helps (#244, the 0.11.0 release trial).

A refusal that is not about the command line has no usage frame; an operating-system error is a sentence;
an installation that only reads the store is told what it can do, once; help names every command.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import re
import socket
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from conftest import plain_words

from cairnmind import cli
from cairnmind.config import Settings
from cairnmind.store import Store

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_session(monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


def no_usage_frame(result):
    """A refusal shown as a refusal: exit 2, an Error line, and none of Click's usage block."""
    assert result.exit_code == 2, result.output
    assert "Usage:" not in result.output and "Invalid value" not in result.output, result.output
    assert result.output.startswith("Error: "), result.output
    return plain_words(result.output)


def entry(monkeypatch, capsys, *arguments):
    """Run the way a user runs a command: through the entrypoint, which prints the store's errors."""
    monkeypatch.setattr("sys.argv", ["cairnmind", *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()
    return stopped.value.code, capsys.readouterr()


def second_installation(tmp_path: Path, first: Store) -> Path:
    """Another installation whose configuration names the first one's database: it reads, never writes."""
    data_dir = tmp_path / "reader"
    data_dir.mkdir(mode=0o700)
    (data_dir / "config.json").write_text(json.dumps({"arcade_database": first.settings.arcade_database}))
    Settings.load(data_dir, create=True)  # its own token, as `cairnmind init` gives it
    return data_dir


@pytest.fixture
def reader(tmp_path):
    first = Store(tmp_path / "first")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def handler():\n    return 1\n")
    first.add_project("demo", root)
    data_dir = second_installation(tmp_path, first)
    assert Store(data_dir).read_only
    return first, data_dir


# --- Refusals that are not about the command line --------------------------------------------------------------

def test_without_a_terminal_the_launcher_says_what_to_do_without_a_usage_block(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(tmp_path / "absent")]))
    assert "interactive terminal" in said and "cairnmind --help" in said
    assert not (tmp_path / "absent").exists()


class StoppedService:
    """A service process that wrote one line to its log and ended, as a reader's service does (#70)."""

    def __init__(self, command, stdout=None, **kwargs):
        stdout.write(b"Cairnmind: This installation only reads this store; abc12345 writes it.\n")
        stdout.flush()

    def poll(self):
        return 1


def test_a_service_that_stops_at_start_is_a_refusal_without_the_services_own_prefix(tmp_path, monkeypatch):
    Settings.load(tmp_path / "memory", create=True)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.subprocess, "Popen", StoppedService)
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "index", "demo"]))
    assert said == ("Error: The service stopped at start: This installation only reads this store; "
                    "abc12345 writes it.")
    # The workspace and the cockpit call it outside a command: there it is an ordinary error, nothing printed.
    with pytest.raises(cli.ServiceDidNotStart, match="^The service stopped at start: This installation"):
        cli.ensure_service(Settings.load(tmp_path / "memory"))


def test_a_service_that_never_answers_is_a_refusal(tmp_path, monkeypatch):
    Settings.load(tmp_path / "memory", create=True)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "index", "demo"]))
    assert "The service did not start" in said and "may be in use" in said


def test_restore_and_engine_stop_with_the_service_running_are_refusals(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "memory", create=True)
    backup = tmp_path / "backup.cairnmind.json.gz"
    backup.write_bytes(b"not read: the service is running")
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli, "store_for", lambda *args, **kwargs: pytest.fail("the store was opened"))
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "restore", str(backup)]))
    assert "restoring requires a stopped service" in said
    with (settings.data_dir / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "restore", str(backup)])
        assert "still holds the store lock" in no_usage_frame(held)
        served = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "serve"])
        assert "already running for this store" in no_usage_frame(served)
    monkeypatch.setattr("cairnmind.database_mode.connect_only", lambda settings: False)
    stopped = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "engine", "stop"])
    assert "Run cairnmind stop first" in no_usage_frame(stopped)


def test_restore_of_a_missing_path_or_a_folder_is_refused_before_anything_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "store_for", lambda *args, **kwargs: pytest.fail("the store was opened"))
    monkeypatch.setattr(cli, "service_alive", lambda *args: pytest.fail("the service was asked"))
    missing = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "restore",
                                      str(tmp_path / "no" / "such.json.gz")])
    assert missing.exit_code == 2 and "does not exist" in plain_words(missing.output)
    assert "corrupt" not in missing.output.lower()
    folder = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "restore", str(tmp_path)])
    assert folder.exit_code == 2 and "is a directory" in plain_words(folder.output)
    assert not (tmp_path / "memory").exists()


# --- An installation that only reads the store (#70) -----------------------------------------------------------

def test_a_write_on_a_reader_says_its_refusal_once(reader, monkeypatch, capsys):
    first, data_dir = reader
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data_dir), "remember", "demo", "a note")
    assert code == 1 and shown.out == ""
    lines = [line for line in shown.err.splitlines() if line.strip()]
    assert len(lines) == 1 and "only reads this store" in lines[0], shown.err
    assert first.installation.id[:8] in lines[0]


def test_backup_on_a_reader_works_without_the_refusal_like_notice(reader, tmp_path, monkeypatch, capsys):
    first, data_dir = reader
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data_dir), "backup", str(tmp_path / "copy.json.gz"))
    assert code in (0, None) and shown.err == "", shown.err
    assert Path(json.loads(shown.out)["backup"]).stat().st_size > 0


def test_retry_jobs_says_what_it_retried(reader, tmp_path, monkeypatch, capsys):
    first, data_dir = reader
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(first.data_dir), "retry-jobs")
    assert code in (0, None), shown.err
    assert json.loads(shown.out) == {"retried": False, "count": 0, "job_ids": []}
    # A reader with nothing to retry is refused too: the notice is the one line, beside no result.
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data_dir), "retry-jobs")
    assert code == 1 and shown.out == "" and len(shown.err.strip().splitlines()) == 1, (shown.out, shown.err)
    assert "only reads this store" in shown.err
    pid = first.resolve_project("demo")["id"]
    first.enqueue("extract", pid, {"source_id": "s"}, "job-244")
    job = first.claim_job("inference", 60, ["extract"])
    first.finish_job(job["id"], "a failure for the test", worker=job["worker"], expected_attempts=job["attempts"])
    # On the reader the notice says why, and the store refuses the write: said once, nothing retried.
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data_dir), "retry-jobs")
    assert code == 1 and shown.out == "" and len(shown.err.strip().splitlines()) == 1, shown.err
    assert first.query("SELECT state FROM jobs WHERE id=:id", {"id": job["id"]})[0]["state"] == "failed"
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(first.data_dir), "retry-jobs")
    assert json.loads(shown.out) == {"retried": True, "count": 1, "job_ids": [job["id"]]}


def test_status_on_a_reader_names_the_role_instead_of_cairnmind_serve(reader):
    first, data_dir = reader
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(data_dir), "status"]))
    assert "only reads this store" in said and "only the writer runs" in said
    assert "cairnmind serve" not in said and "cairnmind store writer" in said
    # The writer, whose service is simply stopped, is still told to start it.
    stopped = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(first.data_dir), "status"]))
    assert "Run `cairnmind serve` explicitly" in stopped


def test_store_takeover_without_yes_suggests_what_fits_the_role(reader, tmp_path, monkeypatch):
    first, data_dir = reader

    def asked(directory):
        shown = runner.invoke(cli.app, ["--data-dir", str(directory), "store", "takeover"])
        assert shown.exit_code == 1, shown.output
        return json.loads(shown.output)

    assert asked(first.data_dir)["next"] == "This installation is already the writer; nothing to do."
    assert asked(data_dir)["next"] == "Run `cairnmind store takeover --yes` to make this installation the writer."
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    for name in ("CAIRNMIND_ARCADE_URL", "DEVBRAIN_ARCADE_URL"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
    stopped = asked(tmp_path / "engine-down")
    assert stopped["role"] == "unknown" and "cairnmind engine start" in stopped["next"]


# --- Backup to a folder ----------------------------------------------------------------------------------------

def test_backup_to_a_path_ending_in_a_slash_creates_the_folder_and_names_the_file(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "memory")
    folder = tmp_path / "x" / "newdir"
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(store.data_dir), "backup", str(folder) + "/")
    assert code in (0, None), shown.err
    written = Path(json.loads(shown.out)["backup"])
    assert written.parent == folder.resolve() and written.name.startswith("cairnmind-")
    assert written.name.endswith(".json.gz") and folder.stat().st_mode & 0o777 == 0o700


# --- Operating-system errors and missing rows --------------------------------------------------------------------

def test_a_missing_file_or_a_too_long_path_is_a_sentence(tmp_path, monkeypatch, capsys):
    data = tmp_path / "memory"
    Settings.load(data, create=True)
    monkeypatch.chdir(tmp_path)
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data), "checkpoint", "demo", "missing.json")
    assert (code, shown.err) == (1, "Cairnmind: missing.json does not exist.\n")
    monkeypatch.setattr(cli, "store_for", lambda *args, **kwargs: pytest.fail("the store was opened"))
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data), "project", "add", "x", "/nope")
    assert (code, shown.err) == (1, "Cairnmind: /nope does not exist.\n")
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data), "project", "add", "x", "/tmp/" + "a" * 5000)
    assert code == 1 and shown.err.startswith("Cairnmind: The path is too long for this system: /tmp/aaa")
    assert len(shown.err) < 300 and "…" in shown.err and "Errno" not in shown.err


def test_an_error_with_a_bytes_path_shows_the_path_as_text():
    import errno
    said = cli._os_error_text(FileNotFoundError(errno.ENOENT, "No such file", b"/tmp/bytes"))
    assert said == "/tmp/bytes does not exist."


def test_a_missing_row_is_named_by_its_kind():
    from cairnmind.sql import one_row
    empty = SimpleNamespace(query=lambda sql, params=None: [])
    for sql, said in (("SELECT FROM plans WHERE id=:id", "Plan does not exist"),
                      ("SELECT FROM sessions WHERE id=:id", "Session does not exist"),
                      ("SELECT id,content FROM sources WHERE id=:id", "Source does not exist"),
                      ("SELECT FROM records WHERE id=:id", "Record does not exist")):
        with pytest.raises(ValueError, match=f"^{said}$"):
            one_row(empty, sql, {"id": "x"})
    assert one_row(empty, "SELECT FROM plans", required=False) is None


@pytest.mark.parametrize("content", ["[]", '[{"id": "a"}]', '"x"', "{}"])
def test_a_question_file_of_another_shape_is_refused_in_a_sentence(tmp_path, content):
    from cairnmind.repository_evaluation import load_questions
    path = tmp_path / "q.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="format_version 1 and a nonempty questions list"):
        load_questions(path, tmp_path)


def test_a_question_file_that_is_missing_or_a_folder_is_a_sentence(tmp_path, monkeypatch):
    from cairnmind.repository_evaluation import load_questions
    for path, said in ((tmp_path / "missing.json", f"The question file {tmp_path / 'missing.json'} does not exist."),
                       (tmp_path, f"The question file {tmp_path} is a folder, not a file.")):
        with pytest.raises(ValueError) as refused:
            load_questions(path, tmp_path)
        assert str(refused.value) == said and "Errno" not in str(refused.value)
    # Through the command, Click says the option's file does not exist, before anything else starts.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(cli, "_managed_only", lambda *args: pytest.fail("the command ran"))
    data = tmp_path / "memory"
    Settings.load(data, create=True)
    shown = runner.invoke(cli.app, ["--data-dir", str(data), "eval", "repository", str(tmp_path),
                                    "--questions", str(tmp_path / "missing.json")])
    assert shown.exit_code == 2 and "does not exist" in plain_words(shown.output)
    assert "Errno" not in shown.output


# --- What the installer prints of doctor -------------------------------------------------------------------------

def test_the_installer_prints_only_what_needs_attention(monkeypatch, capsys):
    import io
    from cairnmind import doctor_attention
    healthy = {"python": "3.12.3", "clients": {"codex": {"installed": True, "verified_resume": True},
                                               "claude": {"installed": False, "verified_resume": False}},
               "java": {"on_path": True, "executable": "/usr/bin/java", "compatible": True},
               "tmux": {"code": "ready"}, "arcadedb": {"available": True, "database_available": True},
               "ollama": {"available": True, "chat_model_installed": True, "embedding_model_installed": True},
               "memory_models": {"chat": {"reachable": True}}, "service": {"available": False},
               "environment_notes": [], "store_writer": {"role": "writer"}}
    assert doctor_attention.attention(healthy) == []
    broken = {**healthy, "environment_notes": ["CAIRNMIND_X and DEVBRAIN_X are both set and differ."],
              "tmux": {"code": "too_old", "minimum_version": "3.4"},
              "ollama": {"available": True, "chat_model_installed": False, "embedding_model_installed": True},
              "clients": {"codex": {"installed": True, "verified_resume": False}},
              "store_writer": {"role": "reader", "reason": "This installation only reads this store.\x1b[31m"}}
    lines = doctor_attention.attention(broken)
    assert lines[0] == "CAIRNMIND_X and DEVBRAIN_X are both set and differ."
    assert any("tmux is not ready (too_old)" in line for line in lines)
    assert any("chat model is not downloaded" in line for line in lines)
    assert any("codex is installed but its contract checks fail" in line for line in lines)
    assert any(line.startswith("Store writer: reader.") and "\x1b" not in line for line in lines)
    for report, first in ((healthy, "Nothing needs attention."), (broken, "Needs attention:"),
                          ([1, 2], "The doctor report could not be read."),
                          ({}, "The doctor report could not be read."),
                          ({"service": {"available": True}}, "The doctor report could not be read."),
                          ({"java": "of another shape"}, "The doctor report could not be read.")):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(report)))
        assert doctor_attention.main() == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed[0].startswith(first) and printed[-1].endswith("cairnmind doctor")
    # Ollama down: one line for one cause, not again for each model role at the same address.
    down = {**healthy, "ollama": {"available": False, "url": "http://127.0.0.1:11434"},
            "memory_models": {"chat": {"reachable": False, "base_url": "http://127.0.0.1:11434/"},
                              "embedding": {"reachable": False, "base_url": "http://127.0.0.1:11434"},
                              "judge": {"reachable": False, "base_url": "http://elsewhere:8000/v1"}}}
    lines = doctor_attention.attention(down)
    assert lines == ["Ollama does not answer; local models need `ollama serve`.",
                     "The judge model server at http://elsewhere:8000/v1 does not answer."]
    # A slow first start of Java is not worded as Java missing.
    slow = {**healthy, "java": {"on_path": True, "executable": "/usr/bin/java", "error": "Java did not answer "
                                "-version within 4 seconds (a first start can be slow)"}}
    assert doctor_attention.attention(slow) == [
        "Java did not answer: Java did not answer -version within 4 seconds (a first start can be slow)"]
    old = {**healthy, "java": {"on_path": True, "executable": "/usr/bin/java", "compatible": False, "version": "11"}}
    assert doctor_attention.attention(old) == ["Java 21 or newer is needed for the engine: 11"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"environment_notes": "abc"})))
    assert doctor_attention.main() == 0
    assert capsys.readouterr().out.startswith("The doctor report could not be read.")
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh").read_text()
    assert '"$cairnmind_bin" doctor | "$(uv tool dir)/cairnmind/bin/python" -I -m cairnmind.doctor_attention' in script


# --- Help ------------------------------------------------------------------------------------------------------

def test_every_top_level_command_has_a_description():
    group = typer.main.get_command(cli.app)
    missing = [name for name, command in group.commands.items() if not (command.help or "").strip()]
    assert missing == []
    # The first paragraph is what the list shows: a hard line break in it would break the line there.
    for name in ("init", "store"):
        command = group.commands[name]
        for sub in getattr(command, "commands", {"": command}).values():
            first = (sub.help or "").strip().split("\n\n")[0]
            assert "\n" not in first, (name, sub.name, first)


def test_plan_save_names_and_explains_its_arguments(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    shown = plain_words(runner.invoke(cli.app, ["plan", "save", "--help"]).output)
    # Each argument and option says what it is, not only its type and [required].
    assert "PROJECT <str> The project's name, id or folder. [required]" in shown
    assert "INPUT_JSON <path> A JSON file with plan and steps" in shown and "> [required]" not in shown
    assert "docs/WORKFLOW.md" in shown and "same key with the same file" in shown


def test_remember_says_how_to_keep_text_that_begins_with_a_dash(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    # Click reads such text as an option: the help says how to pass it.
    refused = runner.invoke(cli.app, ["remember", "team", "-----BEGIN KEY-----"])
    assert refused.exit_code == 2 and "No such option" in plain_words(refused.output)
    shown = plain_words(runner.invoke(cli.app, ["remember", "--help"]).output)
    assert re.search(r"after --, with the options before it", shown)
