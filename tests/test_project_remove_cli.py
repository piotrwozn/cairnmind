"""`cairnmind project remove`, `project list --removed` and what `project add` says about a removed project (#327).

No engine: a fake store, a fake run registry, no tmux. Every test runs unchanged on macOS."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import plain_words

from cairnmind import cli, first_start, ingest, store_writer
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
        "bring_back": "cairnmind project add demo /src/demo", "extra_roots": ["/src/demo-wt"],
        "delete": "cairnmind forget p1 --delete-project"}


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
    assert "adds each again: /src/demo-wt" in text and "cairnmind forget p1 --delete-project" in text


def test_yes_applies_and_the_json_in_a_pipe_is_the_result(remove_world, run):
    said = run("project", "remove", "demo", "--yes", terminal=False)
    result = {**PLAN, "applied": True, "removed_at": REMOVED_AT}
    assert said.exit_code == 0 and said.stdout == dumped(result) and remove_world == [PLAN]
    typed = run("project", "remove", "demo", "--yes")
    assert typed.exit_code == 0 and typed.stdout.splitlines()[0].startswith("Removed demo from the registry")
    assert typed.stdout.splitlines()[-1] == removal.NEXT_LIST
    dry = run("project", "remove", "demo", terminal=False)  # the dry run's JSON, with its exit code
    assert dry.exit_code == 1 and json.loads(dry.stdout)["applied"] is False


def test_a_live_workspace_run_refuses_and_names_it(monkeypatch, run):
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: FakeStore())
    monkeypatch.setattr(store_writer, "require_writer", lambda store: None)
    monkeypatch.setattr(NativeRuntime, "list_runs", lambda self, project_id=None: [
        {"client": "claude", "lifecycle": "running"}, {"client": "codex", "lifecycle": "strange"},
        {"client": "claude", "lifecycle": "exited"}])
    said = run("project", "remove", "demo", "--yes")
    assert said.exit_code == 2
    assert "demo has a session running in the workspace (claude running, codex strange)" in plain_words(said.output)


def test_an_unreadable_run_registry_refuses(monkeypatch, run):
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: FakeStore())
    monkeypatch.setattr(store_writer, "require_writer", lambda store: None)

    def unreadable(self, project_id=None):
        raise ValueError("Native runtime registry must be owner-only")
    monkeypatch.setattr(NativeRuntime, "list_runs", unreadable)
    said = run("project", "remove", "demo", "--yes")
    assert said.exit_code == 2
    assert "could not be read (Native runtime registry must be owner-only), so nothing was changed" \
        in plain_words(said.output)


def test_a_reader_installation_refuses_in_one_line(monkeypatch, run):
    reader = FakeStore()
    reader.not_writer = "Another installation writes this store"
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: reader)
    said = run("project", "remove", "demo", "--yes")
    assert said.exit_code == 1 and isinstance(said.exception, store_writer.StoreNotWriter)
    assert "Another installation writes this store" in str(said.exception)


def test_root_refusals_name_the_main_folder_and_the_known_folders():
    store = FakeStore()
    with pytest.raises(removal.Refused, match=r"/src/demo is the main folder of demo; .*project remove demo removes"):
        removal.plan(None, store, PROJECT, Path("/src/demo"))
    with pytest.raises(removal.Refused, match=r"/elsewhere is not a folder of demo; its folders are: "
                                              r"/src/demo, /src/demo-wt\."):
        removal.plan(None, store, PROJECT, Path("/elsewhere"))
    with pytest.raises(removal.Refused, match=r"scheduled snapshot of demo writes into /src/demo-wt; .*memory auto"):
        removal.plan(None, FakeStore(scheduled=True), PROJECT, Path("/src/demo-wt"))
    assert removal.plan(None, store, PROJECT, Path("/src/demo-wt")) == {
        "project": PROJECT, "root": "/src/demo-wt", "applied": False,
        "bring_back": "cairnmind project root demo /src/demo-wt"}


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
    (tmp_path / "CLAUDE.md").write_text("notes\n<!-- BEGIN cairnmind -->\nresume\n")
    (tmp_path / "AGENTS.md").write_text("our own rules\n")
    (tmp_path / ".memory" / "cairnmind").mkdir(parents=True)
    outside = tmp_path / "elsewhere.json"
    outside.write_text('{"cairnmind": true}')
    (tmp_path / ".mcp.json").symlink_to(outside)
    assert removal._folder_files(str(tmp_path)) == [str(tmp_path / "CLAUDE.md"), str(tmp_path / ".memory" / "cairnmind")]


class AddStore:
    def list_projects(self):
        return []

    def add_project(self, name, root):
        return {"id": "p1", "name": name, "root": str(root)}


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
    assert set(plain) == {"project", "indexed", "history"}  # the JSON of a plain add is what it was


class ListStore:
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
        {"id": "p0", "name": "old", "root": "/old", "roots": ["/w"], "removed_at": REMOVED_AT}]
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


def test_without_a_project_the_usage_names_PROJECT(run):
    said = plain_words(run("project", "remove").output)
    assert "Missing argument 'PROJECT'" in said and "project name comes first" not in said
