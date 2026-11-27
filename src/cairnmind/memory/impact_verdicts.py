"""A person's verdict on one record listed by ``cairnmind impact``: still valid, needs update, not related.

A verdict is an audit note, stored the way a review or a conflict decision stores its own: a ``review://``
source of kind ``manual`` with the origin ``local_user_review``. It is not cited by the record, so the
record, its revisions, its status and a task's state stay exactly as they were, and it is never evidence
(``evidence._is_audit``). Unlike those notes it is never chunked or embedded either (``is_verdict_source``):
it judges a list, it says nothing an answer should quote. It does travel with the project memory.

A verdict holds for the entry as it was listed: the record's content, its links to the changed code and
the current worktree content of that code (the linked definition, or the file). Editing either makes the
entry unreviewed again. The links depend on the commit the list is compared with, so against a base that
links the record to other changes the entry is unreviewed too: that list asks a different question.
Only a person writes one: the inspector route needs the open workspace's key (person_proof, #306), and
``record_verdict`` itself refuses without that proof.
"""
from __future__ import annotations

from cairnmind.memory.impact_core import (
    CLI_REFUSED as CLI_REFUSED,
    MAX_NOTE_CHARS as MAX_NOTE_CHARS,
    PREFIX as PREFIX,
    STALE as STALE,
    VERDICTS as VERDICTS,
    StaleVerdict as StaleVerdict,
    _on_disk as _on_disk,
    _sha as _sha,
    _uri as _uri,
    annotate as annotate,
    current_verdicts as current_verdicts,
    fingerprints as fingerprints,
    is_verdict_source as is_verdict_source,
    verdict_line as verdict_line,
)


def record_verdict(store, project_id, record_id, fingerprint, verdict, since, worktree=None, note=None):
    """Store a person's verdict on one listed record, if the entry is still what the person saw.

    The impact is recomputed: a record that is not in the list is refused (LookupError), and so is one
    whose fingerprint differs from the one shown (StaleVerdict). The same verdict and note again writes
    nothing; another verdict replaces the current one under the same address and the earlier stays as a
    non-current source.
    """
    from cairnmind.memory.confirmation import ConfirmationRefused
    from cairnmind.memory.impact import build_impact
    from cairnmind.terminal.person_proof import PROOF_MISSING, present
    from cairnmind.storage.sql import json_text, utc_now
    if verdict not in VERDICTS:
        raise ValueError("The verdict must be still_valid, needs_update or not_related")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE_CHARS):
        raise ValueError(f"The note must be text of at most {MAX_NOTE_CHARS} characters")
    note = (note or "").strip()
    if verdict == "needs_update" and not note:
        raise ValueError("Saying that a record needs an update requires a note on what to update")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("The verdict requires the fingerprint of the entry that was shown")
    report = build_impact(store, project_id, since, worktree)
    row = next((item for item in report["records"] if item["id"] == record_id), None)
    if row is None:
        raise LookupError("This record is not in the impact list of that project and commit")
    if row["fingerprint"] != fingerprint:
        raise StaleVerdict(STALE)
    project_id, uri = report["project"]["id"], _uri(record_id, fingerprint)
    document = {"actor": "local_user", "action": "impact_verdict", "verdict": verdict, "record_id": record_id,
                "fingerprint": fingerprint, "note": note, "at": utc_now(), "since": since,
                "commit": report["commit"]}
    metadata = {"origin": "local_user_review", "role": "user", "impact_verdict": True, "record_id": record_id,
                "fingerprint": fingerprint}
    if not present():
        raise ConfirmationRefused(PROOF_MISSING)  # a person's act needs the workspace's key (#306)
    with store.transaction():
        earlier = current_verdicts(store, project_id).get(uri)
        said = None if earlier is None else (earlier[1].get("verdict"), earlier[1].get("note") or "")
        if said == (verdict, note):
            return {"record_id": record_id, "fingerprint": fingerprint, "verdict": earlier[1],
                    "audit_source_id": earlier[0]["id"], "already_saved": True}
        source = store.snapshot(project_id, uri, json_text(document), "manual", metadata)
    return {"record_id": record_id, "fingerprint": fingerprint, "verdict": document,
            "audit_source_id": source["id"], "already_saved": False}


def listing(inspector, project, worktree, since, pending=False):
    """The inspector's read: what ``cairnmind impact --json`` prints, with an unknown project answered as not found."""
    from cairnmind.memory.impact import build_impact
    project_id = inspector._project(project)["id"]
    return build_impact(inspector.store, project_id, since, worktree, pending=pending)


def resolve(inspector, project, worktree, since, record_id, fingerprint, verdict, note=None):
    """The inspector's write: a person's verdict, with an unknown project or record answered as not found."""
    from cairnmind.terminal.inspector import InspectorNotFound
    project_id = inspector._project(project)["id"]
    try:
        return record_verdict(inspector.store, project_id, record_id, fingerprint, verdict, since, worktree,
                              note)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None
