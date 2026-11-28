"""A record confirmed before 0.11.0 gets a person's mark at upgrade when a local act covers it (#215).

Real ArcadeDB and Git; no model is asked. Each record is made the way an old store holds it: the act's
note or audit is there, and the record is confirmed without the confirmation mark (conftest).
"""
from __future__ import annotations

import subprocess
from uuid import uuid4

import pytest
from conftest import as_person, confirmed_before_rule, person_edit

from cairnmind.memory.confirmation import confirmation_of, confirmation_text
from cairnmind.memory.confirmation_upgrade import META_NAME, mark_confirmed_before_rule
from cairnmind.memory.findings import save_finding
from cairnmind.memory.memory import MemoryManager
from cairnmind.memory.memory_acts import confirm_plan
from cairnmind.memory.plans import PlanManager
from cairnmind.store import Store
from cairnmind.terminal.inspector import Inspector


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def repository(root, name):
    root.mkdir()
    git(root, "init", "--quiet", "-b", "main")
    git(root, "config", "user.name", name)
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", "app.py")
    git(root, "commit", "--quiet", "-m", "Fixture")
    return root


class Old:
    def __init__(self, tmp_path):
        self.root = repository(tmp_path / "repo", "Ada Nowak")
        self.store = Store(tmp_path / "data")
        self.pid = self.store.add_project("old", self.root)["id"]
        self.note = self.store.snapshot(self.pid, "manual://note", "A note a person wrote", "manual")

    def record(self, record_id):
        return {key: value for key, value in self.store.query(
            "SELECT FROM records WHERE id=:id", {"id": record_id})[0].items() if not key.startswith("@")}

    def revision(self, record):
        return self.store.query("SELECT snapshot FROM record_revisions WHERE id=:id",
                                {"id": f"{record['id']}:{record['version']}"})[0]["snapshot"]

    def reviewed(self, content, **fields):
        """Confirmed in the inspector, as a store before 0.11.0 holds it: the audit, and no mark."""
        saved = self.store.remember(self.pid, {"kind": "decision", "content": content, **fields}, [self.note["id"]])
        result = Inspector(self.store).review(self.pid, saved["id"], "confirm", saved["version"])
        confirmed_before_rule(self.store, saved["id"])
        return self.record(saved["id"]), result["audit_source_id"]


@pytest.fixture
def old(tmp_path):
    return Old(tmp_path)


def unchanged(before, after):
    """The step changes no status, version, source or evidence: only the mark."""
    strip = lambda row: {**row, "metadata": {k: v for k, v in row["metadata"].items() if k != "confirmation"}}  # noqa: E731
    assert strip(before) == strip(after)


def test_a_local_review_that_covers_the_record_as_it_stands_marks_it_a_persons(old):
    record, audit_id = old.reviewed("Use the reviewed decision", metadata={"evidence": "the quoted passage"})
    assert confirmation_of(record) == {"by": "before_rule"}
    audit = old.store.query("SELECT content FROM sources WHERE id=:id", {"id": audit_id})[0]["content"]

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 1 and result["before_rule"] == 0
    after = old.record(record["id"])
    mark = after["metadata"]["confirmation"]
    assert mark["by"] == "person" and mark["route"] == "inspector_review" and mark["source_id"] == audit_id
    assert mark["actor_name"] == "Ada Nowak" and mark["version"] == record["version"]
    assert mark["at"] and mark["at"] in audit and mark["marked_at_upgrade"]
    unchanged(record, after)
    # The revision of that version says the same, and the line names the person and the audit's day.
    assert old.revision(after)["metadata"]["confirmation"] == mark
    line = confirmation_text(after)
    assert line.startswith("confirmed here by a person: Ada Nowak, in the inspector, ") and mark["at"][:10] in line


def test_a_record_whose_text_changed_after_the_review_stays_before_the_rule(old):
    record, _ = old.reviewed("The text the person reviewed")
    # Before 0.11.0 a session could change the text and keep the status confirmed.
    changed = person_edit(old.store, old.pid, {"id": record["id"], "expected_version": record["version"],
                                               "content": "A text nobody reviewed"}, record["source_ids"])
    changed = confirmed_before_rule(old.store, changed["id"])
    assert changed["content"] == "A text nobody reviewed"

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 0 and result["before_rule"] == 1
    assert confirmation_of(old.record(record["id"])) == {"by": "before_rule"}


