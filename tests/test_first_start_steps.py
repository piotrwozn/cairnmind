"""What the commands of the first start say they did and what comes next (#287, #313)."""
import io
import json
import subprocess

import pytest
from typer.testing import CliRunner

from cairnmind import cli, first_start, ingest, integrations


@pytest.fixture
def told(monkeypatch):
    """The sentences a command gives `_human_or_json`; CliRunner is not a terminal, so standard output stays JSON."""
    sentences = []
    real = cli._human_or_json

    def record(result, as_json, make):
        sentences.extend(make())
        real(result, as_json, make)
    monkeypatch.setattr(cli, "_human_or_json", record)
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    return sentences


@pytest.fixture
def run(tmp_path):
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), *args])
    return invoke


def git(root, *arguments):
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def folder(tmp_path, name="my-app", repository=False, commit=False):
    root = tmp_path / name
    root.mkdir()
    (root / "app.py").write_text("value = 1\n")
    if repository:
        git(root, "init", "-q")
        git(root, "config", "user.email", "fixture@example.invalid")
        git(root, "config", "user.name", "Fixture")
    if commit:
        git(root, "add", ".")
        git(root, "commit", "-qm", "fixture")
    return root


def test_init_points_at_doctor_and_project_add_not_at_models(run):
    result = run("init")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["next"] == "cairnmind doctor, then cairnmind project add NAME FOLDER"


def test_project_add_says_what_it_did_and_warns_about_a_folder_without_git(tmp_path, run, told):
    run("init")
    told.clear()
    root = folder(tmp_path)
    added = run("project", "add", "my-app", str(root))
    assert added.exit_code == 0, added.output
    assert json.loads(added.stdout)["project"]["name"] == "my-app"  # the JSON is unchanged
    assert told[0].startswith(f"Registered my-app in {root.resolve()}; files indexed: ")
    assert told[-1].startswith("Next: cairnmind remember my-app")  # #326: the warning comes before the next step
    assert told[1:-1] == [first_start.NOT_GIT.format(root=root.resolve()), *queued(told)]


def queued(told):
    """The sentence about the queued jobs (#366): the stopped service of these tests leaves the file's job waiting."""
    lines = [line for line in told if line.startswith("Extraction and embedding jobs waiting: ")]
    assert len(lines) == 1, told  # exactly one sentence, not zero or several
    return lines


def test_project_add_warns_about_a_repository_without_a_commit_and_stays_quiet_with_one(tmp_path, run, told):
    run("init")
    told.clear()
    empty = folder(tmp_path, "empty", repository=True)
    assert run("project", "add", "empty", str(empty)).exit_code == 0
    assert told[1:-1] == [first_start.NO_COMMIT.format(root=empty.resolve()), *queued(told)]
    told.clear()
    ready = folder(tmp_path, "ready", repository=True, commit=True)
    assert run("project", "add", "ready", str(ready)).exit_code == 0
    assert told[0].startswith("Registered ready in ") and told[-1].startswith("Next: ")
    assert told[1:-1] == queued(told)  # nothing else: no warning


def test_a_project_name_with_a_space_is_quoted_in_the_next_step(tmp_path, run, told):
    run("init")
    told.clear()
    root = folder(tmp_path, repository=True, commit=True)
    assert run("project", "add", "my app", str(root)).exit_code == 0
    assert "cairnmind remember 'my app' " in told[-1]


def test_adding_a_registered_project_says_it_existed(tmp_path, run, told):
    run("init")
    told.clear()
    root = folder(tmp_path, repository=True, commit=True)
    first = run("project", "add", "my-app", str(root))
    assert told[0].startswith("Registered my-app in ")
    told.clear()
    again = run("project", "add", "my-app", str(root))
    assert again.exit_code == 0, again.output
    assert told[0].startswith(f"my-app was already registered for {root.resolve()}; its files were checked again")
    assert json.loads(again.stdout)["project"]["id"] == json.loads(first.stdout)["project"]["id"]  # the JSON is unchanged
    assert set(json.loads(again.stdout)) == {"project", "indexed", "history", "jobs"}


