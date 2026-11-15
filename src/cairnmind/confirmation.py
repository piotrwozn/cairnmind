"""Only a person can store a record as confirmed (#177).

The store keeps one derived field on a record, `metadata.confirmation`, which says how the record came
to be confirmed. A client can neither send it nor clear it. It has these forms:

    {"by": "person", "route", "source_id", "version", "actor_name", "at"}
        a person's act on this machine: the inspector's confirm, a conflict decision, a plan
        confirmation, the workspace form, or the command line run by a person in a terminal;
    {"by": "import", "package", "where", "confirmed_by": [...], "imported_at", "version"}
        confirmed elsewhere and brought here by a memory package; who and where are what the package
        claims, and nothing about it is verified here;
    {"by": "seed", ...}
        data an evaluation wrote for itself; never a person;
    {"by": "lapsed", "confirmed_version", "version"}
        on a record that is proposed again because a writer that is not a person changed what had
        been confirmed.

A confirmed record without the field was confirmed before this rule existed (`before_rule`): its status
is kept and nothing is claimed about who confirmed it.
"""
from __future__ import annotations

import os
import sys

from .config import session_value
from .terminal_safe import safe_text

MARK = "confirmation"
# The routes on which a person acts, each with the source the route itself writes: (URI prefix, origin).
PERSON_ROUTES = {
    "inspector_review": ("review://", "local_user_review"),
    "conflict_decision": ("review://", "local_user_review"),
    "confirm_plan": ("review://", "local_user_review"),
    "workspace_form": ("workspace://finding/", "workspace_form"),
    "cli": ("person://", "cli_person"),
}
# Not a person: what `cairnmind eval` writes into its own throwaway stores.
SEED_ROUTE = "evaluation_seed"
SEED_SOURCE = ("seed://", "evaluation_seed")
# What a confirmation of a task covers besides its text, key and replaced record. Progress (state, reported
# evidence, check runs), a declared scope, a proposed command and the placement in a plan are not part of it.
CONFIRMED_TASK_FIELDS = ("plan_ref", "step_key", "required_profiles", "depends_on", "acceptance_criteria",
                         "requires_test", "requires_review", "criterion_checks")
# A marker a coding client leaves in the environment of the commands it runs.
CLIENT_MARKERS = ("CLAUDECODE", "CODEX_THREAD_ID")

REFUSED = ("Only a person can store a record as confirmed, and this write is not a person's act. Nothing was saved. "
           "Store the record as proposed (leave the status out); the person confirms it in the inspector, "
           "or types `cairnmind remember --confirmed` in their own terminal.")
CLI_REFUSED = ("Only a person can store a record as confirmed. This command runs inside a coding session, or "
               "without a terminal, so it does not count as a person. Nothing was saved. Run it without "
               "--confirmed to store a proposed record; the person confirms it in the inspector, or runs "
               "this command in their own terminal.")


class ConfirmationRefused(ValueError):
    """A write that is not a person's act asked for the status confirmed."""

    def __init__(self, message=REFUSED):
        super().__init__(message)


def said(kind, content, key, supersedes, metadata) -> tuple:
    """What a confirmation covers: when this differs between two versions, the confirmation does not carry over."""
    metadata = metadata if isinstance(metadata, dict) else {}
    evidence = metadata.get("evidence")
    evidence = evidence.replace("\r\n", "\n") if isinstance(evidence, str) and evidence.strip() else None
    task = tuple(repr(metadata.get(name)) for name in CONFIRMED_TASK_FIELDS) if kind == "task" else ()
    return (kind, content, key, supersedes or None, evidence, task)


def person_mark(route, source, version, at) -> dict:
    """The mark of a person's act, from the note the route wrote: a form or command line note names the
    person in its metadata, a review audit in its text."""
    import json
    name = (source.get("metadata") or {}).get("actor_name")
    if name is None and str(source.get("uri", "")).startswith("review://"):
        try:
            audit = json.loads(source.get("content") or "")
            name = audit.get("actor_name") if isinstance(audit, dict) else None
        except ValueError:
            name = None
    by = "seed" if route == SEED_ROUTE else "person"
    return {"by": by, "route": route, "source_id": source["id"], "version": version,
            "actor_name": name if isinstance(name, str) else None, "at": at}


