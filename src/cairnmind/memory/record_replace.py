"""A person replaces one record with a new text in the inspector (#222).

One act in one transaction, as the inspector's review is: the typed text is stored as a note (the shape of a
workspace form note), a review audit names the new record and what it replaces, and the new record is stored as
confirmed by that act. The replaced record becomes superseded through the store's own path; when it can never be
published, on both records as a conflict decision does (#239). Nothing of the old record is rewritten, and its
sources stay with it: they never contained the new text.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from cairnmind.memory.findings import FINDING_KINDS, finding_error
from cairnmind.storage.sql import json_text, utc_now
from cairnmind.storage.storage_query import db_find_one

SCOPE_METADATA = ("root", "worktree", "worktree_root", "path")  # where the record holds, kept on the new one
HISTORICAL = "A historical version cannot be replaced. Nothing was saved. Open the record that replaced it."
CHANGED = ("Replace version conflict: the record changed since it was shown. Nothing was saved. Open it again and "
           "read the current version.")
NOT_REPLACEABLE = ("Only a decision, fact, constraint, question or preference is replaced here: a task changes with "
                   "its plan, and the checkpoint with the next checkpoint. Nothing was saved.")
SAME_TEXT = ("The new text is the same as the current text. Nothing was saved. To keep this text, confirm the "
             "record instead.")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def replace(inspector, project, record_id, content, expected_version, *, reason=None, worktree=None) -> dict:
    """The inspector route's call: replace_record for a project the inspector names."""
    from cairnmind.terminal.inspector import InspectorNotFound
    try:
        return replace_record(inspector.store, inspector._project(project)["id"], record_id, content,
                              expected_version, reason=reason, worktree=worktree)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None


def replace_record(store, project_id, record_id, content, expected_version, *, reason=None, worktree=None) -> dict:
    """{"record": new, "replaced": old as it is now, "audit_source_id", "note_source_id"}; refuses without the
    workspace's key, as adopt_group does, so no other caller writes a note saying a person replaced a record."""
    from cairnmind.memory.confirmation import ConfirmationRefused
    from cairnmind.terminal.person_proof import PROOF_MISSING, present
    if not present():
        raise ConfirmationRefused(PROOF_MISSING)
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("Replacing a record requires the exact version that was shown")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4000):
        raise ValueError("Review reason must be at most 4000 characters")
    from cairnmind.memory.conflicts import _ids, _never_published
    from cairnmind.memory.memory_acts import act_context
    from cairnmind.memory.memory_published_format import identity_hash
    from cairnmind.memory.secret_mask import note_metadata
    context = act_context(store, project_id, worktree)  # before the writer lock, as a review does
    where = {"id": record_id, "project_id": project_id}
    with store.transaction() as tx:
        old = db_find_one(tx, "records", where)
        if not old:
            raise LookupError("Unknown record in requested project")
        if old["status"] == "superseded":
            raise ValueError(HISTORICAL)
        if old["version"] != expected_version:
            raise ValueError(CHANGED)
        if old["kind"] not in FINDING_KINDS:
            raise ValueError(NOT_REPLACEABLE)
        error = finding_error(content, old["kind"], "confirmed")
        if error:
            raise ValueError(error)
        if content.strip() == old["content"].strip():
            raise ValueError(SAME_TEXT)
        chained = _never_published(tx, old)
        new_id = str(uuid4())
        metadata = old.get("metadata") or {}
        kept = {name: metadata[name] for name in SCOPE_METADATA if name in metadata}
        resolved = set(_ids(metadata.get("conflicts_resolved")))
        pending = [mark for mark in _ids(metadata.get("possible_conflicts")) if mark not in resolved]
        if pending:
            kept["possible_conflicts"] = pending  # a pending alarm does not vanish quietly
        facts = {"origin": "workspace_form", "role": "user",
                 "source_timestamp": datetime.now(UTC).isoformat(), **context}
        note = store.snapshot(project_id, f"workspace://finding/{new_id}", content, "manual",
                              note_metadata(store.settings, content, facts))
        audit = {"actor": "local_user", **context, "action": "confirm", "record_id": new_id, "replaces": record_id,
                 "replaced_version": expected_version, "replaced_status": old["status"],
                 "replaced_content_sha256": _sha(old["content"]), "status": "confirmed",
                 "reason": (reason or "").strip(), "at": utc_now(), "content_sha256": _sha(content),
                 "identity_sha256": identity_hash(old["kind"], old["key"], None if chained else record_id),
                 "evidence_sha256": None}
        source = store.snapshot(project_id, "review://" + str(uuid4()), json_text(audit), "manual",
                                {"origin": "local_user_review", "role": "user", "record_id": new_id,
                                 "replaces": record_id})
        new = {"kind": old["kind"], "key": old["key"], "content": content, "status": "confirmed"}
        with store.person_act("inspector_review", source["id"]):  # The person's act (#177).
            if chained:
                # The replaced record can never be published: a link to it would hold the person's text back too (#239).
                store._remember(tx, project_id, {"id": record_id, "expected_version": expected_version,
                                                 "status": "superseded", "metadata": {"superseded_by": new_id}},
                                [*old["source_ids"], source["id"]], conflict_review=True)
                saved = store._remember(tx, project_id, {**new, "metadata": {**kept, "superseded_ids": [record_id]}},
                                        [note["id"], source["id"]], conflict_review=True, new_id=new_id)
            else:
                saved = store._remember(tx, project_id, {**new, "metadata": kept, "supersedes": record_id,
                                                         "expected_version": expected_version},
                                        [note["id"], source["id"]], new_id=new_id)
        replaced = db_find_one(tx, "records", where, required=True)
        return {"record": saved, "replaced": replaced, "audit_source_id": source["id"], "note_source_id": note["id"]}
