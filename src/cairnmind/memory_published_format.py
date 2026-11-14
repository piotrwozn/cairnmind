"""Published memory version 1: one readable file per record or plan, rendered and parsed without a store (#170).

The renderer is the definition of the format. A reader parses a file and requires that rendering what
it parsed gives back the same bytes, so field order, the derived header, the absence of extra lines and
the agreement between the header and the revisions are all checked by one comparison. Free text is
written as blocks indented by four spaces with an explicit line count: it never starts at column 0,
so no line of text can look like a header, a field or a Git conflict marker.

A published revision is a projection of a record state that a person's act covers (a review audit
whose hash matches, a workspace-form note, or mechanical progress after one). It is not a store
version: local versions are compare-and-swap counters that differ between machines.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from uuid import UUID

from .sql import json_text
from .terminal_safe import safe_text

FORMAT = "cairnmind-published-memory"
VERSION = 1
RECORD_HEADER = "published-record 1"
PLAN_HEADER = "published-plan 1"
# Allowed by the format. The version 1 publisher writes only PUBLISHED_KINDS.
RECORD_KINDS = ("constraint", "decision", "fact", "preference", "question", "task")
PUBLISHED_KINDS = ("constraint", "decision", "fact", "task")
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 100_000
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_QUOTE_CHARS = 2000
MAX_NOTE_CHARS = 16000
GITATTRIBUTES = b"* text eol=lf\n"
GITIGNORE = b".cairnmind-memory-*\n"
TASK_DEFAULTS = {"state": "todo", "plan": None, "step_key": None, "depends_on": [], "requires_test": False,
                 "requires_review": False, "required_profiles": [], "acceptance_criteria": [],
                 "criterion_checks": {}, "scope": None}
DEFINITION_FIELDS = ("acceptance_criteria", "criterion_checks", "depends_on", "plan", "required_profiles",
                     "requires_review", "requires_test", "scope", "step_key")
RECORD_FIELDS = ("status", "key", "supersedes", "sources", "quote")
TASK_FIELDS = tuple(TASK_DEFAULTS)
PLAN_FIELDS = ("title", "constraints", "steps", "sources")
# Names that would grant trust if a reader honoured them; a file that carries one is refused by name.
TRUST_FIELDS = frozenset({"verified", "machine_verified", "verification_status", "test_passed", "review_passed",
                          "acceptance_passed", "check_run_ids", "publication", "evidence_state"})
ACT_ACTIONS = frozenset({"confirm", "dispute", "resolve_conflict", "confirm_plan"})
FILE_REF_KINDS = ("code", "document", "file")
_MARKER = re.compile(r"^(<{7}|={7}|>{7}|\|{7})( |$)")
_FIELD = re.compile(r"^([a-z_]+): (.*)$")
_BLOCK = re.compile(r"^([A-Z][a-z]+(?: [a-z]+)?), (\d+) lines?:$")
_SOURCE = re.compile(r"^Source ([0-9a-f-]{36}): (.*)$")


class PublishedFormatError(ValueError):
    """A published memory file that is not exactly what the format allows."""


def J(value) -> str:
    return json_text(value)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def same(first, second) -> bool:
    """Equality of canonical JSON: True and 1 differ, as they do in a file."""
    return J(first) == J(second)


def canonical_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def clean_text(value) -> bool:
    """Text that a terminal shows as it is: no control or bidi characters, no line or paragraph separators."""
    return isinstance(value, str) and safe_text(value) == value and " " not in value and " " not in value


def clean_value(value) -> bool:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return all(clean_value(item) for item in value)
    if isinstance(value, dict):
        return all(clean_text(key) and clean_value(item) for key, item in value.items())
    return value is None or isinstance(value, (bool, int, float))


def parse_json(text):
    from .memory import _unique_pairs
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))
    except (ValueError, RecursionError) as exc:
        raise PublishedFormatError("a value is not valid JSON") from exc


def projection(revision) -> dict:
    """What a revision says; `at` and `covered` are written but never compared."""
    return {name: value for name, value in revision.items() if name not in ("at", "covered")}


def definition_hash(revision) -> str:
    """What a task asks for, hashed: an act on a task covers exactly this."""
    return sha256_text(J({"content": revision["text"],
                          **{name: revision.get(name, TASK_DEFAULTS[name]) for name in DEFINITION_FIELDS}}))


def identity_hash(kind, key, supersedes) -> str:
    """What a record is, hashed: its kind, its key and the record it replaces. An act covers this too."""
    return sha256_text(J({"key": key, "kind": kind, "supersedes": supersedes}))


def timestamp(value) -> bool:
    """A timezone-aware ISO 8601 time in plain characters, as the store writes them."""
    from .sql import parse_utc
    return clean_text(value) and parse_utc(value) is not None


def task_fields(metadata) -> dict:
    """A stored task's metadata as the fields a published revision carries, with their defaults."""
    metadata = metadata if isinstance(metadata, dict) else {}
    values = {name: deepcopy(metadata.get(name, default)) for name, default in TASK_DEFAULTS.items()}
    values["plan"] = metadata.get("plan_ref")
    return values


