"""`cairnmind project remove`, `project list --removed` and what `project add` says about a removed project (#327).

No engine: a fake store, a fake run registry, no tmux. Every test runs unchanged on macOS."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from conftest import plain_words

from cairnmind import cli, client_runtime, first_start, ingest, store_writer
from cairnmind import project_removal as removal
from cairnmind.client_runtime import NativeRuntime
from cairnmind.terminal_i18n import tr

PROJECT = {"id": "p1", "name": "demo", "root": "/src/demo"}
REMOVED_AT = "2026-11-22T10:30:00+00:00"
PLAN = {"project": PROJECT, "root": None, "applied": False, "removed_at": None,
        "roots": ["/src/demo", "/src/demo-wt"], "queued_jobs": 4, "transcripts_followed": 3,
        "scheduled_snapshot": True,
        "kept": {"records": 12, "plans": 2, "sources": 340, "sessions": 5, "check_runs": 1, "chunks": 900,
                 "nodes": 800, "embeddings": 900, "answer_log": "/data/answer-log/p1/answers.jsonl",
                 "backups": "/data/backups", "folder": ["/src/demo/CLAUDE.md"]},
        "nested": [{"root": "/src/demo", "project": "mono", "parent": "/src"}],
        "bring_back": "cairnmind project add demo /src/demo", "no_way_back": False, "extra_roots": ["/src/demo-wt"],
        "delete": "workspace: More, then Delete project memory"}


class FakeStore:
    not_writer = None

    def __init__(self, scheduled=False):
        self.scheduled = scheduled

    def resolve_project(self, value):
        return PROJECT

    def query(self, sql, params=None):
        if "FROM project_roots WHERE project_id" in sql:
            return [{"path": "/src/demo"}, {"path": "/src/demo-wt"}]
        if "FROM memory_settings" in sql:
            return [{"id": "p1"}] if self.scheduled else []
        return []


@pytest.fixture
def run(monkeypatch, tmp_path):
    def invoke(*arguments, terminal=True):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), *arguments])
    return invoke


@pytest.fixture
def remove_world(monkeypatch):
    """A writer with an idle project; `plan` and `apply` are the module's, replaced one by one in a test."""
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: FakeStore())
    monkeypatch.setattr(store_writer, "require_writer", lambda store: None)
    monkeypatch.setattr(removal, "require_idle", lambda settings, store, project: None)
    calls = []
    monkeypatch.setattr(removal, "plan", lambda settings, store, project, root=None: PLAN)
    monkeypatch.setattr(removal, "apply", lambda store, result: calls.append(result) or {
        **result, "applied": True, "removed_at": REMOVED_AT})
    return calls


def dumped(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"


def test_remove_inside_a_session_is_refused_before_the_store_is_opened(monkeypatch, run):
    def forbidden(*args, **kwargs):
        raise AssertionError("the store is not opened inside a session")
    monkeypatch.setattr(cli, "store_for", forbidden)
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "s1")
    said = run("project", "remove", "demo", "--yes")
    assert said.exit_code == 2 and removal.IN_SESSION in plain_words(said.output)


def test_dry_run_says_what_stops_and_what_stays_and_changes_nothing(remove_world, run):
    said = run("project", "remove", "demo")
    lines = said.stdout.splitlines()
    assert said.exit_code == 1 and remove_world == []
    assert lines[0] == removal.DRY_RUN.format(project="demo")
    assert lines[1].startswith("It would stop: demo leaves cairnmind project list")
    assert lines[-1] == removal.NEXT_YES.format(project="demo")
    text = said.stdout
    assert "Queued jobs of demo (4)" in text and removal.SCHEDULE_PAUSED in text
    assert "Kept in the store: 12 records, 2 plans, 340 sources, 5 sessions and 1 check runs" in text
    assert "/data/answer-log/p1/answers.jsonl" in text and "these stay as they are: /src/demo/CLAUDE.md" in text
    assert "lies inside the folder /src of mono" in text
    assert "cairnmind project add demo /src/demo brings back the same project" in text
    assert "adds each again: /src/demo-wt" in text and removal.DELETE_STEP in text and "forget" not in text


