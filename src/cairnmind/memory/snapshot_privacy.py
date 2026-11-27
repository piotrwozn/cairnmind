"""Snapshot privacy engine: team/full profiles, quote-by-reference, fail-closed secrets scan,
opt-in redaction, rebase-resilient reference hydration, and deterministic snapshot merge.
"""
from __future__ import annotations

from copy import copy, deepcopy
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

from cairnmind.scip.ignore_rules import IgnoreMatcher, escapes_root, is_git_worktree
from cairnmind.memory.memory_published_release import CONFIG, config_text, registered_secrets
from cairnmind.memory.memory_published_secrets import (
    HIGH_ENTROPY, Rules, SecretRulesError, load_rules, redact_text, scan_text, value_sha256,
)

PROFILE_TEAM = "team"
PROFILE_FULL = "full"
PROFILES = (PROFILE_TEAM, PROFILE_FULL)
TRANSCRIPT_SOURCE_KINDS = frozenset({"transcript", "transcript_unknown", "agent_transcript"})
FILE_SOURCE_KINDS = frozenset({"code", "document", "file"})
REF_EVIDENCE_TYPES = frozenset({"git_ref", "working_tree_ref", "transcript_ref_omitted"})
_TRANSCRIPT_METADATA_TEXT_KEYS = frozenset({
    "normalized_text", "indexed_text", "prompt", "messages", "raw_line", "raw_text", "transcript_text",
})
_STRUCTURAL_SKIP_KEYS = frozenset({
    "id", "project_id", "record_id", "plan_id", "source_id", "from_id", "to_id", "task_id",
    "profile_source_id", "snapshot_id", "parent_id", "sha256", "content_sha256", "commit_sha",
    "content_hash", "state_hash", "evidence_type", "evidence_status", "kind", "status",
    "created_at", "updated_at", "started_at", "ended_at", "origin",
})
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")


class SnapshotPrivacyError(ValueError):
    """Export refused by the snapshot privacy gate (secrets, uncommitted files, or ignored paths)."""


def validate_profile_options(profile: str, include_transcripts: bool) -> str:
    if profile not in PROFILES:
        raise ValueError(f"Unsupported snapshot profile {profile!r}; choose 'team' or 'full'.")
    if profile == PROFILE_TEAM and include_transcripts:
        raise ValueError("--include-transcripts requires --profile full.")
    if profile == PROFILE_FULL and not include_transcripts:
        raise ValueError("Full profile requires explicit --include-transcripts to include raw transcripts and full sources.")
    return profile


