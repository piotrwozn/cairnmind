"""`memory diff`: what a change to published memory says, read from Git with no store or model (#172)."""
from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import sys
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.memory.memory_published_check import format_bytes
from cairnmind.memory.memory_published_diff import MARKER, compare, render_markdown, render_text
from cairnmind.memory.memory_published_format import (
    GITATTRIBUTES,
    GITIGNORE,
    TASK_DEFAULTS,
    J,
    definition_hash,
    identity_hash,
    path_of,
    plan_hash,
    render,
    sha256_text,
)
from cairnmind.memory.memory_published_git import published_files, resolve

TEAM = "5a1f0c3e-8b2d-4e6f-9a7c-1d3b5e7f9a20"
_COUNTER = itertools.count(1)


def uid():
    return str(uuid.UUID(int=next(_COUNTER) * 7919, version=4))


def at():
    n = next(_COUNTER)
    return f"2026-11-14T{10 + n // 3600 % 10:02d}:{n // 60 % 60:02d}:{n % 60:02d}.000000+00:00"


def review(model, revision, action="confirm", name="Ada Nowak"):
    """A person's act that covers this revision, with its passage in the same file."""
    source, when = uid(), at()
    audit = {"action": action, "actor": "local_user", "actor_name": name, "at": when, "branch": "feature/memory",
             "commit": "c" * 40, "content_sha256": sha256_text(revision["text"]),
             "identity_sha256": identity_hash(model["kind"], revision["key"], revision["supersedes"]),
             "reason": "", "record_id": model["id"], "status": revision["status"]}
    if model["kind"] == "task":
        audit["definition_sha256"] = definition_hash(revision)
    model["sources"][source] = {"ref": {"at": when, "kind": "review"}, "passage": J(audit)}
    revision["sources"] = sorted({*revision["sources"], source})
    revision["covered"] = {"by": "review", "source": source}


def record(kind, text, key, *, quote=None, name="Ada Nowak", **task):
    model = {"kind": kind, "id": uid(), "created_at": at(), "sources": {}}
    talk = uid()
    model["sources"][talk] = {"ref": {"at": at(), "kind": "conversation", "origin": "claude-code"}, "passage": None}
    revision = {"at": at(), "covered": None, "status": "confirmed", "key": key, "supersedes": None,
                "sources": [talk], "quote": None, "text": text}
    if quote:
        guide = uid()
        model["sources"][guide] = {"ref": {"at": at(), "commit": "a" * 40, "kind": "document", "path": "docs/GUIDE.md",
                                           "sha256": "b" * 64, "snapshot": "working_tree"}, "passage": None}
        revision["sources"] = sorted([talk, guide])
        revision["quote"] = {"source": guide, "lines": [3, 3], "text": quote}
    if kind == "task":
        revision.update(deepcopy(TASK_DEFAULTS), **task)
    model["revisions"] = [revision]
    review(model, revision, name=name)
    return model


def progressed(model, state):
    model = deepcopy(model)
    revision = deepcopy(model["revisions"][-1])
    revision.update(at=at(), state=state, covered={"by": "progress"})
    model["revisions"].append(revision)
    return model


def edited(model, *, action="confirm", **changes):
    model = deepcopy(model)
    revision = deepcopy(model["revisions"][-1])
    revision.update(at=at(), **changes)
    review(model, revision, action=action)
    model["revisions"].append(revision)
    return model


def superseded(model):
    model = deepcopy(model)
    revision = deepcopy(model["revisions"][-1])
    revision.update(at=at(), status="superseded", covered={"by": "superseded"})
    model["revisions"].append(revision)
    return model


def files_of(models, team=TEAM):
    files = {"FORMAT.json": format_bytes(team), ".gitattributes": GITATTRIBUTES, ".gitignore": GITIGNORE}
    files.update({path_of(model): render(model) for model in models})
    return files


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repository(root):
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Ada Nowak")
    git(root, "config", "user.email", "ada@example.invalid")
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "code")
    return git(root, "rev-parse", "HEAD")