def test_remember_says_a_proposed_finding_needs_nothing_more_and_a_confirmed_one_what_it_stored(
        run, told, tmp_path, person_terminal):
    run("init")
    run("project", "add", "my-app", str(folder(tmp_path, repository=True, commit=True)))
    told.clear()
    proposed = run("remember", "my-app", "Accounts lock after five failed logins.")
    assert json.loads(proposed.stdout)["status"] == "proposed"
    assert told == [first_start.REMEMBER_PROPOSED.format(kind="fact", project="my-app")]
    told.clear()
    # #214: the command line confirms nothing; --confirmed is refused and stores nothing, so no sentence is said.
    refused = run("remember", "my-app", "Reviews are required", "--kind", "constraint", "--confirmed")
    assert refused.exit_code == 2 and told == []
    # brief leaves tasks and checkpoints out of its review list, so the proposed sentence is not said for them.
    told.clear()
    assert run("remember", "my-app", "Add rate limiting", "--kind", "task").exit_code == 0
    assert told[0].startswith("Stored as proposed (task), record ")


def stub_probe(installed, hooks=None):
    """hooks: the clients whose hooks cannot be verified, though they are installed."""
    def probe(client, cwd=None, **kwargs):
        on = client in installed
        return {"installed": on, "mcp": on, "hooks": on and client not in (hooks or ()), "notes": [],
                "verified_resume": on}
    return probe


def test_install_without_a_client_writes_nothing_and_says_what_works_without_one(
        tmp_path, run, told, monkeypatch):
    run("init")
    root = folder(tmp_path, repository=True, commit=True)
    run("project", "add", "my-app", str(root))
    told.clear()
    monkeypatch.setattr(integrations, "_probe_cli", stub_probe(()))
    result = run("install", "my-app")
    assert result.exit_code == 0, result.output
    assert told == [first_start.INSTALL_NO_CLIENT.format(project="my-app")]
    assert not (root / "AGENTS.md").exists() and not (root / "CLAUDE.md").exists()
    assert json.loads(result.stdout)["changed"] == []
    told.clear()
    assert run("install", "my-app").exit_code == 0  # a second run has nothing to write either
    assert told[0].startswith("Neither Codex nor Claude Code") and "nothing was written" in told[0]


def test_install_with_a_client_says_which_files_and_the_next_command(tmp_path, run, told, monkeypatch):
    run("init")
    root = folder(tmp_path, repository=True, commit=True)
    run("project", "add", "my-app", str(root))
    told.clear()
    monkeypatch.setattr(integrations, "_probe_cli", stub_probe(("codex",)))
    assert run("install", "my-app").exit_code == 0
    assert told[0].startswith("Connected: Codex. Written in the project: AGENTS.md, ")
    assert ".codex/config.toml" in told[0] and told[0].endswith("Next: cairnmind run my-app codex.")
    # The terminal has no JSON: what the JSON warns about is said after it, the absent client once.
    assert told[1:] == [first_start.CLIENT_SKIPPED.format(client="claude"), first_start.TRUST_NOTE]
    assert not (root / "CLAUDE.md").exists()
    told.clear()
    assert run("install", "my-app").exit_code == 0
    assert "nothing new (the files were up to date)" in told[0]


def test_an_installed_client_that_is_not_connected_is_not_called_connected_and_not_sent_to_run(
        tmp_path, run, told, monkeypatch):
    run("init")
    run("project", "add", "my-app", str(folder(tmp_path, repository=True, commit=True)))
    told.clear()
    monkeypatch.setattr(integrations, "_probe_cli", stub_probe(("claude",), hooks=("claude",)))
    assert run("install", "my-app").exit_code == 0
    assert told[0].startswith("Written in the project: ")
    assert "Not connected: Claude Code (not in place: the session hooks)" in told[0]
    assert "Connected" not in told[0] and "cairnmind run" not in told[0] and "doctor --contract" in told[0]
    told.clear()
    monkeypatch.setattr(integrations, "_probe_cli", stub_probe(("claude", "codex"), hooks=("claude",)))
    assert run("install", "my-app").exit_code == 0
    # The connected client is the one `run` is pointed at; the other is named with what it lacks.
    assert told[0].startswith("Connected: Codex. ") and told[0].endswith("Next: cairnmind run my-app codex.")
    assert told[1].startswith("Not connected: Claude Code (not in place: the session hooks)")


def test_run_with_an_unsupported_client_is_not_told_to_install_it(tmp_path, run, monkeypatch):
    run("init")
    run("project", "add", "my-app", str(folder(tmp_path, repository=True, commit=True)))
    monkeypatch.setattr(cli, "_ensure_service_or_refuse", lambda settings: None)
    result = run("run", "my-app", "foo")
    # The ValueError reaches the entry point, which prints it; its text is what the person reads.
    assert str(result.exception) == "Unsupported client: foo"


