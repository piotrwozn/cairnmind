"""Record/task validation helpers, schema collection tuples, and domain exceptions for Store."""
from __future__ import annotations

import re
from typing import Any

from cairnmind.storage.sql import PROFILE_NAME

KINDS = {"decision", "task", "fact", "constraint", "question", "preference", "checkpoint"}
STATUSES = {"proposed", "confirmed", "disputed", "superseded"}
TASK_STATES = {"todo", "in_progress", "needs_test", "needs_review", "done", "blocked"}

LEGACY_DOCUMENT_TYPES = (
    "projects", "project_roots", "sources", "chunks", "records", "record_revisions", "jobs",
    "embeddings", "import_cursors", "ingestion_inbox", "vector_indexes",
)
V03_DOCUMENT_TYPES = (
    "plans", "plan_revisions", "plan_requests", "worktree_states", "sessions",
    "resume_packets", "resume_acks", "session_events", "check_runs",
)
V04_DOCUMENT_TYPES = ("memory_settings", "memory_snapshots", "memory_imports", "memory_bindings")
V05_DOCUMENT_TYPES = ("journal_state", "journal_events")
DOCUMENT_TYPES = (*LEGACY_DOCUMENT_TYPES, *V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES)
LEGACY_EXPORT_TYPES = (*LEGACY_DOCUMENT_TYPES, "nodes", "edges")
V03_EXPORT_TYPES = (*LEGACY_DOCUMENT_TYPES, *V03_DOCUMENT_TYPES, "nodes", "edges")
V04_EXPORT_TYPES = (*LEGACY_DOCUMENT_TYPES, *V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES, "nodes", "edges")
EXPORT_TYPES = (*DOCUMENT_TYPES, "nodes", "edges")
# What restore checks a backup against; an earlier format's payload has the later types empty.
RESTORE_TYPES = {
    "export": EXPORT_TYPES,
    "v03": V03_DOCUMENT_TYPES,
    "v04": V04_DOCUMENT_TYPES,
    "v05": V05_DOCUMENT_TYPES,
}

MANAGED_DEFINITION_METADATA_FIELDS = frozenset({
    "plan_ref", "step_key", "required_profiles", "plan_context_hash", "depends_on", "acceptance_criteria",
    "requires_test", "requires_review", "step_index", "plan_archived", "criterion_checks", "check_proposals", "scope",
})
# Definition fields that say nothing about whether a task was proven: changing one keeps reported evidence.
ADVISORY_DEFINITION_FIELDS = frozenset({"check_proposals", "scope"})

# Where a write came from, set by the service from the session row and never by a client: today only
# for an external client's session, whose conversation is unarchived and whose evidence is degraded.
ORIGIN_METADATA = ("origin_client", "origin_session_id", "origin_history", "origin_evidence")

# Set only by the extraction worker's own write, on the version it wrote, and dropped by any later write:
# the archived source this version was extracted from (#168). A client can neither set nor keep it.
EXTRACTION_MARK = "extraction_source_id"

# How the extraction worker made a record (#192): the extractor version, the prompt hash and the model as
# name@digest (extraction_input.extractor_stamp). Set only by the worker's own write; unlike EXTRACTION_MARK
# it describes the record, not this machine, so later writes keep it and portable memory carries it.
EXTRACTOR_STAMP = "extractor"
EXTRACTOR_MODEL_CHARS = 400
_PROMPT_SHA = re.compile(r"[0-9a-f]{16}")

# Record metadata derived by the store; values supplied by a client are never kept.
# "publication" is set only by the importer of published memory (#170): a client can neither forge it
# nor clear it, and it stays on later local versions the way an origin mark does.
# "confirmation" says how the record came to be confirmed (#177, confirmation.py): the store sets it from a
# person's act it checked itself, an importer sets it for what a package brings, and no client can.
DERIVED_RECORD_METADATA = (
    "evidence_earliest", "evidence_latest", "evidence_basis",
    "supersede_warning", "supersede_replaced_evidence_latest", EXTRACTION_MARK,
    EXTRACTOR_STAMP, *ORIGIN_METADATA, "publication", "confirmation", "quote_ref",
    "symbol_anchor", "comment_anchor", "adr_anchor", "trailer_anchor",
)