def publish(root, models, message, *, project="."):
    folder = root / project / ".memory" / "cairnmind-published"
    shutil.rmtree(folder, ignore_errors=True)
    for path, data in files_of(models).items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    git(root, "add", "-A", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def diff(tmp_path, root, base, head, *extra):
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path / "unused"), "memory", "diff", base, head,
                                          "--project-root", str(root), *extra])
    assert not (tmp_path / "unused").exists()
    return result


def outside_literal_blocks(markdown):
    """The lines Markdown renders as markup: every line not inside a four-space literal block."""
    lines, rendered, inside = markdown.split("\n"), [], False
    for index, line in enumerate(lines):
        previous = lines[index - 1] if index else ""
        if line.startswith("    ") and (inside or previous == ""):
            inside = True
            continue
        if line == "" and inside:
            continue
        inside = False
        rendered.append(line)
    return rendered


def test_a_pull_request_that_adds_two_decisions_and_closes_three_tasks_says_exactly_that(tmp_path):
    root = tmp_path / "repository"
    repository(root)
    tasks = [record("task", f"Write part {n} of the format", f"part-{n}") for n in (1, 2, 3)]
    fact = record("fact", "The service listens on localhost only.", "listen")
    base = publish(root, [*tasks, fact], "Open three tasks")
    git(root, "checkout", "-qb", "feature")
    first = record("decision", "ArcadeDB is the only storage engine.", "storage", quote="The active backend is ArcadeDB.")
    second = record("decision", "No SQLite or file fallback is kept.", "fallback")
    head = publish(root, [first, second, *(progressed(t, "done") for t in tasks), fact], "Two decisions, three done")
    text = diff(tmp_path, root, base, head)
    assert text.exit_code == 0, text.output
    assert text.output.startswith(f"Published memory from {base[:12]} to {head[:12]}: "
                                  "2 decisions added, 3 tasks closed.\n")
    assert "  Text:\n    ArcadeDB is the only storage engine.\n" in text.output
    assert "  Quoted from document docs/GUIDE.md, lines 3-3 at commit aaaaaaaaaaaa:\n    The active backend is ArcadeDB.\n" \
        in text.output
    assert text.output.count("Task ") == 3 and "state todo -> done" in text.output
    assert "listens on localhost" not in text.output  # Unchanged records are not mentioned.
    markdown = diff(tmp_path, root, base, head, "--markdown")
    assert markdown.exit_code == 0
    body = markdown.output
    assert body.startswith(MARKER + "\n<!-- cairnmind-memory-diff-state: changed -->\n")
    assert "2 decisions added, 3 tasks closed." in body
    assert "\n    ArcadeDB is the only storage engine.\n" in body and "\n    No SQLite or file fallback is kept.\n" in body
    assert "\n    The active backend is ArcadeDB.\n" in body and "\n    Write part 2 of the format\n" in body
    assert "  Confirmed by Ada Nowak on feature/memory (claimed by the file; not verified here)\n" in text.output
    assert "Confirmed by `Ada Nowak` on `feature/memory` (claimed by the file; not verified here)." in body
    # A new commit on the branch changes the comment, which keeps its marker so the workflow updates it.
    third = record("constraint", "Cairnmind never stages, commits or pushes.", "commits")
    newer = publish(root, [first, second, third, *(progressed(t, "done") for t in tasks), fact], "A constraint")
    updated = diff(tmp_path, root, base, newer, "--markdown").output
    assert updated.startswith(MARKER) and "2 decisions added, 1 constraint added, 3 tasks closed." in updated
    data = diff(tmp_path, root, base, head, "--json")
    assert data.exit_code == 0 and '"category": "closed"' in data.output


