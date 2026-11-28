"""What the command line says when it refuses, fails or helps (#244, the 0.11.0 release trial).

A refusal that is not about the command line has no usage frame; an operating-system error is a sentence;
an installation that only reads the store is told what it can do, once; help names every command.
"""
from __future__ import annotations

import inspect
import json
import re
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from conftest import own_time, plain_words
from typer.testing import CliRunner

from cairnmind import __version__, cli
from cairnmind.config import Settings
from cairnmind.pal import file_locks
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
    own_time(monkeypatch, cli, sleep=lambda seconds: None)
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
    with file_locks.locked_path(settings.data_dir / "service.lock", blocking=False):
        held = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "restore", str(backup)])
        assert "still holds the store lock" in no_usage_frame(held)
        served = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "serve"])
        assert "already running for this store" in no_usage_frame(served)
    monkeypatch.setattr("cairnmind.storage.database_mode.connect_only", lambda settings: False)
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
    with socket.socket() as probe:  # a port nothing listens on: the default one may be a real installation's (#221)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    for directory in (first.data_dir, data_dir):
        config = directory / "config.json"
        saved = json.loads(config.read_text()) if config.exists() else {}
        config.write_text(json.dumps({**saved, "port": port}))
        config.chmod(0o600)
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(data_dir), "status"]))
    assert "only reads this store" in said and "only the writer runs" in said
    assert "cairnmind serve" not in said and "cairnmind store writer" in said
    # The writer, whose service is simply stopped, is told that the next command starts it (#287: not `serve`).
    stopped = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(first.data_dir), "status"]))
    assert "The next command that needs it starts it" in stopped and "cairnmind doctor" in stopped
    assert "cairnmind serve" not in stopped


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
    from conftest import assert_private
    assert written.name.endswith(".json.gz")
    assert_private(folder, directory=True)


# --- Operating-system errors and missing rows --------------------------------------------------------------------

def test_a_missing_file_or_a_too_long_path_is_a_sentence(tmp_path, monkeypatch, capsys):
    data = tmp_path / "memory"
    Settings.load(data, create=True)
    monkeypatch.chdir(tmp_path)
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data), "checkpoint", "demo", "missing.json")
    assert (code, shown.err) == (1, "Cairnmind: missing.json does not exist.\n")
    monkeypatch.setattr(cli, "store_for", lambda *args, **kwargs: pytest.fail("the store was opened"))
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data), "project", "add", "x", "/nope")
    from cairnmind.cli_cmds.first_start import PROJECT_FOLDER_MISSING  # the sentence of #313: how to go on
    from cairnmind.pal.process_control import quote_arg
    nope = str(Path("/nope").expanduser().absolute())
    assert (code, shown.err) == (1, "Cairnmind: " + PROJECT_FOLDER_MISSING.format(root=nope, quoted=quote_arg(nope)) + "\n")
    import os
    import sys
    long_base = Path(tmp_path.drive + "\\") if sys.platform == "win32" else Path("/tmp").resolve()
    long_arg = str(long_base / ("a" * 5000)) if sys.platform == "win32" else "/tmp/" + "a" * 5000
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(data), "project", "add", "x", long_arg)
    # The path is resolved, and /tmp is /private/tmp on macOS.
    prefix = str(long_base).rstrip(os.sep) + os.sep + "aaa"
    assert code == 1 and shown.err.startswith(f"Cairnmind: The path is too long for this system: {prefix}")
    assert len(shown.err) < 300 and "…" in shown.err and "Errno" not in shown.err


def test_an_error_with_a_bytes_path_shows_the_path_as_text():
    import errno
    said = cli._os_error_text(FileNotFoundError(errno.ENOENT, "No such file", b"/tmp/bytes"))
    assert said == "/tmp/bytes does not exist."


def test_a_missing_row_is_named_by_its_kind():
    from cairnmind.storage.sql import one_row
    empty = SimpleNamespace(query=lambda sql, params=None: [])
    for sql, said in (("SELECT FROM plans WHERE id=:id", "Plan does not exist"),
                      ("SELECT FROM sessions WHERE id=:id", "Session does not exist"),
                      ("SELECT id,content FROM sources WHERE id=:id", "Source does not exist"),
                      ("SELECT FROM records WHERE id=:id", "Record does not exist")):
        with pytest.raises(ValueError, match=f"^{said}$"):
            one_row(empty, sql, {"id": "x"})
    assert one_row(empty, "SELECT FROM plans", required=False) is None


