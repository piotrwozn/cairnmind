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
    {"by": "lapsed", "confirmed_version", "version", "status"}
        on a record that is no longer confirmed because a write that was not a person's act changed what
        had been confirmed, or set another status on it. Only a confirmation that is not a person's verdict
        given here can lapse: an imported one, evaluation seed data, and the checkpoint record.

A confirmed record without the field was confirmed before this rule existed (`before_rule`): its status
is kept and nothing is claimed about who confirmed it.

A write that is not a person's act never takes a person's confirmation away (`kept_verdict`, `VerdictKept`):
it may not replace such a record, set another status on it or change what the confirmation covers.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

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
# A confirmation covers the record's kind, text, key, replaced record and every metadata field, except
# these. A field is covered unless it is named here, so text a writer puts beside a confirmed record in any
# other field (a summary, a next step, a field added later) ends the confirmation like a changed text does.
# What the store derives and the bookkeeping of the conflict inbox: no client can write them.
UNCOVERED_METADATA = frozenset({
    "evidence_earliest", "evidence_latest", "evidence_basis", "supersede_warning",
    "supersede_replaced_evidence_latest", "extraction_source_id", "origin_client", "origin_session_id",
    "origin_history", "origin_evidence", "publication", "confirmation", "conflicts_resolved",
    "conflict_decisions", "conflict_scope_note", "superseded_ids", "superseded_by", "possible_conflicts"})
# On a task only: progress (state, reported evidence, check runs), a declared scope, a proposed command and
# the placement in a plan. None of them is free text, except the path patterns of a scope.
TASK_PROGRESS_METADATA = frozenset({
    "state", "test_passed", "test_source_ids", "review_passed", "review_source_ids", "acceptance_passed",
    "acceptance_source_ids", "check_run_ids", "scope", "check_proposals", "worktree", "plan_context_hash",
    "step_index", "plan_archived", "unresolved_dependencies"})
# What a task holds when a writer leaves the field out: the same as the field with this value.
_TASK_DEFAULTS = {"depends_on": [], "acceptance_criteria": [], "requires_test": False, "requires_review": False,
                  "required_profiles": [], "criterion_checks": {}}
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


class VerdictKept(ConfirmationRefused):
    """A write that is not a person's act would take a person's confirmation away: nothing is stored."""

    def __init__(self, record, change, status=None):
        self.record_id, self.kind, self.key = record["id"], record["kind"], record.get("key")
        self.change, self.status = change, status
        super().__init__(kept_refusal(record, change, status))


# What the refused write would have done to the confirmed record.
_KEPT_CHANGES = {"replace": "replace it (supersedes)", "status": "set its status to {status}",
                 "change": "change what was confirmed (its text, key or a field beside it)"}
# Kinds whose pairs the conflict inbox shows (conflicts.KINDS).
_INBOX_KINDS = ("fact", "decision")


def kept_refusal(record, change, status=None) -> str:
    """The refusal of a write that would take a person's confirmation away: the record, and what to do."""
    record_id, kind = record["id"], record["kind"]
    would = _KEPT_CHANGES[change].format(status=status)
    if kind in _INBOX_KINDS:
        instead = (f"store what you found as a new proposed {kind} under another key, with "
                   f"metadata.possible_conflicts set to [\"{record_id}\"]: the pair then waits in the conflict "
                   "inbox, where the person decides which one stands.")
    elif kind == "task":
        instead = ("report progress on it as before (state, evidence, check runs, scope), or store a new proposed "
                   "task; the person disputes this one in the inspector, or changes it in their own terminal.")
    else:
        instead = (f"store what you found as a new proposed {kind} under another key and tell the person; "
                   "they dispute this record or confirm the new one in the inspector.")
    return (f"A person confirmed record {record_id} ({kind}), and this write is not a person's act: it would "
            f"{would}. Only a person takes a confirmation away. Nothing was saved. Leave that record as it is and "
            + instead)


