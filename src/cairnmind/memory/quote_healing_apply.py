"""Phase 1+2 planning and Phase 3 CAS application for quote self-healing (store and snapshot packages)."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from cairnmind.memory.memory_paths import is_reserved_memory
from cairnmind.memory.quote_healing_git import (
    _FILE_KINDS,
    HEAL_AUDIT_ORIGIN,
    HEAL_AUDIT_PREFIX,
    DiffHunk,
    HealingPlan,
    HealOutcome,
    PlannedHealAction,
    QuoteCASConflict,
    commit_exists,
    git_diff_hunks,
    git_read_file,
    normalize_newlines,
    resolve_commit,
    scan_git_changes,
)
from cairnmind.memory.quote_healing_locate import locate_and_validate_quote
from cairnmind.pal.terminal_safe import safe_text
from cairnmind.storage.sql import json_text, utc_now
from cairnmind.storage.storage_query import db_find_one, db_update_where, desc


def _relative_source_path(source: dict[str, Any], origin_roots: list[str]) -> str | None:
    """Resolve a file source's project-relative POSIX path without touching foreign host symlinks."""
    raw_meta = source.get("metadata")
    metadata = raw_meta if isinstance(raw_meta, dict) else {}
    rel = metadata.get("relative_path")
    if isinstance(rel, str) and rel.strip():
        candidate = PurePosixPath(rel.replace("\\", "/"))
        if not candidate.is_absolute() and ".." not in candidate.parts and not is_reserved_memory(candidate.as_posix()):
            return candidate.as_posix()
    raw_path = metadata.get("path") or source.get("uri")
    if not isinstance(raw_path, str) or not raw_path.strip() or "://" in raw_path:
        return None
    norm_path = os.path.normpath(raw_path).replace("\\", "/")
    candidates = [metadata.get("root"), *origin_roots]
    for base in candidates:
        if not isinstance(base, str) or not base.strip():
            continue
        norm_base = os.path.normpath(base).replace("\\", "/").rstrip("/")
        if norm_path.startswith(norm_base + "/"):
            rel_part = norm_path[len(norm_base) + 1:]
            p = PurePosixPath(rel_part)
            if not p.is_absolute() and ".." not in p.parts and not is_reserved_memory(p.as_posix()):
                return p.as_posix()
    return None


def _select_quote_source(
    record: dict[str, Any],
    sources_by_id: dict[str, dict[str, Any]],
    origin_roots: list[str],
) -> tuple[dict[str, Any], str, str, int | None, int | None, str | None] | None:
    """Return `(source, relative_path, quote_text, old_start, old_end, old_commit)` for a record, or None."""
    if record.get("status") == "superseded":
        return None
    raw_meta = record.get("metadata")
    metadata = raw_meta if isinstance(raw_meta, dict) else {}
    quote = metadata.get("evidence")
    if not isinstance(quote, str) or not quote.strip():
        return None
    norm_quote = normalize_newlines(quote)

    # If the record already carries a quote_ref, prefer that pinned source and line coordinates.
    existing_ref = metadata.get("quote_ref")
    if isinstance(existing_ref, dict):
        src_id = existing_ref.get("source_id")
        if isinstance(src_id, str) and src_id in sources_by_id:
            source = sources_by_id[src_id]
            if source.get("kind") in _FILE_KINDS:
                rel = existing_ref.get("path") or _relative_source_path(source, origin_roots)
                if isinstance(rel, str) and rel:
                    raw_s_meta = source.get("metadata")
                    s_meta = raw_s_meta if isinstance(raw_s_meta, dict) else {}
                    old_commit = existing_ref.get("commit_sha") or s_meta.get("git_commit")
                    return (
                        source, rel, norm_quote,
                        existing_ref.get("line_start") if isinstance(existing_ref.get("line_start"), int) else None,
                        existing_ref.get("line_end") if isinstance(existing_ref.get("line_end"), int) else None,
                        old_commit if isinstance(old_commit, str) else None,
                    )

    # Otherwise derive from the cited file source that literally contains the quote.
    pinned_lines = metadata.get("evidence_lines")
    if (isinstance(pinned_lines, list) and len(pinned_lines) == 2
            and all(type(x) is int and x >= 1 for x in pinned_lines)):
        pinned_start: int | None = pinned_lines[0]
        pinned_end: int | None = pinned_lines[1]
    else:
        pinned_start, pinned_end = None, None

    for source_id in dict.fromkeys(record.get("source_ids") or []):
        source = sources_by_id.get(source_id)
        if not isinstance(source, dict) or source.get("kind") not in _FILE_KINDS:
            continue
        rel = _relative_source_path(source, origin_roots)
        if not rel:
            continue
        body = normalize_newlines(str(source.get("content") or ""))
        if norm_quote not in body:
            continue
        raw_s_meta = source.get("metadata")
        s_meta = raw_s_meta if isinstance(raw_s_meta, dict) else {}
        old_commit = s_meta.get("git_commit")
        return (source, rel, norm_quote, pinned_start, pinned_end, old_commit if isinstance(old_commit, str) else None)
    return None


