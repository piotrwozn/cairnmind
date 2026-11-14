"""Pairs of findings that may contradict each other, and the user's decision about each pair.

Nothing here detects a contradiction. A pair exists because extraction stored a new
finding beside an existing one with the same key and marked it in
``metadata.possible_conflicts``. Only a person resolves a pair, in one of three ways:
one finding supersedes the other, both stay because they hold in different scopes, or
the alarm is dismissed. Every decision is written with an audit note as a source and
a new revision; no source is ever removed, and the bookkeeping fields can only be
written here (the store drops them from any other write).
"""
from __future__ import annotations

import hashlib
from uuid import uuid4

from .sql import evidence_time, json_text, one_row, parse_utc, utc_now

KINDS = ("fact", "decision")
ACTIONS = ("supersede", "different_scopes", "dismiss")
CONTENT_LIMIT = 4000  # the longest text extraction stores, so an extracted pair is shown whole


def _ids(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _metadata(record):
    value = record.get("metadata")
    return value if isinstance(value, dict) else {}


def _successors(store, project_id):
    """Active records by ID, and a function from any record ID to the active record that stands for it.

    A marked counterpart may have been replaced since, here or by an ordinary write. The
    possible contradiction is then with whatever replaced it, not gone.
    """
    rows = store.list_records(project_id, include_history=True)
    active = {row["id"]: row for row in rows if row["status"] != "superseded"}
    forward = {}
    for row in rows:
        later = _metadata(row).get("superseded_by")
        if row["status"] == "superseded" and isinstance(later, str):
            forward[row["id"]] = later
    for row in rows:
        if isinstance(row.get("supersedes"), str):
            forward.setdefault(row["supersedes"], row["id"])

    def standing(identifier):
        seen = set()
        while identifier not in active and identifier in forward and identifier not in seen:
            seen.add(identifier)
            identifier = forward[identifier]
        return identifier if identifier in active else None

    return active, standing


def _pending(record, active, standing):
    """Unresolved pairs of this record: ``{standing counterpart ID: [IDs as they were marked]}``."""
    metadata = _metadata(record)
    if record.get("kind") not in KINDS or record.get("status") == "superseded":
        return {}
    resolved = set(_ids(metadata.get("conflicts_resolved")))
    result = {}
    for marked in dict.fromkeys(_ids(metadata.get("possible_conflicts"))):
        other = standing(marked)
        if (other is None or other == record["id"] or marked in resolved or other in resolved
                or active[other]["kind"] not in KINDS):
            continue
        result.setdefault(other, []).append(marked)
    return result


def _view(record, uris, states, times):
    metadata = _metadata(record)
    sources = [uris[source_id] for source_id in record.get("source_ids") or [] if source_id in uris]
    state, when = states.get(record["id"], {}), times.get(record["id"], {})
    return {"id": record["id"], "kind": record["kind"], "status": record["status"], "version": record["version"],
            "key": record.get("key"), "content": record["content"][:CONTENT_LIMIT],
            "content_truncated": len(record["content"]) > CONTENT_LIMIT, "updated_at": record.get("updated_at"),
            "evidence_earliest": when.get("earliest"), "evidence_latest": when.get("latest"),
            "evidence_basis": when.get("basis"),
            "scope": metadata.get("worktree") or metadata.get("root"),
            "sources": sources[:5], "sources_total": len(sources),
            "evidence_state": state.get("evidence_state"), "quote_survives": state.get("quote_survives")}


def _newer(first, second):
    """Which finding has the later evidence, and whether that order can be trusted.

    An observed time is only an upper bound (the moment a file was snapshotted): the
    evidence may be older. The order is therefore reliable exactly when the later of
    the two times was stated by its source.
    """
    one, two = parse_utc(first.get("evidence_latest")), parse_utc(second.get("evidence_latest"))
    if not one or not two or one == two:
        return None, "unknown"
    later = first if one > two else second
    return later["id"], ("stated" if later.get("evidence_basis") == "stated" else "observed")


def list_conflicts(store, project_id, scoped_records, in_scope=None):
    """Unresolved pairs with both findings in scope; reads only."""
    from .evidence import evidence_states
    active, standing = _successors(store, project_id)
    pairs = [(record, active[other]) for record in scoped_records for other in _pending(record, active, standing)
             if in_scope is None or in_scope(active[other])]
    involved = {record["id"]: record for pair in pairs for record in pair}
    source_ids = sorted({source_id for record in involved.values() for source_id in record.get("source_ids") or []
                         if isinstance(source_id, str)})
    # A review or decision note is not a source of the finding itself.
    uris = {row["id"]: row["uri"] for row in store.query(
        "SELECT id,uri,metadata.origin AS origin FROM sources WHERE id IN :ids AND project_id=:project",
        {"ids": source_ids, "project": project_id}) if row.get("origin") != "local_user_review"} if source_ids else {}
    states = evidence_states(store, project_id, involved.values()) if involved else {}
    with store.transaction() as tx:
        # Read from the sources now: a time stored on a record before audit notes were
        # left out of it may still be the time of a review.
        times = {identifier: evidence_time(tx, record.get("source_ids") or []) for identifier, record in involved.items()}
    items = []
    for candidate, existing in pairs:
        first, second = _view(candidate, uris, states, times), _view(existing, uris, states, times)
        newer, basis = _newer(first, second)
        items.append({"id": f"{candidate['id']}:{existing['id']}", "kind": "conflict", "status": "pending",
                      "content": f"{candidate['content'][:140]}  <>  {existing['content'][:140]}",
                      "candidate": first, "existing": second, "newer_evidence": newer, "newer_evidence_basis": basis,
                      # The store replaces a finding only by one of the same kind.
                      "can_supersede": candidate["kind"] == existing["kind"]})
    items.sort(key=lambda item: item["id"])
    return {"items": items, "total": len(items), "next_cursor": None}


def resolve_conflict(store, project_id, record_id, other_id, action, expected_version, other_expected_version, *,
                     keep=None, reason=None):
    """Apply one user decision to one pending pair, atomically and with an audit source."""
    if action not in ACTIONS:
        raise ValueError("Conflict action must be supersede, different_scopes or dismiss")
    for version in (expected_version, other_expected_version):
        if type(version) is not int or version < 1:
            raise ValueError("Resolving a conflict requires the exact version of both records")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4000):
        raise ValueError("Conflict reason must be at most 4000 characters")
    reason = (reason or "").strip()
    if action == "different_scopes" and not reason:
        raise ValueError("Keeping both findings requires a description of the two scopes")
    if action == "supersede" and keep not in (record_id, other_id):
        raise ValueError("Superseding requires the ID of the finding to keep")
    if action != "supersede" and keep is not None:
        raise ValueError("Only superseding takes a finding to keep")
    with store.transaction() as tx:
        rows = {identifier: one_row(tx, "SELECT FROM records WHERE id=:id AND project_id=:project",
                                 {"id": identifier, "project": project_id}, False) for identifier in (record_id, other_id)}
        candidate, existing = rows[record_id], rows[other_id]
        if not candidate or not existing:
            raise LookupError("Unknown record in requested project")
        if candidate["version"] != expected_version or existing["version"] != other_expected_version:
            raise ValueError("Conflict version conflict; read the current findings")
        active, standing = _successors(store, project_id)
        marked = _pending(candidate, active, standing).get(other_id)
        if not marked:
            raise ValueError("These findings are not a pending conflict")
        if action == "supersede" and candidate["kind"] != existing["kind"]:
            raise ValueError("A fact and a decision cannot replace each other; keep both or dismiss the alarm")
        metadata = _metadata(candidate)
        notes = {"possible_conflicts": [value for value in _ids(metadata.get("possible_conflicts")) if value not in marked],
                 "conflicts_resolved": list(dict.fromkeys([*_ids(metadata.get("conflicts_resolved")), *marked, other_id])),
                 "conflict_decisions": [*_ids(metadata.get("conflict_decisions")), f"{other_id}:{action}"]}
        if action == "different_scopes":
            notes["conflict_scope_note"] = reason[:500]
        audit = {"actor": "local_user", "action": "resolve_conflict", "decision": action, "record_id": record_id,
                 "other_record_id": other_id, "marked_as": marked, "kept_record_id": keep, "reason": reason, "at": utc_now(),
                 "versions": {record_id: expected_version, other_id: other_expected_version},
                 "previous_status": {record_id: candidate["status"], other_id: existing["status"]},
                 "status": "confirmed" if action == "supersede" else None,
                 "content_sha256": {identifier: hashlib.sha256(row["content"].encode()).hexdigest()
                                    for identifier, row in rows.items()}}
        # The same address and origin as a review note: it documents a decision and never
        # counts as evidence that a finding is true, nor as the time of its evidence.
        source = store.snapshot(project_id, "review://" + str(uuid4()), json_text(audit), "manual",
                                {"origin": "local_user_review", "role": "user", "record_id": record_id,
                                 "conflict_with": other_id})

        def write(update, record):
            return store.remember(project_id, update, [*record["source_ids"], source["id"]], conflict_review=True)

        if action != "supersede":
            saved = write({"id": record_id, "expected_version": expected_version, "metadata": notes}, candidate)
            return {"decision": action, "record": saved, "superseded": None, "audit_source_id": source["id"]}
        winner, loser = (candidate, existing) if keep == record_id else (existing, candidate)
        # Choosing which finding stands is a review of that finding, so it becomes confirmed.
        update = {"id": winner["id"], "expected_version": winner["version"], "status": "confirmed",
                  "metadata": dict(notes) if winner is candidate else {}}
        if winner is candidate and metadata.get("suggested_key") == existing["key"]:
            # The marked finding was stored under a suffixed key only because the key was taken.
            update["key"] = existing["key"]
        if winner.get("supersedes") and winner["supersedes"] != loser["id"]:
            # A record has one "supersedes" link. The winner keeps the one it has; this later
            # replacement is recorded on both records instead, so no part of the chain is lost.
            write({"id": loser["id"], "expected_version": loser["version"], "status": "superseded",
                   "metadata": {"superseded_by": winner["id"]}}, loser)
            update["metadata"]["superseded_ids"] = [*_ids(_metadata(winner).get("superseded_ids")), loser["id"]]
        else:
            update["supersedes"] = loser["id"]
        saved = write(update, winner)
        replaced = one_row(tx, "SELECT FROM records WHERE id=:id AND project_id=:project",
                        {"id": loser["id"], "project": project_id})
        return {"decision": action, "record": saved, "superseded": replaced, "audit_source_id": source["id"]}


def inbox(inspector, project, worktree=None):
    """The inspector's view: unresolved pairs with both findings in the requested scope."""
    scope = inspector._scope(project, worktree)
    project_id = scope[0]["id"]
    return list_conflicts(inspector.store, project_id,
                          [row for row in inspector.store.list_records(project_id) if inspector._matches(scope, row)],
                          lambda row: inspector._matches(scope, row))


def resolve(inspector, project, record_id, other_id, action, expected_version, other_expected_version, *,
            keep=None, reason=None):
    from .inspector import InspectorNotFound
    project_id = inspector._project(project)["id"]
    try:
        return resolve_conflict(inspector.store, project_id, record_id, other_id, action, expected_version,
                                other_expected_version, keep=keep, reason=reason)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None
