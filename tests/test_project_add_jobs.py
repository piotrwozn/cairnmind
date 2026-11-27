"""What `project add` says about the jobs it queued, and that it starts nothing (#366)."""
import json

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.cli_cmds import first_start, upgrade_mark
from cairnmind.scip import ingest
from cairnmind.agents import knowledge_mode
from cairnmind.memory import project_removal
from cairnmind.terminal import setup_mark
from cairnmind.storage.store_writer import StoreNotWriter


class Store:
    def __init__(self, indexing=0, extraction=0):
        self.counts = {"indexing": indexing, "extraction": extraction}

    def list_projects(self):
        return []

    def add_project(self, name, root):
        return {"id": "p1", "name": name, "root": str(root)}

    def job_summary(self, project_id):
        return {group: {"pending": self.counts[group], "running": 0} for group in ("indexing", "extraction")}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A project add whose store has jobs; the service is stopped, models are on, no start is allowed."""
    def forbidden(*args, **kwargs):
        raise AssertionError("project add starts nothing")
    for name in ("ensure_service", "_ensure_service_or_refuse"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: Store(indexing=2, extraction=1))
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(knowledge_mode, "enabled", lambda settings: True)
    monkeypatch.setattr(project_removal, "removed_rows", lambda reader: [])
    monkeypatch.setattr(ingest, "index_project", lambda store, pid: {"files": 1})
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    folder = tmp_path / "plain"
    folder.mkdir()

    def add(terminal=True):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        return CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "memory"), "project", "add", "plain",
                                            str(folder)])
    return add


def test_a_stopped_service_leaves_the_jobs_waiting_and_project_add_says_so_and_names_the_command(world):
    said = world().stdout.splitlines()
    assert said[-2] == first_start.JOBS_HELD_STOPPED.format(count=3, project="plain")
    assert said[-1].startswith("Next: cairnmind remember plain")  # the next step stays last
    assert "cairnmind index plain starts it now" in said[-2]
    assert json.loads(world(terminal=False).stdout)["jobs"] == {
        "queued": 3, "service_running": False, "waiting_because": "service_stopped"}


def test_a_running_service_runs_the_jobs_and_nothing_is_said(monkeypatch, world):
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    assert len(world().stdout.splitlines()) == 3  # registered, not a git repository, next
    assert json.loads(world(terminal=False).stdout)["jobs"] == {
        "queued": 3, "service_running": True, "waiting_because": None}


def test_without_models_the_jobs_are_held_and_the_deterministic_only_sentence_is_kept(monkeypatch, world):
    monkeypatch.setattr(knowledge_mode, "enabled", lambda settings: False)
    for alive in (False, True):  # a running service holds them too
        monkeypatch.setattr(cli, "service_alive", lambda settings: alive)
        assert world().stdout.splitlines()[-2] == first_start.JOBS_HELD_NO_MODELS.format(count=3)
        assert json.loads(world(terminal=False).stdout)["jobs"] == {
            "queued": 3, "service_running": alive, "waiting_because": "models_not_configured"}
    assert "deterministic-only mode" in first_start.JOBS_HELD_NO_MODELS and first_start.GET_MODELS in first_start.JOBS_HELD_NO_MODELS


def test_an_upgrade_mark_is_named_as_the_reason_the_jobs_wait(monkeypatch, world):
    monkeypatch.setattr(upgrade_mark, "refusal", lambda: "An upgrade of Cairnmind is in progress.")
    said = world().stdout.splitlines()
    assert said[-2] == first_start.JOBS_HELD_UPGRADE.format(count=3, reason="An upgrade of Cairnmind is in progress.")
    assert json.loads(world(terminal=False).stdout)["jobs"]["waiting_because"] == "upgrade_in_progress"


def test_a_change_of_setup_is_named_as_the_reason_the_jobs_wait_and_index_is_not_advised(monkeypatch, world):
    monkeypatch.setattr(setup_mark, "refusal", lambda data_dir: "A change of where storage and models run is in progress.")
    said = world().stdout.splitlines()
    assert said[-2] == first_start.JOBS_HELD_UPGRADE.format(
        count=3, reason="A change of where storage and models run is in progress.")
    assert "cairnmind index" not in said[-2]
    assert json.loads(world(terminal=False).stdout)["jobs"]["waiting_because"] == "setup_in_progress"


def test_an_invalid_knowledge_agent_does_not_fail_an_add_that_is_done(monkeypatch, world):
    def invalid(settings):
        raise ValueError(first_start.KNOWLEDGE_INVALID.format(value="'maybe'"))
    monkeypatch.setattr(knowledge_mode, "enabled", invalid)
    added = world()
    assert added.exit_code == 0 and added.stdout.splitlines()[0].startswith("Registered plain")
    assert "knowledge_agent in config.json is 'maybe'" in added.stdout
    assert json.loads(world(terminal=False).stdout)["jobs"]["waiting_because"] == "models_not_configured"


def test_no_jobs_say_nothing(monkeypatch, world):
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: Store())
    assert len(world().stdout.splitlines()) == 3
    assert json.loads(world(terminal=False).stdout)["jobs"]["queued"] == 0


def test_a_reader_refuses_before_anything_is_counted_or_started(monkeypatch, world):
    class Reader(Store):
        def add_project(self, name, root):
            raise StoreNotWriter("another installation writes this store")
    monkeypatch.setattr(cli, "store_for", lambda *a, **k: Reader(indexing=1))
    added = world()
    assert added.exit_code != 0 and isinstance(added.exception, StoreNotWriter)


def test_the_new_sentences_have_polish():
    from cairnmind.cli_cmds.first_start import MESSAGES
    for sentence in (first_start.JOBS_HELD_STOPPED, first_start.JOBS_HELD_UPGRADE, first_start.JOBS_HELD_NO_MODELS):
        assert MESSAGES[sentence] != sentence
        for key in ("count", "project", "reason"):
            assert (f"{{{key}}}" in sentence) == (f"{{{key}}}" in MESSAGES[sentence])


def test_on_a_real_store_a_stopped_service_leaves_counted_jobs_waiting(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: False)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.md").write_text("# Notes\n\nThe login locks an account after five failed attempts.\n")
    data = tmp_path / "memory"
    runner = CliRunner()
    assert runner.invoke(cli.app, ["--data-dir", str(data), "init"]).exit_code == 0
    added = runner.invoke(cli.app, ["--data-dir", str(data), "project", "add", "docs", str(root)])
    assert added.exit_code == 0, added.output
    jobs = json.loads(added.stdout)["jobs"]
    assert jobs["queued"] > 0 and jobs["service_running"] is False and jobs["waiting_because"] == "service_stopped"
