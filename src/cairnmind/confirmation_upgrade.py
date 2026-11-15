"""Mark a record confirmed before 0.11.0 as a person's when a local act covers it (#215).

Up to 0.10.0 the store did not record who confirmed a record, so after the upgrade such a record is
"confirmed before the rule" (confirmation.py). Some of them were confirmed by a person's act whose note
is still in the store. This one-time step gives those records the mark of a person, with that note as
its source, and leaves every other one as it is. It claims nothing the store cannot show:

- a review audit (`review://`, origin `local_user_review`: the inspector's confirm, a conflict decision
  that chose the record, a plan confirmation) counts when its hashes equal the record as it stands: the
  text, the kind, key and replaced record when the audit hashes them, the quoted passage when it hashes
  it, and a task's definition;
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

from .confirmation import MARK, PERSON_ROUTES, confirmation_of, local_verdict, person_mark, said
from .memory_acts import record_evidence, record_identity
from .memory_published_format import audit_covers, row_definition_hash
from .sql import clean_row, insert_document, one_row, update_document, utc_now

META_NAME = "person_marks_before_rule"
# The route of a person's act, from the action its audit names.
_AUDIT_ROUTES = {"confirm": "inspector_review", "resolve_conflict": "conflict_decision", "confirm_plan": "confirm_plan"}


def _audit(source):
    try:
        audit = json.loads(source.get("content") or "")
    except ValueError:
        return None
    return audit if isinstance(audit, dict) else None


def _review(record, source):
    """(at, route) of a local review audit that covers the record as it stands, else None."""
    audit = _audit(source)
    route = _AUDIT_ROUTES.get(audit.get("action")) if audit else None
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
    at = audit.get("at")
    return (at if isinstance(at, str) else str(source.get("created_at") or "")), route


def _form(tx, record, source):
    """(at, route) of the workspace form that saved the record as confirmed, if it still says the same."""
    if (record.get("source_ids") or [])[:1] != [source["id"]] or source.get("content") != record["content"]:
        return None
    if str(source.get("uri", "")) != f"workspace://finding/{record.get('key')}":
        return None
    first = one_row(tx, "SELECT snapshot FROM record_revisions WHERE id=:id", {"id": f"{record['id']}:1"}, False)
    saved = (first or {}).get("snapshot")
    if not isinstance(saved, dict) or saved.get("status") != "confirmed":
        return None
    statement = lambda row: said(row["kind"], row["content"], row.get("key"), row.get("supersedes"),  # noqa: E731
                                 row.get("metadata"))
    if statement(saved) != statement(record):
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
            act = _review(record, source)
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
    from .memory_paths import imported_source_ids
    with store.transaction() as tx:
        done = one_row(tx, "SELECT FROM devbrain_meta WHERE name=:name", {"name": META_NAME}, False)
        if done and not force:
            return None
        now, marked, left = utc_now(), 0, 0
        for project in tx.query("SELECT id FROM projects"):
            pid = project["id"]
            records = [clean_row(row) for row in tx.query(
                "SELECT FROM records WHERE project_id=:project AND status='confirmed'", {"project": pid})]
            records = [row for row in records if (confirmation_of(row) or {}).get("by") == "before_rule"]
            if not records:
                continue
            imported = imported_source_ids(store, pid)
            wanted = sorted({source_id for row in records for source_id in row.get("source_ids") or []
                             if isinstance(source_id, str)})
            sources = {}
            for start in range(0, len(wanted), 500):
                for row in tx.query("SELECT id,project_id,uri,metadata,content,created_at FROM sources "
                                    "WHERE id IN :ids AND project_id=:project",
                                    {"ids": wanted[start:start + 500], "project": pid}):
                    sources[row["id"]] = clean_row(row)
            for record in records:
                act = covering_act(tx, record, sources, imported)
                if act is None:
                    left += 1
                    continue
                route, source, at = act
                mark = {**person_mark(route, source, record["version"], at), "marked_at_upgrade": now}
                metadata = {**record["metadata"], MARK: mark}
                update_document(tx, "records", {"metadata": metadata}, "id=:id", {"id": record["id"]})
                revision = one_row(tx, "SELECT snapshot FROM record_revisions WHERE id=:id",
                                   {"id": f"{record['id']}:{record['version']}"}, False)
                snapshot = (revision or {}).get("snapshot")
                if isinstance(snapshot, dict):
                    snapshot = {**snapshot, "metadata": {**(snapshot.get("metadata") or {}), MARK: mark}}
                    update_document(tx, "record_revisions", {"snapshot": snapshot}, "id=:id",
                                    {"id": f"{record['id']}:{record['version']}"})
                marked += 1
        result = {"marked": marked, "before_rule": left, "at": now}
        if done:
            update_document(tx, "devbrain_meta", {"value": 1, **result}, "name=:name", {"name": META_NAME})
        else:
            insert_document(tx, "devbrain_meta", {"name": META_NAME, "value": 1, **result})
    return result