def _git_bytes(root: Path, *args: str, data: bytes | None = None) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(root), *args],
            input=data, capture_output=True, timeout=60,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _head_tree_blobs(root: Path) -> tuple[str | None, dict[str, tuple[str, str]]]:
    """Return `(head_commit, {rel_posix: (mode, oid)})` for HEAD, or `(None, {})`."""
    raw_head = _git_bytes(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if not raw_head:
        return None, {}
    head = raw_head.decode("ascii", "replace").strip()
    if not _COMMIT_SHA.fullmatch(head):
        return None, {}
    raw_tree = _git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", head)
    if raw_tree is None:
        return head, {}
    prefix_raw = _git_bytes(root, "rev-parse", "--show-prefix")
    prefix = prefix_raw.decode("utf-8", "surrogateescape").strip("\n") if prefix_raw else ""
    entries: dict[str, tuple[str, str]] = {}
    for entry in raw_tree.split(b"\0"):
        if not entry:
            continue
        meta, _, name_bytes = entry.partition(b"\t")
        parts = meta.decode("ascii", "replace").split()
        if len(parts) < 3:
            continue
        mode, kind, oid = parts[0], parts[1], parts[2]
        name = name_bytes.decode("utf-8", "surrogateescape")
        if prefix:
            if not name.startswith(prefix):
                continue
            name = name[len(prefix):]
        if kind == "blob":
            entries[name] = (mode, oid)
    return head, entries


def _batch_read_blobs(root: Path, oids: list[str]) -> dict[str, bytes]:
    unique = sorted(set(oids))
    if not unique:
        return {}
    raw = _git_bytes(root, "cat-file", "--batch", data=("".join(oid + "\n" for oid in unique)).encode("ascii"))
    if raw is None:
        return {}
    blobs: dict[str, bytes] = {}
    pos = 0
    while pos < len(raw):
        end = raw.index(b"\n", pos)
        header = raw[pos:end].decode("ascii", "replace").split()
        pos = end + 1
        if len(header) != 3 or header[1] == "missing":
            continue
        size = int(header[2])
        blobs[header[0]] = raw[pos:pos + size]
        pos += size + 1
    return blobs


def _blob_at_commit(root: Path, commit_sha: str, rel_posix: str) -> bytes | None:
    if not _COMMIT_SHA.fullmatch(commit_sha) or not _safe_rel_posix(rel_posix):
        return None
    prefix_raw = _git_bytes(root, "rev-parse", "--show-prefix")
    prefix = prefix_raw.decode("utf-8", "surrogateescape").strip("\n") if prefix_raw else ""
    spec = f"{commit_sha}:{prefix}{rel_posix}"
    return _git_bytes(root, "cat-file", "-p", spec)


def _safe_rel_posix(rel_path: str) -> bool:
    if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path:
        return False
    p = PurePosixPath(rel_path)
    return not p.is_absolute() and ".." not in p.parts


def _source_rel_path(source: dict[str, Any], root: Path) -> str | None:
    """Resolve the project-relative POSIX path for a file-backed source, or None."""
    meta = source.get("metadata") or {}
    if isinstance(meta.get("relative_path"), str) and _safe_rel_posix(meta["relative_path"]):
        return meta["relative_path"]
    candidate = meta.get("path") if isinstance(meta.get("path"), str) else source.get("uri")
    if not isinstance(candidate, str) or "://" in candidate:
        return None
    try:
        path = Path(candidate)
        if not path.is_absolute():
            return candidate.replace("\\", "/") if _safe_rel_posix(candidate.replace("\\", "/")) else None
        resolved_root = root.expanduser().resolve()
        # Lexical relative check first so symlinks are not silently followed outside root
        if path.is_relative_to(resolved_root):
            return path.relative_to(resolved_root).as_posix()
        resolved = path.resolve()
        if resolved.is_relative_to(resolved_root):
            return resolved.relative_to(resolved_root).as_posix()
    except (OSError, ValueError):
        return None
    return None


def _referenced_source_ids(payload: dict[str, list[dict[str, Any]]]) -> set[str]:
    referenced: set[str] = set()

    def collect_record(row: dict[str, Any]):
        referenced.update(row.get("source_ids") or [])
        meta = row.get("metadata") or {}
        for field in ("test_source_ids", "review_source_ids", "acceptance_source_ids"):
            referenced.update(meta.get(field) or [])

    for row in payload.get("records", []):
        collect_record(row)
    for rev in payload.get("record_revisions", []):
        if isinstance(rev.get("snapshot"), dict):
            collect_record(rev["snapshot"])
    for plan in payload.get("plans", []):
        referenced.update(plan.get("source_ids") or [])
    for rev in payload.get("plan_revisions", []):
        snap = rev.get("snapshot") or {}
        if isinstance(snap.get("plan"), dict):
            referenced.update(snap["plan"].get("source_ids") or [])
        for task in snap.get("tasks") or []:
            if isinstance(task, dict):
                collect_record(task)
    for run in payload.get("check_runs", []):
        if run.get("profile_source_id"):
            referenced.add(run["profile_source_id"])
        if isinstance(run.get("source_ids"), dict):
            referenced.update(run["source_ids"].values())
    for edge in payload.get("edges", []):
        if edge.get("source_id"):
            referenced.add(edge["source_id"])
        for endpoint in (edge.get("from_id"), edge.get("to_id")):
            if isinstance(endpoint, str) and endpoint.startswith("source:"):
                referenced.add(endpoint.removeprefix("source:"))
    return referenced


def _drop_sources(payload: dict[str, list[dict[str, Any]]], dropped_ids: set[str]) -> None:
    if not dropped_ids:
        return
    dropped_nodes = {f"source:{sid}" for sid in dropped_ids}
    payload["sources"] = [s for s in payload["sources"] if s["id"] not in dropped_ids]
    payload["nodes"] = [
        n for n in payload["nodes"]
        if n["id"] not in dropped_nodes and n.get("source_id") not in dropped_ids
    ]
    payload["edges"] = [
        e for e in payload["edges"]
        if e.get("from_id") not in dropped_nodes and e.get("to_id") not in dropped_nodes
        and e.get("source_id") not in dropped_ids
    ]


def snapshot_rules(root: Path, settings=None, *, include_registered: bool = True) -> Rules:
    """Load `.cairnmind/publication.toml` rules, optionally with this machine's registered secrets."""
    try:
        cfg = config_text(root)
        reg = registered_secrets(settings) if (include_registered and settings is not None) else ()
        return load_rules(cfg, reg)
    except SecretRulesError as exc:
        raise SnapshotPrivacyError(f"Snapshot refused: {CONFIG}: {exc}") from None


_PATH_LEAF_KEYS = frozenset({
    "path", "uri", "root", "worktree", "origin_root", "local_root", "file_path", "relative_path",
})


def _blob_matches_sha(blob: bytes | None, expected_sha: str) -> bytes | None:
    """Return matching blob bytes (allowing CRLF/LF equivalence from Git core.autocrlf) or None."""
    if blob is None:
        return None
    if hashlib.sha256(blob).hexdigest() == expected_sha:
        return blob
    if b"\r" not in blob and b"\n" in blob:
        crlf = blob.replace(b"\n", b"\r\n")
        if hashlib.sha256(crlf).hexdigest() == expected_sha:
            return crlf
    if b"\r\n" in blob:
        lf = blob.replace(b"\r\n", b"\n")
        if hashlib.sha256(lf).hexdigest() == expected_sha:
            return lf
    return None


def _is_filesystem_path_field(table: str, field_path: str, value: str, row: dict[str, Any]) -> bool:
    """True for structural filesystem path/URI fields where OS temp-dir names must not trigger entropy hits."""
    if value.startswith("file://"):
        return True
    has_sep = "/" in value or "\\" in value
    if not has_sep:
        return False
    leaf = field_path.rsplit(".", 1)[-1]
    if leaf in _PATH_LEAF_KEYS:
        return True
    return table == "nodes" and field_path == "label" and row.get("kind") == "source"


def scan_and_optionally_redact_payload(
    payload: dict[str, list[dict[str, Any]]],
    rules: Rules,
    *,
    redact: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Scan every text value in a snapshot payload for secrets/PII.

    When `redact=False`: returns `(hits, {})` without mutating `payload`.
    When `redact=True`: mutates `payload` in place, updates `source["sha256"]` for modified inline sources,
    and returns `(hits, redaction_counts)`.
    Never includes raw secret values in returned hits.
    """
    hits: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    path_rules = copy(rules)
    path_rules.entropy_bits = 1e9

    def walk_value(table: str, row_id: str, field_path: str, value: Any, row: dict[str, Any]) -> Any:
        if isinstance(value, str):
            active_rules = path_rules if _is_filesystem_path_field(table, field_path, value, row) else rules
            found = scan_text(value, active_rules)
            if active_rules is path_rules:
                found = [(kind, secret_val) for kind, secret_val in found if kind != HIGH_ENTROPY]
            if not found:
                return value
            for kind, secret_val in found:
                hits.append({
                    "table": table,
                    "id": row_id,
                    "field": field_path,
                    "kind": kind,
                    "value_sha256": value_sha256(secret_val),
                })
            if redact:
                masked, local_counts = redact_text(value, active_rules)
                for tag, n in local_counts.items():
                    counts[tag] = counts.get(tag, 0) + n
                return masked
            return value
        if isinstance(value, list):
            return [walk_value(table, row_id, f"{field_path}[{idx}]", item, row) for idx, item in enumerate(value)]
        if isinstance(value, dict):
            updated = {}
            for key, item in value.items():
                if key in _STRUCTURAL_SKIP_KEYS:
                    updated[key] = item
                else:
                    sub = f"{field_path}.{key}" if field_path else key
                    updated[key] = walk_value(table, row_id, sub, item, row)
            return updated
        return value

    for table, rows in payload.items():
        new_rows = []
        for row in rows:
            row_id = str(row.get("id", ""))
            scanned = walk_value(table, row_id, "", row, row)
            if redact and table == "sources":
                ev_type = scanned.get("evidence_type")
                if ev_type not in REF_EVIDENCE_TYPES:
                    scanned["sha256"] = hashlib.sha256(scanned["content"].encode()).hexdigest()
            new_rows.append(scanned)
        if redact:
            payload[table] = new_rows
    if redact and counts:
        rec_labels = {f"record:{r['id']}": r["content"][:1000] for r in payload.get("records", [])}
        plan_labels = {f"plan:{p['id']}": p["title"] for p in payload.get("plans", [])}
        for node in payload.get("nodes", []):
            if node["id"] in rec_labels:
                node["label"] = rec_labels[node["id"]]
            elif node["id"] in plan_labels:
                node["label"] = plan_labels[node["id"]]
    return hits, counts


def apply_snapshot_privacy(
    payload: dict[str, list[dict[str, Any]]],
    root: Path,
    *,
    profile: str = PROFILE_TEAM,
    include_transcripts: bool = False,
    skip_uncommitted: bool = False,
    redact: bool = False,
    settings=None,
) -> dict[str, Any]:
    """Transform `payload` in place according to `profile` and enforce the fail-closed secrets gate.

    Returns privacy metadata dict: `{"profile", "contains_transcripts", "redacted", "redaction_counts",
    "git_refs", "skipped_uncommitted"}`.
    """
    validate_profile_options(profile, include_transcripts)
    root = Path(root).expanduser().resolve()
    git_refs = 0
    skipped_uncommitted: list[str] = []

    if profile == PROFILE_TEAM:
        referenced = _referenced_source_ids(payload)
        dropped_ids: set[str] = set()
        matcher = IgnoreMatcher(root) if root.is_dir() else None
        in_git = root.is_dir() and is_git_worktree(root)
        head_commit, head_tree = _head_tree_blobs(root) if in_git else (None, {})

        # Pre-check ignored paths in one batch for all file sources
        file_rel_by_id: dict[str, str] = {}
        for source in payload["sources"]:
            if source.get("kind") in FILE_SOURCE_KINDS:
                rel = _source_rel_path(source, root)
                if rel is not None:
                    file_rel_by_id[source["id"]] = rel
        ignored_rels = matcher.ignored_set(list(file_rel_by_id.values())) if (matcher and file_rel_by_id) else set()

        # Batch-load HEAD blobs needed for quote-by-reference comparison
        needed_oids = [
            head_tree[rel][1]
            for sid, rel in file_rel_by_id.items()
            if rel in head_tree and head_tree[rel][0] != "120000" and rel not in ignored_rels
        ]
        head_blobs = _batch_read_blobs(root, needed_oids) if (in_git and needed_oids) else {}

        uncommitted_paths: list[str] = []
        ignored_referenced: list[str] = []

        for source in payload["sources"]:
            sid = source["id"]
            kind = source.get("kind")

            # 1. Strip raw transcripts/prompts from team snapshots
            if kind in TRANSCRIPT_SOURCE_KINDS:
                if sid not in referenced:
                    dropped_ids.add(sid)
                    continue
                original_sha = source.get("sha256") or hashlib.sha256(str(source.get("content", "")).encode()).hexdigest()
                source["content"] = ""
                source["sha256"] = original_sha
                source["evidence_type"] = "transcript_ref_omitted"
                source["content_sha256"] = original_sha
                meta = dict(source.get("metadata") or {})
                for key in list(meta):
                    if key in _TRANSCRIPT_METADATA_TEXT_KEYS:
                        meta.pop(key, None)
                meta["transcript_omitted"] = True
                source["metadata"] = meta
                continue

            # 2. File-backed sources: check ignore rules & convert to git_ref / working_tree_ref
            if kind in FILE_SOURCE_KINDS and sid in file_rel_by_id:
                rel = file_rel_by_id[sid]
                if rel in ignored_rels:
                    if sid not in referenced:
                        dropped_ids.add(sid)
                        continue
                    ignored_referenced.append(rel)
                    continue
                if not in_git:
                    continue  # Non-git directory: keep inline content (still scanned for secrets below)
                content = source.get("content", "")
                original_sha = source.get("sha256") or hashlib.sha256(content.encode()).hexdigest()
                matched_commit: str | None = None
                if rel in head_tree and head_tree[rel][0] != "120000":
                    blob = head_blobs.get(head_tree[rel][1])
                    if _blob_matches_sha(blob, original_sha) is not None:
                        matched_commit = head_commit
                if matched_commit is None:
                    hint_commit = (source.get("metadata") or {}).get("git_commit")
                    if isinstance(hint_commit, str) and _COMMIT_SHA.fullmatch(hint_commit):
                        blob = _blob_at_commit(root, hint_commit, rel)
                        if _blob_matches_sha(blob, original_sha) is not None:
                            matched_commit = hint_commit
                if matched_commit is not None:
                    line_count = max(1, len(content.splitlines())) if content else 1
                    source["content"] = ""
                    source["sha256"] = original_sha
                    source["evidence_type"] = "git_ref"
                    source["file_path"] = rel
                    source["commit_sha"] = matched_commit
                    source["line_start"] = 1
                    source["line_end"] = line_count
                    source["content_sha256"] = original_sha
                    git_refs += 1
                else:
                    if not skip_uncommitted:
                        uncommitted_paths.append(rel)
                    elif sid not in referenced:
                        dropped_ids.add(sid)
                        skipped_uncommitted.append(rel)
                    else:
                        source["content"] = ""
                        source["sha256"] = original_sha
                        source["evidence_type"] = "working_tree_ref"
                        source["file_path"] = rel
                        source["content_sha256"] = original_sha
                        skipped_uncommitted.append(rel)

        if ignored_referenced:
            listed = ", ".join(sorted(set(ignored_referenced))[:10])
            raise SnapshotPrivacyError(
                f"Snapshot blocked: referenced files are ignored by .gitignore or .cairnmindignore ({listed})."
            )
        if uncommitted_paths:
            listed = ", ".join(sorted(set(uncommitted_paths))[:10])
            raise SnapshotPrivacyError(
                f"Snapshot blocked: uncommitted or untracked files have no stable commit SHA ({listed}). "
                "Commit your changes or pass --skip-uncommitted."
            )
        _drop_sources(payload, dropped_ids)

    # 3. Fail-closed secrets scan (both team and full profiles)
    rules = snapshot_rules(root, settings, include_registered=True)
    hits, redaction_counts = scan_and_optionally_redact_payload(payload, rules, redact=redact)
    if hits and not redact:
        summary = "; ".join(
            f"{h['table']}/{h['id']}: {h['kind'].replace('_', ' ')} in {h['field']}"
            for h in hits[:5]
        )
        more = f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""
        raise SnapshotPrivacyError(
            f"Snapshot blocked by secrets gate ({len(hits)} hit(s)): {summary}{more}. "
            "Remove the secret, add an allowlist entry in .cairnmind/publication.toml, or pass --redact."
        )

    contains_transcripts = profile == PROFILE_FULL and include_transcripts
    return {
        "profile": profile,
        "contains_transcripts": contains_transcripts,
        "redacted": bool(redact and redaction_counts),
        "redaction_counts": redaction_counts,
        "git_refs": git_refs,
        "skipped_uncommitted": sorted(set(skipped_uncommitted)),
    }


def resolve_snapshot_refs(payload: dict[str, list[dict[str, Any]]], root: Path) -> list[dict[str, Any]]:
    """Hydrate `git_ref` sources against local `root`, marking unresolvable/mismatched ones `stale_ref`.

    Handles rewritten Git history (`rebase`, `amend`, `squash`, `force-push`): if `commit_sha` no longer
    exists in `root`, checks `file_path` + `content_sha256` at current `HEAD` (or working tree if no HEAD).
    Never trusts blindly when content hashes disagree.
    """
    root = Path(root).expanduser().resolve()
    in_git = root.is_dir() and is_git_worktree(root)
    head_commit, head_tree = _head_tree_blobs(root) if in_git else (None, {})
    stale_refs: list[dict[str, Any]] = []

    for source in payload.get("sources", []):
        if source.get("evidence_type") != "git_ref":
            continue
        rel = source.get("file_path")
        commit_sha = source.get("commit_sha")
        expected_sha = source.get("content_sha256") or source.get("sha256")
        meta = dict(source.get("metadata") or {})
        meta["evidence_ref"] = {
            "evidence_type": "git_ref",
            "file_path": rel,
            "commit_sha": commit_sha,
            "line_start": source.get("line_start"),
            "line_end": source.get("line_end"),
            "content_sha256": expected_sha,
        }
        resolved_text: str | None = None
        resolution_mode: str | None = None

        if in_git and isinstance(rel, str) and isinstance(commit_sha, str):
            matched_blob = _blob_matches_sha(_blob_at_commit(root, commit_sha, rel), expected_sha)
            if matched_blob is not None:
                try:
                    resolved_text = matched_blob.decode("utf-8")
                    resolution_mode = "commit_exact"
                except UnicodeDecodeError:
                    resolved_text = None

        if resolved_text is None and isinstance(rel, str) and _safe_rel_posix(rel):
            # Fallback after rebase/amend/squash: check file_path + content_sha256 at HEAD
            head_blob: bytes | None = None
            if in_git and head_commit and rel in head_tree and head_tree[rel][0] != "120000":
                head_blob = _blob_at_commit(root, head_commit, rel)
            elif not in_git:
                local_file = root / rel
                if local_file.is_file() and not local_file.is_symlink() and not escapes_root(local_file, root):
                    try:
                        head_blob = local_file.read_bytes()
                    except OSError:
                        head_blob = None
            matched_head = _blob_matches_sha(head_blob, expected_sha)
            if matched_head is not None:
                try:
                    resolved_text = matched_head.decode("utf-8")
                    resolution_mode = "head_matched"
                except UnicodeDecodeError:
                    resolved_text = None

        if resolved_text is not None:
            source["content"] = resolved_text
            meta["evidence_status"] = resolution_mode
        else:
            source["content"] = ""
            meta["evidence_status"] = "stale_ref"
            meta["evidence_type"] = "stale_ref"
            stale_refs.append({
                "source_id": source["id"],
                "file_path": rel,
                "commit_sha": commit_sha,
                "content_sha256": expected_sha,
                "status": "stale_ref",
            })
        source["metadata"] = meta

    return stale_refs


def merge_snapshots(ours_path: str | Path, theirs_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Deterministically merge two snapshot packages by entity version and fingerprint.

    Resolves branch conflicts in `.memory/cairnmind/` without losing immutable revisions or sources.
    Raises `MemoryConflict` if both sides edited the same entity at the same version with different content.
    """
    from cairnmind.memory.memory import (
        FORMAT, TABLES, VERSION, MemoryConflict, _atomic_write, _bytes, _descriptors, _digest,
        _load_snapshot, _manifest_id, _state_hash, _sync_directory, _validate, _without_rebuilt_graph,
        _write_whole,
    )
    from cairnmind.storage.sql import utc_now

    _, _, ours_manifest, ours_payload = _load_snapshot(ours_path)
    _, _, theirs_manifest, theirs_payload = _load_snapshot(theirs_path)
    if ours_manifest["project"]["id"] != theirs_manifest["project"]["id"]:
        raise MemoryConflict("Cannot merge snapshots from different projects.")
    project_id = ours_manifest["project"]["id"]
    merged: dict[str, list[dict[str, Any]]] = {table: [] for table in TABLES}

    # 1. Immutable collections: union by ID (must match if present in both)
    for table in ("sources", "record_revisions", "plan_revisions", "check_runs"):
        combined: dict[str, dict[str, Any]] = {}
        for row in ours_payload[table]:
            combined[row["id"]] = deepcopy(row)
        for row in theirs_payload[table]:
            rid = row["id"]
            if rid in combined:
                if table == "sources":
                    if combined[rid].get("sha256") != row.get("sha256") or combined[rid].get("uri") != row.get("uri"):
                        raise MemoryConflict(f"Conflicting immutable source {rid} during snapshot merge.")
                    # Keep whichever is_current is False if one side superseded it
                    combined[rid]["is_current"] = bool(combined[rid].get("is_current") and row.get("is_current"))
                elif combined[rid] != row:
                    raise MemoryConflict(f"Conflicting immutable {table} row {rid} during snapshot merge.")
            else:
                combined[rid] = deepcopy(row)
        merged[table] = sorted(combined.values(), key=lambda r: r["id"])

    # Ensure at most one is_current source per URI
    by_uri: dict[str, list[dict[str, Any]]] = {}
    for src in merged["sources"]:
        if src.get("is_current"):
            by_uri.setdefault(src["uri"], []).append(src)
    for uri, group in by_uri.items():
        if len(group) > 1:
            group.sort(key=lambda r: (str(r.get("created_at", "")), r["id"]))
            for older in group[:-1]:
                older["is_current"] = False

    # 2. Versioned heads (records & plans): higher version wins; same version must match
    for table in ("records", "plans"):
        combined = {row["id"]: deepcopy(row) for row in ours_payload[table]}
        for row in theirs_payload[table]:
            rid = row["id"]
            if rid not in combined:
                combined[rid] = deepcopy(row)
            else:
                left = combined[rid]
                if row["version"] > left["version"]:
                    combined[rid] = deepcopy(row)
                elif row["version"] == left["version"] and row != left:
                    raise MemoryConflict(
                        f"Concurrent edit conflict on {table[:-1]} {rid} at version {row['version']}."
                    )
        merged[table] = sorted(combined.values(), key=lambda r: r["id"])

    # 3. Rebuild canonical nodes/edges from merged heads + union of extra non-owned nodes/edges
    nodes_map: dict[str, dict[str, Any]] = {}
    for row in (*ours_payload["nodes"], *theirs_payload["nodes"]):
        nodes_map[row["id"]] = deepcopy(row)
    for src in merged["sources"]:
        nid = f"source:{src['id']}"
        nodes_map.setdefault(nid, {
            "id": nid, "project_id": project_id, "kind": "source",
            "label": src["uri"], "source_id": src["id"], "metadata": {"uri": src["uri"], "kind": src["kind"]},
        })
    owned_edge_keys: set[tuple[str, str]] = set()
    canonical_edges: list[dict[str, Any]] = []
    for rec in merged["records"]:
        nid = f"record:{rec['id']}"
        nodes_map[nid] = {
            "id": nid, "project_id": project_id, "kind": rec["kind"],
            "label": rec["content"][:1000], "source_id": rec["source_ids"][0],
            "metadata": {**rec["metadata"], "record_id": rec["id"], "status": rec["status"]},
        }
        for ek in ("supported_by", "depends_on", "supersedes"):
            owned_edge_keys.add((nid, ek))
        for sid in rec["source_ids"]:
            canonical_edges.append({
                "id": _digest([project_id, nid, "supported_by", f"source:{sid}", sid]),
                "project_id": project_id, "from_id": nid, "to_id": f"source:{sid}",
                "kind": "supported_by", "source_id": sid, "status": rec["status"],
            })
        for dep in rec["metadata"].get("depends_on", []):
            if f"record:{dep}" in nodes_map or any(r["id"] == dep for r in merged["records"]):
                canonical_edges.append({
                    "id": _digest([project_id, nid, "depends_on", f"record:{dep}", rec["source_ids"][0]]),
                    "project_id": project_id, "from_id": nid, "to_id": f"record:{dep}",
                    "kind": "depends_on", "source_id": rec["source_ids"][0], "status": rec["status"],
                })
        if rec.get("supersedes"):
            canonical_edges.append({
                "id": _digest([project_id, nid, "supersedes", f"record:{rec['supersedes']}", rec["source_ids"][0]]),
                "project_id": project_id, "from_id": nid, "to_id": f"record:{rec['supersedes']}",
                "kind": "supersedes", "source_id": rec["source_ids"][0], "status": rec["status"],
            })
    for plan in merged["plans"]:
        nid = f"plan:{plan['id']}"
        srcs = plan["source_ids"]
        last_src = srcs[-1] if srcs else None
        nodes_map[nid] = {
            "id": nid, "project_id": project_id, "kind": "plan",
            "label": plan["title"], "source_id": last_src, "metadata": {"plan_id": plan["id"]},
        }
        for ek in ("supported_by", "has_step"):
            owned_edge_keys.add((nid, ek))
        for sid in srcs:
            canonical_edges.append({
                "id": _digest([project_id, nid, "supported_by", f"source:{sid}", sid]),
                "project_id": project_id, "from_id": nid, "to_id": f"source:{sid}",
                "kind": "supported_by", "source_id": sid, "status": "proposed",
            })
        for step in plan["step_ids"]:
            canonical_edges.append({
                "id": _digest([project_id, nid, "has_step", f"record:{step}", last_src]),
                "project_id": project_id, "from_id": nid, "to_id": f"record:{step}",
                "kind": "has_step", "source_id": last_src, "status": "proposed",
            })

    edges_map: dict[str, dict[str, Any]] = {}
    for edge in (*ours_payload["edges"], *theirs_payload["edges"]):
        if (edge.get("from_id"), edge.get("kind")) not in owned_edge_keys:
            edges_map[edge["id"]] = deepcopy(edge)
    for edge in canonical_edges:
        edges_map[edge["id"]] = edge
    merged["nodes"] = sorted(nodes_map.values(), key=lambda r: r["id"])
    merged["edges"] = sorted(edges_map.values(), key=lambda r: r["id"])
    merged = _without_rebuilt_graph(merged)

    active_plan = ours_manifest.get("active_plan_id") or theirs_manifest.get("active_plan_id")
    if active_plan not in {p["id"] for p in merged["plans"]}:
        active_plan = None
    _validate(merged, project_id, active_plan)

    objects = _descriptors(merged)
    aliases = sorted(set([
        ours_manifest["origin_root"], theirs_manifest["origin_root"],
        *ours_manifest.get("root_aliases", []), *theirs_manifest.get("root_aliases", []),
    ]))
    ancestors = list(dict.fromkeys([
        ours_manifest["snapshot_id"], theirs_manifest["snapshot_id"],
        *ours_manifest.get("ancestors", []), *theirs_manifest.get("ancestors", []),
    ]))[:4096]
    contains_transcripts = bool(
        ours_manifest.get("contains_transcripts") or theirs_manifest.get("contains_transcripts")
    )
    profile = PROFILE_FULL if contains_transcripts else PROFILE_TEAM
    redacted = bool(ours_manifest.get("redacted") or theirs_manifest.get("redacted"))
    captured = {
        "project": deepcopy(ours_manifest["project"]),
        "origin_root": ours_manifest["origin_root"],
        "root_aliases": aliases,
        "active_plan_id": active_plan,
        "counts": {table: len(merged[table]) for table in TABLES},
        "state_hash": _state_hash(merged),
        "profile": profile,
        "contains_transcripts": contains_transcripts,
        "redacted": redacted,
        "processing": deepcopy(ours_manifest.get("processing")),
        "completeness": deepcopy(ours_manifest.get("completeness")),
    }
    content_hash = _digest({
        **{k: v for k, v in captured.items() if k not in {"processing", "completeness"}},
        "objects": objects,
    })
    manifest = {
        "format": FORMAT, "format_version": VERSION, **captured, "objects": objects,
        "created_at": utc_now(), "parent_id": ours_manifest["snapshot_id"],
        "ancestors": ancestors, "content_hash": content_hash,
    }
    manifest["snapshot_id"] = _manifest_id(manifest)
    raw = _bytes(manifest)

    out_dir = Path(output_dir).expanduser().absolute()
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for table, rows in merged.items():
        for descriptor, row in zip(objects[table], rows, strict=True):
            target = out_dir / descriptor["path"]
            data = _bytes(row)
            if not target.exists():
                _write_whole(target, data)
    if any(objects.values()):
        _sync_directory(out_dir / "objects")
    _atomic_write(out_dir / "manifests" / f"{manifest['snapshot_id']}.json", raw)
    _atomic_write(out_dir / "manifest.json", raw)
    return {
        "status": "merged",
        "path": str(out_dir / "manifest.json"),
        "snapshot_id": manifest["snapshot_id"],
        "parents": [ours_manifest["snapshot_id"], theirs_manifest["snapshot_id"]],
        "counts": manifest["counts"],
        "profile": profile,
        "contains_transcripts": contains_transcripts,
    }
