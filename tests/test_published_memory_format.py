"""The published memory format of #170, read and refused without a store: forged, merged or broken files."""
from __future__ import annotations

import hashlib
import json
import os

from cairnmind.memory_published_check import check_directory, check_files, format_bytes, read_tree
from cairnmind.memory_published_format import (
    GITATTRIBUTES, GITIGNORE, definition_hash, path_of, plan_hash, render,
)
from cairnmind.memory_published_parse import parse_file
from cairnmind.sql import json_text

PROJECT = "5a1f0c3e-8b2d-4e6f-9a7c-1d3b5e7f9a20"
RECORD = "0b6c2f9e-5d1a-4c8e-9a43-2f1d7c9e8a10"
OTHER = "4c8e9a43-2f1d-4b6c-8e5d-1a0b6c2f9e77"
REVIEW = "9a2b6c1d-3e4f-4a5b-8c7d-0e1f2a3b4c5d"
REVIEW_TWO = "e3f40516-2738-4a9b-8c0d-1e2f3a4b5c6d"
CONVERSATION = "c5d60718-293a-4b4c-9d5e-6f708192a3b4"
NOTE = "e5f60718-293a-4b4c-9d5e-6f708192a3b5"
DOCUMENT = "7d1e4b2a-0c9f-4e35-8a61-b2c3d4e5f607"
AT = "2026-11-12T09:14:03.120412+00:00"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def audit(record_id, text, *, at=AT, action="confirm", status="confirmed", definition=None):
    value = {"action": action, "actor": "local_user", "actor_name": "Ada Nowak", "at": at, "branch": "feat/storage",
             "commit": "3" * 40, "content_sha256": sha(text), "previous_status": "proposed", "previous_version": 1,
             "reason": "", "record_id": record_id, "status": status}
    if definition is not None:
        value["definition_sha256"] = definition
    return json_text(value)


def decision(record_id=RECORD, text="ArcadeDB is the only storage engine.", key="storage-engine"):
    first = {"at": AT, "covered": {"by": "review", "source": REVIEW}, "status": "confirmed", "key": key,
             "supersedes": None, "sources": sorted([REVIEW, CONVERSATION, DOCUMENT]),
             "quote": {"source": DOCUMENT, "lines": [3, 3], "text": "The active backend is ArcadeDB"}, "text": text}
    second_text = text + "\nNo SQLite or file fallback is kept."
    second = {**first, "at": "2026-11-13T10:01:44.061002+00:00", "covered": {"by": "review", "source": REVIEW_TWO},
              "sources": sorted([REVIEW, REVIEW_TWO, CONVERSATION, DOCUMENT]), "text": second_text}
    sources = {
        REVIEW: {"ref": {"at": AT, "kind": "review"}, "passage": audit(record_id, text)},
        REVIEW_TWO: {"ref": {"at": second["at"], "kind": "review"},
                     "passage": audit(record_id, second_text, at=second["at"])},
        CONVERSATION: {"ref": {"at": AT, "kind": "conversation", "origin": "claude-code"}, "passage": None},
        DOCUMENT: {"ref": {"at": AT, "commit": "3" * 40, "kind": "document", "path": "docs/CONTRACTS.md",
                           "sha256": "a" * 64, "snapshot": "working_tree"}, "passage": None},
    }
    return {"kind": "decision", "id": record_id, "created_at": AT, "sources": sources, "revisions": [first, second]}


def constraint():
    text = "Cairnmind never stages, commits or pushes."
    return {"kind": "constraint", "id": OTHER, "created_at": AT, "sources": {NOTE: {
        "ref": {"actor_name": "Ada Nowak", "at": AT, "branch": "main", "commit": "0" * 40, "kind": "note",
                "origin": "workspace_form"}, "passage": text}},
        "revisions": [{"at": AT, "covered": {"by": "workspace_form", "source": NOTE}, "status": "confirmed",
                       "key": "8d2e4f60-1a3b-4c5d-9e7f-0a1b2c3d4e5f", "supersedes": None, "sources": [NOTE],
                       "quote": None, "text": text}]}


def tree(*models):
    files = {"FORMAT.json": format_bytes(PROJECT), ".gitattributes": GITATTRIBUTES, ".gitignore": GITIGNORE}
    files.update({path_of(model): render(model) for model in models})
    return files


