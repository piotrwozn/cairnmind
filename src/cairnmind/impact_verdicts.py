"""A person's verdict on one record listed by ``cairnmind impact``: still valid, needs update, not related.

A verdict is an audit note, stored the way a review or a conflict decision stores its own: a ``review://``
source of kind ``manual`` with the origin ``local_user_review``. It is not cited by the record, so the
record, its revisions, its status and a task's state stay exactly as they were, and it is never evidence
(``evidence._is_audit``).

A verdict holds for one fingerprint of the listed entry: the record's content and, for every link, the
current worktree text of the linked definition or file. Editing either changes the fingerprint and the
entry is unreviewed again. The commit compared with is not part of it, so a verdict stays valid whatever
base the list was made from. Only a person writes one: the inspector route takes no session and the
command refuses to run inside a coding session.
"""
from __future__ import annotations

import hashlib
import json

VERDICTS = {"still_valid": "still valid", "needs_update": "needs update", "not_related": "not related"}
MAX_NOTE_CHARS = 4000
PREFIX = "review://impact/"
STALE = ("Impact conflict: the code or the record changed since this list was shown; "
         "read the current list and decide again")


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def fingerprints(root, changed, records, rows):
    """``{record id: fingerprint}`` from the worktree as it is now; reads files, writes nothing."""
    from cairnmind.impact import _definitions, _new_text
    from cairnmind.store import _json
    texts, definitions = {}, {}

    def text(name):
        if name not in texts:
            texts[name] = _new_text(root, name)
        return texts[name]

    def current(link):
        name = changed.get(link["path"], {}).get("renamed_to", link["path"])
        body = text(name)
        if body is None:
            return "removed"
        if "symbol" not in link:
            return _sha(body)
        if name not in definitions:
            definitions[name] = _definitions(name, body)
        found = definitions[name]
        if found is None:  # the file cannot be compared by symbol: any change of it counts
            return "file:" + _sha(body)
        return _sha(_json(found[link["symbol"]])) if link["symbol"] in found else "removed"

    result = {}
    for row in rows:
        parts = [_sha(str(records[row["id"]].get("content", "")))]
        parts += sorted([link["via"], link["path"], link.get("symbol") or "", current(link)]
                        for link in row["links"])
        result[row["id"]] = _sha(_json(parts))
    return result


def _uri(record_id, fingerprint):
    return f"{PREFIX}{record_id}/{fingerprint[:32]}"


def current_verdicts(store, project_id):
    """``{uri: (source, verdict document)}`` for the project's current verdicts, in one query."""
    from cairnmind.evidence import _metadata
    found = {}
    for row in store.query("SELECT id,uri,kind,content,metadata,created_at FROM sources "
                           "WHERE project_id=:project AND is_current=true AND kind='manual' "
                           "AND metadata.impact_verdict=true",
                           {"project": project_id}):
        metadata = _metadata(row)
        # Only the shape this module writes counts; no other writer produces a review:// address.
        if (not str(row.get("uri", "")).startswith(PREFIX) or metadata.get("origin") != "local_user_review"
                or metadata.get("impact_verdict") is not True):
            continue
        try:
            document = json.loads(row["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(document, dict) and document.get("verdict") in VERDICTS:
            found[row["uri"]] = (row, document)
    return found


def annotate(store, project_id, root, changed, records, rows):
    """Add ``fingerprint`` and ``verdict`` (None, or the verdict with its time and note) to each row."""
    prints = fingerprints(root, changed, records, rows)
    saved = current_verdicts(store, project_id) if rows else {}
    for row in rows:
        row["fingerprint"] = prints[row["id"]]
        source, document = saved.get(_uri(row["id"], row["fingerprint"]), (None, None))
        matches = (document is not None and document.get("record_id") == row["id"]
                   and document.get("fingerprint") == row["fingerprint"])
        row["verdict"] = None if not matches else {
            "verdict": document["verdict"], "at": document.get("at"), "note": document.get("note") or "",
            "source_id": source["id"]}


def verdict_line(row, line):
    """The short line under a reviewed entry, or None."""
    verdict = row.get("verdict")
    if not verdict:
        return None
    note = f": {line(verdict['note'], 120)}" if verdict.get("note") else ""
    return f"  checked: {VERDICTS[verdict['verdict']]} ({line(str(verdict.get('at') or '')[:10], 10)}){note}"


def record_verdict(store, project_id, record_id, fingerprint, verdict, since, worktree=None, note=None):
    """Store a person's verdict on one listed record, if the entry is still what the person saw.

    The impact is recomputed: a record that is not in the list, or whose fingerprint differs from the one
    shown, is refused. The same verdict and note again writes nothing; another verdict replaces the current
    one under the same address and the earlier stays as a non-current source.
    """
    from cairnmind.impact import build_impact
    from cairnmind.store import _json, _now
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
        raise ValueError(STALE)
    project_id, uri = report["project"]["id"], _uri(record_id, fingerprint)
    document = {"actor": "local_user", "action": "impact_verdict", "verdict": verdict, "record_id": record_id,
                "fingerprint": fingerprint, "note": note, "at": _now(), "since": since,
                "commit": report["commit"]}
    metadata = {"origin": "local_user_review", "role": "user", "impact_verdict": True, "record_id": record_id,
                "fingerprint": fingerprint}
    with store.transaction():
        earlier = current_verdicts(store, project_id).get(uri)
        # The time differs on every save, so an unchanged verdict is recognised by what it says.
        said = None if earlier is None else (earlier[1].get("verdict"), earlier[1].get("note") or "")
        if said == (verdict, note):
            return {"record_id": record_id, "fingerprint": fingerprint, "verdict": earlier[1],
                    "audit_source_id": earlier[0]["id"], "already_saved": True}
        source = store.snapshot(project_id, uri, _json(document), "manual", metadata)
    return {"record_id": record_id, "fingerprint": fingerprint, "verdict": document,
            "audit_source_id": source["id"], "already_saved": False}


def resolve(inspector, project, worktree, since, record_id, fingerprint, verdict, note=None):
    """The inspector's write: a person's verdict, with an unknown project or record answered as not found."""
    from cairnmind.inspector import InspectorNotFound
    project_id = inspector._project(project)["id"]
    try:
        return record_verdict(inspector.store, project_id, record_id, fingerprint, verdict, since, worktree,
                              note)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None