def test_yes_applies_and_the_json_in_a_pipe_is_the_result(remove_world, run):
    said = run("project", "remove", "demo", "--yes", terminal=False)
    result = {**PLAN, "applied": True, "removed_at": REMOVED_AT}
    assert said.exit_code == 0 and said.stdout == dumped(result) and remove_world == [PLAN]
    typed = run("project", "remove", "demo", "--yes")
    assert typed.exit_code == 0 and typed.stdout.splitlines()[0].startswith("Removed demo from the registry")
    assert typed.stdout.splitlines()[-1] == removal.NEXT_LIST
    dry = run("project", "remove", "demo", terminal=False)  # the dry run's JSON, with its exit code
    assert dry.exit_code == 1 and json.loads(dry.stdout)["applied"] is False


def registry(tmp_path, rows, mode=0o600):
    """A real run registry of the workspace in a temporary data directory, and the settings that name it."""
    directory = tmp_path / "runtime"
    directory.mkdir()
    path = directory / "runs.json"
    path.write_text(json.dumps({"version": 1, "runs": rows}))
    path.chmod(mode)
    return path, SimpleNamespace(data_dir=tmp_path)


def run_row(client, lifecycle, project="p1"):
    return {"id": "r-" + client + lifecycle, "client": client, "lifecycle": lifecycle, "project_id": project,
            "tmux_session": "s-" + client, "generation": 1}


def test_a_live_workspace_run_refuses_and_names_it_and_the_registry_is_not_rewritten(monkeypatch, tmp_path):
    path, settings = registry(tmp_path, [run_row("claude", "running"), run_row("codex", "strange"),
                                         run_row("claude", "exited"), run_row("codex", "running", "other")])
    before = path.read_bytes()
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: "tmux")
    monkeypatch.setattr(NativeRuntime, "_pane", lambda self, row: {"pane": "%1", "pid": 1, "dead": False})
    with pytest.raises(removal.Refused) as refused:
        removal.require_idle(settings, FakeStore(), PROJECT)
    assert "demo has a session running in the workspace (claude running, codex strange)" in str(refused.value)
    assert path.read_bytes() == before  # a dry run is read only


def test_a_run_whose_pane_is_gone_does_not_refuse(monkeypatch, tmp_path):
    path, settings = registry(tmp_path, [run_row("claude", "running")])
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: "tmux")
    monkeypatch.setattr(NativeRuntime, "_pane", lambda self, row: None)
    removal.require_idle(settings, FakeStore(), PROJECT)


def test_a_starting_run_or_one_of_an_unknown_lifecycle_refuses_even_without_a_pane(monkeypatch, tmp_path):
    """A spawn in progress saves its starting row before its tmux session exists: no pane is not an end."""
    path, settings = registry(tmp_path, [run_row("claude", "starting"), run_row("codex", "strange")])
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: "tmux")
    monkeypatch.setattr(NativeRuntime, "_pane", lambda self, row: None)
    with pytest.raises(removal.Refused, match=r"demo has a session running in the workspace \(claude starting, "
                                              r"codex strange\)"):
        removal.require_idle(settings, FakeStore(), PROJECT)


def test_without_tmux_a_live_row_refuses_instead_of_being_marked_ended(monkeypatch, tmp_path):
    path, settings = registry(tmp_path, [run_row("claude", "running")])
    before = path.read_bytes()
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: None)
    with pytest.raises(removal.Refused, match=r"could not be read \(tmux cannot be found\), so nothing was changed"):
        removal.require_idle(settings, FakeStore(), PROJECT)
    assert path.read_bytes() == before
    path.write_text(json.dumps({"version": 1, "runs": [run_row("claude", "exited")]}))
    removal.require_idle(settings, FakeStore(), PROJECT)  # nothing live: tmux is not needed


def test_an_unreadable_run_registry_refuses(monkeypatch, tmp_path):
    from conftest import make_world_readable
    path, settings = registry(tmp_path, [], mode=0o600)
    make_world_readable(path)
    monkeypatch.setattr(client_runtime, "resolve_tmux", lambda *a, **k: "tmux")
    with pytest.raises(removal.Refused, match=r"could not be read \(Native runtime registry must be owner-only\)"):
        removal.require_idle(settings, FakeStore(), PROJECT)


