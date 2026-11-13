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
Only a person writes one: the inspector route takes no session and the command refuses to run inside a
coding session.
"""
from __future__ import annotations

import hashlib
import json
import os

VERDICTS = {"still_valid": "still valid", "needs_update": "needs update", "not_related": "not related"}
MAX_NOTE_CHARS = 4000
PREFIX = "review://impact/"
STALE = "The code or the record changed since this list was shown; read the current list and decide again"


class StaleVerdict(ValueError):
    """The entry the person saw is no longer the entry the list shows."""


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def is_verdict_source(row):
    """Whether a source row is an impact verdict: the one test for reading verdicts and for keeping them out
    of chunks. Reviews and conflict notes share the address scheme and origin but are not verdicts."""
    metadata = row.get("metadata")
    if isinstance(metadata, (str, bytes)):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    return (str(row.get("uri", "")).startswith(PREFIX) and row.get("kind") == "manual"
            and isinstance(metadata, dict) and metadata.get("origin") == "local_user_review"
            and metadata.get("impact_verdict") is True)


def _on_disk(root, name):
    """``(marker, text or None)`` for what is at ``name`` now, read without following a link or leaving root.

    A regular file is known by a digest of all its bytes, so a file too large, binary or not UTF-8 still
    changes the fingerprint when edited; a link by its target text; "removed" only when nothing is there.
    """
    from cairnmind.impact import _new_text
    path = root / name
    try:
        if not path.parent.resolve().is_relative_to(root.resolve()):
            return "outside", None
        if path.is_symlink():
            return "link:" + _sha(os.readlink(path)), None
        if not os.path.lexists(path):
            return "removed", None
        if not path.is_file():
            return "not-a-file", None
        digest = hashlib.sha256()
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return "unreadable", None
    return "bytes:" + digest.hexdigest(), _new_text(root, name)


def fingerprints(root, changed, records, rows):
    """``{record id: fingerprint}`` from the worktree as it is now; reads files, writes nothing."""
    from cairnmind.impact import _definitions
    from cairnmind.store import _json
    seen, definitions = {}, {}

    def current(link):
        name = changed.get(link["path"], {}).get("renamed_to", link["path"])
        if name not in seen:
            seen[name] = _on_disk(root, name)
        marker, body = seen[name]
        if "symbol" not in link or body is None:
            return marker  # a file link, or a definition in a file that cannot be read as text
        if name not in definitions:
            definitions[name] = _definitions(name, body)
        found = definitions[name]
        if found is None:  # the file cannot be compared by symbol: any change of it counts
            return "file:" + marker
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
    found = {}
    for row in store.query("SELECT id,uri,kind,content,metadata,created_at FROM sources "
                           "WHERE project_id=:project AND is_current=true AND kind='manual' "
                           "AND metadata.impact_verdict=true",
                           {"project": project_id}):
        # Only the shape this module writes counts. A local writer other than this module produces no
        # review://impact/ address, but an imported memory package can carry a verdict written elsewhere,
        # as it carries review notes; such a verdict counts where its fingerprint matches.
        if not is_verdict_source(row):
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

    The impact is recomputed: a record that is not in the list is refused (LookupError), and so is one
    whose fingerprint differs from the one shown (StaleVerdict). The same verdict and note again writes
    nothing; another verdict replaces the current one under the same address and the earlier stays as a
    non-current source.
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
        raise StaleVerdict(STALE)
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


def listing(inspector, project, worktree, since, pending=False):
    """The inspector's read: what ``cairnmind impact --json`` prints, with an unknown project answered as not found."""
    from cairnmind.impact import build_impact
    project_id = inspector._project(project)["id"]
    return build_impact(inspector.store, project_id, since, worktree, pending=pending)


def resolve(inspector, project, worktree, since, record_id, fingerprint, verdict, note=None):
    """The inspector's write: a person's verdict, with an unknown project or record answered as not found."""
    from cairnmind.inspector import InspectorNotFound
    project_id = inspector._project(project)["id"]
    try:
        return record_verdict(inspector.store, project_id, record_id, fingerprint, verdict, since, worktree,
                              note)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None