def kept_step_refusal(step_key, refusal) -> str:
    """The same refusal for a plan save, with the step named."""
    would = _KEPT_CHANGES[refusal.change].format(status=refusal.status)
    if refusal.change == "status" and refusal.status == "superseded":
        would = "remove it from the plan"
    return (f"Step {str(step_key)[:200]!r} of this plan (task {refusal.record_id}) was confirmed by a person, and "
            f"this save is not a person's act: it would {would}. Only a person takes a confirmation away. Nothing "
            "was saved. Save the plan with that step exactly as it is (other steps can change and new steps can be "
            "added); the person disputes the step in the inspector, or saves the plan with `cairnmind plan save` "
            "in their own terminal.")


def said(kind, content, key, supersedes, metadata) -> tuple:
    """What a confirmation covers: when this differs between two versions, the confirmation does not carry over."""
    import json
    metadata = metadata if isinstance(metadata, dict) else {}
    skipped = UNCOVERED_METADATA | TASK_PROGRESS_METADATA if kind == "task" else UNCOVERED_METADATA
    covered = {name: value for name, value in metadata.items() if name not in skipped}
    evidence = covered.pop("evidence", None)
    evidence = evidence.replace("\r\n", "\n") if isinstance(evidence, str) and evidence.strip() else None
    if kind == "task":
        covered = {**_TASK_DEFAULTS, **{name: value for name, value in covered.items() if value is not None}}
    # An empty field says what a missing one says.
    fields = json.dumps({name: value for name, value in covered.items()
                         if value not in (None, "", [], {}) or (kind == "task" and name in _TASK_DEFAULTS)},
                        sort_keys=True, ensure_ascii=False, default=repr)
    return (kind, content, key, supersedes or None, evidence, fields)


def lowered(old, status, version) -> dict | None:
    """The mark of a write that is not a person's act and takes the status confirmed from a record."""
    if not old or old.get("status") != "confirmed" or status == "confirmed":
        return None
    return {"by": "lapsed", "confirmed_version": old["version"], "version": version, "status": status}


def local_verdict(head) -> bool:
    """Whether a record's head carries a verdict given on this machine: it is confirmed or disputed here
    and that version was not itself written by an import.

    An import that moves such a head on to another statement or status replaces that verdict, so it needs
    a person's explicit choice (`replace_confirmed`). A record confirmed before the rule counts: nothing
    says it was not a person's. A disputed record counts whoever disputed it.
    """
    if not isinstance(head, dict) or head.get("status") not in ("confirmed", "disputed"):
        return False
    metadata = head.get("metadata") if isinstance(head.get("metadata"), dict) else {}
    mark, publication = metadata.get(MARK), metadata.get("publication")
    if isinstance(mark, dict) and head.get("status") == "confirmed" and mark.get("by") in ("person", "import"):
        return mark["by"] == "person"
    # A version that is a published revision an import wrote, confirmed or disputed elsewhere.
    return not (isinstance(publication, dict) and publication.get("version") == head.get("version"))


def kept_verdict(head) -> bool:
    """Whether a record's head is confirmed by a person here, so that only a person takes that away.

    A person's act on this machine, or a record confirmed before the rule (nothing says it was not a
    person's; `local_verdict` reads it the same way for an import). Not a confirmation an import brought,
    and not evaluation seed data: neither is a verdict given here.
    """
    return (isinstance(head, dict) and head.get("status") == "confirmed" and local_verdict(head)
            and confirmed_by(head) in ("person", "before_rule"))


REPLACES_NOTE = ("these records were confirmed or disputed on this machine and the import puts another text or "
                 "status in their place; it is applied only with --replace-confirmed, chosen by a person")


IMPORT_IN_SESSION = ("A memory import is run only by a person, not inside a coding session: it can bring records "
                     "that count as confirmed. Nothing was imported. A session may run the dry run; the person "
                     "runs the import in their own terminal or in the workspace.")


