"""Pairs of findings that may contradict each other, and the user's decision about each pair.

Nothing here detects a contradiction. A pair exists because extraction stored a new
finding beside an existing one with the same key and marked it in
``metadata.possible_conflicts``. Only a person resolves a pair, in one of three ways:
one finding supersedes the other, both stay because they hold in different scopes, or
the alarm is dismissed. Every decision is written with an audit note as a source and
a new revision; no source is ever removed.
"""
from __future__ import annotations

import hashlib
from uuid import uuid4

from .store import _json, _now, _one, _utc

KINDS = ("fact", "decision")
ACTIONS = ("supersede", "different_scopes", "dismiss")


def _ids(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _pending(record, active):
    """IDs this record is still in an unresolved conflict with."""
    metadata = record.get("metadata") or {}
    if record.get("kind") not in KINDS or record.get("status") == "superseded":
        return []
    resolved = set(_ids(metadata.get("conflicts_resolved")))
    return [other for other in dict.fromkeys(_ids(metadata.get("possible_conflicts")))
            if other != record["id"] and other not in resolved and other in active
            and active[other]["kind"] in KINDS]


def _view(record, uris, states):
    metadata = record.get("metadata") or {}
    sources = [uris[source_id] for source_id in record.get("source_ids") or [] if source_id in uris]
    return {"id": record["id"], "kind": record["kind"], "status": record["status"], "version": record["version"],
            "key": record.get("key"), "content": record["content"][:2000],
            "content_truncated": len(record["content"]) > 2000, "updated_at": record.get("updated_at"),
            "evidence_earliest": metadata.get("evidence_earliest"), "evidence_latest": metadata.get("evidence_latest"),
            "evidence_basis": metadata.get("evidence_basis"),
            "scope": metadata.get("worktree") or metadata.get("root"),
            "sources": sources[:5], "sources_total": len(sources),
            "evidence_state": states.get(record["id"], {}).get("evidence_state")}


def _newer(first, second):
    """Which finding has the later evidence, and whether that comparison can be trusted.

    An observed time is only an upper bound (the moment a file was snapshotted), so the
    order is called reliable only when both times were stated by their sources.
    """
    one, two = _utc(first.get("evidence_latest")), _utc(second.get("evidence_latest"))
    if not one or not two or one == two:
        return None, "unknown"
    stated = first.get("evidence_basis") == "stated" and second.get("evidence_basis") == "stated"
    return (first["id"] if one > two else second["id"]), ("stated" if stated else "observed")


def list_conflicts(store, project_id, scoped_records):
    """Unresolved pairs whose marked finding is in scope; reads only."""
    from .evidence import evidence_states
    active = {record["id"]: record for record in store.list_records(project_id)}
    pairs = [(record, active[other]) for record in scoped_records for other in _pending(record, active)]
    involved = {record["id"]: record for pair in pairs for record in pair}
    source_ids = sorted({source_id for record in involved.values() for source_id in record.get("source_ids") or []
                         if isinstance(source_id, str)})
    uris = {row["id"]: row["uri"] for row in store.query(
        "SELECT id,uri FROM sources WHERE id IN :ids AND project_id=:project",
        {"ids": source_ids, "project": project_id})} if source_ids else {}
    states = evidence_states(store, project_id, involved.values(), quotes=False) if involved else {}
    items = []
    for candidate, existing in pairs:
        first, second = _view(candidate, uris, states), _view(existing, uris, states)
        newer, basis = _newer(first, second)
        items.append({"id": f"{candidate['id']}:{existing['id']}", "kind": "conflict", "status": "pending",
                      "content": f"{candidate['content'][:140]}  <>  {existing['content'][:140]}",
                      "candidate": first, "existing": second, "newer_evidence": newer, "newer_evidence_basis": basis})
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
        rows = {identifier: _one(tx, "SELECT FROM records WHERE id=:id AND project_id=:project",
                                 {"id": identifier, "project": project_id}, False) for identifier in (record_id, other_id)}
        candidate, existing = rows[record_id], rows[other_id]
        if not candidate or not existing:
            raise LookupError("Unknown record in requested project")
        if candidate["version"] != expected_version or existing["version"] != other_expected_version:
            raise ValueError("Conflict version conflict; read the current findings")
        if other_id not in _pending(candidate, {other_id: existing} if existing["status"] != "superseded" else {}):
            raise ValueError("These findings are not a pending conflict")
        metadata = candidate.get("metadata") or {}
        remaining = [value for value in _ids(metadata.get("possible_conflicts")) if value != other_id]
        resolved = [*_ids(metadata.get("conflicts_resolved")), other_id]
        audit = {"actor": "local_user", "action": "resolve_conflict", "decision": action, "record_id": record_id,
                 "other_record_id": other_id, "kept_record_id": keep, "reason": reason, "at": _now(),
                 "versions": {record_id: expected_version, other_id: other_expected_version},
                 "content_sha256": {identifier: hashlib.sha256(row["content"].encode()).hexdigest()
                                    for identifier, row in rows.items()}}
        # The same address and origin as a review note: it documents a decision and never
        # counts as evidence that a finding is true.
        source = store.snapshot(project_id, "review://" + str(uuid4()), _json(audit), "manual",
                                {"origin": "local_user_review", "role": "user", "record_id": record_id,
                                 "conflict_with": other_id})
        notes = {"possible_conflicts": remaining, "conflicts_resolved": resolved,
                 "conflict_decisions": [*_ids(metadata.get("conflict_decisions")), f"{other_id}:{action}"]}
        if action != "supersede":
            saved = store.remember(project_id, {"id": record_id, "expected_version": expected_version,
                                                "metadata": notes}, [*candidate["source_ids"], source["id"]])
            return {"decision": action, "record": saved, "superseded": None, "audit_source_id": source["id"]}
        winner, loser = (candidate, existing) if keep == record_id else (existing, candidate)
        update = {"id": winner["id"], "expected_version": winner["version"], "supersedes": loser["id"],
                  # Choosing which finding stands is a review of that finding.
                  "status": "confirmed"}
        if winner is candidate:
            update["metadata"] = notes
            if metadata.get("suggested_key") == existing["key"]:
                # The marked finding was stored under a suffixed key only because the key was taken.
                update["key"] = existing["key"]
        saved = store.remember(project_id, update, [*winner["source_ids"], source["id"]])
        replaced = _one(tx, "SELECT FROM records WHERE id=:id AND project_id=:project",
                        {"id": loser["id"], "project": project_id})
        return {"decision": action, "record": saved, "superseded": replaced, "audit_source_id": source["id"]}


def inbox(inspector, project, worktree=None):
    """The inspector's view: unresolved pairs whose marked finding is in the requested scope."""
    scope = inspector._scope(project, worktree)
    project_id = scope[0]["id"]
    return list_conflicts(inspector.store, project_id,
                          [row for row in inspector.store.list_records(project_id) if inspector._matches(scope, row)])


def resolve(inspector, project, record_id, other_id, action, expected_version, other_expected_version, *,
            keep=None, reason=None):
    from .inspector import InspectorNotFound
    project_id = inspector._project(project)["id"]
    try:
        return resolve_conflict(inspector.store, project_id, record_id, other_id, action, expected_version,
                                other_expected_version, keep=keep, reason=reason)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None
