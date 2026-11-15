"""A transcript record kind that a person accepted as technical for one project (#210).

A record of a kind the transcript reader does not know keeps every resume packet of its project partial
(#155). A person can look at such a record (its kind, client, version and its content, escaped and cut)
and accept its kind as technical for that one project; packets of that project are then ready again
without a new release. The acceptance is kept on the project's own row with the date, a note and who
accepted it, `doctor` and `record-kind list` show it, and it can be withdrawn.

What an acceptance never covers: a record of a kind the reader knows that is malformed (a `mode` record
with a number in it), a record that is not a JSON object, a record whose session differs from the one it
was stored under, and every integrity check of the captured transcript (hashes, offsets, rotation, the
import cursor, capture recovery provenance). Those keep blocking as before.

Only a person's act stores or withdraws an acceptance, as only a person confirms a record (#177): the
command line in a person's own terminal, or the inspector routes the workspace calls. A coding client, the
built-in agent, an MCP call and an imported package have no way to write it: the store checks the open act
and the note its route wrote, as it does for a confirmation.
"""
from __future__ import annotations

import json
import re

from .confirmation import PERSON_ROUTES, person_mark, person_source
from .ingest import CLAUDE_TECHNICAL_RECORDS, CODEX_TECHNICAL_RECORDS
from .sql import one_row, update_document, utc_now
from .terminal_safe import safe_text

FIELD = "accepted_record_kinds"
WITHDRAWN_FIELD = "withdrawn_record_kinds"
CLIENTS = ("claude", "codex", "unknown")
KIND_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}")
# The record types ingest._event_text names in its own branches, besides its technical record sets.
_INTERPRETED_KINDS = {"user", "assistant", "progress", "system", "summary", "file-history-snapshot",
                      "queue-operation", "session_meta", "turn_context", "event_msg", "response_item"}
# Every record type the reader interprets itself: a malformed one is never an unknown kind. Built from the
# reader's own technical sets, so a technical record a later release adds is known here at once.
KNOWN_KINDS = frozenset(_INTERPRETED_KINDS | CLAUDE_TECHNICAL_RECORDS | CODEX_TECHNICAL_RECORDS)
MAX_NOTE = 1000
MAX_ACCEPTED = 64
MAX_WITHDRAWN = 20
EXCERPT = 300
LIST_LIMIT = 20000
_PAGE = 1000

REFUSED = ("Only a person accepts an unknown transcript record kind, and this is not a person's act. Nothing was "
           "stored. The person runs `cairnmind record-kind accept` in their own terminal, or accepts it in the "
           "workspace under More, then Transcript record kinds.")
CLI_REFUSED = ("Only a person accepts or withdraws an unknown transcript record kind. This command runs inside a "
               "coding session, or its input or output is not a terminal (piped or redirected), so it does not count "
               "as a person. Nothing was stored. The person runs it in their own terminal, with input and output not "
               "piped or redirected, or uses More, then Transcript record kinds in the workspace.")


class AcceptanceRefused(ValueError):
    """A write that is not a person's act tried to accept or withdraw a record kind."""

    def __init__(self, message=REFUSED):
        super().__init__(message)


def _version(event) -> str | None:
    value = event.get("version")
    if not isinstance(value, str) and isinstance(event.get("payload"), dict):
        value = event["payload"].get("cli_version")
    return value if isinstance(value, str) and _VERSION_PATTERN.fullmatch(value) else None


def _first_version(first_text) -> str | None:
    """The client version a transcript's first record names (a Codex rollout's session_meta)."""
    try:
        event = json.loads(first_text) if isinstance(first_text, str) else None
    except ValueError:
        return None
    return _version(event) if isinstance(event, dict) else None


def unknown_kind(source, text, first_text=None) -> dict | None:
    """The unknown kind of one archived transcript record, or None when it is anything else.

    `source` is the stored row (kind and metadata), `text` the record's raw line, already proved against
    the row's hash by the caller. Only a record the reader stored as an unknown schema can have one; a
    record a reader of this release interprets, or one of a kind it knows, never does.
    """
    from .ingest import UNKNOWN_TRANSCRIPT_WARNING, _event_text
    metadata = source.get("metadata") or {}
    if source.get("kind") != "transcript_unknown" or metadata.get("schema_warning") != UNKNOWN_TRANSCRIPT_WARNING:
        return None
    try:
        event = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if not isinstance(kind, str) or not KIND_PATTERN.fullmatch(kind) or kind in KNOWN_KINDS:
        return None
    if _event_text(event, "auto")[3]:
        return None
    session = event.get("sessionId")
    if session is not None and session != metadata.get("session_id"):
        return None
    client = metadata.get("client")
    if client not in ("claude", "codex"):
        client = ("claude" if isinstance(session, str) else "codex" if isinstance(event.get("payload"), dict)
                  else "unknown")
    return {"client": client, "kind": kind, "version": _version(event) or _first_version(first_text),
            "excerpt": safe_text(json.dumps(event, ensure_ascii=True), limit=EXCERPT)}