def refused(files):
    result, errors = check_files(files)
    assert result is None and errors
    return errors


def test_a_rendered_file_reads_back_as_the_same_model_and_renders_the_same_bytes():
    model = decision()
    data = render(model)
    assert parse_file(data) == model and render(parse_file(data)) == data
    text = data.decode()
    # A reader of a diff sees the header, the text and who confirmed it, not one line of JSON.
    assert text.startswith('---\ncairnmind: "published-record 1"\nkind: "decision"\n')
    assert '"name":"Ada Nowak"' in text and "\n    ArcadeDB is the only storage engine.\n" in text
    assert "\n## Revision 2\n" in text and text.endswith("No SQLite or file fallback is kept.\n")
    # Free text never starts at column 0, so it can never look like a field or a conflict marker.
    assert all(not line.startswith(("ArcadeDB", "No SQLite")) for line in text.split("\n"))
    result, errors = check_files(tree(model, constraint()))
    assert errors == [] and set(result["records"]) == {RECORD, OTHER}


def test_a_windows_checkout_with_crlf_still_reads():
    files = tree(decision())
    path = path_of(decision())
    files[path] = files[path].replace(b"\n", b"\r\n")
    assert check_files(files)[1] == []


def test_conflict_markers_are_reported_with_path_and_line_before_anything_else():
    files = tree(decision())
    path = path_of(decision())
    lines = files[path].decode().split("\n")
    lines[5:5] = ["<<<<<<< HEAD", "status: \"disputed\"", "=======", ">>>>>>> other"]
    files[path] = "\n".join(lines).encode()
    files["FORMAT.json"] = b"broken"
    assert refused(files) == [(path, f"unresolved conflict in {path} line 6")]


def test_a_hand_merged_text_is_not_covered_by_any_act_and_is_refused():
    model = decision()
    model["revisions"][1]["text"] += "\nAnd a line nobody confirmed."
    files = tree(decision())
    files[path_of(model)] = render(model)
    [(path, reason)] = refused(files)
    assert path == path_of(model) and "not covered by a person's act" in reason and "taking one side" in reason


def test_keeping_both_sides_gives_two_revisions_with_one_number_and_is_refused():
    files = tree(decision())
    path = path_of(decision())
    text = files[path].decode()
    second = text[text.index("## Revision 2"):]
    files[path] = (text + "\n" + second).encode()
    assert "numbers are 1..m" in refused(files)[0][1]


def test_trust_fields_and_unknown_fields_are_refused_by_name():
    files = tree(decision())
    path = path_of(decision())
    text = files[path].decode()
    for line, expected in (("verified: true", "trust fields are never published (verified)"),
                           ("test_source_ids: []", "trust fields are never published (test_source_ids)"),
                           ("colour: \"red\"", "unknown field colour")):
        changed = text.replace('covered: {"by":"review","source":"' + REVIEW_TWO + '"}\n',
                               'covered: {"by":"review","source":"' + REVIEW_TWO + '"}\n' + line + "\n")
        assert expected in refused({**files, path: changed.encode()})[0][1]


def test_a_forged_header_or_status_does_not_match_the_revisions():
    files = tree(decision())
    path = path_of(decision())
    forged = files[path].decode().replace('status: "confirmed"\nrevisions: 2', 'status: "disputed"\nrevisions: 2', 1)
    assert "canonical form" in refused({**files, path: forged.encode()})[0][1]
    # A review audit that covers other text covers nothing.
    model = decision()
    model["sources"][REVIEW_TWO]["passage"] = audit(RECORD, "Some other text", at=model["revisions"][1]["at"])
    assert "not covered" in refused({**files, path: render(model)})[0][1]


def test_a_file_at_the_wrong_path_and_bidi_text_are_refused():
    model = decision()
    files = tree(model)
    files[f"fact/{RECORD}.md"] = files.pop(path_of(model))
    assert refused(files) == [(f"fact/{RECORD}.md", f"fact/{RECORD}.md holds decision {RECORD}; "
                                                    f"expected decision/{RECORD}.md")]
    hidden = decision(text="Use cookies‮ and nothing else.")
    assert "characters the format does not allow" in refused(tree(hidden))[0][1]


