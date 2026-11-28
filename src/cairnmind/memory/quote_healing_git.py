"""Core models, Git diff scanning, hunk projection, and AST tokenization for quote self-healing.

Provides Phase 1A (Git diff scanning, hunk parsing, line range projection) and
Phase 1B (tree-sitter AST enclosing scope extraction and lexical tokenization)
along with shared data models, exceptions, and SHA-256 verification helpers.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cairnmind.memory.memory import MemoryConflict
from cairnmind.memory.memory_paths import git_pathspec, is_reserved_memory

HEAL_AUDIT_PREFIX = "heal://"
HEAL_AUDIT_ORIGIN = "quote_self_healing"
HEAL_STATUSES = ("unchanged", "healed", "stale_ref", "validation_failed", "ambiguous_context")
FAILURE_STATUSES = ("stale_ref", "validation_failed", "ambiguous_context")
CONTEXT_WINDOW = 3
MAX_HEAL_FILE_BYTES = 2 * 1024 * 1024

_FILE_KINDS = ("code", "document", "file")
_HEX_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FALLBACK_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\s\w]")
_MEANINGFUL_TOKEN = re.compile(r"[A-Za-z0-9_]")

_TREE_SITTER_LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
}

_AST_SCOPE_KINDS = {
    "function_definition": "function",
    "class_definition": "class",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}


class QuoteCASConflict(MemoryConflict):
    """Optimistic concurrency (CAS) conflict during concurrent quote/snapshot modification."""


class QuoteAmbiguityConflict(MemoryConflict):
    """Multiple identical code blocks found without unique surrounding or AST context."""


@dataclass(frozen=True)
class DiffHunk:
    """One unified-diff hunk with 1-based line coordinates."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def old_end(self) -> int:
        if self.old_count == 0:
            return self.old_start
        return self.old_start + self.old_count - 1

    @property
    def new_end(self) -> int:
        if self.new_count == 0:
            return self.new_start
        return self.new_start + self.new_count - 1

    @property
    def delta(self) -> int:
        return self.new_count - self.old_count


@dataclass(frozen=True)
class CodeToken:
    """One lexical or syntax-tree token with 1-based line and 0-based char coordinates."""

    kind: str
    text: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class HealOutcome:
    """Result of Phase 1 + Phase 2 quote relocation and SHA-256 verification."""

    status: str
    ref_status: str
    stale_ref: bool
    validation_status: str
    requires_intervention: bool
    old_path: str
    new_path: str | None
    old_commit_sha: str | None
    new_commit_sha: str | None
    old_line_start: int | None
    old_line_end: int | None
    new_line_start: int | None
    new_line_end: int | None
    quote_sha256: str
    candidate_sha256: str | None = None
    candidate_lines: tuple[tuple[int, int], ...] = ()
    strategy: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ref_status": self.ref_status,
            "stale_ref": self.stale_ref,
            "validation_status": self.validation_status,
            "requires_intervention": self.requires_intervention,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "old_commit_sha": self.old_commit_sha,
            "new_commit_sha": self.new_commit_sha,
            "old_lines": [self.old_line_start, self.old_line_end] if self.old_line_start else None,
            "new_lines": [self.new_line_start, self.new_line_end] if self.new_line_start else None,
            "quote_sha256": self.quote_sha256,
            "candidate_sha256": self.candidate_sha256,
            "candidate_lines": [[s, e] for s, e in self.candidate_lines],
            "strategy": self.strategy,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PlannedHealAction:
    """One record's planned Phase 3 write, computed outside the DB writer lock."""

    record_id: str
    expected_version: int
    old_source_id: str
    new_file_content: str | None
    new_file_kind: str
    outcome: HealOutcome


