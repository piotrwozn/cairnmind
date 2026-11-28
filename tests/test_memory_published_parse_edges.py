"""Fast-tier edge-case validation tests for cairnmind.memory.memory_published_parse."""
from __future__ import annotations

from copy import deepcopy

import pytest

from cairnmind.memory.memory_published_format import MAX_NOTE_CHARS, PublishedFormatError, render
from cairnmind.memory.memory_published_parse import (
    _blocks,
    _check_revision,
    _check_source,
    _entries,
    _fields,
    _Lines,
    _plan_entry,
    _record_entry,
    _sources,
    parse_file,
)

UUID1 = "0b6c2f9e-5d1a-4c8e-9a43-2f1d7c9e8a10"
UUID2 = "9a2b6c1d-3e4f-4a5b-8c7d-0e1f2a3b4c5d"
UUID3 = "7d1e4b2a-0c9f-4e35-8a61-b2c3d4e5f607"
AT = "2026-11-12T09:14:03.120412+00:00"


def test_lines_fields_blocks_entries_and_sources_errors():
    with pytest.raises(PublishedFormatError, match="line break"):
        _Lines("no newline")

    lines = _Lines("a\n")
    assert lines.take() == "a"
    with pytest.raises(PublishedFormatError, match="ends too early"):
        lines.take()

    with pytest.raises(PublishedFormatError, match="expected a field"):
        _Lines("not a field\n").field()

    with pytest.raises(PublishedFormatError, match="expected the block"):
        _Lines("Wrong, 1 line:\n\n    hi\n").block("Text")
    with pytest.raises(PublishedFormatError, match="impossible line count"):
        _Lines("Text, 0 lines:\n\n").block("Text")
    with pytest.raises(PublishedFormatError, match="indented by four spaces"):
        _Lines("Text, 1 line:\n\nnot indented\n").block("Text")

    with pytest.raises(PublishedFormatError, match="appears twice"):
        _fields(_Lines('a: 1\na: 2\n'), {"a"})

    with pytest.raises(PublishedFormatError, match="unexpected block"):
        _blocks(_Lines("Other, 1 line:\n\n    hi\n"), {"Text"})

    empty_lines = _Lines("x\n")
    empty_lines.take()
    with pytest.raises(PublishedFormatError, match="no revision"):
        _entries(empty_lines, "Revision", (), set(), lambda *a: None)

    with pytest.raises(PublishedFormatError, match="invalid or repeated source"):
        _sources(_Lines("## Sources\n\nSource not-a-uuid: {}\n\n"))


def test_check_source_rejects_malformed_references_and_passages():
    # Unknown ref class / bad timestamp
    with pytest.raises(PublishedFormatError, match="does not allow"):
        _check_source(UUID1, {"ref": {"at": "bad", "kind": "unknown"}, "passage": None})

    # Review with broken JSON audit
    with pytest.raises(PublishedFormatError, match="not an audit"):
        _check_source(UUID1, {"ref": {"at": AT, "kind": "review"}, "passage": "{bad"})

    # Passage presence mismatch (conversation with passage; note without passage)
    with pytest.raises(PublishedFormatError, match="has a passage where none belongs"):
        _check_source(UUID1, {"ref": {"at": AT, "kind": "conversation", "origin": "codex"}, "passage": "x"})
    with pytest.raises(PublishedFormatError, match="has a passage where none belongs"):
        _check_source(UUID1, {"ref": {"at": AT, "kind": "note", "origin": "workspace_form", "actor_name": "A", "branch": "main", "commit": "0" * 40}, "passage": None})

    # Invalid passage characters (control char) & oversized note
    note_ref = {"at": AT, "kind": "note", "origin": "workspace_form", "actor_name": "A", "branch": "main", "commit": "0" * 40}
    with pytest.raises(PublishedFormatError, match="passage the format does not allow"):
        _check_source(UUID1, {"ref": note_ref, "passage": "bad\x00char"})
    with pytest.raises(PublishedFormatError, match="longer than"):
        _check_source(UUID1, {"ref": note_ref, "passage": "a" * (MAX_NOTE_CHARS + 1)})

    # File source with escaping / absolute / backslash path
    for bad_path in ("../secret.py", "/etc/passwd", "a\\b.py"):
        with pytest.raises(PublishedFormatError, match="not relative"):
            _check_source(UUID1, {"ref": {"at": AT, "kind": "code", "path": bad_path, "sha256": "a" * 64, "commit": "0" * 40, "snapshot": "working_tree"}, "passage": None})


