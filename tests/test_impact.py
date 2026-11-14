"""Records worth re-checking after a code change (issue #8, first slice): read-only and derived."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time

import pytest
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.impact import build_impact
from cairnmind.ingest import index_project
from cairnmind.store import Store
from cairnmind.sql import clean_row

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


def test_a_changed_constant_points_at_the_records_that_name_it_through_its_own_definition(workspace):
    store, pid, root, records = workspace
    constant = store.query("SELECT id FROM nodes WHERE project_id=:project AND kind='symbol' AND label='SESSION_MINUTES'",
                           {"project": pid})[0]["id"]
    link(store, pid, records["rule"], constant, records["rule"]["source_ids"][0])
    (root / "src" / "auth.py").write_text(AUTH.replace("= 30", "= 45"))
    report = build_impact(store, pid, "HEAD")
    # The functions in the same file did not change, so the records naming them are not listed.
    assert titles(report) == ["Sessions last 30 minutes.", "Rotate refresh tokens."]
    assert report["records"][0]["links"] == [{
        "via": "relates_to", "path": "src/auth.py", "change": "modified", "link_status": "proposed",
        "symbol": "SESSION_MINUTES", "symbol_change": "modified"}]
    assert [entry["symbols"] for entry in report["changed_files"]] == [{"SESSION_MINUTES": "modified"}]


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
        return {name: sorted(json.dumps(clean_row(row), sort_keys=True, default=str) for row in store.query(f"SELECT FROM {name}"))
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


def test_a_definition_does_not_absorb_the_decorator_of_its_neighbour(workspace):
    store, pid, root, records = workspace
    tight = ("@cached(30)\ndef refresh_token(token):\n    return token\ndef revoke_token(token):\n    return None\n"
             "class Session:\n    @property\n    def minutes(self):\n        return 30\n    def close(self):\n        return None\n")
    (root / "src" / "auth.py").write_text(tight)
    git(root, "commit", "-qam", "no blank lines")
    (root / "src" / "auth.py").write_text(tight.replace("@cached(30)", "@cached(60)").replace("@property", "@cached_property"))
    # Only the two decorated definitions changed, and the class that contains one of them.
    assert build_impact(store, pid, "HEAD")["changed_files"][0]["symbols"] == {
        "refresh_token": "modified", "minutes": "modified", "Session": "modified"}


@pytest.mark.parametrize("attributes", [None, "*.py text eol=crlf\n"])
def test_carriage_returns_do_not_make_untouched_definitions_look_changed(workspace, attributes):
    store, pid, root, records = workspace
    if attributes:
        (root / ".gitattributes").write_text(attributes)
    (root / "src" / "auth.py").write_bytes(AUTH.replace("\n", "\r\n").encode())
    git(root, "add", ".")
    git(root, "commit", "-qm", "crlf")
    (root / "src" / "auth.py").write_bytes(AUTH.replace("= 30", "= 45").replace("\n", "\r\n").encode())
    # The constant whose value changed is a definition of its own; nothing else looks changed.
    assert build_impact(store, pid, "HEAD")["changed_files"][-1]["symbols"] == {"SESSION_MINUTES": "modified"}
    (root / "src" / "auth.py").write_bytes(AUTH.replace('":refreshed"', '":renewed"').replace("\n", "\r\n").encode())
    assert build_impact(store, pid, "HEAD")["changed_files"][-1]["symbols"] == {"refresh_token": "modified"}


def test_a_path_git_calls_deleted_but_that_exists_again_is_compared_as_modified(workspace):
    store, pid, root, records = workspace
    git(root, "rm", "-q", "--cached", "src/auth.py")  # the index says deleted; the file is still there, untracked
    (root / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":renewed"'))
    report = build_impact(store, pid, "HEAD")
    assert report["changed_files"] == [{"path": "src/auth.py", "change": "modified", "untracked": True,
                                        "symbols": {"refresh_token": "modified"}}]
    assert "Revoking returns nothing." not in titles(report)


def test_a_nested_repository_is_not_a_changed_file(workspace):
    store, pid, root, records = workspace
    (root / "vendor" / "lib").mkdir(parents=True)
    git(root / "vendor" / "lib", "init", "-q")
    (root / "vendor" / "lib" / "inner.py").write_text("INNER = 1\n")
    git(root / "vendor" / "lib", "add", ".")
    git(root / "vendor" / "lib", "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "-qm", "inner")
    assert build_impact(store, pid, "HEAD")["changed_files"] == []


def touch_auth(root):
    """Change the body of ``refresh_token`` only."""
    (root / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":renewed"'))


def commit_files(root, files):
    """Write ``{path: text or bytes}`` and commit them, so that they are not part of the changed set."""
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    git(root, "add", "-f", ".")
    git(root, "commit", "-qm", "docs")


def mentions(report):
    """``{document: {name: [line numbers]}}`` of the documentation section."""
    return {entry["path"]: {item["name"]: item["lines"] for item in entry["mentions"]}
            for entry in report["documentation"]["documents"]}


def test_a_page_that_mentions_a_changed_function_is_listed_with_its_line_and_one_that_does_not_is_not(workspace):
    store, pid, root, records = workspace
    commit_files(root, {"docs/revoke.md": "# Revoke\n\nSee revoke_token for the details.\n"})
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["documents"] == [{
        "path": "docs/login.md", "changed_in_range": False,
        "mentions": [{"name": "refresh_token", "lines": [3], "more_lines": 0}]}]
    assert "  - docs/login.md: refresh_token (line 3)" in report["text"]
    assert "not proof that the page is wrong" in report["text"] and "revoke.md" not in report["text"]
    # The section comes after the records and before the closing summary line.
    assert report["text"].index("Documentation that mentions") > report["text"].index("names the file src/auth.py")
    assert report["text"].splitlines()[-1] == "2 records linked to 1 of 1 changed file"


def test_short_and_common_names_count_only_when_written_as_code(workspace):
    store, pid, root, records = workspace
    api = "def get(key):\n    return {}\n\n\ndef login(user):\n    return user\n\n\ndef renew_all():\n    return 1\n"
    (root / "src" / "api.py").write_text(api)
    commit_files(root, {
        "docs/prose.md": "Log in with the login page.\nUse get to read.\nrenew_all runs nightly.\n",
        "docs/code.md": "Call `login` first.\nThe call login(user) returns a session.\nUse `get` or get(key) to read.\n",
        "docs/fenced.md": "Example:\n\n```\nlogin\n```\n\nlogin is plain here.\n"})
    (root / "src" / "api.py").write_text(api.replace("return {}", "return None").replace("return user", "return 0")
                                         .replace("return 1\n", "return 2\n"))
    report = build_impact(store, pid, "HEAD")
    assert report["changed_files"][0]["symbols"] == {"get": "modified", "login": "modified", "renew_all": "modified"}
    # "get" is shorter than 4 characters: never listed. "login" needs code form; "renew_all" is distinctive.
    assert mentions(report) == {"docs/code.md": {"login": [1, 2]}, "docs/fenced.md": {"login": [4]},
                                "docs/prose.md": {"renew_all": [3]}}


def test_only_a_whole_identifier_is_a_mention(workspace):
    store, pid, root, records = workspace
    commit_files(root, {"docs/longer.md": "refresh_token_v2\nmy_refresh_token\nrefresh_tokens\n_refresh_token\n",
                        "docs/exact.md": "one\ntwo\nSee (refresh_token), then more.\n"})
    touch_auth(root)
    assert mentions(build_impact(store, pid, "HEAD")) == {"docs/exact.md": {"refresh_token": [3]},
                                                          "docs/login.md": {"refresh_token": [3]}}


def test_ignored_and_reserved_directories_are_not_read(workspace, monkeypatch):
    store, pid, root, records = workspace
    text = "refresh_token is documented here.\n"
    commit_files(root, {".gitignore": "build/\n", ".memory/tracked.md": text, "notes/plain.txt": text,
                        "notes/page.rst": text, "notes/page.mdx": text, "notes/code.py": text})
    for name in ("build/ignored.md", ".memory/loose.md", ".cairnmind/loose.md", ".devbrain/loose.md",
                 "docs/.memory/deep.md"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "notes" / "untracked.md").write_text(text)
    from cairnmind import impact_docs
    read = []
    original = impact_docs._doc_text
    monkeypatch.setattr(impact_docs, "_doc_text", lambda *args: read.append(args[2]) or original(*args))
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert sorted(mentions(report)) == ["docs/login.md", "notes/page.mdx", "notes/page.rst", "notes/plain.txt",
                                        "notes/untracked.md"]
    assert sorted(read) == sorted(mentions(report))  # nothing else was opened, not even to be skipped


def test_the_list_is_bounded_and_says_what_it_left_out(workspace):
    store, pid, root, records = workspace
    commit_files(root, {f"docs/many_{number:02}.md": "refresh_token\n" * 5 for number in range(55)})
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    shown = report["documentation"]["documents"]
    assert len(shown) == 50 and shown[0]["path"] == "docs/login.md" and shown[-1]["path"] == "docs/many_48.md"
    assert shown[1]["mentions"] == [{"name": "refresh_token", "lines": [1, 2, 3], "more_lines": 2}]
    assert report["documentation"]["omitted_documents"] == 6 and report["documentation"]["omitted_lines"] == 98
    assert "6 more document(s) mention a changed definition and are not listed" in report["text"]
    assert "98 more line(s) are not listed" in report["text"] and "lines 1, 2, 3, and 2 more" in report["text"]


def test_a_page_over_a_megabyte_or_not_utf8_is_skipped(workspace):
    store, pid, root, records = workspace
    commit_files(root, {"docs/big.md": b"refresh_token\n" * 100000, "docs/bad.md": b"refresh_token \xff\n"})
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert list(mentions(report)) == ["docs/login.md"] and report["documentation"]["skipped_documents"] == 2


def test_a_page_changed_in_the_same_range_is_listed_and_says_so(workspace):
    store, pid, root, records = workspace
    touch_auth(root)
    (root / "docs" / "login.md").write_text("# Login\n\nTypos fixed; renewal is done by `refresh_token`.\n")
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["documents"][0]["changed_in_range"] is True
    assert "  - docs/login.md [also changed in this range]: refresh_token (line 3)" in report["text"]


def test_a_removed_definition_counts_and_an_added_or_unchanged_one_does_not(workspace):
    store, pid, root, records = workspace
    commit_files(root, {"docs/revoke.md": "Use revoke_token to log out.\n"})
    (root / "src" / "auth.py").write_text(AUTH.replace("def revoke_token(token):\n    return None\n", "")
                                          + "\n\ndef brand_new_token():\n    return 1\n")
    (root / "docs" / "new.md").write_text("brand_new_token is new.\n")
    assert mentions(build_impact(store, pid, "HEAD")) == {"docs/revoke.md": {"revoke_token": [1]}}


def test_dunder_names_are_not_looked_up(workspace):
    store, pid, root, records = workspace
    klass = "class Box:\n    def __init__(self):\n        self.items = []\n\n    def render_box(self):\n        return []\n"
    (root / "src" / "box.py").write_text(klass)
    commit_files(root, {"docs/box.md": "Every class has __init__ and `__init__()`; render_box draws it.\n"})
    (root / "src" / "box.py").write_text(klass.replace("[]", "()"))
    report = build_impact(store, pid, "HEAD")
    assert set(report["changed_files"][0]["symbols"]) == {"Box", "__init__", "render_box"}
    assert mentions(report) == {"docs/box.md": {"render_box": [1]}}  # the positive control: only __init__ is skipped


def api_changed(root, docs):
    """Commit ``src/api.py`` (get, login, renew_all) and ``docs``, then change the body of all three functions."""
    api = "def get(key):\n    return {}\n\n\ndef login(user):\n    return user\n\n\ndef renew_all():\n    return 1\n"
    (root / "src" / "api.py").write_text(api)
    commit_files(root, docs)
    (root / "src" / "api.py").write_text(api.replace("return {}", "return None").replace("return user", "return 0")
                                         .replace("return 1\n", "return 2\n"))


def test_fences_are_markdown_only_and_are_tracked_by_the_marker_that_opened_them(workspace):
    store, pid, root, records = workspace
    api_changed(root, {
        "docs/usage.rst": "Sessions\n~~~~~~~~\n\nPlease login to continue.\n",
        "docs/plain.txt": "```\nlogin\n```\n",
        "docs/nested.md": "```\n~~~\nlogin in code\n```\nlogin in prose\n",
        "docs/long.md": "````\n```\nlogin in code\n```\nstill code login\n````\nlogin in prose\n",
        "docs/inline.md": "```renew_all``` renews it\nthen login again\n",
        "docs/tilde.md": "~~~\nlogin\n~~~\n"})
    assert mentions(build_impact(store, pid, "HEAD")) == {
        "docs/inline.md": {"renew_all": [1]}, "docs/long.md": {"login": [3, 5]}, "docs/nested.md": {"login": [3]},
        "docs/tilde.md": {"login": [2]}}


def test_double_backticks_mark_code_as_in_restructuredtext(workspace):
    store, pid, root, records = workspace
    api_changed(root, {"docs/api.rst": "Call ``login`` to start a session.\nPlain login is not code.\n",
                       "docs/two.md": "Use ``login`` and `login` then login.\n"})
    assert mentions(build_impact(store, pid, "HEAD")) == {"docs/api.rst": {"login": [1]}, "docs/two.md": {"login": [1]}}


def test_a_long_line_is_scanned_in_linear_time(workspace):
    store, pid, root, records = workspace
    api_changed(root, {"docs/long.txt": "`login` x " * 50000 + "\n"})
    started = time.monotonic()
    assert mentions(build_impact(store, pid, "HEAD")) == {"docs/long.txt": {"login": [1]}}
    assert time.monotonic() - started < 5


def test_files_that_were_not_read_are_counted_and_said_even_when_no_page_is_listed(workspace):
    store, pid, root, records = workspace
    (root / "docs" / "login.md").unlink()  # a tracked page that is gone is not a skipped one
    (root / "docs" / "link.md").symlink_to(root / "README-missing.md")
    (root / "docs" / "nul.txt").write_bytes(b"refresh_token\0")
    commit_files(root, {"docs/big.md": b"refresh_token\n" * 100000})
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["documents"] == [] and report["documentation"]["skipped_documents"] == 3
    assert "3 documentation file(s) were not read (a link or not a plain file, over 1 MB, or not UTF-8 text)." \
        in report["text"]
    assert "Documentation that mentions" not in report["text"]


def test_a_failure_to_list_the_files_is_said_and_not_taken_for_an_empty_answer(workspace, monkeypatch):
    store, pid, root, records = workspace
    touch_auth(root)
    from cairnmind import impact_docs
    monkeypatch.setattr(impact_docs, "_git_bytes", lambda base, *args: None)
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["unavailable"] is True and report["documentation"]["documents"] == []
    assert "The documentation files could not be listed" in report["text"] and titles(report)


def test_javascript_names_with_a_dollar_are_looked_up_and_private_and_constructor_names_are_not(workspace):
    store, pid, root, records = workspace
    script = ("class Box {\n  constructor() { this.n = 1; }\n  #hidden_part() { return 1; }\n}\n"
              "function $fetch_user(id) { return id; }\n")
    (root / "src" / "box.js").write_text(script)
    commit_files(root, {"docs/js.md": "Call `$fetch_user()` or $fetch_user, not foo$fetch_user.\n`constructor()` and "
                                      "#hidden_part and hidden_part.\n"})
    (root / "src" / "box.js").write_text(script.replace("= 1", "= 2").replace("return 1", "return 2")
                                         .replace("return id", "return 0"))
    report = build_impact(store, pid, "HEAD")
    assert {"constructor", "$fetch_user"} <= set(report["changed_files"][0]["symbols"])
    assert mentions(report) == {"docs/js.md": {"$fetch_user": [1]}}


def test_a_page_behind_a_link_or_outside_the_worktree_is_not_read(workspace, tmp_path):
    store, pid, root, records = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    git(root, "mv", "docs/login.md", "docs/keep.md")
    git(root, "commit", "-qm", "move")
    (root / "docs" / "keep.md").unlink()
    (root / "docs").rmdir()
    (root / "docs").symlink_to(outside)
    (outside / "keep.md").write_text("Also refresh_token.\n")
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["documents"] == [] and report["documentation"]["skipped_documents"] == 1


def test_directories_and_files_the_indexer_skips_are_not_read(workspace, monkeypatch):
    store, pid, root, records = workspace
    text = "refresh_token is here.\n"
    commit_files(root, {"vendor/x.md": text, "node_modules/pkg/y.md": text, ".claude/commands/c.md": text,
                        ".env.local.md": text, "notes/.env.txt": text, "notes/ok.md": text})
    from cairnmind import impact_docs
    read = []
    original = impact_docs._doc_text
    monkeypatch.setattr(impact_docs, "_doc_text", lambda *args: read.append(args[2]) or original(*args))
    touch_auth(root)
    assert sorted(mentions(build_impact(store, pid, "HEAD"))) == ["docs/login.md", "notes/ok.md"]
    assert sorted(read) == ["docs/login.md", "notes/ok.md"]


def test_the_scan_has_work_bounds_and_says_so(workspace, monkeypatch):
    store, pid, root, records = workspace
    commit_files(root, {f"docs/p{number}.md": "refresh_token\n" for number in range(8)})
    from cairnmind import impact_docs
    touch_auth(root)
    monkeypatch.setattr(impact_docs, "MAX_FILES", 4)
    report = build_impact(store, pid, "HEAD")
    assert len(report["documentation"]["documents"]) == 4 and report["documentation"]["not_scanned"] == 5
    assert "5 documentation file(s) were not scanned: at most 4 files" in report["text"]
    monkeypatch.setattr(impact_docs, "MAX_FILES", 2000)
    monkeypatch.setattr(impact_docs, "MAX_TOTAL_BYTES", 30)
    assert build_impact(store, pid, "HEAD")["documentation"]["not_scanned"] > 0
    monkeypatch.setattr(impact_docs, "MAX_TOTAL_BYTES", 20 * 1024 * 1024)
    (root / "src" / "auth.py").write_text(AUTH.replace('":refreshed"', '":renewed"').replace("return None", "return 0"))
    monkeypatch.setattr(impact_docs, "MAX_NAMES", 1)
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["names_not_searched"] == 1
    assert "1 changed name(s) were not searched for: at most 1 names are looked up." in report["text"]


def test_paths_in_the_json_are_made_printable(workspace):
    store, pid, root, records = workspace
    (root / "docs" / "bi‮di\x9b.md").write_text("refresh_token\n")
    touch_auth(root)
    paths = [entry["path"] for entry in build_impact(store, pid, "HEAD")["documentation"]["documents"]]
    assert len(paths) == 2 and not any(char in "".join(paths) for char in "‮\x9b")


def test_a_changed_name_that_is_the_start_of_another_is_found_on_its_own_terms(workspace):
    store, pid, root, records = workspace
    code = "def renew(a):\n    return a\n\n\ndef renew_session(a):\n    return a\n"
    (root / "src" / "pair.py").write_text(code)
    commit_files(root, {"docs/both.md": "Use `renew` then renew_session.\n", "docs/long.md": "Only renew_session.\n"})
    (root / "src" / "pair.py").write_text(code.replace("return a", "return 0"))
    assert mentions(build_impact(store, pid, "HEAD")) == {"docs/both.md": {"renew": [1], "renew_session": [1]},
                                                          "docs/long.md": {"renew_session": [1]}}


def test_a_page_with_windows_line_endings_has_right_line_numbers_and_no_carriage_return(workspace):
    store, pid, root, records = workspace
    commit_files(root, {"docs/win.md": b"one\r\n```\r\ncode\r\n```\r\nSee refresh_token here\r\n"})
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert mentions(report)["docs/win.md"] == {"refresh_token": [5]} and "\r" not in report["text"]


def test_the_json_has_a_documentation_key_after_the_existing_ones(workspace, monkeypatch):
    store, pid, root, records = workspace
    touch_auth(root)
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    result = CliRunner().invoke(app, ["--data-dir", str(store.data_dir), "impact", "impact", "--since", "HEAD", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert list(data) == ["project", "worktree", "since", "commit", "changed_files", "records", "text", "documentation"]
    assert data["documentation"]["documents"][0]["path"] == "docs/login.md"
    assert data["documentation"] == {"documents": data["documentation"]["documents"], "omitted_documents": 0,
                                     "omitted_lines": 0, "skipped_documents": 0, "not_scanned": 0,
                                     "names_not_searched": 0, "unavailable": False}


def test_looking_for_mentions_writes_nothing(workspace):
    store, pid, root, records = workspace
    commit_files(root, {"docs/more.md": "`refresh_token()` again\n"})
    touch_auth(root)

    def everything():
        rows = {name: sorted(json.dumps(clean_row(row), sort_keys=True, default=str) for row in store.query(f"SELECT FROM {name}"))
                for name in ("records", "record_revisions", "sources", "nodes", "edges", "jobs", "projects")}
        files = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                 if path.is_file() and ".git" not in path.relative_to(root).parts}
        return rows, files, git(root, "status", "--porcelain")

    before = everything()
    first = build_impact(store, pid, "HEAD")
    assert sorted(mentions(first)) == ["docs/login.md", "docs/more.md"]
    assert build_impact(store, pid, "HEAD") == first and everything() == before


def test_files_opened_but_not_usable_count_against_the_budgets(workspace, monkeypatch):
    store, pid, root, records = workspace
    commit_files(root, {f"docs/nul{number}.txt": b"refresh_token\0" for number in range(6)})
    touch_auth(root)
    from cairnmind import impact_docs
    monkeypatch.setattr(impact_docs, "MAX_FILES", 3)
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["not_scanned"] == 4 and report["documentation"]["skipped_documents"] == 2


def test_a_file_that_grew_after_the_size_check_is_read_with_a_cap(workspace, monkeypatch):
    store, pid, root, records = workspace
    from types import SimpleNamespace
    from cairnmind import impact_docs

    class Liar(type(root)):
        """A path whose stat claims that the page is tiny, as if it had grown since."""
        def stat(self, **kwargs):
            real = super().stat(**kwargs)
            return SimpleNamespace(st_size=5, st_mode=real.st_mode) if self.name == "login.md" else real

    monkeypatch.setattr(impact_docs, "MAX_DOC_BYTES", 40)
    (root / "docs" / "login.md").write_text("refresh_token " * 20)
    assert impact_docs._doc_text(Liar(root), root.resolve(), "docs/login.md") == (None, 41)


def test_a_link_that_points_at_itself_is_skipped_and_does_not_stop_the_report(workspace):
    store, pid, root, records = workspace
    (root / "docs" / "loop.md").symlink_to("loop.md")
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert report["documentation"]["skipped_documents"] == 1 and mentions(report) == {"docs/login.md": {"refresh_token": [3]}}


def test_fences_need_to_start_a_line_after_at_most_three_spaces_and_code_spans_end_with_the_line(workspace):
    store, pid, root, records = workspace
    api_changed(root, {"docs/three.md": "   ```\nlogin\n   ```\n", "docs/indent.md": "    ```\n    login\n    ```\n",
                       "docs/quote.md": "> ```\n> login\n> ```\n", "docs/span.md": "`call\nlogin`\n"})
    assert mentions(build_impact(store, pid, "HEAD")) == {"docs/three.md": {"login": [2]}}


def test_a_file_name_with_a_leading_blank_or_bytes_that_are_not_utf8_is_still_read(workspace):
    store, pid, root, records = workspace
    (root / " lead.md").write_text("refresh_token\n")
    (root / "docs" / os.fsdecode(b"caf\xe9.md")).write_text("refresh_token\n")
    touch_auth(root)
    report = build_impact(store, pid, "HEAD")
    assert [entry["path"] for entry in report["documentation"]["documents"]] == [
        "lead.md", "docs/caf\\xe9.md", "docs/login.md"]
    json.dumps(report["documentation"], ensure_ascii=False).encode("utf-8")  # no lone surrogate reaches the output


def test_a_record_is_linked_through_a_plain_word_only_when_its_evidence_writes_it_as_code(workspace):
    store, pid, root, records = workspace
    api = "def current(state):\n    return state\n\n\ndef status(job):\n    return job\n\n\ndef load_rows(job):\n    return []\n"
    commit_files(root, {"src/api.py": api})
    index_project(store, pid)
    note = records["rule"]["source_ids"][0]

    def symbol(label):
        return store.query("SELECT id FROM nodes WHERE project_id=:project AND kind='symbol' AND label=:label",
                           {"project": pid, "label": label})[0]["id"]

    prose = store.remember(pid, {"kind": "constraint", "key": "no-commit", "content": "Do not make a commit.",
                                 "metadata": {"evidence": "Stay on the current branch and keep the status quo."}}, [note])
    code = store.remember(pid, {"kind": "fact", "key": "status", "content": "Reading a job goes through status.",
                                "metadata": {"evidence": "Call status(job), or `current` for the state."}}, [note])
    distinct = store.remember(pid, {"kind": "fact", "key": "rows", "content": "Rows are loaded lazily.",
                                    "metadata": {"evidence": "load_rows is lazy"}}, [note])
    for record, labels in ((prose, ("current", "status")), (code, ("current", "status", "load_rows")),
                           (distinct, ("load_rows",))):
        for label in labels:
            link(store, pid, record, symbol(label), note)
    (root / "src" / "api.py").write_text(api.replace("return state", "return None").replace("return job", "return 0")
                                         .replace("return []", "return ()"))
    (root / ".memory" / "cairnmind" / "objects").mkdir(parents=True)
    (root / ".memory" / "cairnmind" / "objects" / f"{'a' * 64}.json").write_text("{}")
    report = build_impact(store, pid, "HEAD")
    named = {row["title"]: [entry["symbol"] for entry in row["links"]] for row in report["records"]}
    # "current" and "status" in a sentence are words, not the functions; load_rows is distinctive anywhere.
    # A published memory snapshot in the worktree is not a changed file.
    assert [entry["path"] for entry in report["changed_files"]] == ["src/api.py"]
    assert named == {"Reading a job goes through status.": ["current", "load_rows", "status"],
                     "Rows are loaded lazily.": ["load_rows"]}