def row_definition_hash(content, metadata) -> str:
    return definition_hash({"text": content, **task_fields(metadata)})


def plan_hash(core) -> str:
    return sha256_text(J({"constraints": core["constraints"], "goal": core["goal"],
                          "steps": [step[0] for step in core["steps"]], "title": core["title"]}))


def _audit(source):
    if not source or source.get("passage") is None or ref_class(source.get("ref")) != "review":
        return None
    try:
        value = parse_json(source["passage"])
    except PublishedFormatError:
        return None
    return value if isinstance(value, dict) and value.get("action") in ACT_ACTIONS else None


def audit_covers(audit, record_id, kind, status, text, definition, identity) -> bool:
    """Whether a person's recorded act covers this state of a record.

    `identity` is identity_hash(kind, key, supersedes). Audits recorded before #170 carry no identity hash;
    for them only the text, the status and a task's definition are covered.
    """
    if not isinstance(audit, dict) or audit.get("action") not in ACT_ACTIONS:
        return False
    action = audit["action"]

    def entry(name):
        value = audit.get(name)
        if isinstance(value, str):
            return value if action in ("confirm", "dispute") and audit.get("record_id") == record_id else None
        return value.get(record_id) if isinstance(value, dict) else None

    if entry("content_sha256") != sha256_text(text):
        return False
    if kind == "task" and entry("definition_sha256") != definition:
        return False
    if "identity_sha256" in audit and entry("identity_sha256") != identity:
        return False
    if action == "resolve_conflict":
        # Only choosing which finding stands judges a finding; keeping both or dismissing the alarm does not.
        if audit.get("decision") != "supersede":
            return False
        allowed = {"confirmed"} if audit.get("kept_record_id") == record_id else {"superseded"}
    else:
        allowed = {"confirm": {"confirmed"}, "dispute": {"disputed"}, "confirm_plan": {"confirmed"}}[action]
    return status in allowed


def _when(audit, source_id):
    return (str(audit.get("at", "")), source_id)