def blocking_message(found, offset, project_id) -> str:
    """The unknown a packet carries for a kind nobody accepted: which kind, and how a person accepts it."""
    version = found["version"] or "version unknown"
    return (f"Unknown transcript record kind '{found['kind']}' from {found['client']} {version} at byte {offset} "
            f"blocks this packet. A person can accept it as technical for this project: `cairnmind record-kind "
            f"accept {project_id} {found['client']} {found['kind']} --note TEXT` in their own terminal, or More, "
            "then Transcript record kinds in the workspace.")


def _entries(value, limit=MAX_ACCEPTED) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict) and entry.get("client") in CLIENTS
            and isinstance(entry.get("kind"), str) and KIND_PATTERN.fullmatch(entry["kind"])][:limit]


def _project_row(reader, project_id) -> dict:
    row = one_row(reader, f"SELECT id,name,{FIELD},{WITHDRAWN_FIELD} FROM projects WHERE id=:id",
                  {"id": project_id}, False)
    if row is None:
        raise ValueError("Unknown project")
    return row


def accepted(reader, project_id) -> set[tuple[str, str]]:
    """(client, kind) of every acceptance standing in the project."""
    return {(entry["client"], entry["kind"]) for entry in _entries(_project_row(reader, project_id).get(FIELD))}


def check(client, kind, note=None, *, withdraw=False):
    """Validate a request at the boundary, before anything is written."""
    if client not in CLIENTS:
        raise ValueError("The client must be claude, codex or unknown")
    if not isinstance(kind, str) or not KIND_PATTERN.fullmatch(kind):
        raise ValueError("A record kind is a letter followed by at most 63 letters, digits or _ . : -")
    if kind in KNOWN_KINDS:
        raise ValueError(f"'{kind}' is a kind the reader knows: a malformed record of it is never accepted")
    if not withdraw and (not isinstance(note, str) or not note.strip() or len(note) > MAX_NOTE):
        raise ValueError(f"Accepting a kind needs a note of 1 to {MAX_NOTE} characters: why it is technical")
    if withdraw and note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise ValueError(f"The note is at most {MAX_NOTE} characters")


def set_acceptance(store, project_id, client, kind, note=None, *, withdraw=False) -> dict:
    """Store or withdraw one acceptance, as the person's act open on this thread; refused otherwise."""
    check(client, kind, note, withdraw=withdraw)
    act = store.open_person_act()
    if not act or act["route"] not in PERSON_ROUTES:
        raise AcceptanceRefused()
    prefix, origin = PERSON_ROUTES[act["route"]]
    with store.transaction() as tx:
        source = one_row(tx, "SELECT id,project_id,uri,metadata,content FROM sources WHERE id=:id",
                         {"id": act["source_id"]}, False)
        if (not source or source.get("project_id") != project_id or not str(source.get("uri", "")).startswith(prefix)
                or (source.get("metadata") or {}).get("origin") != origin):
            raise AcceptanceRefused()
        row = _project_row(tx, project_id)
        entries, withdrawn = _entries(row.get(FIELD)), _entries(row.get(WITHDRAWN_FIELD), MAX_WITHDRAWN)
        at = utc_now()
        mark = person_mark(act["route"], source, None, at)
        current = next((entry for entry in entries if (entry["client"], entry["kind"]) == (client, kind)), None)
        if withdraw:
            if current is None:
                raise ValueError(f"No acceptance of kind '{kind}' from {client} stands in this project")
            entries.remove(current)
            withdrawn = [*withdrawn, {**current, "withdrawn_at": at, "withdrawn_by": mark["actor_name"],
                                      "withdrawn_route": act["route"], "withdrawn_source_id": source["id"],
                                      "withdraw_note": (note or "").strip()}][-MAX_WITHDRAWN:]
            result = {"status": "withdrawn", "acceptance": current}
        elif current is not None:
            return {"status": "already_accepted", "acceptance": current}
        else:
            if len(entries) >= MAX_ACCEPTED:
                raise ValueError(f"A project keeps at most {MAX_ACCEPTED} accepted kinds; withdraw one first")
            current = {"client": client, "kind": kind, "note": note.strip(), "accepted_at": at,
                       "accepted_by": mark["actor_name"], "route": act["route"], "source_id": source["id"]}
            entries.append(current)
            result = {"status": "accepted", "acceptance": current}
        update_document(tx, "projects", {FIELD: entries, WITHDRAWN_FIELD: withdrawn}, "id=:id", {"id": project_id})
    return {"project_id": project_id, **result}