def test_an_unknown_id_is_refused_alike_for_a_task_a_plan_a_source_and_a_session(tmp_path, monkeypatch, capsys):
    # One prefix and one exit code, whether the store or the service finds the row missing (#244).
    import httpx
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    store.add_project("demo", root)
    data = str(store.data_dir)
    unknown = "00000000-0000-0000-0000-000000000000"
    for arguments, said in ((("task", "update", "demo", unknown, "done"), "Task does not exist in this project"),
                            (("check", "list", "demo", "--task", unknown), "Task does not exist in this project"),
                            (("plan", "show", "demo", unknown), "Plan does not exist"),
                            (("source", unknown), "Source does not exist")):
        code, shown = entry(monkeypatch, capsys, "--data-dir", data, *arguments)
        assert (code, shown.out, shown.err) == (1, "", f"Cairnmind: {said}\n"), arguments
    answers = {"/sessions/" + unknown: (400, "Session does not exist"), "/memory/status": (404, "Unknown project"),
               "/sessions/context": (409, "resume_required: the packet changed")}
    real_client = httpx.Client

    def client(**kwargs):
        def answer(request):
            if request.url.path == "/health":  # the version check that precedes every request (#319)
                from cairnmind.cli_cmds.service_version import instance_id
                return httpx.Response(200, json={"service": "cairnmind", "version": __version__,
                                                 "instance": instance_id(Settings.load(Path(data)))})
            status, detail = answers[request.url.path]
            return httpx.Response(status, json={"detail": detail})
        return real_client(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli.httpx, "Client", client)
    code, shown = entry(monkeypatch, capsys, "--data-dir", data, "session", "show", unknown)
    assert (code, shown.err) == (1, "Cairnmind: Session does not exist\n")
    code, shown = entry(monkeypatch, capsys, "--data-dir", data, "memory", "status", "nowhere")
    assert (code, shown.err) == (1, "Cairnmind: Unknown project\n")
    # A refusal that is not about a missing row stays a refusal.
    code, shown = entry(monkeypatch, capsys, "--data-dir", data, "session", "context", unknown)
    assert (code, shown.err) == (2, "Error: resume_required: the packet changed\n")


def test_an_unregistered_folder_is_refused_with_the_command_that_registers_it(tmp_path):
    # The same refusal for memory, retrieval, the inspector, checks and plans (#244): the folder, the project
    # and the command, quoted for the shell.
    from cairnmind.agents.retrieval import Retriever
    from cairnmind.memory.memory import MemoryManager
    from cairnmind.memory.plans import PlanManager
    from cairnmind.memory.verification import _root
    from cairnmind.scip.ingest import register_default_histories
    from cairnmind.terminal.inspector import Inspector
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    project = store.add_project("my team", root)
    folder = (tmp_path / "scratch dir").resolve()
    folder.mkdir()
    from cairnmind.pal.process_control import quote_arg
    said = (f"{folder} is not a registered project root of my team; register it with "
            f"`cairnmind project root {quote_arg('my team')} {quote_arg(str(folder))}`.")
    for refused in (lambda: MemoryManager(store)._scope(project["id"], folder),
                    lambda: Retriever(store, store.settings, ollama=object())._scope(project["id"], folder),
                    lambda: Inspector(store)._scope(project["id"], folder),
                    lambda: _root(store, project["id"], folder),
                    lambda: PlanManager(store).next_task(project["id"], worktree=folder),
                    lambda: register_default_histories(store, project["id"], folder)):
        with pytest.raises(ValueError) as caught:
            refused()
        assert str(caught.value) == said


def test_a_long_unregistered_folder_keeps_the_command_that_registers_it():
    # The sentence is bounded, but never by cutting off the command it is there to give (#244).
    from cairnmind.storage.sql import unregistered_root
    fits = "/a/" + "b" * 600
    said = unregistered_root(fits, {"name": "demo"})
    assert said.endswith(f"register it with `cairnmind project root demo {fits}`.")
    assert said.startswith("/a/bbb") and "…" in said.split(";")[0]  # the leading copy is shortened
    for folder, project in (("/a/" + "b" * 2000, {"name": "demo"}), ("/repo", {"name": "n" * 2000})):
        said = unregistered_root(folder, project)
        assert said.endswith("register it with `cairnmind project root <project> <folder>`.") and len(said) < 700