def test_two_active_records_with_one_key_are_refused_naming_both_files():
    first, second = decision(), decision(record_id="1f2e3d4c-5b6a-4798-8a7b-6c5d4e3f2a10")
    for name in (REVIEW, REVIEW_TWO):
        second["sources"][name]["passage"] = second["sources"][name]["passage"].replace(RECORD, second["id"])
    errors = [error for error in refused(tree(first, second)) if "share the key 'storage-engine'" in error[1]]
    assert {path for path, _ in errors} == {path_of(first), path_of(second)}


def task(task_id, key, depends_on, plan=None, step_key=None):
    text = f"Do {key}"
    revision = {"at": AT, "status": "confirmed", "key": key, "supersedes": None, "quote": None, "text": text,
                "state": "todo", "plan": plan, "step_key": step_key, "depends_on": depends_on, "requires_test": False,
                "requires_review": False, "required_profiles": [], "acceptance_criteria": [], "criterion_checks": {},
                "scope": None}
    review = f"{task_id[:30]}aaaaaa"
    review = review[:36]
    passage = audit(task_id, text, definition=definition_hash(revision))
    revision.update(covered={"by": "review", "source": review}, sources=[review])
    return {"kind": "task", "id": task_id, "created_at": AT, "revisions": [revision],
            "sources": {review: {"ref": {"at": AT, "kind": "review"}, "passage": passage}}}


def test_a_dependency_cycle_across_two_files_is_refused():
    one, two = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    errors = refused(tree(task(one, "one", [two]), task(two, "two", [one])))
    assert "cycle" in errors[0][1]


def test_a_task_definition_changed_after_its_review_is_not_covered():
    one = "11111111-1111-4111-8111-111111111111"
    model = task(one, "one", [])
    model["revisions"][0]["requires_test"] = True
    assert "not covered" in refused(tree(model))[0][1]


def test_a_plan_file_needs_a_confirm_plan_act_and_matching_task_files():
    plan_id, step = "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e", "11111111-1111-4111-8111-111111111111"
    plan_source, plan_review = "c0ffee00-1111-4222-8333-444455556666", "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
    core = {"title": "Publication package", "goal": "Publish confirmed knowledge.", "constraints": [],
            "steps": [[step, "format"]]}
    confirmation = json_text({"action": "confirm_plan", "actor": "local_user", "actor_name": "Ada Nowak", "at": AT,
                              "branch": "main", "commit": "5" * 40, "plan_id": plan_id, "plan_sha256": plan_hash(core),
                              "plan_version": 1, "reason": "", "content_sha256": {}, "definition_sha256": {}})
    plan = {"kind": "plan", "id": plan_id, "created_at": AT, "sources": {
        plan_source: {"ref": {"at": AT, "kind": "plan"}, "passage": None},
        plan_review: {"ref": {"at": AT, "kind": "review"}, "passage": confirmation}},
        "definitions": [{"at": AT, "covered": {"by": "review", "source": plan_review}, **core,
                         "sources": sorted([plan_source, plan_review])}]}
    assert parse_file(render(plan)) == plan
    step_file = task(step, f"plan:{plan_id}:format", [], plan=plan_id, step_key="format")
    assert check_files(tree(plan, step_file))[1] == []
    wrong = task(step, f"plan:{plan_id}:format", [], plan=plan_id, step_key="other")
    assert any("does not match" in reason for _, reason in refused(tree(plan, wrong)))
    plan["definitions"][0]["title"] = "A title nobody confirmed"
    assert "confirm_plan" in refused(tree(plan, step_file))[0][1]


def test_format_json_must_be_exact_and_unknown_entries_or_links_are_refused(tmp_path):
    files = tree(decision())
    files["FORMAT.json"] = json.dumps({"format": "cairnmind-published-memory", "format_version": 1,
                                       "project_id": PROJECT}).encode()
    assert refused(files)[0][0] == "FORMAT.json"
    folder = tmp_path / "published"
    for path, data in tree(decision()).items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    assert check_directory(folder)[1] == []
    (folder / "notes.txt").write_text("hello")
    (folder / "fact").mkdir()
    os.symlink(folder / "notes.txt", folder / "fact" / f"{OTHER}.md")
    _, errors = read_tree(folder)
    assert {path for path, _ in errors} == {"notes.txt", f"fact/{OTHER}.md"}
