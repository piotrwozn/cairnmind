"""Mark a record confirmed before 0.11.0 as a person's when a local act covers it (#215).

Up to 0.10.0 the store did not record who confirmed a record, so after the upgrade such a record is
"confirmed before the rule" (confirmation.py). Some of them were confirmed by a person's act whose note
is still in the store. This one-time step gives those records the mark of a person, with that note as
its source, and leaves every other one as it is. It claims nothing the store cannot show:

- a review audit (`review://`, origin `local_user_review`: the inspector's confirm, a conflict decision
  that chose the record, a plan confirmation) counts when the hashes it holds equal the record as it
  stands (the text; kind, key, replaced record and quoted passage when it hashes them; a task's
  definition) and the record still says, by `said`, what the version that act wrote said. An audit of
  0.10.0 hashes only the text, so the revision is what covers its key, quote and other fields;
- a workspace form note counts when the form saved the record as confirmed (its first revision) and the
  record still says what the form saved;
- only a note written on this machine counts: not one that arrived in a memory package (the import's
  receipt lists the sources it brought, memory_paths.imported_source_ids), and never a published stub.

A command line note hashes nothing, so its record stays before the rule, as does a record of a version a
published import wrote. The step changes no status, version, source or evidence: it writes the mark on
the record and on the revision of its current version, and records in `devbrain_meta` that it ran, with
its counts. Running it again marks nothing new.
"""
from __future__ import annotations

import json

from cairnmind.memory.confirmation import (
    MARK,
    PERSON_ROUTES,
    confirmation_of,
    local_verdict,
    person_mark,
    said,
)
from cairnmind.memory.memory_acts import record_evidence, record_identity
from cairnmind.memory.memory_published_format import audit_covers, row_definition_hash
from cairnmind.storage.sql import clean_row, insert_document, utc_now
from cairnmind.storage.storage_query import asc, db_find, db_find_one, db_update_where, eq, is_in

META_NAME = "person_marks_before_rule"
# The route of a person's act, from the action its audit names.
_AUDIT_ROUTES = {"confirm": "inspector_review", "resolve_conflict": "conflict_decision", "confirm_plan": "confirm_plan"}


def _audit(source):
    try:
        audit = json.loads(source.get("content") or "")
    except ValueError:
        return None
    return audit if isinstance(audit, dict) else None


def _statement(row):
    return said(row.get("kind"), row.get("content"), row.get("key"), row.get("supersedes"), row.get("metadata"))


def _act_revision(tx, record, source_id):
    """The snapshot of the version a person's act wrote: the first confirmed version that cites its note."""
    for row in db_find(
        tx,
        "record_revisions",
        {"record_id": record["id"], "project_id": record["project_id"]},
        columns=("version", "snapshot"),
        order_by=(asc("version"),),
    ):
        snapshot = row.get("snapshot")
        if (isinstance(snapshot, dict) and snapshot.get("status") == "confirmed"
                and source_id in (snapshot.get("source_ids") or [])):
            return snapshot
    return None


def _review(tx, record, source):
    """(at, route) of a local review audit that covers the record as it stands, else None.

    The audit's hashes must match, and the record must still say what the version the act wrote said: its
    kind, text, key, replaced record, quote and every covered metadata field. An audit written by 0.10.0
    hashes only the text, so the revision is what shows that nothing else changed since."""
    audit = _audit(source)
    if audit is None:
        return None
    action = audit.get("action")
    route = _AUDIT_ROUTES.get(action) if isinstance(action, str) else None
    if route is None:
        return None
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    definition = row_definition_hash(record["content"], metadata) if record["kind"] == "task" else None
    if not audit_covers(audit, record["id"], record["kind"], "confirmed", record["content"], definition,
                        record_identity(record)):
        return None
    if "evidence_sha256" in audit:
        # The quoted passage the person saw: a quote added, changed or removed since ends the cover.
        hashed = audit["evidence_sha256"]
        hashed = hashed.get(record["id"]) if isinstance(hashed, dict) else hashed
        if hashed != record_evidence(record):
            return None
    revision = _act_revision(tx, record, source["id"])
    if revision is None or _statement(revision) != _statement(record):
        return None
    at = audit.get("at")
    return (at if isinstance(at, str) else str(source.get("created_at") or "")), route


def _form(tx, record, source):
    """(at, route) of the workspace form that saved the record as confirmed, if it still says the same."""
    if (record.get("source_ids") or [])[:1] != [source["id"]] or source.get("content") != record["content"]:
        return None
    if str(source.get("uri", "")) != f"workspace://finding/{record.get('key')}":
        return None
    first = db_find_one(tx, "record_revisions", {"id": f"{record['id']}:1"}, columns=("snapshot",))
    saved = (first or {}).get("snapshot")
    if not isinstance(saved, dict) or saved.get("status") != "confirmed":
        return None
    if _statement(saved) != _statement(record):
        return None
    return str(source.get("created_at") or ""), "workspace_form"