@dataclass
class HealingPlan:
    """All planned self-healing actions for a snapshot import or project run."""

    root: Path
    target_commit_sha: str | None
    snapshot_id: str | None
    actions: list[PlannedHealAction]
    unchanged: list[str]
    skipped_reason: str | None = None

    def preview(self) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {
            "healed": [], "stale_ref": [], "validation_failed": [], "ambiguous_context": [],
        }
        for action in self.actions:
            entry = {"record_id": action.record_id, "version": action.expected_version, **action.outcome.as_dict()}
            if action.outcome.status in grouped:
                grouped[action.outcome.status].append(entry)
        status = (
            "skipped" if self.skipped_reason
            else "unchanged" if not self.actions
            else "preview"
        )
        result: dict[str, Any] = {
            "status": status,
            "target_commit_sha": self.target_commit_sha,
            "unchanged_count": len(self.unchanged),
            **grouped,
            "audit_entries": [],
        }
        if self.skipped_reason:
            result["reason"] = self.skipped_reason
        return result


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256_quote(text: str) -> str:
    """Canonical SHA-256 hex digest of an LF-normalized quote passage."""
    return hashlib.sha256(normalize_newlines(text).encode("utf-8")).hexdigest()


def validate_quote_checksum(original_quote_sha256: str, candidate_text: str) -> tuple[bool, str]:
    """Phase 2: strict SHA-256 verification of candidate text against original quote digest."""
    candidate_sha = sha256_quote(candidate_text)
    return candidate_sha == original_quote_sha256, candidate_sha


def is_healing_audit_source(row: Any) -> bool:
    """Whether a source row is a quote self-healing audit entry (never chunked, never anchors)."""
    if not isinstance(row, dict):
        return False
    metadata = row.get("metadata")
    if isinstance(metadata, (str, bytes)):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    return (
        str(row.get("uri", "")).startswith(HEAL_AUDIT_PREFIX)
        and row.get("kind") == "manual"
        and isinstance(metadata, dict)
        and metadata.get("origin") == HEAL_AUDIT_ORIGIN
        and metadata.get("healing_audit") is True
    )


# ---------------------------------------------------------------------------
# Phase 1A: Git diff scanning & hunk parsing (read-only)
# ---------------------------------------------------------------------------

def _git_run(root: Path, *args: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(root), *args],
            capture_output=True,
            timeout=30,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def resolve_commit(root: Path, revision: str = "HEAD") -> str | None:
    """Resolve a Git revision to its full 40/64-char commit SHA, or None if unavailable."""
    if (not isinstance(revision, str) or not revision.strip() or revision.startswith("-")
            or len(revision) > 256 or any(ch in revision for ch in "\0\r\n")):
        return None
    out = _git_run(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}")
    if out is None:
        return None
    commit = out.decode("ascii", "replace").strip()
    return commit if _HEX_COMMIT.fullmatch(commit) else None


def commit_exists(root: Path, commit_sha: str | None) -> bool:
    if not isinstance(commit_sha, str) or not _HEX_COMMIT.fullmatch(commit_sha):
        return False
    return _git_run(root, "cat-file", "-e", f"{commit_sha}^{{commit}}") is not None


def scan_git_changes(root: Path, old_commit: str, new_commit: str, nested: tuple[Path, ...] = ()) -> dict[str, dict[str, Any]]:
    """Scan Git diff between two commits with rename detection (`-M`).

    Returns `{old_relative_path: {"status": "M"|"R"|"D"|..., "new_path": str | None}}`.
    """
    pathspec = git_pathspec(nested)
    raw = _git_run(root, "diff", "--name-status", "-M", "-z", "--relative", old_commit, new_commit, *pathspec)
    if raw is None:
        return {}
    parts = [part for part in raw.decode("utf-8", "surrogateescape").split("\0") if part]
    changes: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(parts):
        code = parts[index][:1]
        if code in ("R", "C") and index + 2 < len(parts):
            old_name, new_name = parts[index + 1], parts[index + 2]
            if code == "R":
                changes[old_name] = {"status": "R", "new_path": new_name}
            else:
                changes[new_name] = {"status": "C", "new_path": new_name, "copied_from": old_name}
            index += 3
        elif index + 1 < len(parts):
            name = parts[index + 1]
            changes[name] = {"status": code, "new_path": None if code == "D" else name}
            index += 2
        else:
            break
    return changes


