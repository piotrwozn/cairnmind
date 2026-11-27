"""A finding a person types in the workspace, stored with a source that says where it came from."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from cairnmind.storage.storage_query import db_find

# The record kinds a person writes by hand: a task belongs to a plan and a checkpoint to a session.
FINDING_KINDS = ("decision", "fact", "constraint", "question", "preference")
# A person states a finding or confirms it; disputing and superseding are reviews of an existing record.
FINDING_STATUSES = ("proposed", "confirmed")
MAX_FINDING_CHARS = 16000
ORIGIN = "workspace_form"
# A coding session that passed the resume gate may use the same endpoint; it is not a person typing.
SESSION_ORIGIN = "session_finding"
DIFFERENT = ("This form already saved a different text, kind or status; the first one is in memory. "
             "Open a new form to save the changed one.")


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


def key_error(key) -> str | None:
    """A save key is one canonical UUID, so it can name a source URI and nothing else."""
    try:
        if isinstance(key, str) and str(UUID(key)) == key:
            return None
    except ValueError:
        pass
    return "Invalid finding key"


def save_finding(store, project_id, key, content, kind, status, *, session_id=None, now=None, worktree=None):
    """Store the typed text as the record's only source, then the record; once per key.

    Runs inside the caller's write transaction, which holds the store's writer lock, so a second
    request with the same key sees the first one's rows. The earlier write is found by the record
    whose own key is the save key, and by the source whose URI is derived from it; both are then
    returned unchanged, but only for the same request: a retry with edited text or other choices is
    refused rather than confirmed. The text is kept exactly as typed: it is both the record and its
    evidence.
    """
    error = finding_error(content, kind, status) or key_error(key)
    if error:
        raise ValueError(error)
    if session_id and status == "confirmed":
        # A session's finding is never a person's (#177). Refused before the source is written, so nothing
        # of the request is stored; the store's own rule would refuse the record as well.
        from cairnmind.memory.confirmation import ConfirmationRefused
        raise ConfirmationRefused()
    if session_id:
        uri, metadata = f"session://finding/{key}", {"origin": SESSION_ORIGIN, "session_id": session_id}
    else:
        uri, metadata = f"workspace://finding/{key}", {"origin": ORIGIN, "role": "user"}
    earlier = db_find(store, "records", {"project_id": project_id, "key": key})
    sources = db_find(store, "sources", {"project_id": project_id, "uri": uri},
                      columns=("id", "uri", "kind", "metadata", "content"))
    if earlier and sources:
        record = {name: value for name, value in earlier[0].items() if not name.startswith("@")}
        source = {name: value for name, value in sources[0].items() if not name.startswith("@")}
        # The status the first request asked for is in the record's first revision: a later review
        # (confirm or dispute) changes the record's current status, never that revision.
        first = db_find(store, "record_revisions", {"id": f"{record['id']}:1"}, columns=("snapshot",))
        asked = (first[0].get("snapshot") or {}).get("status") if first else record.get("status")
        if source.pop("content") != content or record.get("kind") != kind or asked != status:
            raise ValueError(DIFFERENT)
        return {"record": record, "source": source, "already_saved": True}
    if earlier or sources:
        # The key names a record or source that is not this kind of finding: never reuse or overwrite it.
        raise ValueError("Invalid finding key")
    # No scope-bearing key (root, worktree, worktree_root, path): a finding belongs to the whole project.
    metadata["source_timestamp"] = (now or datetime.now(timezone.utc)).isoformat()
    if not session_id:
        # Who typed it, on which branch and commit, read when it is saved: a confirmed form travels with
        # published memory (#170) under this name, never under one read at publish time.
        from cairnmind.memory.memory_acts import act_context
        metadata.update(act_context(store, project_id, worktree))
    # Indexed with its known secrets replaced (#182); the text itself is kept as typed.
    from cairnmind.memory.secret_mask import note_metadata
    source = store.snapshot(project_id, uri, content, "manual", note_metadata(store.settings, content, metadata))
    record = {"kind": kind, "status": status, "content": content, "key": key,
              "metadata": {"origin": metadata["origin"]}}
    if session_id:
        record = store.remember(project_id, record, [source["id"]])
    else:
        with store.person_act("workspace_form", source["id"]):  # The person's own form (#177).
            record = store.remember(project_id, record, [source["id"]])
    return {"record": record, "source": {name: source[name] for name in ("id", "uri", "kind", "metadata")},
            "already_saved": False}