def import_mark(package, where, confirmed_by, imported_at, version) -> dict:
    """The mark of a record a package brought as confirmed: confirmed elsewhere, never verified here."""
    return {"by": "import", "origin": "import", "package": package, "where": where,
            "confirmed_by": [{name: entry.get(name) for name in ("name", "action", "branch", "commit", "at")}
                             for entry in confirmed_by if isinstance(entry, dict)][-8:],
            "imported_at": imported_at, "version": version}


def archive_claims(record, sources) -> list:
    """Who a full archive says confirmed a record, read from the notes the package carries for it.

    A package never carries the store's own mark (it is local, like the extraction mark), so the claim is
    read from what travels: a review audit that covers the record as it stands, or the workspace form note
    of a finding saved as confirmed. `sources` maps the package's source IDs to its source rows. The result
    is a claim of the package, in the shape of a published file's `reviewed_by`; empty when the package
    names nobody (a record confirmed on the command line, or before audits existed).
    """
    import json

    from .memory_published_format import audit_covers, identity_hash, row_definition_hash
    if not isinstance(record, dict) or record.get("status") != "confirmed":
        return []
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    definition = row_definition_hash(record["content"], metadata) if record["kind"] == "task" else None
    identity = identity_hash(record["kind"], record["key"], record.get("supersedes"))
    claims = []
    for source_id in record.get("source_ids") or []:
        source = sources.get(source_id) if isinstance(source_id, str) else None
        if not isinstance(source, dict):
            continue
        origin = (source.get("metadata") or {}).get("origin")
        if str(source.get("uri", "")).startswith("review://") and origin == "local_user_review":
            try:
                audit = json.loads(source.get("content") or "")
            except ValueError:
                continue
            if isinstance(audit, dict) and audit_covers(audit, record["id"], record["kind"], "confirmed",
                                                        record["content"], definition, identity):
                claims.append({"name": audit.get("actor_name"), "action": audit.get("action"),
                               "branch": audit.get("branch"), "commit": audit.get("commit"), "at": audit.get("at")})
        elif origin == "workspace_form" and source.get("content") == record["content"]:
            meta = source.get("metadata") or {}
            claims.append({"name": meta.get("actor_name"), "action": "workspace_form", "branch": meta.get("branch"),
                           "commit": meta.get("commit"), "at": source.get("created_at")})
    return sorted(claims, key=lambda claim: str(claim.get("at") or ""))


def confirmation_of(record) -> dict | None:
    """How a record came to be confirmed, read from the record alone; None when it is not confirmed.

    A lapsed confirmation is reported only on the version that lost it.
    """
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata")
    mark = metadata.get(MARK) if isinstance(metadata, dict) else None
    mark = mark if isinstance(mark, dict) else {}
    if record.get("status") != "confirmed":
        if mark.get("by") == "lapsed" and mark.get("version") == record.get("version"):
            return mark
        return None
    if mark.get("by") in ("person", "import", "seed"):
        return mark
    return {"by": "before_rule"}


def confirmed_by(record) -> str | None:
    """One word for lists and the resume packet: person, import, seed, before_rule or lapsed."""
    found = confirmation_of(record)
    return found["by"] if found else None


def is_local_person(record) -> bool:
    """Whether a person on this machine confirmed the record's current statement."""
    return confirmed_by(record) == "person"