def covering_act(tx, record, sources, imported):
    """(route, source, at) of the latest local act that covers a confirmed record as it stands, else None.

    `sources` maps source IDs to rows of the record's project; `imported` holds the IDs of sources a memory
    package brought."""
    if not local_verdict(record):
        return None  # the current version is a revision a published import wrote
    review_prefix, review_origin = PERSON_ROUTES["inspector_review"]
    form_prefix, form_origin = PERSON_ROUTES["workspace_form"]
    found = []
    for source_id in record.get("source_ids") or []:
        source = sources.get(source_id) if isinstance(source_id, str) else None
        if source is None or source_id in imported or source.get("project_id") != record["project_id"]:
            continue
        uri, origin = str(source.get("uri", "")), (source.get("metadata") or {}).get("origin")
        act = None
        if uri.startswith(review_prefix) and origin == review_origin:
            act = _review(tx, record, source)
        elif uri.startswith(form_prefix) and origin == form_origin:
            act = _form(tx, record, source)
        if act:
            found.append((act[0], act[1], source))
    if not found:
        return None
    at, route, source = max(found, key=lambda item: item[0])
    return route, source, at


def report(result) -> dict | None:
    """What `cairnmind init` and `restore` print about the step; None when there was nothing to look at."""
    if not isinstance(result, dict) or not (result.get("marked") or result.get("before_rule")):
        return None
    return {"marked_as_a_persons": result["marked"], "still_before_the_rule": result["before_rule"],
            "note": "a record is marked only when a review audit or workspace form written on this computer "
                    "covers it as it stands; the others keep their status, and you confirm them in the inspector"}


def mark_confirmed_before_rule(store, *, force=False) -> dict | None:
    """Run the step once: {"marked", "before_rule", "at"}, or None when it has already run.

    Called by Store.init when this installation writes the store, and after a restore (force=True), which
    may bring records of an old store back. One transaction: the marks and the record that it ran.
    """
    from cairnmind.memory.memory_paths import imported_source_ids
    with store.transaction() as tx:
        done = db_find_one(tx, "devbrain_meta", {"name": META_NAME})
        if done and not force:
            return None
        now, marked, left = utc_now(), 0, 0
        for project in db_find(tx, "projects", columns=("id",)):
            pid = project["id"]
            records = [clean_row(row) for row in db_find(tx, "records", {"project_id": pid, "status": "confirmed"})]
            records = [row for row in records if (confirmation_of(row) or {}).get("by") == "before_rule"]
            if not records:
                continue
            imported = imported_source_ids(store, pid)
            wanted = sorted({source_id for row in records for source_id in row.get("source_ids") or []
                             if isinstance(source_id, str)})
            sources = {}
            for start in range(0, len(wanted), 500):
                for row in db_find(
                    tx,
                    "sources",
                    [is_in("id", wanted[start:start + 500]), eq("project_id", pid)],
                    columns=("id", "project_id", "uri", "metadata", "content", "created_at"),
                ):
                    sources[row["id"]] = clean_row(row)
            for record in records:
                try:
                    act = covering_act(tx, record, sources, imported)
                except (TypeError, KeyError, ValueError, AttributeError):
                    act = None  # a row the step cannot read stays before the rule, and the store still opens
                if act is None:
                    left += 1
                    continue
                route, source, at = act
                mark = {**person_mark(route, source, record["version"], at), "marked_at_upgrade": now}
                raw_meta = record.get("metadata")
                metadata = raw_meta if isinstance(raw_meta, dict) else {}
                metadata = {**metadata, MARK: mark}
                db_update_where(tx, "records", set_fields={"metadata": metadata}, where={"id": record["id"]})
                revision = db_find_one(
                    tx,
                    "record_revisions",
                    {"id": f"{record['id']}:{record['version']}"},
                    columns=("snapshot",),
                )
                snapshot = (revision or {}).get("snapshot")
                if isinstance(snapshot, dict):
                    raw_kept = snapshot.get("metadata")
                    kept = raw_kept if isinstance(raw_kept, dict) else {}
                    snapshot = {**snapshot, "metadata": {**kept, MARK: mark}}
                    db_update_where(
                        tx,
                        "record_revisions",
                        set_fields={"snapshot": snapshot},
                        where={"id": f"{record['id']}:{record['version']}"},
                    )
                marked += 1
        result = {"marked": marked, "before_rule": left, "at": now}
        if done:
            db_update_where(tx, "devbrain_meta", set_fields={"value": 1, **result}, where={"name": META_NAME})
        else:
            insert_document(tx, "devbrain_meta", {"name": META_NAME, "value": 1, **result})
    return result