def parse_unified_diff_hunks(diff_text: str) -> list[DiffHunk]:
    """Parse `@@ -old_start[,old_count] +new_start[,new_count] @@` headers into DiffHunk objects."""
    hunks: list[DiffHunk] = []
    for line in normalize_newlines(diff_text).split("\n"):
        match = _HUNK_HEADER.match(line)
        if not match:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        hunks.append(DiffHunk(old_start=old_start, old_count=old_count, new_start=new_start, new_count=new_count))
    return hunks


def git_diff_hunks(root: Path, old_commit: str, new_commit: str, old_path: str, new_path: str | None = None) -> list[DiffHunk]:
    """Read unified diff hunks (`-U0 -M`) for a file between two commits."""
    paths = [old_path] if not new_path or new_path == old_path else [old_path, new_path]
    raw = _git_run(root, "diff", "-U0", "--no-color", "-M", "--relative", old_commit, new_commit, "--", *paths)
    if raw is None:
        return []
    return parse_unified_diff_hunks(raw.decode("utf-8", "replace"))


def compute_line_hunks(old_lines: list[str], new_lines: list[str]) -> list[DiffHunk]:
    """Fallback diff hunk calculation when Git history between snapshots is unavailable."""
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    hunks: list[DiffHunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_count, new_count = i2 - i1, j2 - j1
        old_start = i1 if old_count == 0 else i1 + 1
        new_start = j1 if new_count == 0 else j1 + 1
        hunks.append(DiffHunk(old_start=old_start, old_count=old_count, new_start=new_start, new_count=new_count))
    return hunks


def git_read_file(root: Path, commit: str, relative_path: str) -> str | None:
    """Read a UTF-8 regular file from a Git commit (never touches working tree)."""
    if not relative_path or relative_path.startswith("/") or ".." in PurePosixPath(relative_path).parts:
        return None
    if is_reserved_memory(relative_path):
        return None
    spec = f"{commit}:./{relative_path}"
    size_raw = _git_run(root, "cat-file", "-s", spec)
    if size_raw is None:
        return None
    size_text = size_raw.decode("ascii", "replace").strip()
    if not size_text.isdigit() or int(size_text) > MAX_HEAL_FILE_BYTES:
        return None
    raw = _git_run(root, "show", spec)
    if raw is None or b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def project_line_range(
    line_start: int, line_end: int, hunks: list[DiffHunk]
) -> tuple[int, int, bool, tuple[int, int] | None]:
    """Project `[line_start, line_end]` across diff hunks.

    Returns `(projected_start, projected_end, overlapped, mapped_hunk_range)`.
    """
    delta = 0
    overlapped = False
    mapped_starts: list[int] = []
    mapped_ends: list[int] = []
    for hunk in hunks:
        if hunk.old_count == 0:
            # Pure insertion after `old_start`: lines strictly after `old_start` shift down.
            if hunk.old_start < line_start:
                delta += hunk.new_count
            elif line_start <= hunk.old_start < line_end:
                overlapped = True
                mapped_starts.append(hunk.new_start)
                mapped_ends.append(hunk.new_end)
        else:
            if hunk.old_end < line_start:
                delta += hunk.delta
            elif hunk.old_start <= line_end and hunk.old_end >= line_start:
                overlapped = True
                if hunk.new_count > 0:
                    mapped_starts.append(hunk.new_start)
                    mapped_ends.append(hunk.new_end)
    projected_start = max(1, line_start + delta)
    projected_end = max(projected_start, line_end + delta)
    mapped_range = (min(mapped_starts), max(mapped_ends)) if mapped_starts else None
    return projected_start, projected_end, overlapped, mapped_range


# ---------------------------------------------------------------------------
# Phase 1B: AST enclosing scope & lexical/tree-sitter tokenization
# ---------------------------------------------------------------------------

def _byte_to_char_offsets(text: str) -> list[int]:
    """Map UTF-8 byte offset -> Python str char index (length = len(utf8) + 1)."""
    mapping: list[int] = []
    for char_index, ch in enumerate(text):
        byte_len = len(ch.encode("utf-8"))
        mapping.extend([char_index] * byte_len)
    mapping.append(len(text))
    return mapping


def _parse_tree_sitter(path: str, content: str):
    suffix = Path(path).suffix.lower()
    lang_name = _TREE_SITTER_LANGUAGES.get(suffix)
    if not lang_name:
        return None, None
    try:
        from tree_sitter import Parser

        from cairnmind.scip.parsing import _language

        tree = Parser(_language(lang_name)).parse(content.encode("utf-8"))
        return lang_name, tree
    except (ImportError, TypeError, ValueError, RuntimeError):
        return None, None


def _scope_label(node: Any) -> tuple[str, str] | None:
    if node.type in _AST_SCOPE_KINDS:
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            label = name_node.text.decode("utf-8", errors="replace").strip()
            if label:
                return (_AST_SCOPE_KINDS[node.type], label)
    elif node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        name_node = node.child_by_field_name("name")
        if (value is not None and value.type in {"arrow_function", "function_expression", "generator_function"}
                and name_node is not None and name_node.type == "identifier"):
            label = name_node.text.decode("utf-8", errors="replace").strip()
            if label:
                return ("function", label)
    return None


def ast_enclosing_scope(path: str, content: str, line_start: int, line_end: int) -> tuple[tuple[str, str], ...]:
    """Return outermost-to-innermost named AST scopes enclosing `[line_start, line_end]` (1-based)."""
    _, tree = _parse_tree_sitter(path, content)
    if tree is None:
        return ()
    scopes: list[tuple[int, int, tuple[str, str]]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        n_start = node.start_point.row + 1
        n_end = node.end_point.row + 1
        if n_start <= line_start and n_end >= line_end:
            label = _scope_label(node)
            if label is not None:
                scopes.append((n_end - n_start, n_start, label))
            stack.extend(reversed(node.named_children))
    # Outermost (largest span) first, then earliest start line.
    scopes.sort(key=lambda item: (-item[0], item[1]))
    return tuple(item[2] for item in scopes)


def tokenize_code(path: str, content: str) -> list[CodeToken]:
    """Tokenize code using tree-sitter leaf tokens when available, else regex lexical tokens."""
    normalized = normalize_newlines(content)
    _, tree = _parse_tree_sitter(path, normalized)
    if tree is not None:
        byte_map = _byte_to_char_offsets(normalized)
        tokens: list[CodeToken] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if not node.children:
                if node.start_byte < node.end_byte:
                    c_start = byte_map[node.start_byte]
                    c_end = byte_map[node.end_byte]
                    text = normalized[c_start:c_end]
                    if text.strip():
                        tokens.append(CodeToken(
                            kind=node.type,
                            text=text,
                            line_start=node.start_point.row + 1,
                            line_end=node.end_point.row + 1,
                            char_start=c_start,
                            char_end=c_end,
                        ))
            else:
                stack.extend(reversed(node.children))
        return tokens
    # Generic fallback tokenizer for unsupported languages / plain text / Markdown / TOML.
    tokens = []
    line_num = 1
    line_offset = 0
    for line in normalized.split("\n"):
        for match in _FALLBACK_TOKEN.finditer(line):
            tokens.append(CodeToken(
                kind="token",
                text=match.group(0),
                line_start=line_num,
                line_end=line_num,
                char_start=line_offset + match.start(),
                char_end=line_offset + match.end(),
            ))
        line_offset += len(line) + 1
        line_num += 1
    return tokens


def _line_tokens(text: str) -> list[str]:
    return [m.group(0) for m in _FALLBACK_TOKEN.finditer(text)]


def _meaningful_tokens(tokens: list[str]) -> list[str]:
    return [tok for tok in tokens if _MEANINGFUL_TOKEN.search(tok)]