def test_a_reader_installation_refuses_in_one_line(monkeypatch, run):
    reader = FakeStore()
    reader.not_writer = "Another installation writes this store"
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: reader)
    said = run("project", "remove", "demo", "--yes")
    assert said.exit_code == 1 and isinstance(said.exception, store_writer.StoreNotWriter)
    assert "Another installation writes this store" in str(said.exception)


def test_root_refusals_name_the_main_folder_and_the_known_folders(monkeypatch):
    import re
    main, wt, other = str(Path("/src/demo")), str(Path("/src/demo-wt")), str(Path("/elsewhere"))
    project = {**PROJECT, "root": main}

    class LocalStore(FakeStore):
        def query(self, sql, params=None):
            if "FROM project_roots WHERE project_id" in sql:
                return [{"path": main}, {"path": wt}]
            return super().query(sql, params)

    store = LocalStore()
    with pytest.raises(removal.Refused, match=re.escape(main) + r" is the main folder of demo; .*project remove demo removes"):
        removal.plan(None, store, project, Path("/src/demo"))
    with pytest.raises(removal.Refused, match=re.escape(other) + r" is not a folder of demo; its folders are: "
                                              + re.escape(f"{main}, {wt}.")):
        removal.plan(None, store, project, Path("/elsewhere"))
    with pytest.raises(removal.Refused, match=r"scheduled snapshot of demo writes into " + re.escape(wt) + r"; .*memory auto"):
        removal.plan(None, LocalStore(scheduled=True), project, Path("/src/demo-wt"))
    planned = removal.plan(None, store, project, Path("/src/demo-wt"))
    assert planned["project"] == project and planned["root"] == wt and planned["applied"] is False
    assert "cairnmind project root demo" in planned["bring_back"] and wt in planned["bring_back"]


def test_a_root_removal_says_the_effect_and_the_way_back(monkeypatch, remove_world, run):
    plan = {"project": PROJECT, "root": "/src/demo-wt", "applied": False,
            "bring_back": "cairnmind project root demo /src/demo-wt"}
    monkeypatch.setattr(removal, "plan", lambda settings, store, project, root=None: plan)
    dry = run("project", "remove", "demo", "--root", "/src/demo-wt")
    assert dry.exit_code == 1 and dry.stdout.splitlines()[0].endswith("project remove demo --root /src/demo-wt --yes would do.")
    assert removal.ROOT_EFFECT in dry.stdout and dry.stdout.splitlines()[-1].startswith("Next: cairnmind project remove demo --root")
    done = run("project", "remove", "demo", "--root", "/src/demo-wt", "--yes")
    assert done.exit_code == 0 and done.stdout.splitlines()[0] == "Removed the folder /src/demo-wt from demo; nothing was deleted."
    assert done.stdout.splitlines()[-1] == "Next: cairnmind project root demo /src/demo-wt adds it again."


def test_folder_files_are_named_only_when_they_mention_cairnmind(tmp_path):
    from conftest import make_symlink_or_skip
    (tmp_path / "CLAUDE.md").write_text("notes\n<!-- BEGIN cairnmind -->\nresume\n")
    (tmp_path / "AGENTS.md").write_text("our own rules\n")
    (tmp_path / ".memory" / "cairnmind").mkdir(parents=True)
    outside = tmp_path / "elsewhere.json"
    outside.write_text('{"cairnmind": true}')
    make_symlink_or_skip(tmp_path / ".mcp.json", outside)
    assert removal._folder_files(str(tmp_path)) == [str(tmp_path / "CLAUDE.md"), str(tmp_path / ".memory" / "cairnmind")]


class AddStore:
    def list_projects(self):
        return []

    def add_project(self, name, root):
        return {"id": "p1", "name": name, "root": str(root)}

    def job_summary(self, project_id):
        return {group: {state: 0 for state in ("pending", "running")} for group in ("indexing", "extraction")}


@pytest.fixture
def add_world(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: AddStore())
    monkeypatch.setattr(ingest, "index_project", lambda store, pid: {"files": 1})
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    folder = tmp_path / "plain"
    folder.mkdir()
    return folder


