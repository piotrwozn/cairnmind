"""Self-healing quote references for Cairnmind snapshots and records.

Automatically updates file-backed quote references when code moves across commits
using Git diffs, tree-sitter AST scope/token analysis, and surrounding context matching,
while strictly enforcing content immutability via SHA-256 verification and optimistic
concurrency (CAS).

Phases:
1. Detection & change mapping: scan Git diffs (with rename detection and unified diff
   hunks), extract AST enclosing scopes, tokens, and surrounding context lines to
   locate relocated quote blocks.
2. Content integrity validation: verify that the SHA-256 checksum of the original
   normalized quote matches the located candidate verbatim; any content modification
   invalidates the reference (`stale_ref`), and partial-line edits are explicitly
   rejected without guessing (`validation_failed`). Duplicate identical blocks without
   unique surrounding/AST context raise an ambiguity conflict (`ambiguous_context`).
3. Reference rewrite & audit: atomically rewrite record/snapshot quote metadata
   (new `commit_sha`, `line_start`, `line_end`, `path`), bump the record version under
   optimistic concurrency CAS (`expected_version`), and write an immutable audit log
   entry (`heal://<uuid>`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cairnmind.memory.quote_healing_apply import (
    _ensure_healed_file_source as _ensure_healed_file_source,
)
from cairnmind.memory.quote_healing_apply import (
    _plan_records_healing as _plan_records_healing,
)
from cairnmind.memory.quote_healing_apply import (
    _relative_source_path as _relative_source_path,
)
from cairnmind.memory.quote_healing_apply import (
    _select_quote_source as _select_quote_source,
)
from cairnmind.memory.quote_healing_apply import (
    apply_healing_plan as apply_healing_plan,
)
from cairnmind.memory.quote_healing_apply import (
    heal_snapshot_package_impl,
)
from cairnmind.memory.quote_healing_git import (
    _AST_SCOPE_KINDS as _AST_SCOPE_KINDS,
)
from cairnmind.memory.quote_healing_git import (
    _FALLBACK_TOKEN as _FALLBACK_TOKEN,
)
from cairnmind.memory.quote_healing_git import (
    _FILE_KINDS as _FILE_KINDS,
)
from cairnmind.memory.quote_healing_git import (
    _HEX_COMMIT as _HEX_COMMIT,
)
from cairnmind.memory.quote_healing_git import (
    _HUNK_HEADER as _HUNK_HEADER,
)
from cairnmind.memory.quote_healing_git import (
    _MEANINGFUL_TOKEN as _MEANINGFUL_TOKEN,
)
from cairnmind.memory.quote_healing_git import (
    _TREE_SITTER_LANGUAGES as _TREE_SITTER_LANGUAGES,
)
from cairnmind.memory.quote_healing_git import (
    CONTEXT_WINDOW as CONTEXT_WINDOW,
)
from cairnmind.memory.quote_healing_git import (
    FAILURE_STATUSES as FAILURE_STATUSES,
)
from cairnmind.memory.quote_healing_git import (
    HEAL_AUDIT_ORIGIN as HEAL_AUDIT_ORIGIN,
)
from cairnmind.memory.quote_healing_git import (
    HEAL_AUDIT_PREFIX as HEAL_AUDIT_PREFIX,
)
from cairnmind.memory.quote_healing_git import (
    HEAL_STATUSES as HEAL_STATUSES,
)
from cairnmind.memory.quote_healing_git import (
    MAX_HEAL_FILE_BYTES as MAX_HEAL_FILE_BYTES,
)
from cairnmind.memory.quote_healing_git import (
    CodeToken as CodeToken,
)
from cairnmind.memory.quote_healing_git import (
    DiffHunk as DiffHunk,
)
from cairnmind.memory.quote_healing_git import (
    HealingPlan as HealingPlan,
)
from cairnmind.memory.quote_healing_git import (
    HealOutcome as HealOutcome,
)
from cairnmind.memory.quote_healing_git import (
    PlannedHealAction as PlannedHealAction,
)
from cairnmind.memory.quote_healing_git import (
    QuoteAmbiguityConflict as QuoteAmbiguityConflict,
)
from cairnmind.memory.quote_healing_git import (
    QuoteCASConflict as QuoteCASConflict,
)
from cairnmind.memory.quote_healing_git import (
    _byte_to_char_offsets as _byte_to_char_offsets,
)
from cairnmind.memory.quote_healing_git import (
    _git_run as _git_run,
)
from cairnmind.memory.quote_healing_git import (
    _line_tokens as _line_tokens,
)
from cairnmind.memory.quote_healing_git import (
    _meaningful_tokens as _meaningful_tokens,
)
from cairnmind.memory.quote_healing_git import (
    _parse_tree_sitter as _parse_tree_sitter,
)
from cairnmind.memory.quote_healing_git import (
    _scope_label as _scope_label,
)
from cairnmind.memory.quote_healing_git import (
    ast_enclosing_scope as ast_enclosing_scope,
)
from cairnmind.memory.quote_healing_git import (
    commit_exists as commit_exists,
)
from cairnmind.memory.quote_healing_git import (
    compute_line_hunks as compute_line_hunks,
)
from cairnmind.memory.quote_healing_git import (
    git_diff_hunks as git_diff_hunks,
)
from cairnmind.memory.quote_healing_git import (
    git_read_file as git_read_file,
)
from cairnmind.memory.quote_healing_git import (
    is_healing_audit_source as is_healing_audit_source,
)
from cairnmind.memory.quote_healing_git import (
    normalize_newlines as normalize_newlines,
)
from cairnmind.memory.quote_healing_git import (
    parse_unified_diff_hunks as parse_unified_diff_hunks,
)
from cairnmind.memory.quote_healing_git import (
    project_line_range as project_line_range,
)
from cairnmind.memory.quote_healing_git import (
    resolve_commit as resolve_commit,
)
from cairnmind.memory.quote_healing_git import (
    scan_git_changes as scan_git_changes,
)
from cairnmind.memory.quote_healing_git import (
    sha256_quote as sha256_quote,
)
from cairnmind.memory.quote_healing_git import (
    tokenize_code as tokenize_code,
)
from cairnmind.memory.quote_healing_git import (
    validate_quote_checksum as validate_quote_checksum,
)
from cairnmind.memory.quote_healing_locate import (
    _context_lines as _context_lines,
)
from cairnmind.memory.quote_healing_locate import (
    _context_overlap as _context_overlap,
)
from cairnmind.memory.quote_healing_locate import (
    _context_score as _context_score,
)
from cairnmind.memory.quote_healing_locate import (
    _detect_partial_modification as _detect_partial_modification,
)
from cairnmind.memory.quote_healing_locate import (
    _find_exact_occurrences as _find_exact_occurrences,
)
from cairnmind.memory.quote_healing_locate import (
    _find_token_matches as _find_token_matches,
)
from cairnmind.memory.quote_healing_locate import (
    _is_partial_line_edit as _is_partial_line_edit,
)
from cairnmind.memory.quote_healing_locate import (
    locate_and_validate_quote as locate_and_validate_quote,
)
from cairnmind.storage.sql import clean_row
from cairnmind.storage.storage_query import db_find, db_find_one, desc, eq, is_in


def plan_snapshot_healing(
    payload: dict[str, list[dict[str, Any]]],
    root: str | Path,
    *,
    origin_root: str | None = None,
    root_aliases: list[str] | tuple[str, ...] = (),
    snapshot_id: str | None = None,
    target_commit: str = "HEAD",
) -> HealingPlan:
    """Plan Phase 1 + Phase 2 quote healing for a portable memory payload prior to import."""
    sources_by_id = {row["id"]: row for row in payload.get("sources", []) if isinstance(row, dict) and "id" in row}
    origin_roots = [r for r in [origin_root, *root_aliases, str(Path(root).expanduser().resolve())] if isinstance(r, str)]
    return _plan_records_healing(
        payload.get("records", []),
        sources_by_id,
        Path(root),
        origin_roots=origin_roots,
        snapshot_id=snapshot_id,
        target_commit=target_commit,
    )


def heal_project_quotes(
    store,
    project_id: str,
    worktree: str | Path | None = None,
    *,
    target_commit: str = "HEAD",
    dry_run: bool = False,
    expected_versions: dict[str, int] | None = None,
    expected_snapshot_id: str | None = None,
    record_ids: list[str] | tuple[str, ...] | None = None,
    actor: str = "self_healing_engine",
) -> dict[str, Any]:
    """Run quote self-healing on a project's stored records against `target_commit`.

    Phase 1 & 2 (Git diff + tree-sitter AST + SHA-256 verification) execute outside
    the database writer lock; Phase 3 commits atomically under optimistic concurrency CAS.
    """
    from cairnmind.memory.memory import MemoryManager
    from cairnmind.memory.memory_paths import memory_bindings, nested_reserved

    project, root = MemoryManager(store)._scope(project_id, worktree)
    pid = project["id"]
    if expected_versions is not None and (
        not isinstance(expected_versions, dict)
        or any(not isinstance(k, str) or type(v) is not int or v < 1 for k, v in expected_versions.items())
    ):
        raise ValueError("expected_versions must map record IDs to positive integer versions")

    records = [r for r in store.list_records(pid) if record_ids is None or r["id"] in set(record_ids)]
    source_ids = sorted({
        sid for r in records for sid in (
            *(r.get("source_ids") or []),
            ((r.get("metadata") or {}).get("quote_ref") or {}).get("source_id"),
            ((r.get("metadata") or {}).get("quote_ref") or {}).get("original_source_id"),
        )
        if isinstance(sid, str)
    })
    sources_by_id: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(source_ids), 500):
        for row in db_find(
            store,
            "sources",
            [eq("project_id", pid), is_in("id", source_ids[offset:offset + 500])],
        ):
            cleaned = clean_row(row)
            sources_by_id[cleaned["id"]] = cleaned

    bindings = memory_bindings(store, pid)
    origin_roots = [str(root), str(project["root"]), *[str(origin) for origin, _ in bindings]]
    nested = nested_reserved(store, root)
    tip = db_find_one(
        store,
        "memory_snapshots",
        {"project_id": pid},
        columns=("snapshot_id",),
        order_by=(desc("created_at"), desc("id")),
    )

    plan = _plan_records_healing(
        records,
        sources_by_id,
        root,
        origin_roots=origin_roots,
        snapshot_id=tip["snapshot_id"] if tip else None,
        target_commit=target_commit,
        nested=nested,
    )
    if dry_run:
        return {"project_id": pid, "worktree": str(root), "dry_run": True, **plan.preview()}

    # Validate explicit expected_versions even when no actions remain (strict CAS check).
    with store.transaction() as tx:
        if expected_versions:
            for rec_id, exp_ver in expected_versions.items():
                current = db_find_one(
                    tx, "records", {"id": rec_id, "project_id": pid}, columns=("version",),
                )
                if not current or current["version"] != exp_ver:
                    raise QuoteCASConflict(
                        f"Optimistic concurrency CAS conflict for record {rec_id}: "
                        f"expected version {exp_ver}, found {current['version'] if current else 'missing'}."
                    )
        applied = apply_healing_plan(
            store, tx, pid, plan,
            expected_versions=expected_versions,
            expected_snapshot_id=expected_snapshot_id,
            actor=actor,
        )
    return {"project_id": pid, "worktree": str(root), "dry_run": False, **applied}


def heal_snapshot_package(
    snapshot_path: str | Path,
    root: str | Path,
    *,
    expected_snapshot_id: str | None = None,
    target_commit: str = "HEAD",
    dry_run: bool = False,
    actor: str = "snapshot_package_healer",
) -> dict[str, Any]:
    """Heal quote references directly in a portable snapshot package under manifest CAS.

    Rewrites snapshot record metadata (new `commit_sha`, `line_start`, `line_end`),
    appends immutable `record_revisions` + `heal://` audit sources, recomputes object
    hashes and `state_hash`, and replaces `manifest.json` atomically only if the manifest
    on disk still matches the expected snapshot bytes (optimistic concurrency CAS).
    """
    return heal_snapshot_package_impl(
        snapshot_path,
        root,
        plan_snapshot_fn=plan_snapshot_healing,
        expected_snapshot_id=expected_snapshot_id,
        target_commit=target_commit,
        dry_run=dry_run,
        actor=actor,
    )


def healing_audit_log(
    store,
    project_id: str,
    *,
    record_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return immutable quote self-healing audit log entries, newest first."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Audit log limit must be between 1 and 1000")
    project = store.resolve_project(project_id)
    pid = project["id"]
    where = [
        eq("project_id", pid),
        eq("kind", "manual"),
        eq("metadata.healing_audit", True),
    ]
    if record_id:
        where.append(eq("metadata.record_id", record_id))
    rows = db_find(
        store,
        "sources",
        where,
        columns=("id", "uri", "kind", "content", "metadata", "created_at"),
        order_by=(desc("created_at"), desc("id")),
        limit=limit,
    )
    entries: list[dict[str, Any]] = []
    for row in rows:
        cleaned = clean_row(row)
        if not is_healing_audit_source(cleaned):
            continue
        try:
            doc = json.loads(cleaned["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(doc, dict):
            entries.append({"audit_source_id": cleaned["id"], "uri": cleaned["uri"], **doc})
    return entries