def act_as_person(store, project_id, client, kind, note=None, *, route, withdraw=False) -> dict:
    """A person's acceptance or withdrawal: the route's note is written here and the act opened on it.

    The caller has established that a person acts: the command line by `person_at_terminal`, the inspector
    route by being the authenticated request that names no session.
    """
    check(client, kind, note, withdraw=withdraw)
    try:
        with store.transaction():
            text = note.strip() if isinstance(note, str) and note.strip() else f"withdraw {client} {kind}"
            written = person_source(store, project_id, text, route=route,
                                    action="withdraw_record_kind" if withdraw else "accept_record_kind")
            with store.person_act(route, written["id"]):
                result = set_acceptance(store, project_id, client, kind, note, withdraw=withdraw)
            if result["status"] == "already_accepted":
                # Nothing changed, so no audit note says a person accepted something: roll the note back.
                raise _Unchanged(result)
            return result
    except _Unchanged as unchanged:
        return {"project_id": project_id, **unchanged.result}


class _Unchanged(Exception):
    def __init__(self, result):
        super().__init__("unchanged")
        self.result = result


def overview(reader, project_id, limit=LIST_LIMIT) -> dict:
    """The unknown kinds in the project's archived transcripts, and what a person accepted or withdrew.

    Read-only. `blocking` are kinds nobody accepted, each with how many records, one example (escaped and
    cut) and where it is; `malformed` counts unknown records no acceptance can cover. The records are read
    in pages, up to `limit` of them; `complete` is false when more were left unread, and the view says so.
    """
    row = _project_row(reader, project_id)
    standing = _entries(row.get(FIELD))
    keys = {(entry["client"], entry["kind"]) for entry in standing}
    groups, counts, malformed = {}, {}, 0
    scanned, complete = 0, True
    for source in _unknown_rows(reader, project_id, limit):
        if source is None:
            complete = False
            break
        scanned += 1
        found = unknown_kind(source, source.get("content"))
        if found is None:
            malformed += 1
            continue
        key = found["client"], found["kind"]
        counts[key] = counts.get(key, 0) + 1
        if key not in groups:
            metadata = source.get("metadata") or {}
            if found["version"] is None and metadata.get("import_path"):
                first = one_row(reader, "SELECT content FROM sources WHERE project_id=:project AND "
                                "metadata.import_path=:path AND metadata.byte_offset=0 LIMIT 1",
                                {"project": project_id, "path": metadata["import_path"]}, False)
                found["version"] = _first_version((first or {}).get("content"))
            groups[key] = {**found, "path": safe_text(str(metadata.get("import_path") or ""), limit=400),
                           "byte": metadata.get("byte_offset") if type(metadata.get("byte_offset")) is int else None}
    found_kinds = [{**groups[key], "records": counts[key]} for key in sorted(groups)]
    return {"project_id": project_id, "project": safe_text(str(row.get("name") or ""), limit=200),
            "blocking": [item for item in found_kinds if (item["client"], item["kind"]) not in keys],
            "accepted": [{**entry, "records": counts.get((entry["client"], entry["kind"]), 0)} for entry in standing],
            "withdrawn": _entries(row.get(WITHDRAWN_FIELD), MAX_WITHDRAWN), "malformed": malformed,
            "scanned": scanned, "complete": complete}


def _unknown_rows(reader, project_id, limit):
    """The project's unknown transcript records in id order, a page at a time; None after `limit` when more remain."""
    from .ingest import UNKNOWN_TRANSCRIPT_WARNING
    after, seen = None, 0
    while True:
        size = min(_PAGE, limit - seen + 1)  # one more than may be read, to learn whether more remain
        parameters = {"project": project_id, "warning": UNKNOWN_TRANSCRIPT_WARNING, "limit": size}
        if after is not None:
            parameters["position"] = after
        page = reader.query(
            "SELECT id,kind,content,metadata FROM sources WHERE project_id=:project AND is_current=true "
            "AND kind='transcript_unknown' AND metadata.schema_warning=:warning"
            + (" AND id > :position" if after is not None else "") + " ORDER BY id LIMIT :limit", parameters)
        for source in page:
            if seen >= limit:
                yield None
                return
            seen += 1
            yield source
        if len(page) < size:
            return
        after = page[-1]["id"]


def doctor_view(reader) -> list[dict]:
    """Every standing acceptance, per project, for `doctor`."""
    result = []
    for row in reader.query(f"SELECT id,name,{FIELD} FROM projects ORDER BY name"):
        entries = _entries(row.get(FIELD))
        if entries:
            result.append({"project_id": row["id"], "project": row.get("name"), "accepted": entries,
                           "withdraw": f"cairnmind record-kind withdraw {row['id']} CLIENT KIND"})
    return result