def test_project_add_says_it_brought_the_project_back(monkeypatch, run, add_world):
    row = {"id": "p1", "removed_name": "plain", "removed_root": str(add_world.resolve()), "removed_at": REMOVED_AT,
           "removed_roots": ["/x/plain-wt"]}
    monkeypatch.setattr(removal, "removed_rows", lambda reader: [row])
    said = run("project", "add", "plain", str(add_world)).stdout.splitlines()
    assert said[0] == (f"Brought back plain in {add_world.resolve()} with its memory, as it was when removed on "
                       "2026-11-22 10:30 UTC; files indexed: 1.")
    assert said[1] == "Its other folders were not added back; cairnmind project root plain FOLDER adds each again: /x/plain-wt."
    assert said[-1].startswith("Next: cairnmind brief plain") and "not brought back" not in " ".join(said)
    piped = json.loads(run("project", "add", "plain", str(add_world), terminal=False).stdout)
    assert piped["restored"] == {"removed_at": REMOVED_AT, "roots": ["/x/plain-wt"]} and "removed_kept" not in piped


def test_project_add_of_a_new_project_names_the_removed_one_it_did_not_bring_back(monkeypatch, run, add_world):
    other = {"id": "p0", "removed_name": "plain", "removed_root": "/old/plain", "removed_at": REMOVED_AT,
             "removed_roots": []}
    monkeypatch.setattr(removal, "removed_rows", lambda reader: [other])
    said = run("project", "add", "plain", str(add_world)).stdout.splitlines()
    assert said[0].startswith("Registered plain in ")
    assert said[-2] == ("The project removed on 2026-11-22 10:30 UTC as plain in /old/plain keeps its memory and was "
                        "not brought back: only cairnmind project add with that same name and folder brings it back.")
    assert said[-1].startswith("Next: cairnmind remember plain")
    piped = json.loads(run("project", "add", "plain", str(add_world), terminal=False).stdout)
    assert piped["removed_kept"] == [{"id": "p0", "name": "plain", "root": "/old/plain", "removed_at": REMOVED_AT}]
    assert "restored" not in piped
    monkeypatch.setattr(removal, "removed_rows", lambda reader: [])
    plain = json.loads(run("project", "add", "plain", str(add_world), terminal=False).stdout)
    assert set(plain) == {"project", "indexed", "history", "jobs"}  # a plain add's JSON, and what its jobs need (#366)


class ListStore:
    data_dir = Path("/data")

    def list_projects(self):
        return [{"id": "p1", "name": "demo", "root": "/src/demo"}]


def test_project_list_removed_and_the_hint(monkeypatch, run):
    row = {"id": "p0", "removed_name": "old", "removed_root": "/old", "removed_at": REMOVED_AT, "removed_roots": ["/w"]}
    monkeypatch.setattr(cli, "store_for_read", lambda settings: ListStore())
    monkeypatch.setattr(removal, "removed_rows", lambda reader: [row])
    listed = run("project", "list").stdout.splitlines()
    assert listed[-2] == "Removed projects whose memory is kept: 1; cairnmind project list --removed lists them."
    assert listed[-1] == first_start.PROJECTS_NEXT
    removed = run("project", "list", "--removed").stdout.splitlines()
    assert removed[0] == removal.REMOVED_HEADING
    assert removed[1] == "  - old: /old (removed 2026-11-22 10:30 UTC; id p0)"
    assert removed[-1] == removal.REMOVED_NEXT
    assert json.loads(run("project", "list", "--removed", terminal=False).stdout) == [
        {"id": "p0", "name": "old", "root": "/old", "roots": ["/w"], "removed_at": REMOVED_AT, "can_return": True}]
    inside = {**row, "id": "p9", "removed_root": "/data/old"}  # a folder in the data directory cannot be added again
    monkeypatch.setattr(removal, "removed_rows", lambda reader: [inside])
    stuck = run("project", "list", "--removed").stdout
    assert "cairnmind project add cannot bring old back: its folder /data/old lies inside the data directory" in stuck
    assert "deleted in the workspace (More, then Delete project memory)" in stuck and "forget" not in stuck
    monkeypatch.setattr(removal, "removed_rows", lambda reader: [row])
    assert json.loads(run("project", "list", terminal=False).stdout) == ListStore().list_projects()
    monkeypatch.setattr(removal, "removed_rows", lambda reader: [])
    assert run("project", "list", "--removed").stdout.splitlines() == [removal.REMOVED_NONE]
    assert "Removed projects" not in run("project", "list").stdout