def derive_covered(kind, record_id, previous, revision, sources, number):
    """The act that covers revision `number`, derived from the file alone; None when nothing covers it."""
    task = kind == "task"
    if previous is not None:
        before, after = projection(previous), projection(revision)
        if task and not same(before["state"], after["state"]) and same(
                {k: v for k, v in before.items() if k != "state"}, {k: v for k, v in after.items() if k != "state"}):
            return {"by": "progress"}
    if revision["status"] in ("confirmed", "disputed", "superseded"):
        definition = definition_hash(revision) if task else None
        identity = identity_hash(kind, revision["key"], revision["supersedes"])
        found = []
        for source_id in revision["sources"]:
            audit = _audit(sources.get(source_id))
            if audit and audit_covers(audit, record_id, kind, revision["status"], revision["text"], definition, identity):
                found.append(_when(audit, source_id))
        if found:
            return {"by": "review", "source": max(found)[1]}
    # A mechanical supersession changes nothing but the status. A reader also requires what replaced it to be
    # in the same tree (memory_published_check), so three appended lines cannot withdraw a record.
    if (previous is not None and revision["status"] == "superseded" and previous["text"] == revision["text"]
            and previous["key"] == revision["key"] and previous["supersedes"] == revision["supersedes"]
            and (not task or definition_hash(previous) == definition_hash(revision))):
        return {"by": "superseded"}
    if number == 1 and revision["status"] == "confirmed" and len(revision["sources"]) == 1:
        source = sources.get(revision["sources"][0])
        if source and ref_class(source.get("ref")) == "note" and source.get("passage") == revision["text"]:
            return {"by": "workspace_form", "source": revision["sources"][0]}
    return None


def derive_plan_covered(plan_id, previous, definition, sources):
    """The confirm_plan act that covers a definition: the earliest one after the previous definition's act."""
    after = None
    if previous is not None:
        audit = _audit(sources.get(previous["covered"]["source"])) if isinstance(previous.get("covered"), dict) else None
        after = _when(audit, previous["covered"]["source"]) if audit else None
    digest, found = plan_hash(definition), []
    for source_id in definition["sources"]:
        audit = _audit(sources.get(source_id))
        if (audit and audit.get("action") == "confirm_plan" and audit.get("plan_id") == plan_id
                and audit.get("plan_sha256") == digest and (after is None or _when(audit, source_id) > after)):
            found.append(_when(audit, source_id))
    return {"by": "review", "source": min(found)[1]} if found else None


def ref_class(ref):
    """Which kind of source reference this is, by its exact shape; None for anything else."""
    if not isinstance(ref, dict) or not isinstance(ref.get("at"), str) or not isinstance(ref.get("kind"), str):
        return None
    keys, kind = set(ref), ref["kind"]
    if keys == {"at", "commit", "kind", "path", "sha256", "snapshot"} and kind in FILE_REF_KINDS:
        return "file"
    if keys == {"actor_name", "at", "branch", "commit", "kind", "origin"} and kind == "note":
        return "note" if ref["origin"] == "workspace_form" else None
    if keys == {"at", "kind"} and kind in ("review", "plan", "release"):
        return kind  # A release (#171) lets a person's reviewed false alarms of the secrets gate through.
    if keys == {"at", "kind", "origin"} and kind.strip() and kind not in ("release", "published"):
        return "other"
    return None


def reviewed_by(model, revisions):
    entries = []
    for number, revision in enumerate(revisions, 1):
        covered = revision.get("covered") or {}
        source = model["sources"].get(covered.get("source"))
        if covered.get("by") == "review":
            audit = _audit(source) or {}
            entries.append({"action": audit.get("action"), "at": audit.get("at"), "branch": audit.get("branch"),
                            "commit": audit.get("commit"), "name": audit.get("actor_name") or "local user",
                            "revision": number})
        elif covered.get("by") == "workspace_form":
            ref = (source or {}).get("ref") or {}
            entries.append({"action": "workspace_form", "at": ref.get("at"), "branch": ref.get("branch"),
                            "commit": ref.get("commit"), "name": ref.get("actor_name") or "local user",
                            "revision": number})
    return entries


def block(label, text):
    parts = text.split("\n")
    return [f"{label}, {len(parts)} line{'' if len(parts) == 1 else 's'}:", "",
            *[("    " + part) if part else "" for part in parts]]


def _sources_section(model):
    lines = ["## Sources", ""]
    for source_id in sorted(model["sources"]):
        source = model["sources"][source_id]
        lines.append(f"Source {source_id}: {J(source['ref'])}")
        if source["passage"] is not None:
            lines += block("Passage", source["passage"])
        lines.append("")
    return lines