def test_a_printed_command_names_the_data_directory_it_was_refused_in(tmp_path, monkeypatch, capsys):
    # A refusal's command is pasted into a plain shell, where neither --data-dir nor a wrapper's CAIRNMIND_HOME
    # holds: it names this installation's directory, as `stage next` does (#244).
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    store.add_project("demo", root)
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    data = str(store.data_dir)
    named = f"`cairnmind --data-dir {data} project root demo {folder.resolve()}`."
    code, shown = entry(monkeypatch, capsys, "--data-dir", data, "stage", "next", "demo", "--worktree", str(folder))
    assert code == 1 and shown.err.startswith("Cairnmind: ") and named in shown.err, shown.err
    monkeypatch.setattr(cli, "ensure_service", lambda settings: None)
    monkeypatch.setattr("cairnmind.memory.integrations.run_client", lambda *args: pytest.fail("the client started"))
    code, shown = entry(monkeypatch, capsys, "--data-dir", data, "run", "demo", "codex", "--worktree", str(folder))
    assert code == 2 and shown.err.startswith("Error: ") and named in shown.err, shown.err
    # Only the register command, which writes to the store, is given the directory.
    code, shown = entry(monkeypatch, capsys, "--data-dir", data, "answers", "show", "demo", "x")
    assert code == 2 and "`cairnmind answers list`" in shown.err, shown.err
    for name in ("CAIRNMIND_HOME", "DEVBRAIN_HOME"):
        monkeypatch.setenv(name, data)
        code, shown = entry(monkeypatch, capsys, "stage", "next", "demo", "--worktree", str(folder))
        assert code == 1 and named in shown.err, (name, shown.err)
        monkeypatch.delenv(name)
    # The default installation, the one a plain shell uses: the command is printed as it is.
    monkeypatch.setattr("cairnmind.config.default_data_dir", lambda environ=None: store.data_dir)
    code, shown = entry(monkeypatch, capsys, "--data-dir", data, "stage", "next", "demo", "--worktree", str(folder))
    assert code == 1 and f"`cairnmind project root demo {folder.resolve()}`." in shown.err, shown.err
    assert "--data-dir" not in shown.err


def test_an_unknown_answer_id_is_refused_as_an_unknown_task_is(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    store.add_project("demo", root)
    monkeypatch.setattr("cairnmind.config.default_data_dir", lambda environ=None: store.data_dir)
    code, shown = entry(monkeypatch, capsys, "--data-dir", str(store.data_dir), "answers", "show", "demo", "f" * 32)
    assert (code, shown.out, shown.err) == (1, "", "Cairnmind: No logged answer with this id in this project\n")


@pytest.mark.parametrize("content", ["[]", '[{"id": "a"}]', '"x"', "{}"])
def test_a_question_file_of_another_shape_is_refused_in_a_sentence(tmp_path, content):
    from cairnmind.agents.repository_evaluation import load_questions
    path = tmp_path / "q.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="format_version 1 and a nonempty questions list"):
        load_questions(path, tmp_path)


def test_a_question_file_that_is_missing_or_a_folder_is_a_sentence(tmp_path, monkeypatch):
    from cairnmind.agents.repository_evaluation import load_questions
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


@pytest.mark.parametrize("content", ["[]", "{not json", '{"format_version": 2, "questions": [{"id": "a"}]}',
                                     '{"format_version": 1, "questions": [{"question": "no id"}]}'])
def test_a_bad_question_file_through_the_command_is_a_refusal_not_a_usage_error(tmp_path, monkeypatch, content):
    # The file is wrong, not the command line: no usage frame, and the run starts nothing (#244).
    monkeypatch.setattr(cli, "_managed_only", lambda *args: None)
    monkeypatch.setattr("cairnmind.agents.repository_evaluation._preflight", lambda *args: pytest.fail("a model was asked"))
    data = tmp_path / "memory"
    Settings.load(data, create=True)
    questions = tmp_path / "q.json"
    questions.write_text(content)
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(data), "eval", "repository", str(tmp_path),
                                                  "--questions", str(questions)]))
    assert len(said) > len("Error: ")


