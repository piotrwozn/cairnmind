"""Records worth re-checking after a code change (issue #8, first slice): read-only and derived."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.impact import build_impact
from cairnmind.ingest import index_project
from cairnmind.store import Store, _clean

AUTH = '''SESSION_MINUTES = 30


def refresh_token(token):
    return token + ":refreshed"


def revoke_token(token):
    return None
'''


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def link(store, pid, record, node_id, source_id, status="proposed"):
    """The edge extraction writes when a record's evidence names a file or a symbol."""
    start = f"record:{record['id']}"
    store.upsert_edge({"id": hashlib.sha256(f"{start}:relates_to:{node_id}".encode()).hexdigest(), "project_id": pid,
                       "from_id": start, "to_id": node_id, "kind": "relates_to", "source_id": source_id,
                       "status": status})


@pytest.fixture
def workspace(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "auth.py").write_text(AUTH)
    (root / "src" / "billing.py").write_text("def charge(amount):\n    return amount\n")
    (root / "docs" / "login.md").write_text("# Login\n\nA session lasts 30 minutes and is renewed by refresh_token.\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    store = Store(settings.data_dir)
    pid = store.add_project("impact", root)["id"]
    index_project(store, pid)

    def source(name):
        return store.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                           {"project": pid, "uri": str(root / name)})[0]["id"]

    def node(kind, label):
        return store.query("SELECT id FROM nodes WHERE project_id=:project AND kind=:kind AND label=:label",
                           {"project": pid, "kind": kind, "label": label})[0]["id"]

    note = store.snapshot(pid, "chat://session/1", "We agreed that sessions last 30 minutes.", kind="conversation")["id"]
    records = {
        "rule": store.remember(pid, {"kind": "decision", "key": "session-length", "content": "Sessions last 30 minutes."}, [note]),
        "doc": store.remember(pid, {"kind": "fact", "key": "login-doc", "content": "The login page documents renewal.",
                                    "metadata": {"evidence": "renewed by refresh_token"}}, [source("docs/login.md")]),
        "revoke": store.remember(pid, {"kind": "fact", "key": "revoke", "content": "Revoking returns nothing."}, [note]),
        "billing": store.remember(pid, {"kind": "decision", "key": "billing", "content": "Charges are not rounded."},
                                  [source("src/billing.py")]),
        "task": store.remember(pid, {"kind": "task", "key": "rotate", "content": "Rotate refresh tokens."}, [note]),
    }
    link(store, pid, records["rule"], node("symbol", "refresh_token"), note)
    link(store, pid, records["revoke"], node("symbol", "revoke_token"), note)
    link(store, pid, records["task"], node("file", str(root / "src" / "auth.py")), note, status="observed")
    return store, pid, root, records


def titles(report):
    return [row["title"] for row in report["records"]]