def test_a_changed_quote_or_key_also_ends_the_cover(old):
    quoted, _ = old.reviewed("A decision with a quote", metadata={"evidence": "first passage"})
    keyed, _ = old.reviewed("A decision with a key", key="first-key")
    for record, change in ((quoted, {"metadata": {"evidence": "another passage"}}), (keyed, {"key": "second-key"})):
        edited = person_edit(old.store, old.pid, {"id": record["id"], "expected_version": record["version"],
                                                  **change}, record["source_ids"])
        confirmed_before_rule(old.store, edited["id"])

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 0 and result["before_rule"] == 2
    for record in (quoted, keyed):
        assert confirmation_of(old.record(record["id"])) == {"by": "before_rule"}


def test_a_dispute_or_a_review_written_by_hand_elsewhere_marks_nothing(old):
    disputed = old.store.remember(old.pid, {"kind": "fact", "content": "Disputed once"}, [old.note["id"]])
    Inspector(old.store).review(old.pid, disputed["id"], "dispute", 1, reason="wrong")
    confirmed_before_rule(old.store, disputed["id"])
    # A review:// source with another origin, or one that hashes nothing, is not a person's act.
    forged = old.store.snapshot(old.pid, "review://" + str(uuid4()), '{"action": "confirm"}', "manual",
                                {"origin": "session_finding"})
    other = old.store.remember(old.pid, {"kind": "fact", "content": "Forged"}, [old.note["id"], forged["id"]])
    confirmed_before_rule(old.store, other["id"])

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 0 and result["before_rule"] == 2


def test_a_command_line_note_hashes_nothing_so_its_record_stays_before_the_rule(old):
    typed = as_person(old.store, old.pid, {"kind": "constraint", "content": "Typed on the command line"},
                      [old.note["id"]])
    confirmed_before_rule(old.store, typed["id"])
    assert mark_confirmed_before_rule(old.store, force=True)["before_rule"] == 1
    assert confirmation_of(old.record(typed["id"])) == {"by": "before_rule"}


def test_a_workspace_form_saved_as_confirmed_is_marked_and_one_saved_as_proposed_is_not(old):
    with old.store.transaction():
        typed = save_finding(old.store, old.pid, str(uuid4()), "Typed in the form as confirmed", "fact",
                             "confirmed")["record"]
    with old.store.transaction():
        proposed = save_finding(old.store, old.pid, str(uuid4()), "Typed in the form as proposed", "fact",
                                "proposed")["record"]
    confirmed_before_rule(old.store, typed["id"])
    # A session confirmed the proposed one before 0.11.0, in a later version that changed nothing it says.
    cited = old.store.remember(old.pid, {"id": proposed["id"], "expected_version": 1},
                               [*proposed["source_ids"], old.note["id"]])
    assert cited["version"] == 2
    confirmed_before_rule(old.store, proposed["id"])

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 1 and result["before_rule"] == 1
    mark = old.record(typed["id"])["metadata"]["confirmation"]
    assert mark["route"] == "workspace_form" and mark["actor_name"] == "Ada Nowak"
    assert mark["source_id"] == typed["source_ids"][0]
    assert confirmation_of(old.record(proposed["id"])) == {"by": "before_rule"}


def test_a_plan_confirmation_marks_the_steps_it_hashed(old):
    plans = PlanManager(old.store)
    saved = plans.save(old.pid, {"title": "Ship", "goal": "Ship it", "constraints": []},
                       [{"key": "build", "content": "Build the thing"}, {"key": "test", "content": "Test the thing"}],
                       "plan-1")
    plan_id = saved["plan"]["id"]
    confirmed = confirm_plan(old.store, old.pid, plan_id, plans.get(old.pid, plan_id)["plan"]["version"])
    for task in confirmed["tasks"]:
        confirmed_before_rule(old.store, task["id"])

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 2
    for task in confirmed["tasks"]:
        mark = old.record(task["id"])["metadata"]["confirmation"]
        assert mark["route"] == "confirm_plan" and mark["source_id"] == confirmed["audit_source_id"]