def test_check_revision_and_entry_builders_reject_every_invalid_field():
    sources = {
        UUID2: {"ref": {"at": AT, "kind": "note", "origin": "workspace_form", "actor_name": "A", "branch": "main", "commit": "0" * 40}, "passage": "line1\nline2"},
    }
    base_rev = {
        "at": AT,
        "covered": {"by": "workspace_form", "source": UUID2},
        "status": "confirmed",
        "key": "k1",
        "supersedes": None,
        "sources": [UUID2],
        "quote": None,
        "text": "Valid text",
        "state": "todo",
        "plan": None,
        "step_key": None,
        "depends_on": [],
        "requires_test": False,
        "requires_review": False,
        "required_profiles": [],
        "acceptance_criteria": [],
        "criterion_checks": {},
        "scope": None,
    }
    _check_revision(1, base_rev, sources, True)

    bad_mutations = [
        ({"status": "bogus"}, "unknown status"),
        ({"key": "   "}, "invalid key"),
        ({"supersedes": "not-uuid"}, "record ID"),
        ({"sources": [UUID3]}, "listed under Sources"),
        ({"quote": {"source": UUID2, "lines": [1, 2], "text": "one-line"}}, "invalid quote"),
        ({"quote": {"source": UUID2, "lines": [2, 2], "text": "line1"}}, "not at those lines"),
        ({"state": "bogus"}, "unknown task state"),
        ({"plan": "bad-uuid"}, "plan ID"),
        ({"step_key": "   "}, "invalid step key"),
        ({"depends_on": ["bad"]}, "task IDs"),
        ({"requires_test": "yes"}, "true or false"),
        ({"required_profiles": [""]}, "must list text"),
        ({"criterion_checks": "bad"}, "must be an object"),
        ({"scope": "not-a-valid-scope"}, "invalid scope"),
    ]
    for patch, msg in bad_mutations:
        rev = {**deepcopy(base_rev), **patch}
        with pytest.raises(PublishedFormatError, match=msg):
            _check_revision(1, rev, sources, True)

    # _record_entry builder guards
    build_rec = _record_entry({"kind": "decision", "id": UUID1, "sources": sources}, False)
    with pytest.raises(PublishedFormatError, match="revision 1 lacks"):
        build_rec(1, {}, {}, None)
    with pytest.raises(PublishedFormatError, match="needs its Quote block"):
        build_rec(1, {**base_rev, "quote": {"source": UUID2, "lines": [1, 1]}}, {"Text": "hi"}, None)
    with pytest.raises(PublishedFormatError, match="without its quote field"):
        build_rec(2, {}, {"Quote": "q"}, base_rev)
    with pytest.raises(PublishedFormatError, match="at and covered"):
        build_rec(2, {}, {}, base_rev)

    # _plan_entry builder guards
    plan_sources = {
        UUID2: {"ref": {"at": AT, "kind": "plan"}, "passage": None},
        UUID3: {"ref": {"at": AT, "kind": "review"}, "passage": "{}"},
    }
    build_plan = _plan_entry({"kind": "plan", "id": UUID1, "sources": plan_sources})
    with pytest.raises(PublishedFormatError, match="definition 1 lacks"):
        build_plan(1, {}, {}, None)
    plan_base = {
        "at": AT,
        "covered": {"by": "review", "source": UUID3},
        "title": "Plan A",
        "constraints": ["c1"],
        "steps": [[UUID1, "Step 1"]],
        "sources": sorted([UUID2, UUID3]),
    }
    with pytest.raises(PublishedFormatError, match="at and covered"):
        build_plan(1, {k: v for k, v in plan_base.items() if k != "covered"}, {"Goal": "g"}, None)
    with pytest.raises(PublishedFormatError, match="invalid title"):
        build_plan(1, {**plan_base, "title": "  "}, {"Goal": "g"}, None)
    with pytest.raises(PublishedFormatError, match="one plan source"):
        build_plan(1, {**plan_base, "sources": [UUID3]}, {"Goal": "g"}, None)
    with pytest.raises(PublishedFormatError, match="repeats the one before it"):
        build_plan(2, {**plan_base, "goal": "g"}, {}, {**plan_base, "goal": "g"})


def test_parse_file_top_level_error_paths():
    # Conflict marker
    with pytest.raises(PublishedFormatError, match="unresolved conflict"):
        parse_file(b"<<<<<<< HEAD\na\n=======\nb\n>>>>>>> main\n")
    # Duplicate frontmatter field
    with pytest.raises(PublishedFormatError, match="appears twice"):
        parse_file(b'---\ncairnmind: "published-record 1"\ncairnmind: "published-record 1"\n---\n')
    # Header/kind mismatch
    with pytest.raises(PublishedFormatError, match="version 1"):
        parse_file(b'---\ncairnmind: "published-plan 1"\nkind: "decision"\nid: "' + UUID1.encode() + b'"\ncreated_at: "' + AT.encode() + b'"\n---\n')
    # Bad UUID
    with pytest.raises(PublishedFormatError, match="canonical UUID"):
        parse_file(b'---\ncairnmind: "published-record 1"\nkind: "decision"\nid: "bad"\ncreated_at: "' + AT.encode() + b'"\n---\n')

    # Uncited source rejected
    from test_published_memory_format import constraint
    model = constraint()
    raw = render(model).decode("utf-8")
    extra_src = f'Source {UUID3}: {{"at":"{AT}","kind":"conversation","origin":"codex"}}\n\n'
    tampered = raw.replace("## Revision 1", extra_src + "## Revision 1")
    with pytest.raises(PublishedFormatError, match="must list exactly"):
        parse_file(tampered.encode("utf-8"))
