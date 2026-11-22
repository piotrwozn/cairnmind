"""`cairnmind restore` keeps a person's acts only with the open workspace's key (#214, restore_claims).

Real ArcadeDB; the workspace is answered by patching `person_proof.restore_proof` (a real tmux server is a
manual check). The command line is run with CliRunner, so standard output and standard error are read apart.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cairnmind import person_proof, record_kinds
from cairnmind.cli import app
from cairnmind.confirmation import confirmation_of, import_mark, local_verdict
from cairnmind.inspector import Inspector
from cairnmind.memory_acts import confirm_plan
from cairnmind.plans import PlanManager
from cairnmind.restore_claims import BackupClaims
from cairnmind.sql import update_document
from cairnmind.store import Store

from conftest import as_person, confirmed_before_rule
from test_backup_restore_stream import comparable

KIND = "future-claude-record"


class World:
    def __init__(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        self.tmp = tmp_path
        self.store = Store(tmp_path / "data")
        self.pid = self.store.add_project("demo", root)["id"]
        note = self.store.snapshot(self.pid, "manual://note", "A note a person wrote", "manual")
        self.note = [note["id"]]
        remember = lambda content, **extra: self.store.remember(  # noqa: E731
            self.pid, {"kind": "decision", "content": content, **extra}, self.note)
        self.typed = as_person(self.store, self.pid, {"kind": "decision", "content": "Typed by a person"}, self.note)
        reviewed = remember("Reviewed in the inspector")
        Inspector(self.store).review(self.pid, reviewed["id"], "confirm", reviewed["version"])
        self.reviewed = reviewed["id"]
        self.plans = PlanManager(self.store)
        saved = self.plans.save(self.pid, {"title": "Ship", "goal": "Ship it", "constraints": []},
                                [{"key": "build", "content": "Build it"}, {"key": "test", "content": "Test it"}],
                                "plan-1")
        self.plan_id = saved["plan"]["id"]
        confirmed = confirm_plan(self.store, self.pid, self.plan_id, self.plans.get(self.pid, self.plan_id)["plan"]["version"])
        self.steps = [task["id"] for task in confirmed["tasks"]]
        self.before_rule = remember("Confirmed before the rule")["id"]
        confirmed_before_rule(self.store, self.before_rule)
        self.imported = remember("Brought by an archive")["id"]
        self.mark(self.imported, "confirmed", import_mark("archive", "other.json", [{"name": "Bo"}], "2026-01-01", 1))
        disputed = remember("Disputed by a person")
        Inspector(self.store).review(self.pid, disputed["id"], "dispute", disputed["version"], reason="wrong")
        self.disputed = disputed["id"]
        record_kinds.act_as_person(self.store, self.pid, "claude", KIND, "Because", route="inspector_review")
        self.backup = self.store.backup(tmp_path / "b.cairnmind.json.gz")

    def mark(self, record_id, status, mark):
        row = self.row(record_id)
        metadata = {**row["metadata"], "confirmation": mark}
        with self.store.transaction() as tx:
            update_document(tx, "records", {"status": status, "metadata": metadata}, "id=:id", {"id": record_id})
            update_document(tx, "record_revisions", {"snapshot": {**row, "status": status, "metadata": metadata}},
                            "id=:id", {"id": f"{record_id}:{row['version']}"})

    def row(self, record_id):
        return {key: value for key, value in self.store.query(
            "SELECT FROM records WHERE id=:id", {"id": record_id})[0].items() if not key.startswith("@")}

    def head(self, record_id):
        return self.store.query("SELECT snapshot FROM record_revisions WHERE id=:id",
                                {"id": f"{record_id}:{self.row(record_id)['version']}"})[0]["snapshot"]

    def records(self):
        return {row["id"]: {key: row[key] for key in ("status", "version", "content", "key", "source_ids")}
                for row in self.store.query("SELECT FROM records")}

    def project(self):
        return self.store.query("SELECT FROM projects WHERE id=:id", {"id": self.pid})[0]

    def restore(self, monkeypatch, proof, backup=None):
        monkeypatch.setattr("cairnmind.cli.service_alive", lambda settings: False)
        monkeypatch.setattr(person_proof, "restore_proof", lambda settings: proof)
        return CliRunner().invoke(app, ["--data-dir", str(self.store.data_dir), "restore", str(backup or self.backup)])


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_restore_without_the_workspace_turns_confirmations_into_the_backups_claims(world, monkeypatch):
    before = world.records()
    assert [confirmation_of(world.row(i))["by"] for i in (world.typed["id"], world.reviewed, *world.steps)] == ["person"] * 4
    result = world.restore(monkeypatch, "unavailable")
    assert result.exit_code == 0, (result.output, result.exception)
    said = json.loads(result.stdout)
    assert said == {"restored": True, "person_confirmations": "claims_of_the_backup",
                    "confirmations_turned_into_claims": 5, "record_kinds_withdrawn": 1}
    assert "Restored without the person's workspace" in result.stderr
    assert "5 confirmed record(s)" in result.stderr and "1 accepted transcript record kind(s)" in result.stderr
    assert world.records() == before
    for record_id in (world.typed["id"], world.reviewed, *world.steps, world.before_rule):
        for mark in (world.row(record_id)["metadata"]["confirmation"], world.head(record_id)["metadata"]["confirmation"]):
            assert (mark["by"], mark["package"], mark["where"]) == ("import", "backup", world.backup.name)
    assert world.row(world.typed["id"])["metadata"]["confirmation"]["confirmed_by"][0]["action"] == "cli"
    assert world.row(world.before_rule)["metadata"]["confirmation"]["confirmed_by"] == []
    assert world.row(world.imported)["metadata"]["confirmation"]["package"] == "archive"
    assert world.row(world.disputed)["status"] == "disputed" and "confirmation" not in world.row(world.disputed)["metadata"]
    project = world.project()
    assert project[record_kinds.FIELD] == []
    assert [entry["withdrawn_route"] for entry in project[record_kinds.WITHDRAWN_FIELD]] == ["restore"]
    assert "confirmations_before_rule" not in said


def test_restore_with_the_workspace_open_keeps_the_persons_confirmations(world, monkeypatch):
    # A record a review audit covers and that carries no mark: the upgrade step marks it as it always did.
    old = world.store.remember(world.pid, {"kind": "decision", "content": "Reviewed long ago"}, world.note)
    Inspector(world.store).review(world.pid, old["id"], "confirm", old["version"])
    confirmed_before_rule(world.store, old["id"])
    backup = world.store.backup(world.tmp / "kept.cairnmind.json.gz")
    marks = {i: world.row(i)["metadata"].get("confirmation") for i in (world.typed["id"], world.reviewed, *world.steps)}
    accepted = world.project()[record_kinds.FIELD]
    result = world.restore(monkeypatch, "key", backup)
    assert result.exit_code == 0, (result.output, result.exception)
    said = json.loads(result.stdout)
    assert said["person_confirmations"] == "kept" and said["confirmations_before_rule"]["marked_as_a_persons"] == 1
    assert result.stderr == ""
    assert {i: world.row(i)["metadata"].get("confirmation") for i in marks} == marks
    assert world.project()[record_kinds.FIELD] == accepted and accepted
    assert confirmation_of(world.row(old["id"]))["by"] == "person"


def test_an_old_workspace_is_named_and_kept_nothing(world, monkeypatch):
    result = world.restore(monkeypatch, "no_key")
    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.stdout)["person_confirmations"] == "claims_of_the_backup"
    assert "started by an earlier version" in result.stderr and "cairnmind workspace stop" in result.stderr
    assert confirmation_of(world.row(world.reviewed))["by"] == "import"


def test_a_restored_claim_is_not_a_kept_verdict(world, monkeypatch):
    assert world.restore(monkeypatch, "unavailable").exit_code == 0
    typed = world.row(world.typed["id"])
    changed = world.store.remember(world.pid, {"id": typed["id"], "expected_version": typed["version"],
                                               "content": "A session changed it"}, typed["source_ids"])
    assert changed["status"] == "proposed" and confirmation_of(changed)["by"] == "lapsed"
    current = world.plans.get(world.pid, world.plan_id)
    world.plans.save(world.pid, {"id": world.plan_id, "title": "Ship differently"},
                     [{"key": "build", "content": "Build it"}, {"key": "test", "content": "Test it"}], "plan-2",
                     expected_version=current["plan"]["version"],
                     task_versions={task["id"]: task["version"] for task in current["tasks"]})
    assert world.plans.get(world.pid, world.plan_id)["plan"]["title"] == "Ship differently"
    for record_id in (world.reviewed, *world.steps):
        assert not local_verdict(world.row(record_id)) and not local_verdict(world.head(record_id))


def test_the_person_confirms_a_restored_claim_again(world, monkeypatch):
    assert world.restore(monkeypatch, "unavailable").exit_code == 0
    reviewed = world.row(world.reviewed)
    Inspector(world.store).review(world.pid, reviewed["id"], "confirm", reviewed["version"])
    mark = world.row(world.reviewed)["metadata"]["confirmation"]
    assert mark["by"] == "person" and mark["route"] == "inspector_review"
    plan = world.plans.get(world.pid, world.plan_id)
    confirm_plan(world.store, world.pid, world.plan_id, plan["plan"]["version"])
    for step in world.steps:
        assert world.row(step)["metadata"]["confirmation"]["route"] == "confirm_plan"
        assert world.row(step)["metadata"]["confirmation"]["by"] == "person"
    again = record_kinds.act_as_person(world.store, world.pid, "claude", KIND, "Still technical", route="inspector_review")
    assert again["status"] == "accepted" and world.project()[record_kinds.FIELD]


def test_a_failure_in_the_middle_leaves_the_store_as_it_was(world, monkeypatch):
    before = comparable(world.store.export())
    turn, seen = BackupClaims.turn, []

    def failing(self, collection, row):
        if collection == "records":
            seen.append(row["id"])
            if len(seen) == 2:
                raise RuntimeError("the second record fails")
        return turn(self, collection, row)

    monkeypatch.setattr(BackupClaims, "turn", failing)
    with pytest.raises(RuntimeError, match="second record"):
        world.store.restore(world.backup, claims=BackupClaims(world.backup.name))
    assert comparable(world.store.export()) == before
    assert confirmation_of(world.row(world.reviewed))["by"] == "person"
    assert world.project()[record_kinds.FIELD]


def test_the_before_restore_copy_undoes_a_restore_without_the_workspace(world, monkeypatch):
    marks = {i: world.row(i)["metadata"]["confirmation"] for i in (world.typed["id"], world.reviewed, *world.steps)}
    accepted = world.project()[record_kinds.FIELD]
    assert world.restore(monkeypatch, "unavailable").exit_code == 0
    copies = list((world.store.data_dir / "backups").glob("before-restore-*"))
    assert len(copies) == 1
    result = world.restore(monkeypatch, "key", copies[0])
    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.stdout)["person_confirmations"] == "kept"
    assert {i: world.row(i)["metadata"]["confirmation"] for i in marks} == marks
    assert world.project()[record_kinds.FIELD] == accepted
