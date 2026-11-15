"""A person's acts on memory, recorded so that they can travel with published memory (#170).

A review, a conflict decision and a plan confirmation each write an audit source. The audit names who
acted (`git config user.name` of the project, read when the act happens, never at publish time), the
branch and the commit, and hashes exactly what was judged: the text, the kind, key and replaced record,
the quoted passage (#172), and for a task what it asks for.
A published revision is covered only by such an audit, so an agent's later edit is never published as
something a person confirmed.
"""
from __future__ import annotations

import json
import subprocess
from uuid import uuid4

from .memory_published_format import (J, audit_covers, evidence_hash, identity_hash, plan_hash, row_definition_hash,
                                      sha256_text)
from .sql import clean_row, one_row, utc_now


def act_context(store, project_id) -> dict:
    """actor_name, branch and commit of the project's root, read-only; each None when Git cannot say."""
    from .verification import _git
    rows = store.query("SELECT root FROM projects WHERE id=:id", {"id": project_id})
    root = rows[0].get("root") if rows else None
    values = {"actor_name": None, "branch": None, "commit": None}
    if not root:
        return values
    for name, args in (("actor_name", ("config", "user.name")), ("branch", ("rev-parse", "--abbrev-ref", "HEAD")),
                       ("commit", ("rev-parse", "HEAD"))):
        try:
            value = _git(root, *args).decode("utf-8", "replace").strip()
        except (OSError, ValueError, subprocess.SubprocessError):
            continue  # A missing or slow Git never blocks a person's act.
        values[name] = value[:200] if value else None
    return values


def record_identity(record) -> str:
    return identity_hash(record["kind"], record["key"], record.get("supersedes"))


def record_evidence(record):
    """The hash of the passage the record quotes, or None: a published quote must be this one (#172)."""
    return evidence_hash((record.get("metadata") or {}).get("evidence"))


def definition_fields(record) -> dict:
    """What a review hashes besides the text: kind, key and supersedes, the quoted passage, and for a task
    what it asks for."""
    fields = {"identity_sha256": record_identity(record), "evidence_sha256": record_evidence(record)}
    if record["kind"] == "task":
        fields["definition_sha256"] = row_definition_hash(record["content"], record.get("metadata"))
    return fields


def review_refused(tx, record, status, action) -> bool:
    """A review that changes nothing is refused, except confirming a confirmed record no act here covers."""
    return record["status"] == "superseded" or (record["status"] == status and (
        action != "confirm" or covered_by_local_act(tx, record)))


def covered_by_local_act(tx, record) -> bool:
    """Whether a person on this machine covered the record's current version (a review, or a confirmed form).

    For a confirmed record the answer is the store's own mark (#177): a review, a conflict decision, a plan
    confirmation or the workspace form, made here. A field a client can send says nothing, and neither does
    an audit that arrived in a memory package. A command line confirmation is a person's, but it is not an
    act that travels with published memory, so the inspector may still confirm such a record.
    """
    if record["status"] == "confirmed":
        from .confirmation import confirmation_of
        mark = confirmation_of(record) or {}
        return mark.get("by") == "person" and mark.get("route") in (
            "inspector_review", "conflict_decision", "confirm_plan", "workspace_form")
    metadata = record.get("metadata") or {}
    definition = row_definition_hash(record["content"], metadata) if record["kind"] == "task" else None
    for source_id in record.get("source_ids") or []:
        source = one_row(tx, "SELECT uri,content,metadata FROM sources WHERE id=:id", {"id": source_id}, False)
        if (not source or not str(source.get("uri", "")).startswith("review://")
                or (source.get("metadata") or {}).get("origin") != "local_user_review"):
            continue
        try:
            audit = json.loads(source["content"])
        except ValueError:
            continue
        if audit_covers(audit, record["id"], record["kind"], record["status"], record["content"], definition,
                        record_identity(record)):
            return True
    return False


def confirm(inspector, project, plan_id, expected_plan_version, *, reason=None):
    """The inspector's action: confirm_plan for a project the inspector names."""
    from .inspector import InspectorNotFound
    try:
        return confirm_plan(inspector.store, inspector._project(project)["id"], plan_id, expected_plan_version, reason)
    except LookupError as exc:
        raise InspectorNotFound(str(exc)) from None


def confirm_plan(store, project_id, plan_id, expected_plan_version, reason=None):
    """Confirm a plan's definition and every active step in one transaction, with one audit source."""
    from .plans import _tasks
    if type(expected_plan_version) is not int or expected_plan_version < 1:
        raise ValueError("Confirming a plan requires the exact plan version")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4000):
        raise ValueError("Review reason must be at most 4000 characters")
    context = act_context(store, project_id)
    with store.transaction() as tx:
        plan = one_row(tx, "SELECT FROM plans WHERE id=:id AND project_id=:project",
                       {"id": plan_id, "project": project_id}, False)
        if not plan:
            raise LookupError("Unknown plan in requested project")
        if plan["version"] != expected_plan_version:
            raise ValueError("Review plan version conflict; read the current plan")
        tasks = [task for task in _tasks(tx, project_id, clean_row(plan)) if task["status"] != "superseded"]
        if not tasks:
            raise ValueError("A plan without steps cannot be confirmed")
        core = {"title": plan["title"], "goal": plan["goal"], "constraints": plan["constraints"],
                "steps": [[task["id"], task["metadata"].get("step_key")] for task in tasks]}
        audit = {"action": "confirm_plan", "actor": "local_user", **context, "at": utc_now(), "plan_id": plan_id,
                 "plan_version": plan["version"], "plan_sha256": plan_hash(core), "reason": (reason or "").strip(),
                 "content_sha256": {task["id"]: sha256_text(task["content"]) for task in tasks},
                 "definition_sha256": {task["id"]: row_definition_hash(task["content"], task["metadata"])
                                       for task in tasks},
                 "identity_sha256": {task["id"]: record_identity(task) for task in tasks},
                 "evidence_sha256": {task["id"]: record_evidence(task) for task in tasks}}
        source = store.snapshot(project_id, "review://" + str(uuid4()), J(audit), "manual",
                                {"origin": "local_user_review", "role": "user", "plan_id": plan_id})
        version, saved = plan["version"], []
        for task in tasks:
            # Each step write moves the plan version on (note_task_mutation), so the next one names it.
            with store.person_act("confirm_plan", source["id"]):  # The person's act (#177).
                saved.append(store._remember(
                    tx, project_id, {"id": task["id"], "expected_version": task["version"], "status": "confirmed",
                                     "expected_plan_version": version}, [*task["source_ids"], source["id"]]))
            version += 1
        return {"plan_id": plan_id, "plan_version": version, "tasks": saved, "audit_source_id": source["id"]}