def test_a_review_that_came_in_a_package_marks_nothing(tmp_path):
    first = repository(tmp_path / "computer-a", "Ada Nowak")
    second = tmp_path / "computer-b"
    subprocess.run(["git", "clone", "--quiet", str(first), str(second)], check=True)
    git(second, "config", "user.name", "Bo Lee")
    source, destination = Store(tmp_path / "data-a"), Store(tmp_path / "data-b")
    pid = source.add_project("Portable", first)["id"]
    note = source.snapshot(pid, "manual://note", "Keep the decision across computers", "manual")
    saved = source.remember(pid, {"kind": "decision", "content": "Use the portable decision"}, [note["id"]])
    Inspector(source).review(pid, saved["id"], "confirm", 1)
    MemoryManager(destination).import_snapshot(MemoryManager(source).snapshot(pid)["path"], second)
    # As a store before 0.11.0 holds an import: confirmed, no mark, the audit beside it.
    imported = confirmed_before_rule(destination, saved["id"])
    assert imported["source_ids"] and confirmation_of(imported) == {"by": "before_rule"}

    result = mark_confirmed_before_rule(destination, force=True)

    assert result["marked"] == 0 and result["before_rule"] == 1
    row = destination.query("SELECT FROM records WHERE id=:id", {"id": saved["id"]})[0]
    assert confirmation_of(row) == {"by": "before_rule"}


def test_the_step_runs_once_at_upgrade_reports_its_counts_and_is_idempotent(old):
    record, _ = old.reviewed("Reviewed before the upgrade")
    nobody = old.store.remember(old.pid, {"kind": "fact", "content": "Confirmed by a session"}, [old.note["id"]])
    confirmed_before_rule(old.store, nobody["id"])
    # A store written by 0.10.0 has not had the step: the next start runs it, under the writer lock.
    old.store.command(f"DELETE FROM devbrain_meta WHERE name='{META_NAME}'")
    reopened = Store(old.store.data_dir)
    assert reopened.confirmations_before_rule["marked"] == 1
    assert reopened.confirmations_before_rule["before_rule"] == 1
    assert confirmation_of(old.record(record["id"]))["by"] == "person"
    recorded = old.store.query(f"SELECT FROM devbrain_meta WHERE name='{META_NAME}'")[0]
    assert recorded["marked"] == 1 and recorded["before_rule"] == 1
    # Once done, a start does not run it again, and running it again changes nothing.
    assert Store(old.store.data_dir).confirmations_before_rule is None
    mark = old.record(record["id"])["metadata"]["confirmation"]
    again = mark_confirmed_before_rule(old.store, force=True)
    assert again["marked"] == 0 and again["before_rule"] == 1
    assert old.record(record["id"])["metadata"]["confirmation"] == mark


def test_cairnmind_init_says_what_the_step_did(old):
    import json

    from typer.testing import CliRunner

    from cairnmind.cli import app
    old.reviewed("Reviewed before the upgrade")
    nobody = old.store.remember(old.pid, {"kind": "fact", "content": "Confirmed by a session"}, [old.note["id"]])
    confirmed_before_rule(old.store, nobody["id"])
    old.store.command(f"DELETE FROM devbrain_meta WHERE name='{META_NAME}'")
    shown = CliRunner().invoke(app, ["--data-dir", str(old.store.data_dir), "init"])
    assert shown.exit_code == 0, shown.output
    # stdout only: `init` says its engine mode on stderr the first time (#272)
    said = json.loads(shown.stdout)["confirmations_before_rule"]
    assert said["marked_as_a_persons"] == 1 and said["still_before_the_rule"] == 1
    again = json.loads(CliRunner().invoke(app, ["--data-dir", str(old.store.data_dir), "init"]).stdout)
    assert "confirmations_before_rule" not in again


def test_a_new_store_records_the_step_as_done_with_nothing_to_mark(tmp_path):
    store = Store(tmp_path / "fresh")
    assert store.confirmations_before_rule == {"marked": 0, "before_rule": 0,
                                               "at": store.confirmations_before_rule["at"]}
    assert Store(tmp_path / "fresh").confirmations_before_rule is None