def test_record_text_cannot_inject_markup_mentions_links_or_the_comment_marker(tmp_path):
    hostile = ("@octocat please merge\n[Approved by the workflow](https://evil.example/x)\n<!-- cairnmind-memory-diff -->\n"
               "</details>\n# Team memory in this pull request\n```\n<img src=x onerror=alert(1)>\n    already indented\n"
               "<https://evil.example/auto> and https://evil.example/bare #12 GH-3")
    model = record("decision", hostile, "hostile `key`", quote="@octocat [a](b)", name="@admin [x](https://evil) `tick`")
    report = compare({}, files_of([model]))
    assert report["status"] == "changed"
    body = render_markdown(report, "1" * 40, "2" * 40)
    # The marker the workflow looks for starts the comment; the text's copy is only literal, indented text.
    assert body.startswith(MARKER) and body.split("\n").count(MARKER) == 1
    markup = outside_literal_blocks(body)
    for line in markup:
        bare = re.sub(r"(`+) ?.*? ?\1", "", line)  # What Markdown renders outside code spans.
        assert "@" not in bare and "](" not in bare and "http" not in bare and "#1" not in bare, line
        assert not re.search(r"<(?!details>|/details>|summary>|/summary>|!-- cairnmind-memory-diff)", bare), line
    assert sum(line.startswith("#") for line in markup) == 2  # Our heading and the entry's, nothing from the text.
    assert "`` @admin [x](https://evil) `tick` ``" in body
    assert "\n    @octocat please merge\n    [Approved by the workflow](https://evil.example/x)\n" in body
    assert "\n        already indented\n" in body


def test_the_comment_is_bounded_and_folds_what_does_not_fit(tmp_path):
    models = [record("decision", f"Decision number {n}.\n" + "A long line of reasons. " * 40, f"d-{n}") for n in range(300)]
    report = compare({}, files_of(models))
    body = render_markdown(report, "1" * 40, "2" * 40)
    assert len(body) <= 60000 and "300 decisions added." in body
    assert "<details><summary>" in body and body.count("<details>") == body.count("</details>") == 1
    assert "more entries are not shown here; run `cairnmind memory diff 111111111111 222222222222`" in body
    small = render_markdown(report, "1" * 40, "2" * 40, max_chars=5000)
    assert len(small) <= 5000 and small.count("<details>") == small.count("</details>")
    long_text = record("fact", "\n".join(f"line {n}" for n in range(100)), "long")
    one = render_markdown(compare({}, files_of([long_text])), "1" * 40, "2" * 40)
    assert "\n    line 39\n" in one and "line 40" not in one and "(60 more lines not shown;" in one


def test_changes_supersessions_disputes_withdrawals_and_rewritten_history():
    kept = record("decision", "Use cookies.", "auth")
    old = record("decision", "Use sessions.", "sessions")
    doubted = record("fact", "The cache is shared.", "cache")
    gone = record("constraint", "Never push.", "push")
    base = files_of([kept, old, doubted, gone])
    replacement = record("decision", "Use short sessions.", "sessions-2")
    replacement["revisions"][0]["supersedes"] = old["id"]
    replacement["sources"] = {s: v for s, v in replacement["sources"].items() if v["ref"]["kind"] != "review"}
    replacement["revisions"][0]["sources"] = list(replacement["sources"])
    review(replacement, replacement["revisions"][0])
    head = files_of([edited(kept, text="Use secure HttpOnly cookies."), superseded(old), replacement,
                     edited(doubted, status="disputed", action="dispute")])
    report = compare(base, head)
    found = {(entry["kind"], entry["category"], entry["id"]) for entry in report["entries"]}
    assert found == {("decision", "changed", kept["id"]), ("decision", "superseded", old["id"]),
                     ("decision", "added", replacement["id"]), ("fact", "disputed", doubted["id"]),
                     ("constraint", "withdrawn", gone["id"])}
    text = render_text(report, "1" * 40, "2" * 40)
    assert "  Text:\n    Use secure HttpOnly cookies.\n  Before:\n    Use cookies.\n" in text
    assert "confirmed -> disputed" in text and "confirmed -> superseded" in text
    # A published revision is never rewritten: a head that changes one is refused, naming the file.
    rewritten = deepcopy(kept)
    rewritten["revisions"][0]["at"] = at()
    refused = compare(base, files_of([rewritten, old, doubted, gone]))
    assert refused["status"] == "refused" and refused["errors"][0][0] == path_of(kept)
    assert "rewritten" in refused["errors"][0][1]
    assert compare(base, base)["status"] == "unchanged" and compare({}, {})["status"] == "unchanged"