def _plan_records_healing(
    records: list[dict[str, Any]],
    sources_by_id: dict[str, dict[str, Any]],
    root: Path,
    *,
    origin_roots: list[str],
    snapshot_id: str | None = None,
    target_commit: str = "HEAD",
    nested: tuple[Path, ...] = (),
) -> HealingPlan:
    """Compute Phase 1 + Phase 2 healing plan for records against Git `target_commit` in `root`."""
    resolved_root = Path(root).expanduser().resolve()
    target_sha = resolve_commit(resolved_root, target_commit)
    if target_sha is None:
        return HealingPlan(
            root=resolved_root,
            target_commit_sha=None,
            snapshot_id=snapshot_id,
            actions=[],
            unchanged=[],
            skipped_reason="not_a_git_repository",
        )

    diff_cache: dict[str, dict[str, dict[str, Any]]] = {}
    blob_cache: dict[tuple[str, str], str | None] = {}
    hunk_cache: dict[tuple[str, str, str | None], list[DiffHunk]] = {}
    actions: list[PlannedHealAction] = []
    unchanged: list[str] = []

    def changes_since(old_sha: str) -> dict[str, dict[str, Any]]:
        if old_sha not in diff_cache:
            diff_cache[old_sha] = scan_git_changes(resolved_root, old_sha, target_sha, nested)
        return diff_cache[old_sha]

    def read_blob(commit: str, rel_path: str) -> str | None:
        key = (commit, rel_path)
        if key not in blob_cache:
            blob_cache[key] = git_read_file(resolved_root, commit, rel_path)
        return blob_cache[key]

    def read_hunks(old_sha: str, old_path: str, new_path: str | None) -> list[DiffHunk]:
        key = (old_sha, old_path, new_path)
        if key not in hunk_cache:
            hunk_cache[key] = git_diff_hunks(resolved_root, old_sha, target_sha, old_path, new_path)
        return hunk_cache[key]

    for record in sorted(records, key=lambda r: str(r.get("id", ""))):
        selected = _select_quote_source(record, sources_by_id, origin_roots)
        if selected is None:
            continue
        source, old_path, quote_text, old_start, old_end, old_commit = selected
        old_content = str(source.get("content") or "")
        old_norm = normalize_newlines(old_content)
        old_sha256 = hashlib.sha256(old_content.encode("utf-8")).hexdigest()

        # Check Git diff between old_commit and target_sha when old_commit is reachable.
        new_path: str | None = old_path
        hunks: list[DiffHunk] | None = None
        if old_commit and commit_exists(resolved_root, old_commit) and old_commit != target_sha:
            changed = changes_since(old_commit)
            entry = changed.get(old_path)
            if entry is not None:
                new_path = entry.get("new_path")
                if new_path is not None:
                    # Use Git unified diff hunks when the archived source matches old_commit's blob.
                    committed_old = read_blob(old_commit, old_path)
                    if committed_old is not None and normalize_newlines(committed_old) == old_norm:
                        hunks = read_hunks(old_commit, old_path, new_path)
        new_content = read_blob(target_sha, new_path) if new_path is not None else None

        # If target file has identical content to the cited archived source, nothing drifted.
        if new_content is not None and new_path == old_path:
            new_sha256 = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
            if new_sha256 == old_sha256 or normalize_newlines(new_content) == old_norm:
                unchanged.append(record["id"])
                continue

        # Also skip if record already has a quote_ref recorded for this exact target_commit_sha and path.
        existing_ref = (record.get("metadata") or {}).get("quote_ref")
        if (isinstance(existing_ref, dict)
                and existing_ref.get("commit_sha") == target_sha
                and existing_ref.get("checked_commit_sha", target_sha) == target_sha
                and existing_ref.get("status") == "healed"
                and existing_ref.get("path") == new_path):
            unchanged.append(record["id"])
            continue

        outcome = locate_and_validate_quote(
            old_content=old_content,
            new_content=new_content,
            quote_text=quote_text,
            old_line_start=old_start,
            old_line_end=old_end,
            old_path=old_path,
            new_path=new_path,
            old_commit_sha=old_commit,
            new_commit_sha=target_sha,
            diff_hunks=hunks,
        )
        if outcome.status == "unchanged":
            unchanged.append(record["id"])
            continue
        actions.append(PlannedHealAction(
            record_id=record["id"],
            expected_version=record["version"],
            old_source_id=source["id"],
            new_file_content=new_content,
            new_file_kind=source.get("kind") or "code",
            outcome=outcome,
        ))

    return HealingPlan(
        root=resolved_root,
        target_commit_sha=target_sha,
        snapshot_id=snapshot_id,
        actions=actions,
        unchanged=unchanged,
    )