# Not part of what a plan step says: plan-level or transient fields and everything the store derives.
_UNCOMPARED_METADATA = frozenset({"plan_context_hash", "plan_archived", "worktree", *DERIVED_RECORD_METADATA})

# Bookkeeping of conflict decisions. Only the conflict inbox, acting for a person, may
# write these; from any other writer they are dropped, like derived metadata.
CONFLICT_REVIEW_METADATA = (
    "conflicts_resolved", "conflict_decisions", "conflict_scope_note",
    "superseded_ids", "superseded_by",
)

# Journal entity types that do not move the answer cache stamp. Jobs, sessions, imports and
# snapshots cannot alter an answer. The derived graph is rebuilt, and reported as reset, on
# every indexing even when it comes out identical; a real change of it is seen through the
# project row, whose graph fingerprint changes, and through the sources and records behind it.
CACHE_STAMP_IGNORED = frozenset({
    "job", "session", "import", "inbox", "memory", "plan_request",
    "graph", "node", "edge",
})


def validate_scope(scope: Any) -> None:
    """The files a task says it will touch: path patterns as in an ignore file, relative to the worktree."""
    if (
        not isinstance(scope, list)
        or len(scope) > 64
        or not all(isinstance(pattern, str) and pattern.strip() and len(pattern) <= 500 for pattern in scope)
    ):
        raise ValueError("scope must be a list of at most 64 nonempty path patterns")
    for pattern in scope:
        parts = pattern.replace("\\", "/").split("/")
        if (
            pattern.startswith(("/", "!", "#", "./"))
            or "\\" in pattern
            or "\0" in pattern
            or "\n" in pattern
            or ".." in parts
            or ".git" in {part.casefold() for part in parts}
            or re.match(r"^[A-Za-z]:", pattern)
        ):
            raise ValueError("scope patterns must stay inside the worktree, cannot negate and cannot start with # or ./")
    try:
        import pathspec
        pathspec.GitIgnoreSpec.from_lines(scope)
    except Exception as exc:  # noqa: BLE001 - the library raises more than one kind of error for a bad pattern
        raise ValueError(f"a scope pattern cannot be read: {str(exc)[:200]}") from None


def valid_argv(argv: Any) -> bool:
    """A command that may be proposed for a check: a short list of non-empty strings."""
    return (
        isinstance(argv, list)
        and 0 < len(argv) <= 64
        and all(isinstance(part, str) and part and "\0" not in part and len(part) <= 2000 for part in argv)
    )


def validate_criterion_links(metadata: dict[str, Any], criteria: list[Any]) -> None:
    """The stored form of the links: every link belongs to a criterion, every proposal to a linked check."""
    links = metadata.get("criterion_checks", {})
    if (
        not isinstance(links, dict)
        or ("criterion_checks" in metadata and not links)
        or not all(
            text in criteria and isinstance(check, str) and PROFILE_NAME.fullmatch(check)
            for text, check in links.items()
        )
    ):
        raise ValueError("criterion_checks must link acceptance criteria to check profile names")
    proposals = metadata.get("check_proposals", {})
    if (
        not isinstance(proposals, dict)
        or ("check_proposals" in metadata and not proposals)
        or not all(name in links.values() and valid_argv(argv) for name, argv in proposals.items())
    ):
        raise ValueError("check_proposals must give an argument list for a linked check")