def plan_definition(model, goal, steps):
    plan_source = next((s for s, v in model["sources"].items() if v["ref"]["kind"] == "plan"), None)
    if plan_source is None:
        plan_source = uid()
        model["sources"][plan_source] = {"ref": {"at": at(), "kind": "plan"}, "passage": None}
    definition = {"at": at(), "title": "Publication", "goal": goal, "constraints": ["No new dependency"],
                  "steps": steps}
    audit_source, when = uid(), at()
    audit = {"action": "confirm_plan", "actor": "local_user", "actor_name": "Ada Nowak", "at": when, "branch": "main",
             "commit": "c" * 40, "plan_id": model["id"], "plan_sha256": plan_hash(definition), "plan_version": 1,
             "reason": ""}
    model["sources"][audit_source] = {"ref": {"at": when, "kind": "review"}, "passage": J(audit)}
    earlier = model["definitions"][-1]["sources"] if model["definitions"] else [plan_source]
    definition.update(sources=sorted({*earlier, audit_source}), covered={"by": "review", "source": audit_source})
    model["definitions"].append(definition)
    return model


def test_a_new_plan_is_added_and_a_new_definition_is_a_redefinition():
    plan = {"kind": "plan", "id": uid(), "created_at": at(), "sources": {}, "definitions": []}
    step = record("task", "Write the format", "plan-step", plan=plan["id"], step_key="format")
    plan_definition(plan, "Publish confirmed knowledge.", [[step["id"], "format"]])
    added = compare({}, files_of([step, plan]))
    assert {(e["kind"], e["category"]) for e in added["entries"]} == {("task", "opened"), ("plan", "added")}
    later = plan_definition(deepcopy(plan), "Publish confirmed knowledge with a branch.", [[step["id"], "format"]])
    report = compare(files_of([step, plan]), files_of([step, later]))
    assert [(e["kind"], e["category"], e["revisions"]) for e in report["entries"]] == [("plan", "redefined", [2])]
    text = render_text(report, "1" * 40, "2" * 40)
    assert "Plan " in text and "definition 2" in text and "  Title: Publication; 1 step(s)\n" in text
    assert "  Goal:\n    Publish confirmed knowledge with a branch.\n  Before:\n    Publish confirmed knowledge.\n" in text
    assert "Confirmed with its plan by `Ada Nowak` on `main`" in render_markdown(report, "1" * 40, "2" * 40)


def test_a_head_that_would_not_import_fails_with_its_path_and_no_memory_is_no_change(tmp_path):
    root = tmp_path / "repository"
    start = repository(root)
    nothing = diff(tmp_path, root, start, "HEAD", "--markdown")
    assert nothing.exit_code == 0 and "<!-- cairnmind-memory-diff-state: unchanged -->" in nothing.output
    assert "No published memory changed." in nothing.output
    model = record("decision", "Use cookies.", "auth")
    base = publish(root, [model], "Publish")
    path = root / ".memory" / "cairnmind-published" / path_of(model)
    path.write_text(path.read_text().replace("    Use cookies.", "<<<<<<< ours\n    Use cookies.", 1))
    git(root, "commit", "-qam", "A conflict committed by mistake")
    refused = diff(tmp_path, root, base, "HEAD", "--markdown")
    assert refused.exit_code == 1 and "<!-- cairnmind-memory-diff-state: refused -->" in refused.output
    assert f"\n    {path_of(model)}: unresolved conflict in {path_of(model)} line " in refused.output
    (root / ".memory" / "cairnmind-published" / "notes.txt").write_text("hello\n")
    path.write_bytes(render(model))
    git(root, "add", "-A", ".")
    git(root, "commit", "-qm", "A stray file")
    stray = diff(tmp_path, root, base, "HEAD")
    assert stray.exit_code == 1 and "  notes.txt: unknown entry" in stray.output
    with pytest.raises(ValueError, match="commit name"):
        resolve(root, "--output=/tmp/x")
    assert diff(tmp_path, root, "no-such-branch", "HEAD").exit_code == 2


