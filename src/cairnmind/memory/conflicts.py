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

from cairnmind.memory.confirmation import confirmed_by
from cairnmind.storage.sql import evidence_time, json_text, parse_utc, utc_now
from cairnmind.storage.storage_query import db_find, db_find_one, eq, is_in

KINDS = ("fact", "decision")
ACTIONS = ("supersede", "different_scopes", "dismiss")
CONTENT_LIMIT = 4000  # the longest text extraction stores, so an extracted pair is shown whole


def _ids(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _recorded_on_both(tx, winner, loser):
    """Whether a supersession is recorded on both records instead of the winner's one "supersedes" link:
    the winner already replaces another record, or the loser can never be published (#239)."""
    return (bool(winner.get("supersedes")) and winner["supersedes"] != loser["id"]) or _never_published(tx, loser)


def _never_published(tx, record):
    """Whether publication always holds this record (#239), by the checks of memory_published that depend
    on the record alone: a non-canonical ID, degraded origin, or a cited source of degraded origin. A link
    to such a record would hold the person's decision back from publication too."""
    from cairnmind.memory.memory_published_format import canonical_uuid
    from cairnmind.store import degraded_origin
    if not canonical_uuid(record["id"]) or degraded_origin(record.get("metadata")):
        return True
    cited = set(_ids(record.get("source_ids")))
    for row in db_find(tx, "record_revisions", {"project_id": record["project_id"], "record_id": record["id"]},
                       columns=("snapshot",)):
        snapshot = row.get("snapshot")
        cited |= set(_ids(snapshot.get("source_ids") if isinstance(snapshot, dict) else None))
    return bool(cited) and any(degraded_origin(row.get("metadata")) for row in db_find(
        tx, "sources", [is_in("id", sorted(cited))], columns=("id", "metadata")))


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


def standing_marks(store, project_id, present, marks):
    """Each record's ``possible_conflicts`` with every replaced record resolved to the one standing for it (#241).

    ``marks[i]`` is a stored mark list (anything else is passed through) and ``present`` the IDs already at
    hand. History is read only when a mark names an ID that is not present; an ID with no standing
    record is kept as marked, so the caller can ignore it.
    """
    if all(mark in present for value in marks for mark in _ids(value)):
        return list(marks)
    _, standing = _successors(store, project_id)
    return [[standing(mark) or mark for mark in _ids(value)] if isinstance(value, list) else value
            for value in marks]


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
            "evidence_state": state.get("evidence_state"), "quote_survives": state.get("quote_survives"),
            # How it came to be confirmed (#177): the store's own mark. Names in it are a package's claim.
            "confirmed_by": confirmed_by({**record, "metadata": metadata}), "confirmation": metadata.get("confirmation")}


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
    from cairnmind.memory.evidence import evidence_states
    active, standing = _successors(store, project_id)
    pairs = [(record, active[other]) for record in scoped_records for other in _pending(record, active, standing)
             if in_scope is None or in_scope(active[other])]
    involved = {record["id"]: record for pair in pairs for record in pair}
    source_ids = sorted({source_id for record in involved.values() for source_id in record.get("source_ids") or []
                         if isinstance(source_id, str)})
    # A review or decision note is not a source of the finding itself.
    uris = {row["id"]: row["uri"] for row in db_find(
        store, "sources", [is_in("id", source_ids), eq("project_id", project_id)],
        columns=("id", "uri", ("metadata.origin", "origin")))
        if row.get("origin") not in ("local_user_review", "cli_person", "evaluation_seed")} if source_ids else {}
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
                     keep=None, reason=None, worktree=None):
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
    from cairnmind.memory.memory_acts import act_context, identity_hash, record_evidence, row_definition_hash
    context = act_context(store, project_id, worktree)  # Who decided, on which worktree's branch and commit (#170).
    with store.transaction() as tx:
        rows = {identifier: db_find_one(tx, "records", {"id": identifier, "project_id": project_id})
                for identifier in (record_id, other_id)}
        candidate, existing = rows[record_id], rows[other_id]
        if not candidate or not existing:
            raise LookupError("Unknown record in requested project")
        pair_rows = {record_id: candidate, other_id: existing}
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
        audit = {"actor": "local_user", **context, "action": "resolve_conflict", "decision": action, "record_id": record_id,
                 "other_record_id": other_id, "marked_as": marked, "kept_record_id": keep, "reason": reason, "at": utc_now(),
                 "versions": {record_id: expected_version, other_id: other_expected_version},
                 "previous_status": {record_id: candidate["status"], other_id: existing["status"]},
                 "status": "confirmed" if action == "supersede" else None,
                 "content_sha256": {identifier: hashlib.sha256(row["content"].encode()).hexdigest()
                                    for identifier, row in pair_rows.items()},
                 # The quoted passage each finding carries: a published quote must be this one (#172).
                 "evidence_sha256": {identifier: record_evidence(row) for identifier, row in pair_rows.items()}}
        if any(row["kind"] == "task" for row in pair_rows.values()):
            audit["definition_sha256"] = {identifier: row_definition_hash(row["content"], row.get("metadata"))
                                          for identifier, row in pair_rows.items() if row["kind"] == "task"}
        if action == "supersede":
            # Kind, key and replaced record of both findings as this decision leaves them (#170).
            winner, loser = (candidate, existing) if keep == record_id else (existing, candidate)
            renamed = winner is candidate and metadata.get("suggested_key") == existing["key"]
            chained = _recorded_on_both(tx, winner, loser)
            audit["identity_sha256"] = {
                winner["id"]: identity_hash(winner["kind"], existing["key"] if renamed else winner["key"],
                                            winner.get("supersedes") if chained else loser["id"]),
                loser["id"]: identity_hash(loser["kind"], loser["key"], loser.get("supersedes"))}
        # The same address and origin as a review note: it documents a decision and never
        # counts as evidence that a finding is true, nor as the time of its evidence.
        source = store.snapshot(project_id, "review://" + str(uuid4()), json_text(audit), "manual",
                                {"origin": "local_user_review", "role": "user", "record_id": record_id,
                                 "conflict_with": other_id})

        def write(update, record):
            # A person's decision (#177): the finding chosen to stand is confirmed by this act.
            with store.person_act("conflict_decision", source["id"]):
                return store.remember(project_id, update, [*record["source_ids"], source["id"]],
                                      conflict_review=True)

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
        if chained:
            # A record has one "supersedes" link. The winner keeps the one it has; this later
            # replacement is recorded on both records instead, so no part of the chain is lost.
            # The same when the loser can never be published, such as an external session's proposal or
            # one citing its source (#239): a link to it would hold the person's decision back too.
            write({"id": loser["id"], "expected_version": loser["version"], "status": "superseded",
                   "metadata": {"superseded_by": winner["id"]}}, loser)
            update["metadata"]["superseded_ids"] = [*_ids(_metadata(winner).get("superseded_ids")), loser["id"]]
        else:
            update["supersedes"] = loser["id"]
        saved = write(update, winner)
        replaced = db_find_one(tx, "records", {"id": loser["id"], "project_id": project_id}, required=True)
        return {"decision": action, "record": saved, "superseded": replaced, "audit_source_id": source["id"]}


def inbox(inspector, project, worktree=None):
    """The inspector's view: unresolved pairs with both findings in the requested scope."""
    scope = inspector._scope(project, worktree)
    project_id = scope[0]["id"]
    return list_conflicts(inspector.store, project_id,
                          [row for row in inspector.store.list_records(project_id) if inspector._matches(scope, row)],
                          lambda row: inspector._matches(scope, row))


def resolve(inspector, project, record_id, other_id, action, expected_version, other_expected_version, *,
            keep=None, reason=None, worktree=None):
    from cairnmind.terminal.inspector import InspectorNotFound
    project_id = inspector._project(project)["id"]
    try:
        return resolve_conflict(inspector.store, project_id, record_id, other_id, action, expected_version,
                                other_expected_version, keep=keep, reason=reason, worktree=worktree)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None