def _build_audit_doc(
    *,
    outcome: HealOutcome,
    record_id: str,
    project_id: str,
    snapshot_id: str | None,
    old_source_id: str,
    new_source_id: str | None,
    old_version: int,
    new_version: int,
    actor: str,
    now: str,
) -> dict[str, Any]:
    return {
        "action": "quote_self_heal",
        "outcome": outcome.status,
        "ref_status": outcome.ref_status,
        "stale_ref": outcome.stale_ref,
        "validation_status": outcome.validation_status,
        "requires_intervention": outcome.requires_intervention,
        "record_id": record_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "source_id": old_source_id,
        "new_source_id": new_source_id,
        "previous_version": old_version,
        "new_version": new_version,
        "old_path": outcome.old_path,
        "new_path": outcome.new_path,
        "old_commit_sha": outcome.old_commit_sha,
        "new_commit_sha": outcome.new_commit_sha,
        "old_lines": [outcome.old_line_start, outcome.old_line_end] if outcome.old_line_start else None,
        "new_lines": [outcome.new_line_start, outcome.new_line_end] if outcome.new_line_start else None,
        "quote_sha256": outcome.quote_sha256,
        "candidate_sha256": outcome.candidate_sha256,
        "candidate_lines": [[s, e] for s, e in outcome.candidate_lines],
        "strategy": outcome.strategy,
        "reason": outcome.reason,
        "actor": actor,
        "at": now,
    }


def _build_audit_metadata(
    *,
    record_id: str,
    outcome: HealOutcome,
    snapshot_id: str | None,
    actor: str,
) -> dict[str, Any]:
    return {
        "origin": HEAL_AUDIT_ORIGIN,
        "healing_audit": True,
        "record_id": record_id,
        "outcome": outcome.status,
        "ref_status": outcome.ref_status,
        "snapshot_id": snapshot_id,
        "actor": actor,
    }