def test_a_project_in_a_subdirectory_reads_its_own_directory(tmp_path):
    root = tmp_path / "repository"
    start = repository(root)
    (root / "services" / "a").mkdir(parents=True)
    ours = record("decision", "Service A keeps its own memory.", "own")
    head = publish(root, [ours], "Service A publishes", project="services/a")
    publish(root, [record("fact", "Another team's fact.", "other")], "The root publishes too")
    commit, files, errors = published_files(root / "services" / "a", head)
    assert commit == head and errors == [] and path_of(ours) in files
    result = diff(tmp_path, root / "services" / "a", start, "HEAD")
    assert result.exit_code == 0 and "1 decision added." in result.output and "Another team" not in result.output


def test_a_revert_of_a_memory_merge_is_reported_and_passes(tmp_path):
    root = tmp_path / "repository"
    repository(root)
    decision = record("decision", "Use cookies.", "auth")
    task = record("task", "Write the format", "format")
    publish(root, [decision, task], "Publish")
    git(root, "checkout", "-qb", "a")
    publish(root, [edited(decision, text="Use secure cookies."), progressed(task, "done")], "Change on a")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--no-ff", "-m", "Merge a", "a")
    merged = git(root, "rev-parse", "HEAD")
    git(root, "revert", "--no-edit", "-m", "1", merged)
    result = diff(tmp_path, root, merged, "HEAD")
    # The head keeps the first revisions and drops the rest: it imports, so the pull request is not failed.
    assert result.exit_code == 0, result.output
    assert "1 decision reverted, 1 task reverted." in result.output
    assert "(revision 2 removed, confirmed)" in result.output
    assert "  Text:\n    Use cookies.\n  Before:\n    Use secure cookies.\n" in result.output
    assert "Confirmed by" not in result.output  # The removed revisions' reviewers are not shown as acting.
    # A head whose kept revisions differ from the base's is still refused.
    rewritten = deepcopy(decision)
    rewritten["revisions"][0]["at"] = at()
    refused = compare(files_of([edited(decision, text="Use secure cookies.")]), files_of([rewritten]))
    assert refused["status"] == "refused" and "rewritten" in refused["errors"][0][1]


def test_a_linked_published_directory_in_a_commit_is_refused_not_read_as_empty(tmp_path):
    from conftest import make_symlink_or_skip
    root = tmp_path / "repository"
    start = repository(root)
    elsewhere = root / "elsewhere"
    for path, data in files_of([record("decision", "Use cookies.", "auth")]).items():
        (elsewhere / path).parent.mkdir(parents=True, exist_ok=True)
        (elsewhere / path).write_bytes(data)
    (root / ".memory").mkdir()
    make_symlink_or_skip(root / ".memory" / "cairnmind-published", Path("../elsewhere"))
    git(root, "add", "-A", ".")
    git(root, "commit", "-qm", "A linked directory")
    commit, files, errors = published_files(root, "HEAD")
    assert files == {} and errors == [(".memory/cairnmind-published", "not a directory (or a symbolic link)")]
    result = diff(tmp_path, root, start, "HEAD")
    assert result.exit_code == 1 and ".memory/cairnmind-published: not a directory" in result.output