def test_help_says_the_memory_is_kept(run):
    said = plain_words(run("project", "remove", "--help").output)
    assert "memory is kept" in said and "--root" in said and "--yes" in said and "nothing is deleted" in said


def test_every_sentence_has_polish_in_the_catalog():
    sentences = {name: value for name, value in vars(removal).items() if name.isupper() and isinstance(value, str)}
    sentences.pop("UNKNOWN_PROJECT_NEXT")  # first_start's, translated there
    assert set(sentences.values()) == set(removal.MESSAGES)
    for english, polish in removal.MESSAGES.items():
        assert polish != english and tr(english, "pl") == polish


def test_the_workspace_says_in_both_languages_that_a_removed_project_came_back():
    back = {"removed_at": REMOVED_AT}
    assert removal.workspace_notice(lambda text, **values: tr(text, "en", **values), PROJECT, "/src/demo", back) == (
        "Brought back demo in /src/demo with its memory, as it was when removed on 2026-11-22 10:30 UTC; it is not a "
        "new project.")
    assert removal.workspace_notice(lambda text, **values: tr(text, "pl", **values), PROJECT, "/src/demo", back) \
        .startswith("Przywrócono demo w /src/demo wraz z pamięcią")


def test_the_workspace_names_the_folders_left_out_and_a_removed_project_it_did_not_bring_back():
    """What Add folder shows after POST /projects, in both languages; no engine, the same on macOS."""
    def say(language, result):
        return removal.workspace_lines(lambda text, **values: tr(text, language, **values), PROJECT, "/src/demo",
                                       result)
    back = {"restored": {"removed_at": REMOVED_AT, "roots": ["/src/demo-wt"]}}
    assert say("en", back)[1] == ("Its other folders were not added back; cairnmind project root demo FOLDER adds each "
                                  "again: /src/demo-wt.")
    kept = {"removed_kept": [{"id": "p0", "name": "demo", "root": "/old/demo", "removed_at": REMOVED_AT}]}
    assert say("en", kept) == ["The project removed on 2026-11-22 10:30 UTC as demo in /old/demo keeps its memory and "
                               "was not brought back: only cairnmind project add with that same name and folder brings "
                               "it back."]
    assert say("pl", kept)[0].startswith("Projekt usunięty 2026-11-22 10:30 UTC jako demo w /old/demo")
    assert say("en", {}) == []


def test_a_refusal_about_a_removed_project_reaches_a_polish_workspace_in_polish():
    """The service sends the English sentence; the panel puts it back together from the Polish template."""
    from cairnmind.terminal_client import BrokerError
    from cairnmind.terminal_i18n_messages import broker_error_text

    def polish(text):
        return tr(text, "pl")
    refused = removal.NEW_REFUSED.format(name="demo", root="/src/demo", when="2026-11-22 10:30 UTC")
    said = broker_error_text(BrokerError(400, refused), polish)
    assert said == "Broker 400: " + removal.MESSAGES[removal.NEW_REFUSED].format(
        name="demo", root="/src/demo", when="2026-11-22 10:30 UTC")
    gone = "Unknown project: p1. " + removal.PROJECT_REMOVED.format(name="demo", root="/src/demo",
                                                                    when="2026-11-22 10:30 UTC")
    assert broker_error_text(BrokerError(404, gone), polish).startswith(
        "Broker 404: Nieznany projekt: p1. Projekt demo został usunięty z rejestru 2026-11-22 10:30 UTC")
    assert broker_error_text(BrokerError(404, "Something else."), polish) == "Broker 404: Something else."


def test_without_a_project_the_usage_names_PROJECT(run):
    said = plain_words(run("project", "remove").output)
    assert "Missing argument 'PROJECT'" in said and "project name comes first" not in said