def _build_quote_ref(
    *,
    outcome: HealOutcome,
    old_source_id: str,
    new_source_id: str | None,
    audit_source_id: str,
    now: str,
) -> dict[str, Any]:
    quote_ref: dict[str, Any] = {
        "source_id": new_source_id or old_source_id,
        "original_source_id": old_source_id,
        "path": outcome.new_path if outcome.status == "healed" and outcome.new_path else outcome.old_path,
        "target_path": outcome.new_path,
        "commit_sha": outcome.new_commit_sha if outcome.status == "healed" else outcome.old_commit_sha,
        "checked_commit_sha": outcome.new_commit_sha,
        "line_start": outcome.new_line_start if outcome.status == "healed" else outcome.old_line_start,
        "line_end": outcome.new_line_end if outcome.status == "healed" else outcome.old_line_end,
        "quote_sha256": outcome.quote_sha256,
        "candidate_sha256": outcome.candidate_sha256,
        "candidate_lines": [[s, e] for s, e in outcome.candidate_lines],
        "status": outcome.status,
        "ref_status": outcome.ref_status,
        "stale_ref": outcome.stale_ref,
        "validation_status": outcome.validation_status,
        "requires_intervention": outcome.requires_intervention,
        "strategy": outcome.strategy,
        "reason": outcome.reason,
        "healed_at": now,
        "audit_source_id": audit_source_id,
    }
    if outcome.status == "healed":
        quote_ref["previous_commit_sha"] = outcome.old_commit_sha
        quote_ref["previous_path"] = outcome.old_path
        quote_ref["previous_lines"] = (
            [outcome.old_line_start, outcome.old_line_end] if outcome.old_line_start else None
        )
    return quote_ref


def _ensure_healed_file_source(
    store, tx, project_id: str, root: Path, action: PlannedHealAction
) -> dict[str, Any]:
    """Create or reuse the current file source row for a healed quote location inside `tx`."""
    from cairnmind.scip.parsing import analyze_code, parser_identity

    if action.new_file_content is None or action.outcome.new_path is None:
        raise ValueError("Healed file source requires new_file_content and new_path")
    abs_path = (root / action.outcome.new_path).resolve()
    uri = str(abs_path)
    content = action.new_file_content
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    current_key = json_text([project_id, uri])
    existing = db_find_one(tx, "sources", {"current_key": current_key, "is_current": True})
    if existing and existing.get("sha256") == digest:
        return existing
    if existing:
        db_update_where(tx, "sources", set_fields={"is_current": False, "current_key": None}, where={"id": existing["id"]})
        store.drop_chunks(tx, [existing["id"]])
        store._delete_old_vectors(tx, [existing["id"]])
    analysis = analyze_code(abs_path, content)
    kind = "code" if analysis["language"] != "text" else (action.new_file_kind if action.new_file_kind in _FILE_KINDS else "document")
    metadata = {
        "path": uri,
        "root": str(root),
        "relative_path": action.outcome.new_path,
        "git_commit": action.outcome.new_commit_sha,
        "content_hash": digest,
        "analysis": analysis,
        "analysis_version": parser_identity(),
        "language": analysis["language"],
        "snapshot": "git_commit",
    }
    return store.new_source(tx, project_id, uri, content, kind, metadata)