@pytest.mark.parametrize("content", ["[]", "{not json"])
def test_a_bad_transcript_file_through_the_command_is_a_refusal_not_a_usage_error(tmp_path, content):
    data = tmp_path / "memory"
    Settings.load(data, create=True)
    transcripts = tmp_path / "t.json"
    transcripts.write_text(content)
    no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(data), "eval", "extraction",
                                           "--transcripts", str(transcripts)]))


def test_a_plan_file_or_an_evidence_file_of_another_shape_is_a_refusal(tmp_path):
    store = Store(tmp_path / "memory")
    root = tmp_path / "repo"
    root.mkdir()
    store.add_project("demo", root)
    plan = tmp_path / "plan.json"
    plan.write_text("[]")
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "plan", "save", "demo",
                                                  str(plan), "--idempotency-key", "k"]))
    assert "plan and steps" in said
    # A file that is not JSON at all is refused the same way, naming the file, before any service starts.
    plan.write_text("{not json")
    for arguments in (("plan", "save", "demo", str(plan), "--idempotency-key", "k"), ("checkpoint", "demo", str(plan)),
                      ("session", "ack", "s", "p", "h", str(plan))):
        said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(store.data_dir), *arguments]))
        assert said.startswith(f"Error: The file {plan} is not valid JSON: Expecting property name"), arguments
    pid = store.resolve_project("demo")["id"]
    source = store.snapshot(pid, "manual://task", "Build it", "manual")
    task = store.remember(pid, {"kind": "task", "content": "Build it", "metadata": {"state": "todo"}},
                          [source["id"]])
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("  \n")
    said = no_usage_frame(runner.invoke(cli.app, ["--data-dir", str(store.data_dir), "task", "update", "demo",
                                                  task["id"], "done", "--evidence", str(evidence), "--test-passed"]))
    assert "non-empty check result" in said


# --- What the installer prints of doctor -------------------------------------------------------------------------

def test_the_installer_prints_only_what_needs_attention(monkeypatch, capsys):
    import io

    from cairnmind.cli_cmds import doctor_attention
    healthy = {"python": "3.12.3", "clients": {"codex": {"installed": True, "verified_resume": True},
                                               "claude": {"installed": False, "verified_resume": False}},
               "java": {"on_path": True, "executable": "/usr/bin/java", "compatible": True},
               "tmux": {"code": "ready"}, "arcadedb": {"available": True, "database_available": True},
               "ollama": {"available": True, "chat_model_installed": True, "embedding_model_installed": True},
               "memory_models": {"chat": {"reachable": True}}, "service": {"available": False},
               "environment_notes": [], "data_dir": "/home/me/.local/share/cairnmind",
               "store_writer": {"role": "writer"}}
    assert doctor_attention.attention(healthy) == []
    broken = {**healthy, "environment_notes": ["CAIRNMIND_X and DEVBRAIN_X are both set and differ."],
              "tmux": {"code": "too_old", "minimum_version": "3.4"},
              "ollama": {"available": True, "chat_model_installed": False, "embedding_model_installed": True},
              "clients": {"codex": {"installed": True, "verified_resume": False}},
              "store_writer": {"role": "lost", "reason": "Installation abc took over this store.\x1b[31m"}}
    lines = doctor_attention.attention(broken)
    # #287: environment notes and the optional workspace are in the summary's own groups, not among the faults.
    assert not any("CAIRNMIND_X" in line or "cockpit" in line for line in lines)
    assert any("chat model is not downloaded" in line for line in lines)
    assert any("codex is installed but its contract checks fail" in line for line in lines)
    assert any(line.startswith("Store writer: lost.") and "\x1b" not in line for line in lines)
    unreadable = "The doctor report could not be read."
    for report, first, last in ((healthy, "Memory is kept in /home/me/.local/share/cairnmind.", "Next: cairnmind project add"),
                                (broken, "Memory is kept in /home/me/.local/share/cairnmind.", "Next: fix what is listed"),
                                ([1, 2], unreadable, "cairnmind doctor --json"),
                                ({}, unreadable, "cairnmind doctor --json"),
                                ({"service": {"available": True}}, unreadable, "cairnmind doctor --json"),
                                # What `cairnmind init` prints, and a part of a report: not a report that found nothing.
                                ({"data_dir": "/x", "next": "cairnmind doctor"}, unreadable, "cairnmind doctor --json"),
                                ({"store_writer": []}, unreadable, "cairnmind doctor --json"),
                                ({"java": "of another shape"}, unreadable, "cairnmind doctor --json")):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(report)))
        assert doctor_attention.main() == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed[0].startswith(first) and (printed[-1].startswith(last) if first != unreadable
                                                 else printed[-1].endswith(last))
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
    [needed] = doctor_attention.attention(old)
    assert needed.startswith("Java 21 or newer is needed for the engine: 11. Install Java 21") and needed.endswith(
        "or run the engine in Docker: cairnmind engine start --docker.")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"environment_notes": "abc"})))
    assert doctor_attention.main() == 0
    assert capsys.readouterr().out.startswith("The doctor report could not be read.")
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh").read_text()
    assert '"$cairnmind_bin" doctor | "$(uv tool dir)/cairnmind/bin/python" -I -m cairnmind.cli_cmds.doctor_attention' in script


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