def _clean(value, limit=120):
    text = " ".join(safe_text(str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


_ROUTE_WORDS = {"inspector_review": "in the inspector", "conflict_decision": "by a conflict decision",
                "confirm_plan": "with its plan", "workspace_form": "in the workspace form",
                "cli": "on the command line"}


def confirmation_text(record) -> str | None:
    """One English line for wherever a record is shown. Names and paths come from memory or a package and
    are escaped here; a caller that translates does it before calling, through `confirmation_parts`."""
    parts = confirmation_parts(record)
    if parts is None:
        return None
    template, values = parts
    return template.format(**values)


def confirmation_parts(record):
    """(template, values) of the line: the template is a fixed source string, the values are escaped text."""
    found = confirmation_of(record)
    if found is None:
        return None
    by = found["by"]
    if by == "person":
        name = _clean(found.get("actor_name") or "the local user")
        return ("confirmed here by a person: {name}, {route}, {at}",
                {"name": name, "route": _ROUTE_WORDS.get(found.get("route"), "by a person's act"),
                 "at": _clean(str(found.get("at") or "")[:10] or "time unknown")})
    if by == "import":
        where = _clean(found.get("where") or "a memory package", 200)
        day = _clean(str(found.get("imported_at") or "")[:10] or "an unknown day")
        claims = [entry for entry in found.get("confirmed_by") or [] if isinstance(entry, dict)]
        if not claims:
            return ("confirmed elsewhere, who confirmed it is not recorded; imported from {where} on {day}; "
                    "not verified here", {"where": where, "day": day})
        last = claims[-1]
        return ("confirmed elsewhere by {name} on {branch} at {commit} (claimed by the package); "
                "imported from {where} on {day}; not verified here",
                {"name": _clean(last.get("name") or "an unnamed person"),
                 "branch": _clean(last.get("branch") or "an unknown branch"),
                 "commit": _clean(str(last.get("commit") or "an unknown commit")[:40]),
                 "where": where, "day": day})
    if by == "seed":
        return ("confirmed by evaluation seed data, not by a person", {})
    if by == "lapsed":
        return ("was confirmed at version {version}; a session or agent changed it since, so it is proposed again",
                {"version": _clean(found.get("confirmed_version") or "?")})
    return ("confirmed before only a person could confirm; who confirmed it was not recorded", {})


def person_at_terminal(environ=None, stdin=None, stdout=None) -> bool:
    """Whether a command line is run by a person: no coding session around it, and a terminal on both ends.

    A coding client launched by Cairnmind carries the session variables, and the known clients leave their
    own marker. The built-in agent's shell has neither, but it has no terminal: its input is /dev/null and
    its output is a pipe. This tells a person from the tools handed to a model; it is not a defence against
    a program that runs as the same user and fakes a terminal.
    """
    environ = os.environ if environ is None else environ
    try:
        in_session = bool(session_value("SESSION_ID", environ) or session_value("PACKET_ID", environ))
    except ValueError:
        in_session = True
    if in_session or any(environ.get(name) for name in CLIENT_MARKERS):
        return False
    stdin, stdout = (sys.stdin if stdin is None else stdin), (sys.stdout if stdout is None else stdout)
    try:
        return bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, ValueError):
        return False


def person_source(store, project_id, text, *, route="cli", origin=None):
    """The note a person's command line confirmation leaves: who confirmed, on which branch and commit.

    Like a review's audit it says that a person judged the record, never why the record is true: it is
    not evidence, it does not anchor the record and it is not the time of its evidence. Unlike a review
    it hashes nothing, so it is not an act that travels with published memory.
    """
    import json
    from uuid import uuid4

    if route == SEED_ROUTE:
        # Says only that a fixture wrote it: no text an evaluation could retrieve, and no person's name.
        return store.snapshot(project_id, f"{SEED_SOURCE[0]}{uuid4()}", "Evaluation seed data.", "manual",
                              {"origin": SEED_SOURCE[1]})
    from .memory_acts import act_context
    prefix, source_origin = PERSON_ROUTES[route]
    context = act_context(store, project_id)
    metadata = {"origin": origin or source_origin, "role": "user", **context}
    audit = {"actor": "local_user", **context, "action": "command_line_confirm", "text_chars": len(text)}
    return store.snapshot(project_id, f"{prefix}{uuid4()}", json.dumps(audit, ensure_ascii=False, sort_keys=True),
                          "manual", metadata)


def remember_as_person(store, project_id, record, sources=None, *, route="cli"):
    """Store a record as a person's own act on the command line (or as evaluation seed data).

    The caller has established that a person is acting: the CLI by `person_at_terminal`, an evaluation by
    being one. The note source is written here and is the one the store checks the act against.
    """
    with store.transaction():
        note = person_source(store, project_id, record.get("content") or "confirmed", route=route)
        with store.person_act(route, note["id"]):
            return store.remember(project_id, record, [*(sources or []), note["id"]])
