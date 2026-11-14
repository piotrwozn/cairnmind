"""Strict reading of one published memory file (#170).

The parser rebuilds the model a file describes and then requires three things: every value has the
type and characters the format allows, every revision is covered by a person's act whose passage is in
the same file, and rendering the model gives back the file's bytes. A hand-merged or hand-edited file
fails one of them and is refused with the reason; nothing in a file can make a record verified.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath

from .memory_published_format import (
    _BLOCK, _FIELD, _SOURCE, MAX_NOTE_CHARS, PLAN_FIELDS, PLAN_HEADER, RECORD_FIELDS, RECORD_HEADER,
    RECORD_KINDS, TASK_FIELDS, TRUST_FIELDS, PublishedFormatError, canonical_uuid,
    clean_text, clean_value, conflict_marker, decode, derive_covered, derive_plan_covered, parse_json,
    ref_class, render, same,
)
from .store import STATUSES, TASK_STATES


class _Lines:
    def __init__(self, text):
        self.lines, self.index = text[:-1].split("\n") if text.endswith("\n") else text.split("\n"), 0
        if not text.endswith("\n"):
            raise PublishedFormatError("the file does not end with a line break")

    def peek(self, offset=0):
        position = self.index + offset
        return self.lines[position] if position < len(self.lines) else None

    def take(self):
        line = self.peek()
        if line is None:
            raise PublishedFormatError("the file ends too early")
        self.index += 1
        return line

    def expect(self, value, what=None):
        if self.take() != value:
            raise PublishedFormatError(f"line {self.index}: expected {what or repr(value)}")

    def field(self):
        match = _FIELD.match(self.take())
        if not match:
            raise PublishedFormatError(f"line {self.index}: expected a field")
        return match.group(1), parse_json(match.group(2))

    def block(self, label):
        match = _BLOCK.match(self.take())
        if not match or match.group(1) != label:
            raise PublishedFormatError(f"line {self.index}: expected the block {label!r}")
        count = int(match.group(2))
        if not 1 <= count <= len(self.lines):
            raise PublishedFormatError(f"line {self.index}: impossible line count")
        self.expect("", "an empty line after the block label")
        parts = []
        for _ in range(count):
            line = self.take()
            if line and not line.startswith("    "):
                raise PublishedFormatError(f"line {self.index}: text must be indented by four spaces")
            parts.append(line[4:])
        return "\n".join(parts)


def _fields(lines, allowed):
    found = {}
    while lines.peek() is not None and _FIELD.match(lines.peek()):
        name, value = lines.field()
        if name in found:
            raise PublishedFormatError(f"line {lines.index}: the field {name} appears twice")
        if name not in allowed:
            if name in TRUST_FIELDS or name.endswith("_source_ids") or name.startswith("origin_"):
                raise PublishedFormatError(f"line {lines.index}: trust fields are never published ({name})")
            raise PublishedFormatError(f"line {lines.index}: unknown field {name}")
        found[name] = value
    return found


def _blocks(lines, allowed):
    found = {}
    while lines.peek() is not None and _BLOCK.match(lines.peek()):
        label = _BLOCK.match(lines.peek()).group(1)
        if label not in allowed or label in found:
            raise PublishedFormatError(f"line {lines.index + 1}: unexpected block {label!r}")
        found[label] = lines.block(label)
        if lines.peek() == "" and lines.peek(1) is not None and _BLOCK.match(lines.peek(1)):
            lines.take()
    return found


def _entries(lines, word, fields, labels, build):
    entries = []
    while lines.peek() is not None:
        number = len(entries) + 1
        lines.expect(f"## {word} {number}", f"{word.lower()} {number} (numbers are 1..m with no gap or repeat)")
        lines.expect("", "an empty line")
        found = _fields(lines, {"at", "covered", *fields})
        blocks = _blocks(lines, labels)
        if lines.peek() is not None:
            lines.expect("", "an empty line between entries")
        entries.append(build(number, found, blocks, entries[-1] if entries else None))
    if not entries:
        raise PublishedFormatError(f"the file has no {word.lower()}")
    return entries


def _sources(lines):
    lines.expect("## Sources")
    lines.expect("", "an empty line")
    sources = {}
    while (lines.peek() or "").startswith("Source "):
        match = _SOURCE.match(lines.take())
        if not match or not canonical_uuid(match.group(1)) or match.group(1) in sources:
            raise PublishedFormatError(f"line {lines.index}: invalid or repeated source line")
        ref = parse_json(match.group(2))
        passage = lines.block("Passage") if (lines.peek() or "").startswith("Passage, ") else None
        lines.expect("", "an empty line after a source")
        sources[match.group(1)] = {"ref": ref, "passage": passage}
    return sources


def _check_source(source_id, source):
    ref, passage, kind = source["ref"], source["passage"], ref_class(source["ref"])
    if kind is None or not clean_value(ref):
        raise PublishedFormatError(f"source {source_id} has a reference the format does not allow")
    if (passage is not None) != (kind in ("note", "review")):
        raise PublishedFormatError(f"source {source_id} has a passage where none belongs, or lacks one")
    if passage is not None and (not clean_text(passage) or not passage or len(passage) > MAX_NOTE_CHARS * 4):
        raise PublishedFormatError(f"source {source_id} has a passage the format does not allow")
    if kind == "note" and len(passage) > MAX_NOTE_CHARS:
        raise PublishedFormatError(f"source {source_id} is a note longer than {MAX_NOTE_CHARS} characters")
    if kind == "file":
        path = ref["path"]
        if (not isinstance(path, str) or not path or "\\" in path or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts or not isinstance(ref["sha256"], str)):
            raise PublishedFormatError(f"source {source_id} has a path that is not relative to the project")


def _uuid_list(value):
    return isinstance(value, list) and all(canonical_uuid(item) for item in value)


def _check_revision(number, revision, sources, task):
    def bad(what):
        raise PublishedFormatError(f"revision {number}: {what}")
    if not isinstance(revision["at"], str) or not isinstance(revision["covered"], dict):
        bad("at and covered are required")
    if revision["status"] not in STATUSES:
        bad("unknown status")
    if not clean_text(revision["key"]) or not revision["key"].strip():
        bad("invalid key")
    if revision["supersedes"] is not None and not canonical_uuid(revision["supersedes"]):
        bad("supersedes must be a record ID")
    if (not _uuid_list(revision["sources"]) or not revision["sources"]
            or revision["sources"] != sorted(set(revision["sources"]))
            or any(source_id not in sources for source_id in revision["sources"])):
        bad("sources must be sorted, distinct and listed under Sources")
    if not clean_text(revision["text"]) or not revision["text"].strip():
        bad("the text is empty or holds characters the format does not allow")
    quote = revision["quote"]
    if quote is not None:
        lines = quote.get("lines")
        if (set(quote) != {"lines", "source", "text"} or quote["source"] not in revision["sources"]
                or not isinstance(lines, list) or len(lines) != 2 or not all(type(n) is int for n in lines)
                or lines[0] < 1 or lines[1] != lines[0] + quote["text"].count("\n")
                or not clean_text(quote["text"]) or not quote["text"].strip()):
            bad("invalid quote")
        passage = sources[quote["source"]]["passage"]
        if passage is not None and (quote["text"] not in passage
                                    or passage.split(quote["text"], 1)[0].count("\n") + 1 != lines[0]):
            bad("the quote is not at those lines of its source")
    if not task:
        return
    if revision["state"] not in TASK_STATES:
        bad("unknown task state")
    if revision["plan"] is not None and not canonical_uuid(revision["plan"]):
        bad("plan must be a plan ID")
    if revision["step_key"] is not None and not (clean_text(revision["step_key"]) and revision["step_key"].strip()):
        bad("invalid step key")
    if not _uuid_list(revision["depends_on"]):
        bad("depends_on must list task IDs")
    for name in ("requires_test", "requires_review"):
        if type(revision[name]) is not bool:
            bad(f"{name} must be true or false")
    for name in ("required_profiles", "acceptance_criteria"):
        if not isinstance(revision[name], list) or not all(clean_text(i) and i.strip() for i in revision[name]):
            bad(f"{name} must list text")
    if not isinstance(revision["criterion_checks"], dict) or not clean_value(revision["criterion_checks"]):
        bad("criterion_checks must be an object")
    if revision["scope"] is not None:
        from .store import validate_scope
        try:
            validate_scope(revision["scope"])
        except ValueError:
            bad("invalid scope")


def _record_entry(model, task):
    fields = RECORD_FIELDS + (TASK_FIELDS if task else ())

    def build(number, found, blocks, previous):
        if previous is None:
            missing = [name for name in fields if name not in found]
            if missing or "Text" not in blocks:
                raise PublishedFormatError(f"revision 1 lacks {', '.join(missing) or 'its text'}")
            revision = {}
        else:
            revision = deepcopy(previous)
        revision.update({name: value for name, value in found.items() if name != "quote"})
        if "Text" in blocks:
            revision["text"] = blocks["Text"]
        if "quote" in found:
            value = found["quote"]
            if value is None:
                revision["quote"] = None
            elif not isinstance(value, dict) or "Quote" not in blocks:
                raise PublishedFormatError(f"revision {number}: a quote needs its Quote block")
            else:
                revision["quote"] = {**value, "text": blocks["Quote"]}
        elif "Quote" in blocks:
            raise PublishedFormatError(f"revision {number}: a Quote block without its quote field")
        if "at" not in found or "covered" not in found:
            raise PublishedFormatError(f"revision {number}: at and covered are required")
        _check_revision(number, revision, model["sources"], task)
        derived = derive_covered(model["kind"], model["id"], previous, revision, model["sources"], number)
        if derived is None or not same(derived, revision["covered"]):
            raise PublishedFormatError(
                f"revision {number} is not covered by a person's act in this file; resolve a conflict by "
                "taking one side whole, never by merging text")
        return revision
    return build


def _plan_entry(model):
    def build(number, found, blocks, previous):
        if previous is None and (set(PLAN_FIELDS) - set(found) or "Goal" not in blocks):
            raise PublishedFormatError("definition 1 lacks a field or its goal")
        definition = deepcopy(previous) if previous else {}
        definition.update(found)
        if "Goal" in blocks:
            definition["goal"] = blocks["Goal"]
        if "at" not in found or "covered" not in found or not isinstance(definition["at"], str):
            raise PublishedFormatError(f"definition {number}: at and covered are required")
        steps, sources = definition["steps"], definition["sources"]
        if (not clean_text(definition["title"]) or not definition["title"].strip()
                or not clean_text(definition["goal"]) or not isinstance(definition["constraints"], list)
                or not all(clean_text(c) and c.strip() for c in definition["constraints"])
                or not isinstance(steps, list) or not all(
                    isinstance(s, list) and len(s) == 2 and canonical_uuid(s[0]) and clean_text(s[1]) and s[1].strip()
                    for s in steps) or len({s[0] for s in steps}) != len(steps)):
            raise PublishedFormatError(f"definition {number}: invalid title, goal, constraints or steps")
        if (not _uuid_list(sources) or sources != sorted(set(sources)) or any(s not in model["sources"] for s in sources)
                or [ref_class(model["sources"][s]["ref"]) for s in sources].count("plan") != 1
                or any(ref_class(model["sources"][s]["ref"]) not in ("plan", "review") for s in sources)):
            raise PublishedFormatError(f"definition {number}: sources must be one plan source and its reviews")
        if previous is not None and same({k: definition[k] for k in ("title", "goal", "constraints", "steps")},
                                         {k: previous[k] for k in ("title", "goal", "constraints", "steps")}):
            raise PublishedFormatError(f"definition {number} repeats the one before it")
        derived = derive_plan_covered(model["id"], previous, definition, model["sources"])
        if derived is None or not same(derived, definition["covered"]):
            raise PublishedFormatError(
                f"definition {number} is not covered by a confirm_plan act in this file; take one side whole")
        return definition
    return build


def parse_file(raw: bytes) -> dict:
    """The model of one file, or PublishedFormatError naming what is wrong."""
    text = decode(raw)
    marker = conflict_marker(text)
    if marker is not None:
        raise PublishedFormatError(f"unresolved conflict at line {marker}")
    lines = _Lines(text)
    lines.expect("---")
    front = {}
    while lines.peek() != "---":
        name, value = lines.field()
        if name in front:
            raise PublishedFormatError(f"line {lines.index}: the field {name} appears twice")
        front[name] = value
    lines.take()
    header, kind = front.get("cairnmind"), front.get("kind")
    if header not in (RECORD_HEADER, PLAN_HEADER) or (header == PLAN_HEADER) != (kind == "plan"):
        raise PublishedFormatError("not a published memory file of version 1")
    if (kind != "plan" and kind not in RECORD_KINDS) or not canonical_uuid(front.get("id")) \
            or not isinstance(front.get("created_at"), str):
        raise PublishedFormatError("invalid kind, id or created_at")
    lines.expect("", "an empty line")
    lines.take()  # The title line; the rendering comparison checks it.
    lines.expect("", "an empty line")
    lines.block("Current goal" if kind == "plan" else "Current text")
    lines.expect("", "an empty line")
    model = {"kind": kind, "id": front["id"], "created_at": front["created_at"], "sources": _sources(lines)}
    for source_id, source in model["sources"].items():
        _check_source(source_id, source)
    if kind == "plan":
        model["definitions"] = _entries(lines, "Definition", PLAN_FIELDS, {"Goal"}, _plan_entry(model))
        cited = {s for definition in model["definitions"] for s in definition["sources"]}
    else:
        task = kind == "task"
        model["revisions"] = _entries(lines, "Revision", RECORD_FIELDS + (TASK_FIELDS if task else ()),
                                      {"Quote", "Text"}, _record_entry(model, task))
        cited = {s for revision in model["revisions"] for s in revision["sources"]}
    if cited != set(model["sources"]):
        raise PublishedFormatError("the Sources section must list exactly the sources the revisions cite")
    if render(model) != text.encode():
        raise PublishedFormatError("the file is not in canonical form (edited or merged by hand); take one side whole")
    return model