def _file_value(name, value):
    if name == "quote" and value is not None:
        return {"lines": value["lines"], "source": value["source"]}
    return value


def _entry_lines(fields, entry, previous, blocks):
    lines = [f"at: {J(entry['at'])}", f"covered: {J(entry['covered'])}"]
    for name in fields:
        if previous is None or not same(entry[name], previous[name]):
            lines.append(f"{name}: {J(_file_value(name, entry[name]))}")
    written = [block(label, text) for label, text, changed in blocks if changed]
    for index, lines_of_block in enumerate(written):
        lines += ([""] if index else []) + lines_of_block
    return lines


def render_record(model) -> bytes:
    revisions, kind = model["revisions"], model["kind"]
    last, task = revisions[-1], kind == "task"
    front = [("cairnmind", RECORD_HEADER), ("kind", kind), ("id", model["id"]), ("created_at", model["created_at"]),
             ("status", last["status"]), ("revisions", len(revisions))]
    if task:
        front += [("state", last["state"]), ("plan", last["plan"])]
    front.append(("reviewed_by", reviewed_by(model, revisions)))
    lines = ["---", *[f"{name}: {J(value)}" for name, value in front], "---", "",
             f"# {kind.capitalize()} {model['id'][:8]}", "", *block("Current text", last["text"]), ""]
    lines += _sources_section(model)
    fields = RECORD_FIELDS + (TASK_FIELDS if task else ())
    for number, revision in enumerate(revisions, 1):
        previous = revisions[number - 2] if number > 1 else None
        quote = revision["quote"]
        lines += [f"## Revision {number}", ""]
        lines += _entry_lines(fields, revision, previous, [
            ("Quote", quote["text"] if quote else "", quote is not None
             and (previous is None or not same(quote, previous["quote"]))),
            ("Text", revision["text"], previous is None or revision["text"] != previous["text"])])
        if number < len(revisions):
            lines.append("")
    return ("\n".join(lines) + "\n").encode()


def render_plan(model) -> bytes:
    definitions = model["definitions"]
    front = [("cairnmind", PLAN_HEADER), ("kind", "plan"), ("id", model["id"]), ("created_at", model["created_at"]),
             ("definitions", len(definitions)), ("reviewed_by", reviewed_by(model, definitions))]
    lines = ["---", *[f"{name}: {J(value)}" for name, value in front], "---", "",
             f"# Plan {model['id'][:8]}", "", *block("Current goal", definitions[-1]["goal"]), ""]
    lines += _sources_section(model)
    for number, definition in enumerate(definitions, 1):
        previous = definitions[number - 2] if number > 1 else None
        lines += [f"## Definition {number}", ""]
        lines += _entry_lines(PLAN_FIELDS, definition, previous, [
            ("Goal", definition["goal"], previous is None or definition["goal"] != previous["goal"])])
        if number < len(definitions):
            lines.append("")
    return ("\n".join(lines) + "\n").encode()


def render(model) -> bytes:
    return render_plan(model) if model["kind"] == "plan" else render_record(model)


def path_of(model) -> str:
    return f"{model['kind']}/{model['id']}.md"


def decode(raw: bytes) -> str:
    """UTF-8 without BOM. A file whose every line ends in CRLF (a Windows checkout) is read as LF."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PublishedFormatError("the file starts with a byte order mark")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PublishedFormatError("the file is not UTF-8") from None
    lines = text.split("\n")
    if "\r" in text and all(line.endswith("\r") for line in lines[:-1]) and lines[-1] == "":
        text = text.replace("\r\n", "\n")
    return text


def conflict_marker(text: str):
    """The first line, numbered from 1, that is a Git conflict marker; None when there is none."""
    for number, line in enumerate(text.split("\n"), 1):
        if _MARKER.match(line):
            return number
    return None