def test_a_changed_function_points_at_the_records_that_name_it_and_not_at_its_neighbours(workspace):
    store, pid, root, records = workspace
    (root / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":renewed"'))
    report = build_impact(store, pid, "HEAD")
    assert titles(report) == ["Sessions last 30 minutes.", "Rotate refresh tokens."]
    rule, task = report["records"]
    assert rule["links"] == [{"via": "relates_to", "path": "src/auth.py", "change": "modified", "link_status": "proposed",
                              "symbol": "refresh_token", "symbol_change": "modified"}]
    assert task["links"] == [{"via": "relates_to", "path": "src/auth.py", "change": "modified", "link_status": "observed"}]
    assert task["task_state"] == "todo"
    text = report["text"]
    assert "[decision] proposed: Sessions last 30 minutes." in text
    assert "  - names refresh_token in src/auth.py, which was modified; its definition changed (link proposed)" in text
    assert "  - names the file src/auth.py, which was modified" in text
    assert "Revoking returns nothing." not in text and "Charges are not rounded." not in text
    assert text.splitlines()[-1] == "2 records linked to 1 of 1 changed file"
    assert [entry["symbols"] for entry in report["changed_files"]] == [{"refresh_token": "modified"}]


def test_a_changed_document_points_at_the_records_that_cite_it_with_the_state_of_the_quote(workspace):
    store, pid, root, records = workspace
    (root / "docs" / "login.md").write_text("# Login\n\nA session lasts 60 minutes.\n")
    before_indexing = build_impact(store, pid, "HEAD")
    assert titles(before_indexing) == ["The login page documents renewal."]
    assert before_indexing["records"][0]["evidence_state"] == "anchored"  # the index has not seen the edit yet
    index_project(store, pid)
    report = build_impact(store, pid, "HEAD")
    row = report["records"][0]
    assert (row["evidence_state"], row["quote_survives"]) == ("evidence_replaced", False)
    assert row["links"][0]["via"] == "supported_by" and row["links"][0]["snapshot_is_current"] is False
    assert "of docs/login.md, which was modified; the quoted passage is gone" in report["text"]


def test_deleted_renamed_and_committed_changes_are_all_seen(workspace):
    store, pid, root, records = workspace
    base = git(root, "rev-parse", "HEAD")
    (root / "src" / "auth.py").write_text(AUTH.replace("def revoke_token(token):\n    return None\n", ""))
    git(root, "commit", "-qam", "drop revoke")
    git(root, "mv", "src/billing.py", "src/payments.py")
    git(root, "commit", "-qm", "rename billing")
    report = build_impact(store, pid, base)
    assert titles(report) == ["Charges are not rounded.", "Revoking returns nothing.", "Rotate refresh tokens."]
    text = report["text"]
    assert "of src/billing.py, which was renamed to src/payments.py" in text
    assert "names revoke_token in src/auth.py, which was modified; it is no longer defined there" in text
    assert build_impact(store, pid, "HEAD")["records"] == []
    assert "Nothing in this worktree differs from that commit." in build_impact(store, pid, "HEAD")["text"]
    (root / "src" / "auth.py").unlink()
    deleted = build_impact(store, pid, base)
    assert "Sessions last 30 minutes." in titles(deleted)  # every definition of a deleted file is gone
    assert "names refresh_token in src/auth.py, which was deleted; it is no longer defined there" in deleted["text"]


def test_the_report_is_derived_and_writes_nothing(workspace):
    store, pid, root, records = workspace
    (root / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":renewed"'))

    def everything():
        return {name: sorted(json.dumps(_clean(row), sort_keys=True, default=str) for row in store.query(f"SELECT FROM {name}"))
                for name in ("records", "record_revisions", "sources", "nodes", "edges", "jobs", "projects")}

    before = everything()
    first = build_impact(store, pid, "HEAD")
    assert build_impact(store, pid, "HEAD") == first  # repeating the scan adds and duplicates nothing
    assert everything() == before
    task = next(row for row in store.list_records(pid) if row["id"] == records["task"]["id"])
    assert task["metadata"]["state"] == "todo" and task["version"] == records["task"]["version"]


def test_superseded_records_are_left_out(workspace):
    store, pid, root, records = workspace
    rule = records["rule"]
    store.remember(pid, {"id": rule["id"], "expected_version": rule["version"], "status": "superseded"}, rule["source_ids"])
    (root / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":renewed"'))
    assert titles(build_impact(store, pid, "HEAD")) == ["Rotate refresh tokens."]


@pytest.mark.parametrize("since, message", [
    ("no-such-commit", "Git does not know the commit"), ("--output=/tmp/x", "--since needs a commit"),
    ("", "--since needs a commit"), ("^HEAD", "--since needs a commit"), ("HEAD..HEAD", "Git does not know the commit"),
    ("HEAD:src/auth.py", "Git does not know the commit")])
def test_an_unknown_or_unsafe_commit_is_refused(workspace, since, message):
    store, pid, root, records = workspace
    with pytest.raises(ValueError, match=message):
        build_impact(store, pid, since)


def test_text_from_the_repository_cannot_forge_report_lines(workspace):
    store, pid, root, records = workspace
    hostile = "evil\x1b[2K\rname.py"
    (root / "src" / hostile).write_text("def refresh_token(token):\n    return token\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "hostile name")
    index_project(store, pid)
    node = store.query("SELECT id FROM nodes WHERE project_id=:project AND kind='file' AND label=:label",
                       {"project": pid, "label": str(root / "src" / hostile)})[0]["id"]
    link(store, pid, records["task"], node, records["task"]["source_ids"][0])
    (root / "src" / hostile).write_text("def refresh_token(token):\n    return None\n")
    text = build_impact(store, pid, "HEAD")["text"]
    assert "\x1b" not in text and "\r" not in text and "names the file src/evil" in text


def test_the_command_prints_the_report_and_json(workspace, monkeypatch):
    store, pid, root, records = workspace
    (root / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":renewed"'))
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    runner = CliRunner()
    shown = runner.invoke(app, ["--data-dir", str(store.data_dir), "impact", "impact", "--since", "HEAD"])
    assert shown.exit_code == 0, shown.output
    assert "Records worth re-checking in impact" in shown.output and "names refresh_token" in shown.output
    as_json = runner.invoke(app, ["--data-dir", str(store.data_dir), "impact", "impact", "--since", "HEAD", "--json"])
    assert [row["title"] for row in json.loads(as_json.output)["records"]] == ["Sessions last 30 minutes.", "Rotate refresh tokens."]
    missing = runner.invoke(app, ["--data-dir", str(store.data_dir), "impact", "impact"])
    assert missing.exit_code != 0


def test_outside_a_git_worktree_the_command_says_so(workspace, tmp_path):
    store, pid, root, records = workspace
    plain = tmp_path / "plain"
    plain.mkdir()
    other = store.add_project("plain", plain)["id"]
    with pytest.raises(ValueError, match="impact needs a Git worktree"):
        build_impact(store, other, "HEAD")


def test_a_changed_decorator_is_a_changed_definition(workspace):
    store, pid, root, records = workspace
    decorated = AUTH.replace("def refresh_token", "@cached(\n    seconds=30,\n)\ndef refresh_token")
    (root / "src" / "auth.py").write_text(decorated)
    git(root, "commit", "-qam", "decorate")
    (root / "src" / "auth.py").write_text(decorated.replace("seconds=30", "seconds=60"))
    report = build_impact(store, pid, "HEAD")
    assert report["changed_files"][0]["symbols"] == {"refresh_token": "modified"}
    assert "Sessions last 30 minutes." in titles(report) and "Revoking returns nothing." not in titles(report)


def test_a_form_feed_above_a_definition_does_not_shift_the_comparison(workspace):
    store, pid, root, records = workspace
    paged = AUTH.replace("SESSION_MINUTES = 30\n", "SESSION_MINUTES = 30\n\x0c\n")
    (root / "src" / "auth.py").write_text(paged)
    git(root, "commit", "-qam", "page break")
    (root / "src" / "auth.py").write_text(paged.replace("    return None\n", "    return False\n"))
    assert build_impact(store, pid, "HEAD")["changed_files"][0]["symbols"] == {"revoke_token": "modified"}


def test_a_renamed_file_still_points_at_the_records_that_name_its_definitions(workspace):
    store, pid, root, records = workspace
    node = store.query("SELECT id FROM nodes WHERE project_id=:project AND kind='symbol' AND label='charge'",
                       {"project": pid})[0]["id"]
    link(store, pid, records["revoke"], node, records["revoke"]["source_ids"][0])
    git(root, "mv", "src/billing.py", "src/payments.py")
    report = build_impact(store, pid, "HEAD")
    row = next(row for row in report["records"] if row["title"] == "Revoking returns nothing.")
    assert row["links"] == [{"via": "relates_to", "path": "src/billing.py", "change": "renamed to src/payments.py",
                             "link_status": "proposed", "symbol": "charge", "symbol_change": "moved"}]
    assert "it is now defined in the renamed file" in report["text"]
    # One renamed file, although Git reports it under two names.
    assert report["text"].splitlines()[-1] == "2 records linked to 1 of 1 changed file"


def test_the_other_name_of_a_rename_cannot_forge_report_lines(workspace):
    store, pid, root, records = workspace
    git(root, "mv", "src/billing.py", "src/pay\x1b[2K\rments\nFAKE: line.py")
    report = build_impact(store, pid, "HEAD")
    assert not any(char in report["text"] for char in "\x1b\r") and "\nFAKE: line.py" not in report["text"]
    assert "which was renamed to src/pay" in report["text"]


def test_a_file_git_does_not_track_yet_counts_as_changed(workspace):
    store, pid, root, records = workspace
    (root / "src" / "tokens.py").write_text("def rotate(token):\n    return token\n")
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "ignored.py").write_text("IGNORED = 1\n")
    index_project(store, pid)
    node = store.query("SELECT id FROM nodes WHERE project_id=:project AND kind='symbol' AND label='rotate'",
                       {"project": pid})[0]["id"]
    link(store, pid, records["task"], node, records["task"]["source_ids"][0])
    report = build_impact(store, pid, "HEAD")
    assert [entry["path"] for entry in report["changed_files"]] == [".gitignore", "src/tokens.py"]
    assert "names rotate in src/tokens.py, which was added (not tracked by Git yet); it is newly defined there" in report["text"]


def test_a_record_written_from_the_file_as_it_is_now_is_not_listed(workspace):
    store, pid, root, records = workspace
    (root / "docs" / "login.md").write_text("# Login\n\nA session lasts 60 minutes.\n")
    index_project(store, pid)
    current = store.query("SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
                          {"project": pid, "uri": str(root / "docs" / "login.md")})[0]["id"]
    store.remember(pid, {"kind": "fact", "key": "sixty", "content": "Sessions last 60 minutes now."}, [current])
    report = build_impact(store, pid, "HEAD")
    # The record that cites the earlier version is listed; the one written from the edited file is not.
    assert titles(report) == ["The login page documents renewal."]


def test_a_file_of_another_registered_root_is_not_mistaken_for_one_of_this_worktree(workspace, tmp_path):
    store, pid, root, records = workspace
    second = tmp_path / "second"
    git(root, "worktree", "add", "-q", str(second), "-b", "second")
    store.add_project_root(pid, second)
    index_project(store, pid)
    (second / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":elsewhere"'))
    # The records are linked to the nodes of the first root; an edit in the second worktree is not theirs.
    assert build_impact(store, pid, "HEAD", worktree=str(root))["records"] == []
