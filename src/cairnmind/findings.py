"""A finding a person types in the workspace, stored with a source that says where it came from."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

# The record kinds a person writes by hand: a task belongs to a plan and a checkpoint to a session.
FINDING_KINDS = ("decision", "fact", "constraint", "question", "preference")
# A person states a finding or confirms it; disputing and superseding are reviews of an existing record.
FINDING_STATUSES = ("proposed", "confirmed")
MAX_FINDING_CHARS = 16000
ORIGIN = "workspace_form"


def finding_error(content, kind, status) -> str | None:
    """Why a finding cannot be saved, or None. The form, the broker and the service ask the same question."""
    if not isinstance(content, str) or not content.strip():
        return "Write the finding first."
    if len(content) > MAX_FINDING_CHARS:
        return "The finding is longer than 16000 characters."
    if kind not in FINDING_KINDS:
        return "Choose a kind: decision, fact, constraint, question or preference."
    if status not in FINDING_STATUSES:
        return "Choose a status: proposed or confirmed."
    return None


def save_finding(store, project_id, worktree, content, kind, status, *, now=None):
    """Snapshot the typed text as the record's only source, then store the record.

    Runs inside the caller's write transaction. The text is kept exactly as typed: it is both the
    record and its evidence, so nothing is stripped or rewritten between the two.
    """
    error = finding_error(content, kind, status)
    if error:
        raise ValueError(error)
    when = now or datetime.now(timezone.utc)
    uri = f"workspace://finding/{when.strftime('%Y%m%dT%H%M%SZ')}/{uuid4()}"
    metadata = {"role": "user", "origin": ORIGIN, "worktree": worktree, "source_timestamp": when.isoformat()}
    source = store.snapshot(project_id, uri, content, "manual", metadata)
    record = {"kind": kind, "status": status, "content": content,
              "metadata": {"origin": ORIGIN, "worktree": worktree}}
    record = store.remember(project_id, record, [source["id"]])
    return {"record": record, "source": {key: source[key] for key in ("id", "uri", "kind", "metadata")}}
