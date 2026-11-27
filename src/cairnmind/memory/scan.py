"""Serverless, read-only diff scan for CI (#413).

Compares a Git diff (``--diff REVISION`` or ``BASE...HEAD`` / ``BASE..HEAD``) against
records anchored to symbols (`metadata.symbol_anchor` or `relates_to` symbol links).
Read-only: starts no service or model, takes no locks, and writes nothing to the store.

Exit codes (enforced by the CLI command):
- ``0``: no anchored record has a changed or removed symbol in the diff;
- ``1``: at least one record has ``symbol_changed`` or ``symbol_gone``;
- ``2``: usage error, invalid Git revision/worktree, unknown project, or unready store.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from typing import Any

from cairnmind.memory.brief import _git_out, _line, _scoped_records, _title
from cairnmind.memory.evidence import _metadata
from cairnmind.memory.impact import _STATUS, _batches, _changes, _change_text, _new_text, _old_text, _relative
from cairnmind.memory.impact_docs import written_as_code
from cairnmind.memory.memory_paths import bound_metadata, git_pathspec, memory_bindings, nested_reserved
from cairnmind.scip.parsing import analyze_code
from cairnmind.storage.storage_query import db_find, eq, is_in
from cairnmind.memory.symbol_anchor import (
    SYMBOL_ANCHOR_KINDS,
    find_symbol_in_analysis,
    posix_path,
    validate_symbol_anchor,
)

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


def _resolve_commit(root: Path, ref: str, label: str = "revision") -> str:
    if not isinstance(ref, str) or not ref.strip() or ref.startswith(("-", "^")):
        raise ValueError(f"--diff needs a Git commit, branch, tag or range (got invalid {label})")
    commit = _git_out(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    if not commit or _COMMIT_SHA_RE.fullmatch(commit) is None:
        raise ValueError(f"Git does not know the {label} {_line(ref, 80)!r} in {_line(root)}")
    return commit


def _diff_commits(root: Path, base: str, target: str, nested=()) -> dict[str, dict[str, Any]]:
    """Diff two committed trees ``base`` -> ``target`` with rename detection."""
    pathspec = git_pathspec(nested)
    raw = _git_out(root, "diff", "--name-status", "-M", "-z", "--relative", base, target, *pathspec)
    if raw is None:
        raise ValueError("Cannot compare those Git revisions")
    parts = [part for part in raw.split("\0") if part]
    changed: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(parts):
        code = parts[index][:1]
        if code in "RC" and index + 2 < len(parts):
            old, new = parts[index + 1], parts[index + 2]
            if code == "R":
                changed[old] = {"change": "renamed", "renamed_to": new}
                changed[new] = {"change": "renamed", "renamed_from": old}
            else:
                changed[new] = {"change": "added"}
            index += 3
        elif index + 1 < len(parts):
            changed[parts[index + 1]] = {"change": _STATUS.get(code, "modified")}
            index += 2
        else:
            break
    return changed


def _parse_diff_spec(root: Path, diff_rev: str) -> tuple[str, str | None]:
    """Return ``(base_commit, target_commit_or_None)`` for a single ref or ``A...B`` / ``A..B`` range."""
    if not isinstance(diff_rev, str) or not diff_rev.strip() or diff_rev.startswith(("-", "^")):
        raise ValueError("--diff needs a Git commit, branch, tag or range")
    spec = diff_rev.strip()
    if "..." in spec:
        left, right = spec.split("...", 1)
        left_sha = _resolve_commit(root, left or "HEAD", "base revision")
        right_sha = _resolve_commit(root, right or "HEAD", "target revision")
        base = _git_out(root, "merge-base", left_sha, right_sha)
        if not base or _COMMIT_SHA_RE.fullmatch(base) is None:
            raise ValueError(f"Git found no merge-base for {_line(spec, 80)!r} in {_line(root)}")
        return base, right_sha
    if ".." in spec:
        left, right = spec.split("..", 1)
        left_sha = _resolve_commit(root, left or "HEAD", "base revision")
        right_sha = _resolve_commit(root, right or "HEAD", "target revision")
        return left_sha, right_sha
    return _resolve_commit(root, spec, "revision"), None


def _analyze_or_none(name: str, text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    analysis = analyze_code(Path(name), text)
    if analysis["language"] == "text" or any(str(err).startswith("Parser unavailable") for err in analysis["errors"]):
        return None
    return analysis


def scan_diff(store, project_id: str, diff_rev: str, worktree=None) -> dict[str, Any]:
    """Scan anchored records against a Git diff. Strictly read-only."""
    from cairnmind.memory.verification import _root

    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    if _git_out(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError(f"Git could not be read in {_line(root)}; scan needs a Git worktree")

    base_commit, target_commit = _parse_diff_spec(root, diff_rev)
    nested = nested_reserved(store, root)
    if target_commit is None:
        changed = _changes(root, base_commit, nested)
    else:
        changed = _diff_commits(root, base_commit, target_commit, nested)

    roots = {
        Path(row["path"]).resolve()
        for row in db_find(store, "project_roots", {"project_id": project["id"]}, columns=("path",))
    } | {root}
    bindings = memory_bindings(store, project["id"])

    def local_name(path: Any) -> str | None:
        name = _relative(path, root, roots)
        if name is None and bindings and isinstance(path, str):
            name = _relative(
                bound_metadata(store, project["id"], {"path": path}, bindings=bindings).get("path"),
                root,
                roots,
            )
        return name

    # Lazily parse old & new versions of touched files only when referenced by at least one record.
    old_cache: dict[str, dict[str, Any] | None] = {}
    new_cache: dict[str, dict[str, Any] | None] = {}

    def old_analysis(path: str) -> dict[str, Any] | None:
        if path not in old_cache:
            entry = changed.get(path) or {}
            if entry.get("change") == "added":
                old_cache[path] = None
            else:
                src_path = entry.get("renamed_from", path)
                old_cache[path] = _analyze_or_none(src_path, _old_text(root, base_commit, src_path))
        return old_cache[path]

    def new_analysis(path: str) -> dict[str, Any] | None:
        if path not in new_cache:
            entry = changed.get(path) or {}
            if entry.get("change") == "deleted":
                new_cache[path] = None
            else:
                dst_path = entry.get("renamed_to", path)
                text = _new_text(root, dst_path) if target_commit is None else _old_text(root, target_commit, dst_path)
                new_cache[path] = _analyze_or_none(dst_path, text)
        return new_cache[path]

    records = {
        rec["id"]: rec
        for rec in _scoped_records(store, project["id"], root)
        if rec.get("status") != "superseded"
    }

    findings: list[dict[str, Any]] = []
    seen_finding: set[tuple[str, str, str]] = set()
    unchanged_count = 0
    handled_record_ids: set[str] = set()

    # 1. Records with an explicit normalised symbol_anchor (#411, #412).
    for record_id in sorted(records, key=lambda rid: (records[rid]["kind"], rid)):
        record = records[record_id]
        own = _metadata(record)
        anchor = validate_symbol_anchor(own.get("symbol_anchor"))
        if anchor is None:
            continue
        path = posix_path(anchor["path"])
        if path not in changed:
            continue
        handled_record_ids.add(record_id)
        entry = changed[path]
        target_path = entry.get("renamed_to", path)
        if entry.get("change") == "deleted":
            state = "symbol_gone"
        else:
            analysis = new_analysis(path)
            if analysis is None:
                # Could not parse target file (binary/unsupported/missing) -> treat as gone if missing on disk.
                raw_text = _new_text(root, target_path) if target_commit is None else _old_text(root, target_commit, target_path)
                state = "symbol_gone" if raw_text is None else "symbol_changed"
            else:
                found = find_symbol_in_analysis(analysis, anchor["symbol"], anchor["kind"])
                if found is None:
                    state = "symbol_gone"
                elif found.get("body_hash") == anchor["body_hash"]:
                    state = "unchanged"
                else:
                    state = "symbol_changed"

        if state == "unchanged":
            unchanged_count += 1
            continue

        dedupe = (record_id, path, anchor["symbol"])
        if dedupe in seen_finding:
            continue
        seen_finding.add(dedupe)
        findings.append({
            "id": record_id,
            "kind": record["kind"],
            "status": record["status"],
            "title": _title(record),
            "state": state,
            "symbol": anchor["symbol"],
            "symbol_kind": anchor["kind"],
            "path": path,
            "target_path": target_path,
            "file_change": _change_text(entry),
            "via": "symbol_anchor",
        })

    # 2. Records without a symbol_anchor that still carry relates_to edges to symbol nodes.
    edges = [
        edge
        for edge in db_find(
            store,
            "edges",
            {"project_id": project["id"], "kind": "relates_to"},
            columns=("from_id", "to_id", "status"),
        )
        if str(edge.get("from_id", "")).startswith("record:")
        and edge["from_id"][7:] in records
        and edge["from_id"][7:] not in handled_record_ids
    ]
    nodes: dict[str, dict[str, Any]] = {}
    for batch in _batches(sorted({edge["to_id"] for edge in edges})):
        nodes.update({
            row["id"]: row
            for row in db_find(
                store,
                "nodes",
                [eq("project_id", project["id"]), is_in("id", batch)],
                columns=("id", "kind", "label", "metadata"),
            )
        })
    for edge in edges:
        node = nodes.get(edge["to_id"])
        if not node or node.get("kind") != "symbol":
            continue
        node_meta = _metadata(node)
        sym_kind = node_meta.get("symbol_kind")
        if sym_kind and sym_kind not in SYMBOL_ANCHOR_KINDS:
            continue
        name = local_name(node_meta.get("path") or node.get("label"))
        if name not in changed:
            continue
        record_id = edge["from_id"][7:]
        record = records[record_id]
        sym_label = str(node.get("label") or "")
        if not written_as_code(sym_label, _metadata(record).get("evidence")):
            continue
        entry = changed[name]
        old_a = old_analysis(name)
        new_a = new_analysis(name)
        old_sym = find_symbol_in_analysis(old_a, sym_label, sym_kind)
        new_sym = find_symbol_in_analysis(new_a, sym_label, sym_kind)
        if old_sym is None and new_sym is None:
            continue
        if old_sym is not None and new_sym is None:
            state = "symbol_gone"
        elif old_sym is not None and new_sym is not None:
            if old_sym.get("body_hash") == new_sym.get("body_hash"):
                unchanged_count += 1
                continue
            state = "symbol_changed"
        else:
            continue

        dedupe = (record_id, name, sym_label)
        if dedupe in seen_finding:
            continue
        seen_finding.add(dedupe)
        findings.append({
            "id": record_id,
            "kind": record["kind"],
            "status": record["status"],
            "title": _title(record),
            "state": state,
            "symbol": (old_sym.get("qualified_name") if old_sym else None) or sym_label,
            "symbol_kind": (old_sym.get("kind") if old_sym else sym_kind) or "symbol",
            "path": name,
            "target_path": entry.get("renamed_to", name),
            "file_change": _change_text(entry),
            "via": "relates_to",
        })

    findings.sort(key=lambda item: (0 if item["state"] == "symbol_gone" else 1, item["path"], item["symbol"], item["id"]))
    changed_count = sum(1 for item in findings if item["state"] == "symbol_changed")
    gone_count = sum(1 for item in findings if item["state"] == "symbol_gone")
    files = sorted(
        (name for name, entry in changed.items() if "renamed_to" not in entry),
        key=lambda item: PurePosixPath(item).parts,
    )

    target_label = target_commit[:12] if target_commit else "working tree"
    lines = [
        f"Cairnmind scan for {_line(project['name'], 60)} ({_line(root)})",
        f"Diff: {_line(diff_rev, 80)} ({base_commit[:12]} -> {target_label}); {len(files)} changed file(s)",
    ]
    if not findings:
        lines.append(
            f"Clean: 0 drifted records ({unchanged_count} anchored symbol(s) unchanged in this diff)."
        )
    else:
        lines.append(
            f"Found {len(findings)} record(s) needing review "
            f"({changed_count} symbol_changed, {gone_count} symbol_gone; {unchanged_count} unchanged):"
        )
        for item in findings:
            lines.append(
                f"  [{item['state']}] [{item['kind']}] {item['status']}: {item['title']} "
                f"({item['symbol']} in {item['path']}, {item['file_change']}) [{item['id']}]"
            )

    return {
        "project": {"id": project["id"], "name": project["name"]},
        "worktree": str(root),
        "diff": diff_rev,
        "base_commit": base_commit,
        "target_commit": target_commit,
        "changed_files": files,
        "records": findings,
        "counts": {
            "total": len(findings),
            "symbol_changed": changed_count,
            "symbol_gone": gone_count,
            "unchanged": unchanged_count,
        },
        "exit_code": 1 if findings else 0,
        "text": "\n".join(lines),
    }