# --- nothing starts in the background without a word --------------------------------------------------------------

def test_the_service_is_announced_when_a_command_starts_it_and_not_when_it_already_runs(tmp_path, monkeypatch, capsys):
    from cairnmind.config import Settings
    settings = Settings.load(tmp_path / "data", create=True)
    commands = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, **kwargs: commands.append(command) or io.StringIO())
    alive = iter([False, True])
    monkeypatch.setattr(cli, "service_alive", lambda _: next(alive))
    assert cli.ensure_service(settings) is True
    alive = iter([False, True])
    cli._ensure_service_or_refuse(settings)
    said = capsys.readouterr().err
    assert said == f"Cairnmind: {first_start.SERVICE_STARTED.format(url=settings.url, data_dir=settings.data_dir)}\n"
    monkeypatch.setattr(cli, "service_alive", lambda _: True)
    assert cli.ensure_service(settings) is False
    cli._ensure_service_or_refuse(settings)
    assert capsys.readouterr().err == "" and len(commands) == 2


def test_the_engine_is_announced_before_a_command_starts_it_and_not_when_it_answers(tmp_path, monkeypatch, capsys):
    from cairnmind import engine
    from cairnmind.config import Settings
    settings = Settings.load(tmp_path / "data", create=True)
    started = []
    monkeypatch.setattr(engine, "engine_mode", lambda _: "native")
    monkeypatch.setattr(engine, "connect_only", lambda _: False)
    monkeypatch.setattr(engine, "engine_status", lambda *a, **k: {"available": True, "version_matches": True})
    monkeypatch.setattr(engine, "start_engine", lambda _: started.append(capsys.readouterr().err))
    engine.ensure_engine(settings)
    assert started == [] and capsys.readouterr().err == ""
    monkeypatch.setattr(engine, "engine_status", lambda *a, **k: {"available": False})
    engine.ensure_engine(settings)
    assert started == ["cairnmind: " + first_start.ENGINE_STARTING.format(mode="native", data_dir=settings.data_dir)
                       + "\n"]  # printed before start_engine ran, without a word about running on
    assert capsys.readouterr().err == "cairnmind: " + first_start.ENGINE_STARTED.format(mode="native") + "\n"
    assert "keeps running" not in first_start.ENGINE_STARTING
    # A start that is refused at once never says the engine keeps running.
    monkeypatch.setattr(engine, "start_engine", lambda _: (_ for _ in ()).throw(engine.ArcadeError("not installed")))
    with pytest.raises(engine.ArcadeError):
        engine.ensure_engine(settings)
    assert "keeps running" not in capsys.readouterr().err


def test_the_service_started_for_an_external_client_is_said_on_standard_error(tmp_path, monkeypatch, capsys):
    from cairnmind import mcp_server
    from cairnmind.config import Settings
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr("cairnmind.store_writer.inspect", lambda _: {"role": "writer"})  # no engine is asked
    monkeypatch.setattr(cli, "ensure_service", lambda _: True)
    mcp_server.start_service(settings)
    captured = capsys.readouterr()
    assert captured.out == "" and first_start.SERVICE_STARTED.format(url=settings.url, data_dir=settings.data_dir) in captured.err
    monkeypatch.setattr(cli, "ensure_service", lambda _: False)
    mcp_server.start_service(settings)
    assert capsys.readouterr().err == ""


def test_the_brief_of_an_empty_project_says_how_to_start_and_of_a_proposal_how_it_is_reviewed(
        tmp_path, run, told, person_terminal):
    run("init")
    run("project", "add", "my-app", str(folder(tmp_path, repository=True, commit=True)))
    empty = run("brief", "my-app")
    assert empty.exit_code == 0, empty.output
    start = first_start.BRIEF_START.format(project="my-app")
    assert empty.output.splitlines()[-1] == start
    assert json.loads(run("brief", "my-app", "--json").output)["next"] == start
    run("remember", "my-app", "Accounts lock after five failed logins.")
    shown = run("brief", "my-app")
    assert "1 proposed finding waiting for review" in shown.output
    assert shown.output.splitlines()[-1] == first_start.BRIEF_REVIEW
    payload = json.loads(run("brief", "my-app", "--json").output)
    assert payload["next"] == first_start.BRIEF_REVIEW
    assert not any(line["text"] == first_start.BRIEF_REVIEW for line in payload["lines"])  # the marked lines are as before