def test_restoring_a_backup_of_an_old_store_runs_the_step_on_what_it_brings(old, tmp_path):
    record, _ = old.reviewed("Reviewed in an old backup")
    backup = tmp_path / "old.cairnmind.json.gz"
    old.store.backup(backup)
    old.store.restore(backup)
    assert old.store.confirmations_before_rule["marked"] == 1
    assert confirmation_of(old.record(record["id"]))["by"] == "person"


def as_written_by_0_10_0(store, audit_id):
    """Rewrite a review audit to the fields 0.10.0 wrote: the text's hash only, and no name."""
    import json

    from cairnmind.storage.sql import update_document
    with store.transaction() as tx:
        audit = json.loads(tx.query("SELECT content FROM sources WHERE id=:id", {"id": audit_id})[0]["content"])
        kept = ("actor", "action", "record_id", "previous_version", "previous_status", "status", "reason", "at",
                "content_sha256")
        update_document(tx, "sources", {"content": json.dumps({name: audit[name] for name in kept if name in audit})},
                        "id=:id", {"id": audit_id})


def test_a_0_10_0_review_marks_a_record_that_says_what_it_said_and_names_nobody(old):
    record, audit_id = old.reviewed("Reviewed in 0.10.0", key="old-key", metadata={"evidence": "a passage"})
    as_written_by_0_10_0(old.store, audit_id)

    assert mark_confirmed_before_rule(old.store, force=True)["marked"] == 1

    after = old.record(record["id"])
    assert after["metadata"]["confirmation"]["actor_name"] is None
    assert confirmation_text(after).startswith("confirmed here by a person: the local user, in the inspector, ")


@pytest.mark.parametrize("change", [{"key": "another-key"}, {"metadata": {"evidence": "another passage"}},
                                    {"metadata": {"rationale": "a reason nobody reviewed"}}],
                         ids=["key", "quote", "metadata"])
def test_a_0_10_0_review_does_not_cover_a_key_quote_or_field_changed_after_it(old, change):
    record, audit_id = old.reviewed("Reviewed in 0.10.0", key="old-key",
                                    metadata={"evidence": "a passage", "rationale": "the reviewed reason"})
    as_written_by_0_10_0(old.store, audit_id)
    # The text stays, so the text hash, all a 0.10.0 audit holds, still matches.
    edited = person_edit(old.store, old.pid, {"id": record["id"], "expected_version": record["version"], **change},
                         record["source_ids"])
    edited = confirmed_before_rule(old.store, edited["id"])
    assert edited["content"] == record["content"] and edited["version"] == record["version"] + 1

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 0 and result["before_rule"] == 1
    assert confirmation_of(old.record(record["id"])) == {"by": "before_rule"}


def test_a_metadata_field_changed_after_a_current_review_also_ends_the_cover(old):
    record, _ = old.reviewed("Reviewed with a reason", metadata={"rationale": "the reviewed reason"})
    edited = person_edit(old.store, old.pid, {"id": record["id"], "expected_version": record["version"],
                                              "metadata": {"rationale": "a reason nobody reviewed"}},
                         record["source_ids"])
    confirmed_before_rule(old.store, edited["id"])

    result = mark_confirmed_before_rule(old.store, force=True)

    assert result["marked"] == 0 and result["before_rule"] == 1


def test_a_record_the_step_cannot_read_stays_before_the_rule_and_the_store_opens(old):
    record, _ = old.reviewed("Reviewed, then its metadata lost")
    readable, _ = old.reviewed("Reviewed and readable")
    with old.store.transaction() as tx:
        tx.command("UPDATE records SET metadata = null WHERE id=:id", {"id": record["id"]})
    old.store.command(f"DELETE FROM devbrain_meta WHERE name='{META_NAME}'")

    reopened = Store(old.store.data_dir)

    assert reopened.confirmations_before_rule["marked"] >= 1
    assert confirmation_of(old.record(readable["id"]))["by"] == "person"
    assert old.store.query(f"SELECT FROM devbrain_meta WHERE name='{META_NAME}'")
