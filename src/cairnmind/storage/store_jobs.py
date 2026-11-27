"""Job queue, diagnostics, retries, status counts, and missing-embedding queries for Store."""
from __future__ import annotations

import hashlib
import math
import re
import time
from uuid import uuid4

from cairnmind.storage.sql import clean_row, json_text, utc_now
from cairnmind.storage.storage_query import (
    all_of,
    any_of,
    asc,
    db_find,
    db_find_one,
    db_insert_document,
    db_update_where,
    desc,
    eq,
    is_in,
    is_not_null,
    is_null,
    lt,
    normalize_where,
    not_in,
)

_DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_JOB_UNSET = object()
JOB_STATES = {"pending", "running", "completed", "failed"}


class JobNotFoundError(ValueError):
    """No job is visible in the requested project."""


def job_diagnostics(value):
    """Retain bounded operational codes/counts, never model/source text."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("stage", "code", "repair_outcome", "failure_reason", "persistence_stage"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _DIAGNOSTIC_CODE.fullmatch(candidate):
            result[key] = candidate
    if type(value.get("repair_attempted")) is bool:
        result["repair_attempted"] = value["repair_attempted"]
    codes = value.get("validation_codes")
    if isinstance(codes, list):
        result["validation_codes"] = [
            code for code in codes[:8]
            if isinstance(code, str) and _DIAGNOSTIC_CODE.fullmatch(code)
        ]
    for group, keys in (
        ("budget", ("timeout_seconds", "max_steps", "context_limit_chars", "elapsed_seconds")),
        ("counts", ("candidate_records", "accepted_records", "skipped_records", "written_records",
                    "superseded_records")),
    ):
        original = value.get(group)
        if isinstance(original, dict):
            result[group] = {
                key: original[key]
                for key in keys
                if type(original.get(key)) in (int, float)
                and 0 <= original[key] <= 10**15
                and math.isfinite(original[key])
            }
    rejections = value.get("rejection_counts")
    if isinstance(rejections, dict):
        result["rejection_counts"] = {
            key: rejections[key]
            for key in ("blank_content", "blank_evidence", "quote_absent_source", "reserved_metadata")
            if type(rejections.get(key)) is int and 0 <= rejections[key] <= 10**15
        }
    calls = value.get("model_calls")
    if isinstance(calls, list):
        result["model_calls"] = []
        for call in calls[-2:]:
            if not isinstance(call, dict):
                continue
            item = {
                key: call[key]
                for key in ("phase", "done_reason")
                if isinstance(call.get(key), str) and _DIAGNOSTIC_CODE.fullmatch(call[key])
            }
            item.update({
                key: call[key]
                for key in ("prompt_eval_count", "eval_count", "total_duration")
                if type(call.get(key)) is int and 0 <= call[key] <= 10**18
            })
            result["model_calls"].append(item)
    return result


def enqueue_new(store, tx, kind, project_id, payload, dedupe_key):
    """A job whose key cannot exist yet, because it names a row created in this same transaction.

    The row is the one ``enqueue`` writes. The unique index on the key still refuses a second one.
    """
    _ = store
    if not kind or not isinstance(dedupe_key, str) or not dedupe_key:
        raise ValueError("A job requires a kind and deduplication key")
    job_id, now = str(uuid4()), utc_now()
    db_insert_document(
        tx,
        "jobs",
        {
            "id": job_id,
            "project_id": project_id,
            "kind": kind,
            "payload": payload,
            "dedupe_key": dedupe_key,
            "job_key": json_text([kind, project_id, dedupe_key]),
            "state": "pending",
            "lease_until": None,
            "worker": None,
            "error": None,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    return job_id


def enqueue(store, kind, project_id, payload, dedupe_key):
    if not kind or not isinstance(dedupe_key, str) or not dedupe_key:
        raise ValueError("A job requires a kind and deduplication key")
    with store.transaction() as tx:
        store._require_project(tx, project_id)
        job_key = json_text([kind, project_id, dedupe_key])
        old = db_find_one(tx, "jobs", {"job_key": job_key}, columns=("id",))
        if old:
            return old["id"]
        job_id, now = str(uuid4()), utc_now()
        db_insert_document(
            tx,
            "jobs",
            {
                "id": job_id,
                "project_id": project_id,
                "kind": kind,
                "payload": payload,
                "dedupe_key": dedupe_key,
                "job_key": job_key,
                "state": "pending",
                "lease_until": None,
                "worker": None,
                "error": None,
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        return job_id


def claim_job(store, worker, lease_seconds=60, kinds=None, *, project_ids=None):
    if lease_seconds <= 0 or not worker:
        raise ValueError("Worker and positive lease duration are required")
    if kinds is not None and not kinds:
        return None
    if project_ids is not None:
        if (
            not isinstance(project_ids, (list, tuple))
            or any(not isinstance(value, str) or not value for value in project_ids)
        ):
            raise ValueError("Job project filter must contain explicit project IDs")
        if not project_ids:
            return None  # An empty authorization set must never claim global work.
    if store.not_writer:
        # Only the writer's worker claims jobs (#70): a lease is measured by the claiming machine's clock.
        from cairnmind.storage.store_writer import StoreNotWriter
        raise StoreNotWriter(store.not_writer)
    with store.transaction() as tx:
        # The jobs of a removed project wait until it is back (#327).
        removed = [r["id"] for r in db_find(tx, "projects", is_not_null("removed_at"), columns=("id",))]
        conds = [any_of(eq("state", "pending"), all_of(eq("state", "running"), lt("lease_until", time.time())))]
        if kinds is not None:
            conds.append(is_in("kind", list(kinds)))
        if project_ids is not None:
            conds.append(is_in("project_id", list(project_ids)))
        if removed:
            conds.append(any_of(is_null("project_id"), not_in("project_id", removed)))
        job = db_find_one(tx, "jobs", conds, order_by=(asc("created_at"), asc("id")))
        if not job:
            return None
        if job["state"] == "running":
            job["attempt_history"] = store._job_event(job, "lease_expired")
        job.update(
            state="running",
            worker=worker,
            lease_until=time.time() + lease_seconds,
            attempts=job.get("attempts", 0) + 1,
            updated_at=utc_now(),
        )
        job["diagnostics"] = {}
        db_update_where(tx, "jobs", set_fields=job, where={"id": job["id"]})
        return job


def job_event(job, event, *, state=None, error=_JOB_UNSET):
    history = list(job.get("attempt_history") or [])
    history.append({
        "event": event,
        "attempts": job.get("attempts", 0),
        "state": state or job["state"],
        "at": utc_now(),
        "worker": job.get("worker"),
        "error": job.get("error") if error is _JOB_UNSET else error,
        "diagnostics": job_diagnostics(job.get("diagnostics")),
    })
    return history


def set_job_diagnostics(store, job_id, diagnostics, *, expected_attempts=None, worker=None):
    """Publish sanitized attempt diagnostics without completing/reclaiming a job."""
    cleaned = job_diagnostics(diagnostics)
    with store.transaction() as tx:
        job = db_find_one(tx, "jobs", {"id": job_id})
        if not job:
            return False  # Project deletion never recreates operational rows.
        if (
            expected_attempts is not None
            and (job["state"] != "running" or job.get("attempts", 0) != expected_attempts)
        ):
            return False
        if worker is not None and (job["state"] != "running" or job.get("worker") != worker):
            return False
        db_update_where(
            tx, "jobs", set_fields={"diagnostics": cleaned, "updated_at": utc_now()}, where={"id": job_id}
        )
    return True


def finish_job(store, job_id, error=None, *, worker=None, expected_attempts=None):
    with store.transaction() as tx:
        job = db_find_one(tx, "jobs", {"id": job_id})
        if (
            not job
            or (worker is not None and (job.get("worker") != worker or job["state"] != "running"))
            or (
                expected_attempts is not None
                and (job["state"] != "running" or job.get("attempts", 0) != expected_attempts)
            )
        ):
            raise ValueError("Unknown job or stale job lease")
        state = "failed" if error else "completed"
        updates = {
            "state": state,
            "error": error,
            "lease_until": None,
            "updated_at": utc_now(),
            "attempt_history": store._job_event(job, "finished", state=state, error=error),
        }
        if error:
            updates.update(last_error=error, last_error_at=updates["updated_at"])
        db_update_where(tx, "jobs", set_fields=updates, where={"id": job_id})


def job_summary(store, project_id=None):
    store._require_project(store, project_id)
    summary = {
        name: {state: 0 for state in sorted(JOB_STATES)}
        for name in ("archive", "indexing", "extraction", "other")
    }
    for row in store.group_counts(
        "jobs", ("kind", "state"), {"project_id": project_id} if project_id else (), count_alias="total"
    ):
        group = {"import": "archive", "embed": "indexing", "extract": "extraction"}.get(row["kind"], "other")
        if row["state"] in JOB_STATES:
            summary[group][row["state"]] += int(row["total"])
    return summary


def list_jobs(store, project_id, state=None, kind=None, limit=50):
    store._require_project(store, project_id)
    if state is not None and state not in JOB_STATES:
        raise ValueError("Unknown job state")
    if kind is not None and (not isinstance(kind, str) or not 1 <= len(kind) <= 80):
        raise ValueError("Invalid job kind")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Job list limit must be between 1 and 100")
    conds = [eq("project_id", project_id)]
    if state:
        conds.append(eq("state", state))
    if kind:
        conds.append(eq("kind", kind))
    rows = store.find("jobs", conds, order_by=(desc("updated_at"), asc("id")), limit=limit)
    packets = store._job_proofs(project_id) if any(row["kind"] == "import" for row in rows) else []
    return [store._public_job(clean_row(row), packets=packets) for row in rows]


def job_status(store, project_id, job_id):
    store._require_project(store, project_id)
    job = store.find_one("jobs", {"id": job_id, "project_id": project_id})
    if not job:
        raise JobNotFoundError("Unknown job in requested project")
    return store._public_job(
        job,
        detail=True,
        packets=store._job_proofs(project_id) if job["kind"] == "import" else [],
    )


def job_proofs(store, project_id):
    # Read already published bounded evidence; never prepare, repair or
    # recapture a transcript merely to display operational diagnostics.
    return store.find(
        "resume_packets",
        {"project_id": project_id},
        columns=("id", "created_at", "payload"),
        order_by=(desc("created_at"), asc("id")),
        limit=100,
    )


def import_job_coverage(store, job, packets):
    unknown = {"status": "scope_unknown", "scope": "requested_capture"}
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        return {**unknown, "reason": "legacy_missing_capture"}
    capture, event_id = payload.get("requested_watermark"), payload.get("origin_event_id")
    path, client = payload.get("path"), payload.get("client")
    from cairnmind.pal.private_files import decode_file_id
    if (
        not isinstance(capture, dict)
        or not isinstance(event_id, str)
        or not isinstance(path, str)
        or client not in {"codex", "claude"}
        or type(capture.get("size")) is not int
        or capture["size"] < 0
        or decode_file_id(capture.get("device")) is None
        or decode_file_id(capture.get("inode")) is None
        or type(capture.get("head_bytes")) is not int
        or not isinstance(capture.get("head_sha256"), str)
    ):
        return {**unknown, "reason": "legacy_missing_capture"}
    event = store.find_one("session_events", {"id": event_id, "project_id": job["project_id"]})
    if (
        not event
        or event.get("transcript_path") != path
        or event.get("client") != client
        or event.get("transcript_watermark") != capture
    ):
        return {**unknown, "reason": "capture_receipt_unavailable"}
    for packet in packets:
        for row in (packet.get("payload") or {}).get("archive_watermarks", []):
            if (
                row.get("path") != path
                or row.get("client") != client
                or decode_file_id(row.get("device")) != decode_file_id(capture["device"])
                or decode_file_id(row.get("inode")) != decode_file_id(capture["inode"])
                or any(row.get(key) != capture[key] for key in ("head_bytes", "head_sha256"))
            ):
                continue
            if type(row.get("size")) is not int or row["size"] < capture["size"]:
                continue
            pending = capture.get("pending_tail_bytes", 0)
            if pending and (
                row.get("pending_tail_sha256") != capture.get("pending_tail_sha256")
                or row.get("captured_tail_start", row["size"]) != capture["size"]
            ):
                continue
            proof = {"packet_id": packet["id"], "at": packet["created_at"], "captured_size": row["size"]}
            if (
                row.get("status") == "complete"
                and row.get("archived_offset", -1) >= row["size"]
                and type(row.get("source_count")) is int
                and isinstance(row.get("source_ids_hash"), str)
            ):
                sources = store.find(
                    "sources",
                    [
                        eq("project_id", job["project_id"]),
                        eq("metadata.import_path", path),
                        lt("metadata.byte_offset", row["size"]),
                    ],
                    columns=("id",),
                )
                ids = sorted(source["id"] for source in sources)
                digest = hashlib.sha256(json_text(ids).encode()).hexdigest()
                if len(ids) == row["source_count"] and digest == row["source_ids_hash"]:
                    return {"status": "captured_complete", "scope": "requested_capture", "proof_ref": proof}
                return {**unknown, "reason": "published_source_set_unavailable"}
            if row.get("status") == "partial" and row["size"] == capture["size"]:
                return {"status": "captured_blocked", "scope": "requested_capture", "proof_ref": proof}
    return {**unknown, "reason": "no_matching_published_proof"}


def public_job(store, job, *, detail=False, packets=()):
    result = {
        key: job.get(key)
        for key in (
            "id",
            "project_id",
            "kind",
            "state",
            "attempts",
            "created_at",
            "updated_at",
            "lease_until",
            "worker",
            "retry_count",
            "last_retry_at",
        )
    }
    result["attempts"] = job.get("attempts", 0)
    result["diagnostics"] = job_diagnostics(job.get("diagnostics"))
    result["has_error"] = bool(job.get("error"))
    result.update(store._job_error(job.get("error"), result["diagnostics"]))
    history = job.get("attempt_history") or []
    # Events an old completed job no longer keeps (store_prune.compact_old_jobs, #190) still count.
    pruned = job.get("attempt_history_pruned") if type(job.get("attempt_history_pruned")) is int else 0
    result["attempt_history_count"] = len(history) + pruned
    if detail:
        result["attempt_history"] = [
            {key: event.get(key) for key in ("event", "attempts", "state", "at")}
            | {
                "has_error": bool(event.get("error")),
                "diagnostics": job_diagnostics(event.get("diagnostics")),
            }
            | store._job_error(event.get("error"), job_diagnostics(event.get("diagnostics")))
            for event in history[-20:]
            if isinstance(event, dict)
        ]
        result["attempt_history_omitted"] = max(0, len(history) - 20) + pruned
    payload = job.get("payload") or {}
    result["target"] = (
        {
            key: payload[key][:1024]
            for key in ("path", "root", "client", "source_id", "chunk_id", "origin_event_id")
            if isinstance(payload.get(key), str)
        }
        if isinstance(payload, dict)
        else {}
    )
    if job["kind"] == "import":
        result["coverage"] = store._import_job_coverage(job, packets)
    return result


def public_job_error(error, diagnostics):
    """Expose actionable fixed categories; keep original exceptions in storage/export."""
    if not error:
        return {"error_code": None, "error": None}
    text = str(error).lower()
    categories = (
        (("history does not exist", "no such file or directory", "filenotfounderror"),
         "transcript_missing", "The captured transcript file was unavailable."),
        (("unknown transcript schema", "unrecognized transcript", "unsupported transcript"),
         "unknown_transcript_schema", "A transcript record did not match a supported schema."),
        (("jsondecodeerror", "invalid complete jsonl", "expecting value", "expecting property name", "invalid json"),
         "invalid_jsonl", "A complete transcript record could not be parsed as JSON."),
        (("unparsed or conflicting transcript", "unparsed transcript", "unknown_schema"),
         "unparsed_transcript_record", "A transcript record requires compatible parsing or conflict review."),
        (("another project", "outside project", "project scope", "different project"),
         "project_scope_conflict", "Transcript project attribution did not match this project."),
        (("cursor has not archived", "captured eof", "archived prefix"),
         "archive_incomplete", "The captured transcript horizon was not completely archived."),
        (("transcript eof is unknown",),
         "capture_not_initialized", "The original event did not capture a finite transcript horizon."),
        (("arcadedb",), "storage_error", "The storage request failed."),
        # The texts of ollama.OllamaUnavailable (#238); a model call that timed out is the model's, not the job's,
        # and is still called a timeout.
        (("runtime reported a context-length limit",), "model_context_length",
         "The model refused an input longer than its context length."),
        (("response read timed out", "request timed out"), "model_timeout",
         "The model runtime did not answer in time."),
        (("ollama /api/", "runtime reported", "runtime rejected", "runtime returned", "embedding model returned",
          "returned no chat message"), "model_runtime_error", "The model runtime refused or failed the request."),
        (("timeout", "timed out"), "job_timeout", "The job exceeded its execution budget."),
    )
    for needles, code, message in categories:
        if any(needle in text for needle in needles):
            return {"error_code": diagnostics.get("code", code), "error": message}
    return {
        "error_code": diagnostics.get("code", "job_error"),
        "error": "The job failed; its original error is retained in the database and export.",
    }


def retry_job(store, project_id, job_id, expected_attempts, *, include_omissions=False):
    if type(expected_attempts) is not int or expected_attempts < 0:
        raise ValueError("Expected attempts must be a nonnegative integer")
    if type(include_omissions) is not bool:
        raise ValueError("Include omissions must be a boolean")
    with store.transaction() as tx:
        store._require_project(tx, project_id)
        job = db_find_one(tx, "jobs", {"id": job_id, "project_id": project_id})
        if not job:
            raise JobNotFoundError("Unknown job in requested project")
        eligible = job["state"] == "failed" or (include_omissions and store._all_proposals_rejected(job))
        if not eligible or job.get("attempts", 0) != expected_attempts:
            raise ValueError("Job retry conflict: read the current eligible attempt before retrying")
        store._retry_job_locked(tx, job)
    return store.job_status(project_id, job_id)


def all_proposals_rejected(job):
    diagnostics = job.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return False
    counts = diagnostics.get("counts")
    codes = diagnostics.get("validation_codes")
    return (
        job.get("kind") == "extract"
        and job["state"] == "completed"
        and diagnostics.get("code") == "completed_with_omissions"
        and isinstance(counts, dict)
        and isinstance(codes, list)
        and "proposal_rejected" in codes
        and all(
            type(counts.get(key)) is int
            for key in ("candidate_records", "accepted_records", "written_records", "skipped_records")
        )
        and counts["candidate_records"] > 0
        and counts["accepted_records"] == 0
        and counts["written_records"] == 0
        and counts["skipped_records"] == counts["candidate_records"]
    )


def retry_job_locked(store, tx, job):
    now = utc_now()
    updates = {
        "state": "pending",
        "error": None,
        "lease_until": None,
        "worker": None,
        "updated_at": now,
        "last_retry_at": now,
        "retry_count": job.get("retry_count", 0) + 1,
        "attempt_history": store._job_event(job, "retry_requested"),
    }
    if job.get("error"):
        updates.update(last_error=job["error"], last_error_at=job.get("updated_at"))
    db_update_where(tx, "jobs", set_fields=updates, where={"id": job["id"], "state": job["state"]})


def count_rows(store, collection, project_id=None, extra=()):
    conds = []
    if project_id:
        conds.append(eq("project_id", project_id))
    if extra:
        conds.extend(normalize_where(extra))
    return store.count(collection, conds)


def status(store, project_id=None):
    store._require_project(store, project_id)
    counts = {
        key: store._count(key, project_id)
        for key in ("sources", "chunks", "records", "nodes", "edges", "embeddings")
    }
    counts["projects"] = 1 if project_id else store._count("projects", extra=is_null("removed_at"))
    counts["removed_projects"] = 0 if project_id else store._count("projects", extra=is_not_null("removed_at"))
    counts["jobs"] = {
        state: store._count("jobs", project_id, eq("state", state))
        for state in ("pending", "running", "completed", "failed")
    }
    counts["archived"] = counts["sources"]
    counts["indexed"] = len(
        store.find("chunks", {"project_id": project_id} if project_id else (), columns=("source_id",), distinct=True)
    )
    counts["processed"] = store._count("jobs", project_id, [eq("kind", "extract"), eq("state", "completed")])
    counts["unassigned_sources"], counts["inbox"] = (
        store._count("sources", extra=is_null("project_id")),
        store._count("ingestion_inbox"),
    )
    for collection in ("plans", "sessions", "check_runs"):
        counts[collection] = store._count(collection, project_id)
    return counts


def recent_job_errors(store, project_id=None, limit=10):
    conds = [eq("state", "failed")]
    if project_id:
        conds.append(eq("project_id", project_id))
    rows = store.find("jobs", conds, order_by=(desc("updated_at"),), limit=max(1, min(100, limit)))
    proofs = {}
    for row in rows:
        if row["kind"] == "import" and row["project_id"] not in proofs:
            proofs[row["project_id"]] = store._job_proofs(row["project_id"])
    return [store._public_job(clean_row(row), packets=proofs.get(row["project_id"], ())) for row in rows]


def retry_jobs(store):
    """Explicit legacy global compatibility; every requeue retains its audit."""
    with store.transaction() as tx:
        rows = db_find(tx, "jobs", {"state": "failed"})
        for row in rows:
            store._retry_job_locked(tx, clean_row(row))
    return {"retried": len(rows), "job_ids": [row["id"] for row in rows]}


def missing_embeddings(store, namespace, project_id=None):
    conds = [eq("is_current", True)]
    if project_id:
        conds.append(eq("project_id", project_id))
    chunks = store.find("chunks", conds, columns=("id", "project_id"))
    existing = {r["chunk_id"] for r in store.find("embeddings", {"namespace": namespace}, columns=("chunk_id",))}
    return [{"id": row["id"], "project_id": row.get("project_id")} for row in chunks if row["id"] not in existing]