def _leaf_commands(command, path=()):
    for name, sub in (getattr(command, "commands", None) or {}).items():
        yield from _leaf_commands(sub, (*path, name))
    if not getattr(command, "commands", None):
        yield " ".join(path), command


def test_every_argument_of_every_command_is_named_and_described_and_no_paragraph_breaks_early():
    # The first round fixed `plan save` and the first paragraphs only; every command is walked now (#244).
    commands = list(_leaf_commands(typer.main.get_command(cli.app)))
    assert len(commands) > 90
    bare = [(path, param.name) for path, command in commands for param in command.params
            if param.param_type_name == "argument" and not (getattr(param, "help", None) or "").strip()]
    assert bare == []
    lowercase = [(path, param.name) for path, command in commands for param in command.params
                 if param.param_type_name == "argument" and param.metavar != (param.metavar or "").upper()]
    assert lowercase == []
    # Typer joins the lines of the first paragraph and keeps those of the later ones as they are written.
    wrapped = [path for path, command in commands
               if any("\n" in part for part in inspect.cleandoc(command.help or "").split("\n\n")[1:])]
    assert wrapped == []


def test_remember_and_store_takeover_help_read_as_sentences(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    remember = runner.invoke(cli.app, ["remember", "--help"]).output
    assert "remember [OPTIONS] {PROJECT} {CONTENT}" in plain_words(remember)  # not {project}
    assert "PROJECT <str> The project's name, id or folder. [required]" in plain_words(remember)
    takeover = runner.invoke(cli.app, ["store", "takeover", "--help"]).output
    # At 200 columns the second paragraph is one line: it does not break after "with its". Each line is
    # compared without colour codes: on CI the help is coloured, and the codes stand at both ends of the line.
    lines = [plain_words(line) for line in takeover.splitlines()]
    assert any(line.startswith("The previous writer stops") and line.endswith("kept in the store.")
               for line in lines), takeover


def test_the_menu_help_and_the_guide_do_not_say_the_menu_holds_every_command():
    # The activity panel has its own key and no menu entry (Notices stays the last entry): the help and the
    # guide say "main commands" and name the key (#244).
    from cairnmind.terminal.terminal_commands import COMMANDS
    from cairnmind.terminal.terminal_i18n import tr
    assert "activ" not in " ".join(title.lower() for title, *_ in COMMANDS)
    source = (Path(__file__).resolve().parents[1] / "src" / "cairnmind" / "terminal" / "terminal_commands.py").read_text(encoding="utf-8")
    assert "search all commands" not in source and "Ctrl+B, then Space: search the main commands" in source
    assert "Ctrl+B, then a: activity panel" in source
    english = source.split("help_text(self.translate(")[1].split("))")[0]
    text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', english)).encode().decode("unicode_escape")
    polish = tr(text, "pl")
    assert polish != text and "głównych poleceń" in polish
    guide = (Path(__file__).resolve().parents[1] / "docs" / "TERMINAL.md").read_text(encoding="utf-8")
    assert "menu of all commands" not in guide and "the activity panel has its own key, `a`" in guide


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