def test_a_stray_file_in_the_base_leaves_out_only_that_file(tmp_path):
    root = tmp_path / "repository"
    repository(root)
    kept = record("decision", "Use cookies.", "auth")
    publish(root, [kept], "Publish")
    (root / ".memory" / "cairnmind-published" / "notes.txt").write_text("hello\n")
    git(root, "add", "-A", ".")
    git(root, "commit", "-qm", "A stray file")
    base = git(root, "rev-parse", "HEAD")
    commit, files, errors = published_files(root, base)
    assert errors == [("notes.txt", "unknown entry")] and path_of(kept) in files
    added = record("fact", "The service listens on localhost only.", "listen")
    head = publish(root, [kept, added], "A fact")
    result = diff(tmp_path, root, base, head)
    assert result.exit_code == 0, result.output
    assert "1 fact added." in result.output and "Use cookies." not in result.output
    assert "  1 file(s) of the base could not be read and were left out." in result.output


REPOSITORY = Path(__file__).resolve().parents[1]


def test_the_workflow_needs_only_the_default_token_and_leaves_the_required_jobs_alone():
    workflow = (REPOSITORY / ".github" / "workflows" / "memory.yml").read_text()
    assert "\n  pull_request:\n" in workflow and "pull_request_target" not in workflow
    assert "secrets." not in workflow and "GH_TOKEN: ${{ github.token }}" in workflow
    assert "      pull-requests: write\n" in workflow and "\n  contents: read\n" in workflow
    assert 'cairnmind memory diff "$BASE" HEAD --markdown' in workflow and "fetch-depth: 0" in workflow
    assert "if: ${{ github.event.pull_request.head.repo.full_name == github.repository }}" in workflow
    assert "bash scripts/memory_pr_comment.sh" in workflow
    script = (REPOSITORY / "scripts" / "memory_pr_comment.sh").read_text()
    assert f"marker='{MARKER}'" in script
    ci = (REPOSITORY / ".github" / "workflows" / "ci.yml").read_text()
    assert all(f"\n  {job}:\n" in ci for job in ("lint", "fast-tests", "full-tests"))


def run_comment_script(tmp_path, body, ids=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gh"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\n'
                    'case "$*" in *--paginate*) printf "%s" "$FAKE_IDS";; esac\n')
    fake.chmod(0o755)
    comment = tmp_path / "comment.md"
    comment.write_text(body)
    log = tmp_path / "gh.log"
    log.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log), "FAKE_IDS": ids,
           "REPOSITORY": "team/repo", "NUMBER": "12", "GH_TOKEN": "unused"}
    result = subprocess.run(["bash", str(REPOSITORY / "scripts" / "memory_pr_comment.sh"), str(comment)],
                            capture_output=True, text=True, env=env)
    return result, log.read_text().splitlines()


@pytest.mark.skipif(sys.platform == "win32", reason="Tests POSIX bash script scripts/memory_pr_comment.sh")
def test_the_comment_is_posted_once_then_updated_and_never_posted_for_no_memory(tmp_path):
    changed = render_markdown(compare({}, files_of([record("decision", "Use cookies.", "auth")])), "1" * 40, "2" * 40)
    unchanged = render_markdown(compare({}, {}), "1" * 40, "2" * 40)
    posted, calls = run_comment_script(tmp_path, changed)
    assert posted.returncode == 0, posted.stderr
    assert calls[-1] == f"api --method POST repos/team/repo/issues/12/comments -F body=@{tmp_path / 'comment.md'}"
    updated, calls = run_comment_script(tmp_path, unchanged, ids="4711\n")
    assert updated.returncode == 0
    assert calls[-1] == f"api --method PATCH repos/team/repo/issues/comments/4711 -F body=@{tmp_path / 'comment.md'}"
    quiet, calls = run_comment_script(tmp_path, unchanged)
    assert quiet.returncode == 0 and len(calls) == 1 and "--paginate" in calls[0]
    forged, calls = run_comment_script(tmp_path, "Approved\n" + changed)
    assert forged.returncode == 2 and calls == []