def replaces_refusal(names) -> str:
    return (f"This import would replace {len(names)} record(s) that were confirmed or disputed on this machine: "
            + ", ".join(str(name) for name in names[:50]) + (" and more" if len(names) > 50 else "")
            + ". Nothing was imported. Read them in the dry run, then run the import again with "
              "--replace-confirmed if the imported side is the one to keep.")


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

    A lapsed confirmation is reported for as long as the record is not confirmed again and no person has
    given another verdict on it.
    """
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata")
    mark = metadata.get(MARK) if isinstance(metadata, dict) else None
    mark = mark if isinstance(mark, dict) else {}
    if record.get("status") != "confirmed":
        if mark.get("by") == "lapsed":
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


class Fixed(str):
    """A word of ours inside the line, not text from memory: a caller that translates the line translates it."""


def _clean(value, limit=120):
    text = " ".join(safe_text(str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


_ROUTE_WORDS = {"inspector_review": "in the inspector", "conflict_decision": "by a conflict decision",
                "confirm_plan": "with its plan", "workspace_form": "in the workspace form",
                "cli": "on the command line"}
# Said in place of a value the mark does not hold. Fixed words, translated with the line.
FALLBACK_WORDS = ("the local user", "time unknown", "an unnamed person", "an unknown branch", "an unknown commit",
                  "a memory package", "an unknown day", "by a person's act")


def _or(value, fallback, limit=120):
    return _clean(value, limit) if value else Fixed(fallback)


def confirmation_text(record, translate=None) -> str | None:
    """One line for wherever a record is shown, in English or through `translate`. Names and paths come
    from memory or a package and are escaped here and never translated; the sentence and the words that
    stand for a missing value are ours and are."""
    parts = confirmation_parts(record)
    if parts is None:
        return None
    template, values = parts
    if translate is not None:
        template = translate(template)
        values = {name: translate(str(value)) if isinstance(value, Fixed) else value for name, value in values.items()}
    return template.format(**values)


def confirmation_parts(record):
    """(template, values) of the line: the template is a fixed source string; a value is escaped text from
    memory or a package, or a `Fixed` word of ours that stands where the mark holds nothing."""
    found = confirmation_of(record)
    if found is None:
        return None
    by = found["by"]
    if by == "person":
        return ("confirmed here by a person: {name}, {route}, {at}",
                {"name": _or(found.get("actor_name"), "the local user"),
                 "route": Fixed(_ROUTE_WORDS.get(found.get("route"), "by a person's act")),
                 "at": _or(str(found.get("at") or "")[:10], "time unknown")})
    if by == "import":
        where = _or(found.get("where"), "a memory package", 200)
        day = _or(str(found.get("imported_at") or "")[:10], "an unknown day")
        claims = [entry for entry in found.get("confirmed_by") or [] if isinstance(entry, dict)]
        if not claims:
            return ("confirmed elsewhere, who confirmed it is not recorded; imported from {where} on {day}; "
                    "not verified here", {"where": where, "day": day})
        last = claims[-1]
        return ("confirmed elsewhere by {name} on {branch} at {commit} (claimed by the package); "
                "imported from {where} on {day}; not verified here",
                {"name": _or(last.get("name"), "an unnamed person"),
                 "branch": _or(last.get("branch"), "an unknown branch"),
                 "commit": _or(str(last.get("commit") or "")[:40], "an unknown commit"),
                 "where": where, "day": day})
    if by == "seed":
        return ("confirmed by evaluation seed data, not by a person", {})
    if by == "lapsed":
        status = found.get("status") if found.get("status") in ("disputed", "superseded") else "proposed"
        return ("was confirmed at version {version}; a later write that was not a person's confirmation "
                "changed it, so it is now {status}",
                {"version": _clean(found.get("confirmed_version") or "?"), "status": Fixed(status)})
    return ("confirmed before only a person could confirm; who confirmed it was not recorded", {})


# Beside the state in a list, for a confirmation that is not a person's own here. Fixed words.
LIST_WORDS = {"import": "confirmed elsewhere", "before_rule": "confirmed before the rule",
              "seed": "confirmed by seed data", "lapsed": "confirmation lapsed"}


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


def person_source(store, project_id, text, *, route="cli", origin=None, action="command_line_confirm"):
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
    audit = {"actor": "local_user", **context, "action": action, "text_chars": len(text)}
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


@contextmanager
def person_command(store, project_id, action):
    """A command a person types in their own terminal that may change records they confirmed (a plan save).

    Elsewhere (a coding session's shell, an agent's command, a script) nothing is opened, and the store
    refuses what would take a confirmation away. The note says who ran the command; it confirms nothing.
    """
    if not person_at_terminal():
        yield False
        return
    with store.transaction():
        note = person_source(store, project_id, action, action=action)
        with store.person_act("cli", note["id"]):
            yield True