def normalise_criteria(metadata: dict[str, Any]) -> None:
    """Accept a criterion written as {text, check, argv} and store the link beside the text.

    `acceptance_criteria` stays a list of strings, which every version reads. The link from a
    criterion to the check profile that proves it lives in `criterion_checks` (text -> profile),
    and a command proposed for a profile that is not declared yet in `check_proposals`
    (profile -> argument list). A proposal is text for a person to review: nothing runs it.
    """
    criteria = metadata.get("acceptance_criteria")
    links, proposals = metadata.get("criterion_checks", {}), metadata.get("check_proposals", {})
    if not isinstance(criteria, list) or not isinstance(links, dict) or not isinstance(proposals, dict):
        return  # left as it is for the validation that follows to refuse
    links, proposals = dict(links), dict(proposals)
    if any(isinstance(item, dict) for item in criteria):
        texts: list[str] = []
        for item in criteria:
            if isinstance(item, str):
                texts.append(item)
                continue
            if not isinstance(item, dict) or set(item) - {"text", "check", "argv"} or not isinstance(item.get("text"), str):
                raise ValueError("A criterion is text, or an object with text and optionally check and argv")
            text, check, argv = item["text"], item.get("check"), item.get("argv")
            texts.append(text)
            if check is None:
                if argv is not None:
                    raise ValueError("A criterion can propose a command only for a named check")
                continue
            if not isinstance(check, str) or not PROFILE_NAME.fullmatch(check):
                raise ValueError("criterion_checks must link acceptance criteria to check profile names")
            if links.get(text, check) != check:
                raise ValueError("One criterion cannot be linked to two checks")
            links[text] = check
            if argv is not None:
                if proposals.get(check, argv) != argv:
                    raise ValueError("Two different commands were proposed for one check")
                proposals[check] = argv
        metadata["acceptance_criteria"] = texts
    # A link or a proposal whose criterion or check is gone has nothing left to describe.
    links = {text: check for text, check in links.items() if text in metadata["acceptance_criteria"]}
    proposals = {name: argv for name, argv in proposals.items() if name in links.values()}
    for key, value in (("criterion_checks", links), ("check_proposals", proposals)):
        if value:
            metadata[key] = value
        else:
            metadata.pop(key, None)


class KeyTaken(ValueError):
    """Another current record of the same kind holds the key of this write; it names that record (#238)."""

    def __init__(self, record_id: Any) -> None:
        self.record_id = record_id
        super().__init__(
            f"Active record key already exists: record {record_id} holds it. Update that record "
            "using its id and expected_version, or store this one under another key"
        )


class RecordRefused(ValueError):
    """A record of a shape the store does not save (kind, status, content, key, metadata). The service marks
    it, so that the built-in agent hands the model the reason instead of a bare tool failure (#240)."""


def validate_task_state(value: Any) -> None:
    """Validate a supplied task state without coercing or inferring a value."""
    if not isinstance(value, str) or value not in TASK_STATES:
        raise ValueError(f"Task state must be one of: {', '.join(sorted(TASK_STATES))}")


def validate_extractor_stamp(value: Any) -> dict[str, Any]:
    """A copy of a well-formed extractor stamp; ValueError for anything else."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "prompt_sha256", "model"}
        or type(value["version"]) is not int
        or not 1 <= value["version"] <= 10**6
        or not isinstance(value["prompt_sha256"], str)
        or not _PROMPT_SHA.fullmatch(value["prompt_sha256"])
        or not isinstance(value["model"], str)
        or not 3 <= len(value["model"]) <= EXTRACTOR_MODEL_CHARS
        or "@" not in value["model"][1:-1]
    ):
        raise ValueError("Invalid extractor stamp")
    return dict(value)


def outcome_changed(old: Any, content: Any, key: Any, metadata: dict[str, Any]) -> bool:
    """Whether a write stores a record that says something else than the stored version.

    Compares the result, not the request: a field the write drops as a consequence counts as well.
    """
    if not old:
        return True
    before = {name: value for name, value in (old.get("metadata") or {}).items() if name not in _UNCOMPARED_METADATA}
    after = {name: value for name, value in metadata.items() if name not in _UNCOMPARED_METADATA}
    return content != old["content"] or key != old["key"] or before != after


def degraded_origin(metadata: Any) -> bool:
    """Whether a record, source or plan was written by an external session (never verified)."""
    return isinstance(metadata, dict) and metadata.get("origin_evidence") == "degraded"