def apply_healing_plan(
    store,
    tx,
    project_id: str,
    plan: HealingPlan,
    *,
    expected_versions: dict[str, int] | None = None,
    expected_snapshot_id: str | None = None,
    actor: str = "self_healing_engine",
) -> dict[str, Any]:
    """Phase 3: apply planned healing updates under optimistic concurrency CAS + immutable audit log."""
    if expected_snapshot_id is not None:
        tip = db_find_one(
            tx,
            "memory_snapshots",
            {"project_id": project_id},
            columns=("snapshot_id",),
            order_by=(desc("created_at"), desc("id")),
        )
        imp = db_find_one(
            tx,
            "memory_imports",
            {"project_id": project_id},
            columns=("snapshot_id",),
            order_by=(desc("imported_at"), desc("id")),
        )
        current_tip = (tip or imp or {}).get("snapshot_id")
        if current_tip != expected_snapshot_id:
            raise QuoteCASConflict(
                f"Optimistic concurrency CAS conflict: expected snapshot {expected_snapshot_id}, "
                f"found {current_tip or 'none'}."
            )

    grouped: dict[str, list[dict[str, Any]]] = {
        "healed": [], "stale_ref": [], "validation_failed": [], "ambiguous_context": [],
    }
    audit_entries: list[dict[str, Any]] = []

    for action in plan.actions:
        outcome = action.outcome
        old = db_find_one(tx, "records", {"id": action.record_id, "project_id": project_id})
        expected_ver = (
            expected_versions[action.record_id]
            if expected_versions and action.record_id in expected_versions
            else action.expected_version
        )
        if not old:
            raise QuoteCASConflict(f"CAS conflict: record {action.record_id} no longer exists.")
        if type(expected_ver) is not int or old["version"] != expected_ver:
            raise QuoteCASConflict(
                f"Optimistic concurrency CAS conflict for record {action.record_id}: "
                f"expected version {expected_ver}, found version {old['version']}."
            )

        now = utc_now()
        new_version = old["version"] + 1
        new_source_id: str | None = None
        sources = list(dict.fromkeys(old.get("source_ids") or []))
        if outcome.status == "healed" and action.new_file_content is not None and outcome.new_path is not None:
            new_source = _ensure_healed_file_source(store, tx, project_id, plan.root, action)
            new_source_id = new_source["id"]
            if new_source_id not in sources:
                sources.append(new_source_id)

        audit_doc = _build_audit_doc(
            outcome=outcome, record_id=old["id"], project_id=project_id, snapshot_id=plan.snapshot_id,
            old_source_id=action.old_source_id, new_source_id=new_source_id,
            old_version=old["version"], new_version=new_version, actor=actor, now=now,
        )
        audit_source = store.new_source(
            tx,
            project_id,
            f"{HEAL_AUDIT_PREFIX}{uuid4()}",
            json_text(audit_doc),
            "manual",
            _build_audit_metadata(record_id=old["id"], outcome=outcome, snapshot_id=plan.snapshot_id, actor=actor),
        )
        quote_ref = _build_quote_ref(
            outcome=outcome, old_source_id=action.old_source_id, new_source_id=new_source_id,
            audit_source_id=audit_source["id"], now=now,
        )

        update_payload: dict[str, Any] = {"id": old["id"], "expected_version": expected_ver}
        if old["kind"] == "task" and (old.get("metadata") or {}).get("plan_ref"):
            plan_row = db_find_one(
                tx,
                "plans",
                {"id": old["metadata"]["plan_ref"], "project_id": project_id},
                columns=("version",),
            )
            if plan_row:
                update_payload["expected_plan_version"] = plan_row["version"]

        try:
            saved = store._remember(tx, project_id, update_payload, sources, quote_ref=quote_ref)
        except ValueError as exc:
            if "version conflict" in str(exc).lower():
                raise QuoteCASConflict(str(exc)) from None
            raise

        audit_entries.append({"audit_source_id": audit_source["id"], **audit_doc})
        summary_entry = {
            "record_id": saved["id"],
            "previous_version": old["version"],
            "version": saved["version"],
            "audit_source_id": audit_source["id"],
            **outcome.as_dict(),
        }
        if outcome.status in grouped:
            grouped[outcome.status].append(summary_entry)

    status = (
        "skipped" if plan.skipped_reason
        else "unchanged" if not plan.actions
        else "completed"
    )
    result: dict[str, Any] = {
        "status": status,
        "target_commit_sha": plan.target_commit_sha,
        "unchanged_count": len(plan.unchanged),
        **grouped,
        "audit_entries": audit_entries,
    }
    if plan.skipped_reason:
        result["reason"] = plan.skipped_reason
    return result


