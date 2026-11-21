"""What the commands of the first start say they did and what comes next (#287)."""
import io
import json
import subprocess

import pytest
from typer.testing import CliRunner

from cairnmind import cli, first_start, ingest, integrations


@pytest.fixture
def told(monkeypatch):
    """The sentences `_tell` was given; nothing reaches a terminal."""
    sentences = []
    monkeypatch.setattr(cli, "_tell", sentences.append)
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


def test_tell_prints_only_in_a_terminal_and_on_standard_error(monkeypatch, capsys):
    cli._tell("hello")
    quiet = capsys.readouterr()
    assert quiet.out == quiet.err == ""
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    terminal = Terminal()
    monkeypatch.setattr("sys.stderr", terminal)
    cli._tell("hello")
    assert terminal.getvalue() == "Cairnmind: hello\n"


def test_project_add_says_what_it_did_and_warns_about_a_folder_without_git(tmp_path, run, told):
    run("init")
    root = folder(tmp_path)
    added = run("project", "add", "my-app", str(root))
    assert added.exit_code == 0, added.output
    assert json.loads(added.stdout)["project"]["name"] == "my-app"  # the JSON is unchanged
    assert told[0].startswith(f"Registered my-app in {root.resolve()}: ") and "Next: cairnmind remember my-app" in told[0]
    assert told[1:] == [first_start.NOT_GIT.format(root=root.resolve())]


def test_project_add_warns_about_a_repository_without_a_commit_and_stays_quiet_with_one(tmp_path, run, told):
    run("init")
    empty = folder(tmp_path, "empty", repository=True)
    assert run("project", "add", "empty", str(empty)).exit_code == 0
    assert told[1:] == [first_start.NO_COMMIT.format(root=empty.resolve())]
    told.clear()
    ready = folder(tmp_path, "ready", repository=True, commit=True)
    assert run("project", "add", "ready", str(ready)).exit_code == 0
    assert len(told) == 1 and told[0].startswith("Registered ready in ")


def test_a_project_name_with_a_space_is_quoted_in_the_next_step(tmp_path, run, told):
    run("init")
    root = folder(tmp_path, repository=True, commit=True)
    assert run("project", "add", "my app", str(root)).exit_code == 0
    assert "cairnmind remember 'my app' " in told[0]


def test_remember_says_a_proposed_finding_is_not_confirmed_and_where_to_see_it(run, told, tmp_path, person_terminal):
    run("init")
    run("project", "add", "my-app", str(folder(tmp_path, repository=True, commit=True)))
    told.clear()
    proposed = run("remember", "my-app", "Accounts lock after five failed logins.")
    assert json.loads(proposed.stdout)["status"] == "proposed"
    assert told == [first_start.REMEMBER_PROPOSED.format(kind="fact", project="my-app")]
    told.clear()
    confirmed = run("remember", "my-app", "Reviews are required", "--kind", "constraint", "--confirmed")
    assert json.loads(confirmed.stdout)["status"] == "confirmed" and told == []


def stub_probe(installed):
    def probe(client, cwd=None, **kwargs):
        on = client in installed
        return {"installed": on, "mcp": on, "hooks": on, "notes": [], "verified_resume": on}
    return probe


def test_install_without_a_client_names_the_files_it_wrote_and_what_works_without_one(
        tmp_path, run, told, monkeypatch):
    run("init")
    root = folder(tmp_path, repository=True, commit=True)
    run("project", "add", "my-app", str(root))
    told.clear()
    monkeypatch.setattr(integrations, "_probe_cli", stub_probe(()))
    result = run("install", "my-app")
    assert result.exit_code == 0, result.output
    assert told == [first_start.INSTALL_NO_CLIENT.format(files="AGENTS.md, CLAUDE.md", project="my-app")]
    assert any("is not installed, so only its instruction block was written" in w
               for w in json.loads(result.stdout)["warnings"])
    told.clear()
    assert run("install", "my-app").exit_code == 0  # a second run has nothing new to write
    assert told[0].startswith("Neither Codex nor Claude Code") and "nothing new" in told[0]


def test_install_with_a_client_says_which_files_and_the_next_command(tmp_path, run, told, monkeypatch):
    run("init")
    root = folder(tmp_path, repository=True, commit=True)
    run("project", "add", "my-app", str(root))
    told.clear()
    monkeypatch.setattr(integrations, "_probe_cli", stub_probe(("codex",)))
    assert run("install", "my-app").exit_code == 0
    assert len(told) == 1 and told[0].startswith("Connected: Codex. Written in the project: AGENTS.md, ")
    assert ".codex/config.toml" in told[0] and told[0].endswith("Next: cairnmind run my-app codex.")
    told.clear()
    assert run("install", "my-app").exit_code == 0
    assert "nothing new (the files were up to date)" in told[0]


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
                       + "\n"]  # printed before start_engine ran


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
