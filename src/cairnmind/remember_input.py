"""What a client may send to /remember, checked at the service boundary before anything is written.

A field the store does not read was dropped without a word: a task moved with ``state`` beside the
record instead of in its metadata got a new version and kept its old state. Such a request is now
refused with the field named. An external session, which has no archived conversation to cite, may
send no source: its report is then kept as the record's own source, stamped with the session's mark
like everything it writes, so it never anchors the record or looks verified.
"""
from __future__ import annotations

from uuid import uuid4

from .sql import json_text

RECORD_FIELDS = frozenset({"id", "expected_version", "expected_plan_version", "kind", "status", "content", "key",
                           "metadata", "supersedes", "worktree"})
# Fields that belong in record.metadata and are easily put beside the record by mistake.
METADATA_FIELDS = frozenset({
    "state", "depends_on", "acceptance_criteria", "requires_test", "requires_review", "required_profiles",
    "check_run_ids", "plan_ref", "step_key", "step_index", "scope", "test_passed", "test_source_ids",
    "review_passed", "review_source_ids", "acceptance_passed", "acceptance_source_ids", "criterion_checks",
    "evidence", "topic", "reported_state"})
REPORT_ORIGIN = {"origin": "client", "role": "external_report"}


def _shown(name) -> str:
    text = str(name)
    return "".join(char if char.isprintable() else "?" for char in text[:60]) + ("…" if len(text) > 60 else "")


def record_error(record) -> str | None:
    """Why the record cannot be saved as sent, or None."""
    if not isinstance(record, dict):
        return "record must be an object"
    unknown = sorted((name for name in record if name not in RECORD_FIELDS), key=str)
    if not unknown:
        return None
    hints = []
    for name in unknown[:8]:
        if name in METADATA_FIELDS:
            hints.append(f"'{name}' belongs in record.metadata.{name}")
        elif name == "version":
            hints.append("'version' is read back, not written; an update sends the one it read as "
                         "record.expected_version")
        else:
            hints.append(f"'{_shown(name)}' is not a record field")
    more = f" (and {len(unknown) - 8} more)" if len(unknown) > 8 else ""
    return ("Nothing was saved: " + "; ".join(hints) + more + ". A record has id, kind, status, content, key, "
            "metadata, supersedes and worktree; an update adds expected_version (and expected_plan_version "
            "for a plan's task).")


def external_report_source(store, project_id: str, record: dict) -> str:
    """The source of a record an external session reports without one; written inside its write."""
    content = record.get("content")
    text = content if isinstance(content, str) and content.strip() else json_text(record)
    # store.snapshot adds the session's origin mark (origin_evidence: degraded) from the write it runs in.
    return store.snapshot(project_id, f"report://{uuid4()}", text, "manual", dict(REPORT_ORIGIN))["id"]