def heal_snapshot_package_impl(
    snapshot_path: str | Path,
    root: str | Path,
    *,
    plan_snapshot_fn: Callable[..., HealingPlan],
    expected_snapshot_id: str | None = None,
    target_commit: str = "HEAD",
    dry_run: bool = False,
    actor: str = "snapshot_package_healer",
) -> dict[str, Any]:
    """Heal quote references directly in a portable snapshot package under manifest CAS."""
    from cairnmind.memory import memory as portable

    path, previous_raw, manifest, payload = portable._load_snapshot(snapshot_path)
    snapshot_id = manifest["snapshot_id"]
    if expected_snapshot_id is not None and expected_snapshot_id != snapshot_id:
        raise QuoteCASConflict(
            f"Snapshot CAS conflict: expected snapshot {expected_snapshot_id}, found {snapshot_id}."
        )
    project_id = manifest["project"]["id"]
    plan = plan_snapshot_fn(
        payload,
        root,
        origin_root=manifest.get("origin_root"),
        root_aliases=manifest.get("root_aliases", ()),
        snapshot_id=snapshot_id,
        target_commit=target_commit,
    )
    if dry_run or not plan.actions:
        return {"snapshot_id": snapshot_id, "dry_run": dry_run, **plan.preview()}

    updated_payload = deepcopy(payload)
    records_by_id = {r["id"]: r for r in updated_payload["records"]}
    sources_by_id = {s["id"]: s for s in updated_payload["sources"]}
    nodes_by_id = {n["id"]: n for n in updated_payload["nodes"]}
    edges_by_id = {e["id"]: e for e in updated_payload["edges"]}

    grouped: dict[str, list[dict[str, Any]]] = {
        "healed": [], "stale_ref": [], "validation_failed": [], "ambiguous_context": [],
    }
    audit_entries: list[dict[str, Any]] = []

    for action in plan.actions:
        outcome = action.outcome
        rec = records_by_id[action.record_id]
        if rec["version"] != action.expected_version:
            raise QuoteCASConflict(
                f"Snapshot record CAS conflict for {rec['id']}: "
                f"expected version {action.expected_version}, found {rec['version']}."
            )
        now = utc_now()
        old_version = rec["version"]
        new_version = old_version + 1
        new_source_id: str | None = None
        if outcome.status == "healed" and action.new_file_content is not None and outcome.new_path is not None:
            abs_path = str((plan.root / outcome.new_path).resolve())
            content = action.new_file_content
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            # Deactivate any existing current source with the same URI in the package.
            for existing_src in updated_payload["sources"]:
                if existing_src.get("uri") == abs_path and existing_src.get("is_current"):
                    existing_src["is_current"] = False
            new_source = {
                "id": str(uuid4()),
                "project_id": project_id,
                "uri": abs_path,
                "kind": action.new_file_kind if action.new_file_kind in _FILE_KINDS else "code",
                "content": content,
                "sha256": digest,
                "metadata": {
                    "path": abs_path,
                    "root": str(plan.root),
                    "relative_path": outcome.new_path,
                    "git_commit": outcome.new_commit_sha,
                    "content_hash": digest,
                    "snapshot": "git_commit",
                },
                "is_current": True,
                "created_at": now,
            }
            updated_payload["sources"].append(new_source)
            sources_by_id[new_source["id"]] = new_source
            new_source_id = new_source["id"]
            if new_source_id not in rec["source_ids"]:
                rec["source_ids"].append(new_source_id)
            src_node_id = f"source:{new_source_id}"
            if src_node_id not in nodes_by_id:
                src_node = {
                    "id": src_node_id, "project_id": project_id, "kind": "source",
                    "label": abs_path, "source_id": new_source_id,
                    "metadata": {"source_id": new_source_id, "source_kind": new_source["kind"]},
                }
                updated_payload["nodes"].append(src_node)
                nodes_by_id[src_node_id] = src_node
            rec_node_id = f"record:{rec['id']}"
            edge_id = hashlib.sha256(f"{rec_node_id}\0supported_by\0{src_node_id}".encode()).hexdigest()
            if edge_id not in edges_by_id:
                edge = {
                    "id": edge_id, "project_id": project_id,
                    "from_id": rec_node_id, "to_id": src_node_id,
                    "kind": "supported_by", "source_id": new_source_id, "status": "observed",
                }
                updated_payload["edges"].append(edge)
                edges_by_id[edge_id] = edge

        audit_doc = _build_audit_doc(
            outcome=outcome, record_id=rec["id"], project_id=project_id, snapshot_id=snapshot_id,
            old_source_id=action.old_source_id, new_source_id=new_source_id,
            old_version=old_version, new_version=new_version, actor=actor, now=now,
        )
        audit_text = json_text(audit_doc)
        audit_source = {
            "id": str(uuid4()),
            "project_id": project_id,
            "uri": f"{HEAL_AUDIT_PREFIX}{uuid4()}",
            "kind": "manual",
            "content": audit_text,
            "sha256": hashlib.sha256(audit_text.encode("utf-8")).hexdigest(),
            "metadata": _build_audit_metadata(record_id=rec["id"], outcome=outcome, snapshot_id=snapshot_id, actor=actor),
            "is_current": True,
            "created_at": now,
        }
        updated_payload["sources"].append(audit_source)

        quote_ref = _build_quote_ref(
            outcome=outcome, old_source_id=action.old_source_id, new_source_id=new_source_id,
            audit_source_id=audit_source["id"], now=now,
        )

        rec["version"] = new_version
        rec["updated_at"] = now
        rec["metadata"] = {**(rec.get("metadata") or {}), "quote_ref": quote_ref}
        updated_payload["record_revisions"].append({
            "id": f"{rec['id']}:{new_version}",
            "project_id": project_id,
            "record_id": rec["id"],
            "version": new_version,
            "snapshot": deepcopy(rec),
            "recorded_at": now,
        })
        rec_node = nodes_by_id.get(f"record:{rec['id']}")
        if rec_node is not None:
            rec_node["metadata"] = {**rec["metadata"], "record_id": rec["id"], "status": rec["status"]}

        audit_entries.append({"audit_source_id": audit_source["id"], **audit_doc})
        if outcome.status in grouped:
            grouped[outcome.status].append({
                "record_id": rec["id"],
                "previous_version": old_version,
                "version": new_version,
                "audit_source_id": audit_source["id"],
                **outcome.as_dict(),
            })

    for table in portable.TABLES:
        updated_payload[table].sort(key=lambda r: r["id"])
    portable._validate(updated_payload, project_id, manifest.get("active_plan_id"))

    objects = portable._descriptors(updated_payload)
    ancestors = list(dict.fromkeys([snapshot_id, *manifest.get("ancestors", [])]))[:4096]
    new_manifest = {
        **manifest,
        "parent_id": snapshot_id,
        "ancestors": ancestors,
        "counts": {table: len(updated_payload[table]) for table in portable.TABLES},
        "state_hash": portable._state_hash(updated_payload),
        "objects": objects,
        "created_at": utc_now(),
    }
    captured_subset = {
        k: v for k, v in new_manifest.items()
        if k not in {"format", "format_version", "objects", "created_at", "parent_id", "ancestors",
                     "content_hash", "snapshot_id", "processing", "completeness"}
    }
    new_manifest["content_hash"] = portable._digest({**captured_subset, "objects": objects})
    new_manifest.pop("snapshot_id", None)
    new_manifest["snapshot_id"] = portable._manifest_id(new_manifest)
    raw = portable._bytes(new_manifest)

    folder = path.parent
    for table, rows in updated_payload.items():
        for descriptor, row in zip(objects[table], rows, strict=True):
            target = folder / descriptor["path"]
            data = portable._bytes(row)
            if not target.exists():
                portable._write_whole(target, data)
    portable._sync_directory(folder / "objects")
    portable._atomic_write(folder / "manifests" / f"{new_manifest['snapshot_id']}.json", raw)

    # CAS check immediately before replacing the mutable manifest pointer.
    current_raw = portable._read(path, portable.MAX_MANIFEST_BYTES)
    if current_raw != previous_raw:
        raise QuoteCASConflict(
            f"Snapshot CAS conflict: manifest at {safe_text(str(path))} was modified concurrently."
        )
    portable._atomic_write(path, raw)
    return {
        "status": "completed",
        "previous_snapshot_id": snapshot_id,
        "snapshot_id": new_manifest["snapshot_id"],
        "target_commit_sha": plan.target_commit_sha,
        "unchanged_count": len(plan.unchanged),
        **grouped,
        "audit_entries": audit_entries,
    }
