"""Offline privacy and integrity audit for `.memory/cairnmind/` and `.memory/cairnmind-published/`.

Needs no database, engine, service, or model. Exits non-zero when a snapshot carries
`contains_transcripts: true`, raw transcripts, uncommitted `working_tree_ref` entries,
secrets/PII, or files violating `.cairnmindignore`/`.gitignore`, and warns when Git history
contains an earlier commit with `"contains_transcripts": true`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from cairnmind.memory.memory import _load_snapshot
from cairnmind.memory.memory_paths import MEMORY_DIRECTORIES, PUBLISHED_RELATIVE
from cairnmind.memory.memory_published_check import check_directory
from cairnmind.memory.memory_published_release import checkout_rules, config_text
from cairnmind.memory.memory_published_secrets import Rules, load_rules
from cairnmind.memory.snapshot_privacy import (
    FILE_SOURCE_KINDS,
    TRANSCRIPT_SOURCE_KINDS,
    _source_rel_path,
    scan_and_optionally_redact_payload,
)
from cairnmind.pal.terminal_safe import safe_text
from cairnmind.scip.ignore_rules import IgnoreMatcher, is_git_worktree


def _git_history_transcript_leaks(repo_root: Path) -> list[dict[str, Any]]:
    """Inspect Git history for historical commits where a snapshot manifest had `contains_transcripts: true`."""
    if not is_git_worktree(repo_root):
        return []
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    targets = [
        (directory / "manifest.json").as_posix()
        for directory in MEMORY_DIRECTORIES
    ]
    prefix_res = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--show-prefix"],
        capture_output=True, timeout=15, env=env, check=False,
    )
    prefix = prefix_res.stdout.decode("utf-8", "surrogateescape").strip("\n") if prefix_res.returncode == 0 else ""

    leaked: list[dict[str, Any]] = []
    seen_commits: set[tuple[str, str]] = set()
    for rel in targets:
        try:
            log = subprocess.run(
                ["git", "--literal-pathspecs", "-C", str(repo_root), "log", "--all", "--format=%H", "--", rel],
                capture_output=True, timeout=30, env=env, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if log.returncode != 0:
            continue
        commits = [line.strip() for line in log.stdout.decode("ascii", "replace").splitlines() if line.strip()]
        for commit in commits[:200]:
            if (commit, rel) in seen_commits:
                continue
            seen_commits.add((commit, rel))
            try:
                blob = subprocess.run(
                    ["git", "--literal-pathspecs", "-C", str(repo_root), "cat-file", "-p", f"{commit}:{prefix}{rel}"],
                    capture_output=True, timeout=15, env=env, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if blob.returncode != 0:
                continue
            try:
                manifest = json.loads(blob.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and manifest.get("contains_transcripts") is True:
                leaked.append({"commit": commit, "path": rel})
    return leaked


def _infer_repo_root(target: Path) -> Path:
    resolved = target.expanduser().absolute()
    if resolved.is_file() and resolved.name == "manifest.json":
        resolved = resolved.parent
    if resolved.name.casefold() in {"cairnmind", "devbrain", "cairnmind-published"} and resolved.parent.name.casefold() == ".memory":
        return resolved.parent.parent
    if resolved.name.casefold() == ".memory":
        return resolved.parent
    return resolved


def _load_audit_rules(repo_root: Path, config: Path | None) -> Rules:
    if config is not None:
        return checkout_rules(repo_root, config.expanduser().absolute())
    text = config_text(repo_root)
    return load_rules(text)


def _audit_snapshot(snapshot_path: Path, repo_root: Path, rules: Rules) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    label = str(snapshot_path)
    try:
        _, _, manifest, payload = _load_snapshot(snapshot_path)
    except ValueError as exc:
        findings.append({"code": "invalid_snapshot", "path": label, "message": str(exc)})
        return findings

    if manifest.get("contains_transcripts") is True:
        findings.append({
            "code": "contains_transcripts",
            "path": label,
            "message": "Snapshot manifest sets contains_transcripts=true; do not commit or push raw transcripts.",
        })

    # Also catch raw transcript content even in older manifests without the header flag
    raw_transcripts = [
        src["id"] for src in payload.get("sources", [])
        if src.get("kind") in TRANSCRIPT_SOURCE_KINDS
        and src.get("evidence_type") != "transcript_ref_omitted"
        and bool(str(src.get("content", "")).strip())
    ]
    if raw_transcripts and manifest.get("contains_transcripts") is not True:
        findings.append({
            "code": "contains_transcripts",
            "path": label,
            "message": f"Snapshot contains {len(raw_transcripts)} raw transcript source(s) without team redaction.",
        })

    # Flag uncommitted working_tree_ref entries
    dirty_refs = [
        src.get("file_path") or src.get("uri") or src["id"]
        for src in payload.get("sources", [])
        if src.get("evidence_type") == "working_tree_ref"
    ]
    if dirty_refs:
        findings.append({
            "code": "working_tree_ref",
            "path": label,
            "message": f"Snapshot references uncommitted working-tree file(s): {', '.join(sorted(set(dirty_refs))[:10])}.",
        })

    # Secrets scan across the whole payload (never prints secret values)
    hits, _ = scan_and_optionally_redact_payload(payload, rules, redact=False)
    for hit in hits:
        findings.append({
            "code": "secret_detected",
            "path": f"{hit['table']}/{hit['id']}",
            "field": hit["field"],
            "kind": hit["kind"],
            "message": f"{hit['table']}/{hit['id']}: {hit['kind'].replace('_', ' ')} found in {hit['field']}",
        })

    # .cairnmindignore and .gitignore enforcement on file-backed sources
    if repo_root.is_dir():
        matcher = IgnoreMatcher(repo_root)
        file_rels: list[tuple[str, str]] = []
        for src in payload.get("sources", []):
            if src.get("kind") in FILE_SOURCE_KINDS or src.get("evidence_type") in {"git_ref", "working_tree_ref"}:
                rel = src.get("file_path") if isinstance(src.get("file_path"), str) else _source_rel_path(src, repo_root)
                if rel:
                    file_rels.append((src["id"], rel))
        if file_rels:
            ignored = matcher.ignored_set([rel for _, rel in file_rels])
            for sid, rel in file_rels:
                if rel in ignored:
                    findings.append({
                        "code": "ignored_path",
                        "path": rel,
                        "source_id": sid,
                        "message": f"{rel} violates .cairnmindignore or .gitignore (source {sid}).",
                    })
    return findings


def audit_memory(
    path: str | Path | None = None,
    *,
    config: str | Path | None = None,
    check_history: bool = True,
) -> dict[str, Any]:
    """Audit `.memory/cairnmind/` snapshots and `.memory/cairnmind-published/` directories.

    Raises `SecretRulesError` or `ValueError` on invalid config or non-existent target path.
    """
    target = Path(path if path is not None else ".").expanduser().absolute()
    if not target.exists():
        raise ValueError(f"{target} does not exist")
    repo_root = _infer_repo_root(target)
    rules = _load_audit_rules(repo_root, Path(config) if config is not None else None)

    checked: list[dict[str, str]] = []
    findings: list[dict[str, Any]] = []

    if target.is_file() and target.name == "manifest.json":
        checked.append({"kind": "snapshot", "path": str(target)})
        findings.extend(_audit_snapshot(target, repo_root, rules))
    elif (target / "manifest.json").is_file():
        manifest_path = target / "manifest.json"
        checked.append({"kind": "snapshot", "path": str(manifest_path)})
        findings.extend(_audit_snapshot(manifest_path, repo_root, rules))
    elif (target / "FORMAT.json").is_file() or target.name.casefold() == "cairnmind-published":
        checked.append({"kind": "published", "path": str(target)})
        _, errors = check_directory(target, rules)
        for location, reason in errors:
            findings.append({
                "code": "published_error",
                "path": location or str(target),
                "message": f"{location or '(tree)'}: {reason}",
            })
    else:
        for rel_dir in MEMORY_DIRECTORIES:
            manifest_path = target / rel_dir / "manifest.json"
            if manifest_path.exists() or manifest_path.is_symlink():
                checked.append({"kind": "snapshot", "path": str(manifest_path)})
                findings.extend(_audit_snapshot(manifest_path, repo_root, rules))
        published_dir = target / PUBLISHED_RELATIVE
        if published_dir.exists() or published_dir.is_symlink():
            checked.append({"kind": "published", "path": str(published_dir)})
            _, errors = check_directory(published_dir, rules)
            for location, reason in errors:
                findings.append({
                    "code": "published_error",
                    "path": location or str(published_dir),
                    "message": f"{location or '(tree)'}: {reason}",
                })

    history_warnings: list[dict[str, Any]] = []
    if check_history and repo_root.is_dir():
        leaks = _git_history_transcript_leaks(repo_root)
        if leaks:
            commits = sorted({item["commit"] for item in leaks})
            short = ", ".join(c[:12] for c in commits[:5])
            more = f" (+{len(commits) - 5} more)" if len(commits) > 5 else ""
            history_warnings.append({
                "code": "historical_transcript_leak",
                "commits": commits,
                "paths": sorted({item["path"] for item in leaks}),
                "message": (
                    f"WARNING: Git history contains snapshot commit(s) with contains_transcripts=true ({short}{more}). "
                    "Deleting or overwriting the file in a newer commit does NOT remove transcripts from Git history; "
                    "rewrite history (for example with git filter-repo) before pushing to a remote repository."
                ),
            })

    return {
        "status": "failed" if findings else "passed",
        "path": str(target),
        "checked": checked,
        "findings": findings,
        "history_warnings": history_warnings,
    }


def render_audit_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    if not report["checked"]:
        lines.append(f"audit passed: no memory packages found under {safe_text(report['path'])}")
    elif report["status"] == "passed":
        summary = ", ".join(f"{item['kind']} ({safe_text(item['path'])})" for item in report["checked"])
        lines.append(f"audit passed: {summary}")
    else:
        lines.append(f"audit failed: {len(report['findings'])} finding(s)")
        for finding in report["findings"]:
            lines.append(f"  - [{finding['code']}] {safe_text(finding['message'])}")
    for warning in report.get("history_warnings", []):
        lines.append(safe_text(warning["message"]))
    return "\n".join(lines)
