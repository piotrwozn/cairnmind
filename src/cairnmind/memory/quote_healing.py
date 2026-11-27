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

from copy import deepcopy
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any
from uuid import uuid4

from cairnmind.memory.memory import MemoryConflict
from cairnmind.memory.memory_paths import git_pathspec, is_reserved_memory
from cairnmind.storage.sql import clean_row, json_text, utc_now
from cairnmind.storage.storage_query import db_find, db_find_one, db_update_where, desc, eq, is_in
from cairnmind.pal.terminal_safe import safe_text

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


# ---------------------------------------------------------------------------
# Phase 1C + Phase 2: Context matching, ambiguity guard, partial-edit guard, SHA-256
# ---------------------------------------------------------------------------

def _context_lines(
    lines: list[str],
    line_start: int,
    line_end: int,
    window: int = CONTEXT_WINDOW,
    *,
    exclude: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Up to `window` stripped non-empty lines immediately before and after `[line_start, line_end]`.

    Lines listed in `exclude` (such as lines of a repeated boilerplate quote itself) are skipped
    so adjacent identical copies of the quote are never mistaken for unique external context.
    """
    excluded = exclude or set()
    before: list[str] = []
    idx = line_start - 2  # 0-based line immediately before line_start
    while idx >= 0 and len(before) < window:
        stripped = lines[idx].strip()
        if stripped and stripped not in excluded:
            before.append(stripped)
        idx -= 1
    before.reverse()

    after: list[str] = []
    idx = line_end  # 0-based line immediately after line_end
    while idx < len(lines) and len(after) < window:
        stripped = lines[idx].strip()
        if stripped and stripped not in excluded:
            after.append(stripped)
        idx += 1
    return before, after


def _context_overlap(orig: list[str], cand: list[str], *, from_end: bool = False) -> int:
    """Count aligned matching context lines closest to the quote boundary."""
    if not orig or not cand:
        return 0
    left = list(reversed(orig)) if from_end else list(orig)
    right = list(reversed(cand)) if from_end else list(cand)
    exact_aligned = sum(1 for a, b in zip(left, right) if a == b)
    shared_set = len(set(orig) & set(cand))
    return exact_aligned * 2 + shared_set


def _find_exact_occurrences(content: str, quote: str, *, full_lines: bool) -> list[tuple[int, int, str]]:
    """All exact occurrences of `quote` in `content`, returned as `(line_start, line_end, matched_text)`."""
    matches: list[tuple[int, int, str]] = []
    start = 0
    quote_len = len(quote)
    while True:
        idx = content.find(quote, start)
        if idx < 0:
            break
        end = idx + quote_len
        start = idx + 1
        if full_lines:
            if idx > 0 and content[idx - 1] != "\n":
                continue
            if end < len(content) and content[end] != "\n":
                continue
        line_start = content.count("\n", 0, idx) + 1
        line_end = line_start + quote.count("\n")
        matches.append((line_start, line_end, content[idx:end]))
    return matches


def _context_score(
    old_scope: tuple[tuple[str, str], ...],
    old_before: list[str],
    old_after: list[str],
    cand_scope: tuple[tuple[str, str], ...],
    cand_before: list[str],
    cand_after: list[str],
) -> tuple[int, int, int, int]:
    """Rank how uniquely a candidate's AST scope + surrounding context matches the original."""
    ast_exact = int(bool(old_scope) and cand_scope == old_scope)
    ast_inner = int(bool(old_scope) and bool(cand_scope) and cand_scope[-1] == old_scope[-1])
    immediate = (
        int(bool(old_before) and bool(cand_before) and old_before[-1] == cand_before[-1])
        + int(bool(old_after) and bool(cand_after) and old_after[0] == cand_after[0])
    )
    window = (
        _context_overlap(old_before, cand_before, from_end=True)
        + _context_overlap(old_after, cand_after, from_end=False)
    )
    return (ast_exact, ast_inner, immediate, window)


def _find_token_matches(
    old_path: str, new_path: str, quote: str, new_content: str, *, full_lines: bool
) -> list[tuple[int, int, str]]:
    """Find contiguous token-identical occurrences of `quote` in `new_content`."""
    orig_tokens = tokenize_code(old_path, quote)
    if not orig_tokens:
        return []
    new_tokens = tokenize_code(new_path, new_content)
    if len(new_tokens) < len(orig_tokens):
        return []
    target = [(t.kind, t.text) for t in orig_tokens]
    stream = [(t.kind, t.text) for t in new_tokens]
    size = len(target)
    first_item = target[0]
    new_lines = new_content.split("\n")
    found: list[tuple[int, int, str]] = []
    for idx in range(len(stream) - size + 1):
        if stream[idx] != first_item:
            continue
        if stream[idx:idx + size] == target:
            first_tok = new_tokens[idx]
            last_tok = new_tokens[idx + size - 1]
            if full_lines:
                cand_text = "\n".join(new_lines[first_tok.line_start - 1 : last_tok.line_end])
            else:
                cand_text = new_content[first_tok.char_start : last_tok.char_end]
            found.append((first_tok.line_start, last_tok.line_end, cand_text))
    return found


def _is_partial_line_edit(q_line: str, c_line: str) -> bool:
    """Whether `c_line` is a partial modification of a fragment of `q_line`."""
    if q_line == c_line:
        return False
    q_toks = _line_tokens(q_line)
    c_toks = _line_tokens(c_line)
    q_meaningful = _meaningful_tokens(q_toks)
    c_meaningful = _meaningful_tokens(c_toks)
    # Require at least 2 meaningful tokens on at least one side so a complete rewrite of a
    # 1-token line (e.g. `FOO` -> `BAR`) or unrelated `A = 1` -> `B = 2` is not mistaken
    # for a partial-line fragment edit.
    if max(len(q_meaningful), len(c_meaningful)) < 2:
        return False
    if not q_meaningful or not c_meaningful:
        return False
    shared = set(q_meaningful) & set(c_meaningful)
    if not shared:
        return False
    union = set(q_meaningful) | set(c_meaningful)
    jaccard = len(shared) / len(union)
    # Partial line modification: shares meaningful tokens (e.g., variable/function name)
    # while altering a fragment of the line (value, argument, condition, etc.).
    if jaccard >= 0.34:
        return True
    char_ratio = difflib.SequenceMatcher(a=q_line.strip(), b=c_line.strip(), autojunk=False).ratio()
    return char_ratio >= 0.55 and len(shared) >= 1


def _detect_partial_modification(
    quote_lines: list[str],
    new_lines: list[str],
    old_scope: tuple[tuple[str, str], ...],
    old_before: list[str],
    old_after: list[str],
    new_path: str,
    new_content: str,
    mapped_range: tuple[int, int] | None,
    projected_start: int,
    projected_end: int,
) -> tuple[int, int, str] | None:
    """Detect whether a fragment of a quoted line or subset of quoted lines was edited."""
    q_len = len(quote_lines)
    if q_len == 0 or not new_lines:
        return None

    candidate_starts: list[int] = []
    seen: set[int] = set()

    def add_window(start_1based: int) -> None:
        if 1 <= start_1based <= len(new_lines) - q_len + 1 and start_1based not in seen:
            seen.add(start_1based)
            candidate_starts.append(start_1based)

    # 1. Check projected and hunk-mapped regions first.
    add_window(projected_start)
    if mapped_range is not None:
        m_start, m_end = mapped_range
        for s in range(max(1, m_start - 1), min(len(new_lines) - q_len + 1, m_end + 1) + 1):
            add_window(s)

    # 2. Also scan lines sharing meaningful tokens with any quoted line (covers moved + partially edited blocks).
    q_token_sets = [set(_meaningful_tokens(_line_tokens(line))) for line in quote_lines]
    anchor_tokens = set().union(*q_token_sets) if q_token_sets else set()
    if anchor_tokens:
        for idx, line in enumerate(new_lines):
            line_meaningful = set(_meaningful_tokens(_line_tokens(line)))
            if line_meaningful & anchor_tokens:
                for offset in range(q_len):
                    add_window(idx + 1 - offset)

    best: tuple[tuple[int, int, float], int, int, str] | None = None
    for start_1based in candidate_starts:
        end_1based = start_1based + q_len - 1
        window = new_lines[start_1based - 1 : end_1based]
        if window == quote_lines:
            continue  # Exact matches are handled in Step A.
        exact_lines = sum(1 for a, b in zip(quote_lines, window) if a == b)
        partial_lines = sum(1 for a, b in zip(quote_lines, window) if _is_partial_line_edit(a, b))
        if q_len == 1:
            is_partial = partial_lines == 1
            ratio = difflib.SequenceMatcher(a=quote_lines[0].strip(), b=window[0].strip(), autojunk=False).ratio()
        else:
            # Multi-line quote: at least one line partially edited, or a mix of unchanged + edited lines.
            is_partial = (partial_lines >= 1) or (exact_lines >= 1 and exact_lines < q_len)
            ratio = (exact_lines + 0.7 * partial_lines) / q_len
            is_partial = is_partial and ratio >= 0.35
        if not is_partial:
            continue
        cand_before, cand_after = _context_lines(new_lines, start_1based, end_1based)
        ctx_overlap = (
            _context_overlap(old_before, cand_before, from_end=True)
            + _context_overlap(old_after, cand_after, from_end=False)
        )
        scope_hit = int(bool(old_scope) and ast_enclosing_scope(new_path, new_content, start_1based, end_1based) == old_scope)
        key = (scope_hit, ctx_overlap, ratio)
        if best is None or key > best[0]:
            best = (key, start_1based, end_1based, "\n".join(window))
    return None if best is None else (best[1], best[2], best[3])


def locate_and_validate_quote(
    *,
    old_content: str,
    new_content: str | None,
    quote_text: str,
    old_line_start: int | None,
    old_line_end: int | None,
    old_path: str,
    new_path: str | None = None,
    old_commit_sha: str | None = None,
    new_commit_sha: str | None = None,
    diff_hunks: list[DiffHunk] | None = None,
) -> HealOutcome:
    """Phase 1 + Phase 2 pure engine: relocate a quote and validate SHA-256 content integrity."""
    norm_old = normalize_newlines(old_content)
    norm_quote = normalize_newlines(quote_text)
    quote_sha = sha256_quote(norm_quote)
    target_path = new_path if new_path is not None else old_path

    if new_content is None:
        return HealOutcome(
            status="stale_ref",
            ref_status="stale_ref",
            stale_ref=True,
            validation_status="checksum_mismatch",
            requires_intervention=True,
            old_path=old_path,
            new_path=None,
            old_commit_sha=old_commit_sha,
            new_commit_sha=new_commit_sha,
            old_line_start=old_line_start,
            old_line_end=old_line_end,
            new_line_start=None,
            new_line_end=None,
            quote_sha256=quote_sha,
            strategy="file_deleted",
            reason="Cited file was deleted or is unavailable in the target commit.",
        )

    norm_new = normalize_newlines(new_content)
    old_lines = norm_old.split("\n")
    new_lines = norm_new.split("\n")

    # Resolve original line bounds if not supplied.
    if old_line_start is None or old_line_end is None:
        old_hits = _find_exact_occurrences(norm_old, norm_quote, full_lines=False)
        if not old_hits:
            return HealOutcome(
                status="stale_ref",
                ref_status="stale_ref",
                stale_ref=True,
                validation_status="checksum_mismatch",
                requires_intervention=True,
                old_path=old_path,
                new_path=target_path,
                old_commit_sha=old_commit_sha,
                new_commit_sha=new_commit_sha,
                old_line_start=None,
                old_line_end=None,
                new_line_start=None,
                new_line_end=None,
                quote_sha256=quote_sha,
                strategy="ungrounded_original_quote",
                reason="Original quote does not occur verbatim in the archived source snapshot.",
            )
        if len(old_hits) > 1:
            new_hits = _find_exact_occurrences(norm_new, norm_quote, full_lines=False)
            return HealOutcome(
                status="ambiguous_context",
                ref_status="ambiguous_context",
                stale_ref=False,
                validation_status="ambiguous_context",
                requires_intervention=True,
                old_path=old_path,
                new_path=target_path,
                old_commit_sha=old_commit_sha,
                new_commit_sha=new_commit_sha,
                old_line_start=None,
                old_line_end=None,
                new_line_start=None,
                new_line_end=None,
                quote_sha256=quote_sha,
                candidate_lines=tuple((h[0], h[1]) for h in new_hits),
                strategy="ambiguity_conflict",
                reason=f"Original quote occurs {len(old_hits)} times in the source without pinned line numbers.",
            )
        old_line_start, old_line_end, _ = old_hits[0]

    orig_slice = "\n".join(old_lines[old_line_start - 1 : old_line_end])
    full_lines = norm_quote == orig_slice

    hunks = diff_hunks if diff_hunks is not None else compute_line_hunks(old_lines, new_lines)
    proj_start, proj_end, overlapped, mapped_range = project_line_range(old_line_start, old_line_end, hunks)

    old_scope = ast_enclosing_scope(old_path, norm_old, old_line_start, old_line_end)
    old_before, old_after = _context_lines(old_lines, old_line_start, old_line_end)

    # Step A: Exact verbatim occurrences in new_content.
    exact_matches = _find_exact_occurrences(norm_new, norm_quote, full_lines=full_lines)
    if not exact_matches and full_lines:
        # Also check inline occurrence only if the exact full-line match failed (e.g., prefix/suffix added on same line).
        # Note: adding characters to the same line is a partial-line modification, NOT an intact full-line block!
        pass

    if len(exact_matches) == 1:
        cand_start, cand_end, cand_text = exact_matches[0]
        ok, cand_sha = validate_quote_checksum(quote_sha, cand_text)
        if not ok:
            return HealOutcome(
                status="stale_ref",
                ref_status="stale_ref",
                stale_ref=True,
                validation_status="checksum_mismatch",
                requires_intervention=True,
                old_path=old_path,
                new_path=target_path,
                old_commit_sha=old_commit_sha,
                new_commit_sha=new_commit_sha,
                old_line_start=old_line_start,
                old_line_end=old_line_end,
                new_line_start=cand_start,
                new_line_end=cand_end,
                quote_sha256=quote_sha,
                candidate_sha256=cand_sha,
                strategy="sha256_mismatch",
                reason="SHA-256 checksum mismatch between original quote and candidate location.",
            )
        if (target_path == old_path and cand_start == old_line_start and cand_end == old_line_end
                and old_commit_sha == new_commit_sha and norm_old == norm_new):
            return HealOutcome(
                status="unchanged",
                ref_status="valid",
                stale_ref=False,
                validation_status="passed",
                requires_intervention=False,
                old_path=old_path,
                new_path=target_path,
                old_commit_sha=old_commit_sha,
                new_commit_sha=new_commit_sha,
                old_line_start=old_line_start,
                old_line_end=old_line_end,
                new_line_start=cand_start,
                new_line_end=cand_end,
                quote_sha256=quote_sha,
                candidate_sha256=cand_sha,
                strategy="unchanged",
                reason="Quote location and content are unchanged.",
            )
        cand_scope = ast_enclosing_scope(target_path, norm_new, cand_start, cand_end)
        if not overlapped and (cand_start, cand_end) == (proj_start, proj_end):
            strategy = "git_diff+hunk_projection"
        elif old_scope and cand_scope == old_scope:
            strategy = "git_diff+ast+context"
        else:
            strategy = "git_diff+exact_unique"
        return HealOutcome(
            status="healed",
            ref_status="valid",
            stale_ref=False,
            validation_status="passed",
            requires_intervention=False,
            old_path=old_path,
            new_path=target_path,
            old_commit_sha=old_commit_sha,
            new_commit_sha=new_commit_sha,
            old_line_start=old_line_start,
            old_line_end=old_line_end,
            new_line_start=cand_start,
            new_line_end=cand_end,
            quote_sha256=quote_sha,
            candidate_sha256=cand_sha,
            strategy=strategy,
            reason=f"Relocated quote from lines {old_line_start}-{old_line_end} to {cand_start}-{cand_end} with verified SHA-256.",
        )

    if len(exact_matches) >= 2:
        # Edge Case 2: Multiple identical blocks -> require strictly unique AST/surrounding context.
        # Exclude the quote's own stripped lines so neighboring identical boilerplate copies never
        # impersonate unique external context.
        quote_line_set = {line.strip() for line in norm_quote.split("\n") if line.strip()}
        ext_old_before, ext_old_after = _context_lines(old_lines, old_line_start, old_line_end, exclude=quote_line_set)
        scored: list[tuple[tuple[int, int, int, int], tuple[int, int, str]]] = []
        for cand in exact_matches:
            c_start, c_end, _ = cand
            c_scope = ast_enclosing_scope(target_path, norm_new, c_start, c_end)
            c_before, c_after = _context_lines(new_lines, c_start, c_end, exclude=quote_line_set)
            # Only immediate non-quote boundary lines (and AST scopes) count as distinguishing context
            # when multiple identical blocks exist; a distant shared file header does not disambiguate.
            ast_exact = int(bool(old_scope) and c_scope == old_scope)
            ast_inner = int(bool(old_scope) and bool(c_scope) and c_scope[-1] == old_scope[-1])
            imm_before = int(bool(ext_old_before) and bool(c_before) and ext_old_before[-1] == c_before[-1])
            imm_after = int(bool(ext_old_after) and bool(c_after) and ext_old_after[0] == c_after[0])
            score = (ast_exact, ast_inner, imm_before + imm_after, imm_before * imm_after)
            scored.append((score, cand))
        best_score = max(item[0] for item in scored)
        winners = [item for item in scored if item[0] == best_score]
        if len(winners) == 1 and best_score > (0, 0, 0, 0):
            cand_start, cand_end, cand_text = winners[0][1]
            ok, cand_sha = validate_quote_checksum(quote_sha, cand_text)
            if ok:
                return HealOutcome(
                    status="healed",
                    ref_status="valid",
                    stale_ref=False,
                    validation_status="passed",
                    requires_intervention=False,
                    old_path=old_path,
                    new_path=target_path,
                    old_commit_sha=old_commit_sha,
                    new_commit_sha=new_commit_sha,
                    old_line_start=old_line_start,
                    old_line_end=old_line_end,
                    new_line_start=cand_start,
                    new_line_end=cand_end,
                    quote_sha256=quote_sha,
                    candidate_sha256=cand_sha,
                    candidate_lines=tuple((c[0], c[1]) for c in exact_matches),
                    strategy="git_diff+ast_context_disambiguated",
                    reason=(
                        f"Disambiguated {len(exact_matches)} identical blocks using unique AST/surrounding "
                        f"context; relocated to lines {cand_start}-{cand_end} with verified SHA-256."
                    ),
                )
        return HealOutcome(
            status="ambiguous_context",
            ref_status="ambiguous_context",
            stale_ref=False,
            validation_status="ambiguous_context",
            requires_intervention=True,
            old_path=old_path,
            new_path=target_path,
            old_commit_sha=old_commit_sha,
            new_commit_sha=new_commit_sha,
            old_line_start=old_line_start,
            old_line_end=old_line_end,
            new_line_start=None,
            new_line_end=None,
            quote_sha256=quote_sha,
            candidate_lines=tuple((c[0], c[1]) for c in exact_matches),
            strategy="ambiguity_conflict",
            reason=(
                f"Ambiguity conflict: found {len(exact_matches)} identical code blocks without "
                "unique surrounding or AST context; automatic healing refused."
            ),
        )

    # Step B: Zero exact verbatim matches -> check token-identical match (Phase 1 token locator + Phase 2 SHA-256).
    token_matches = _find_token_matches(old_path, target_path, norm_quote, norm_new, full_lines=full_lines)
    if token_matches:
        # Pick closest context match for diagnostic reporting; Phase 2 SHA-256 MUST fail since Step A found no exact match.
        chosen = token_matches[0]
        if len(token_matches) > 1:
            chosen = max(
                token_matches,
                key=lambda cand: _context_score(
                    old_scope, old_before, old_after,
                    ast_enclosing_scope(target_path, norm_new, cand[0], cand[1]),
                    *_context_lines(new_lines, cand[0], cand[1]),
                ),
            )
        cand_start, cand_end, cand_text = chosen
        _, cand_sha = validate_quote_checksum(quote_sha, cand_text)
        return HealOutcome(
            status="stale_ref",
            ref_status="stale_ref",
            stale_ref=True,
            validation_status="checksum_mismatch",
            requires_intervention=True,
            old_path=old_path,
            new_path=target_path,
            old_commit_sha=old_commit_sha,
            new_commit_sha=new_commit_sha,
            old_line_start=old_line_start,
            old_line_end=old_line_end,
            new_line_start=cand_start,
            new_line_end=cand_end,
            quote_sha256=quote_sha,
            candidate_sha256=cand_sha,
            strategy="token_match+sha256_mismatch",
            reason=(
                f"Located token-equivalent block at lines {cand_start}-{cand_end}, but SHA-256 "
                "checksum does not match original quote; reference invalidated as stale_ref."
            ),
        )

    # Step C: Edge Case 1 — Partial modification of a quoted line or subset of quoted lines.
    quote_lines = norm_quote.split("\n")
    partial = _detect_partial_modification(
        quote_lines, new_lines, old_scope, old_before, old_after,
        target_path, norm_new, mapped_range, proj_start, proj_end,
    )
    if partial is not None:
        cand_start, cand_end, cand_text = partial
        _, cand_sha = validate_quote_checksum(quote_sha, cand_text)
        return HealOutcome(
            status="validation_failed",
            ref_status="stale_ref",
            stale_ref=True,
            validation_status="validation_failed",
            requires_intervention=True,
            old_path=old_path,
            new_path=target_path,
            old_commit_sha=old_commit_sha,
            new_commit_sha=new_commit_sha,
            old_line_start=old_line_start,
            old_line_end=old_line_end,
            new_line_start=cand_start,
            new_line_end=cand_end,
            quote_sha256=quote_sha,
            candidate_sha256=cand_sha,
            candidate_lines=((cand_start, cand_end),),
            strategy="partial_modification_rejected",
            reason=(
                f"Partial modification detected near lines {cand_start}-{cand_end}; automatic "
                "repair rejected without guessing (validation_failed; reference invalidated as stale_ref)."
            ),
        )

    # Step D: Quoted block completely replaced or removed -> stale_ref.
    mapped_sha = None
    if mapped_range is not None:
        m_start, m_end = mapped_range
        if 1 <= m_start <= m_end <= len(new_lines):
            mapped_sha = sha256_quote("\n".join(new_lines[m_start - 1 : m_end]))
    return HealOutcome(
        status="stale_ref",
        ref_status="stale_ref",
        stale_ref=True,
        validation_status="checksum_mismatch",
        requires_intervention=True,
        old_path=old_path,
        new_path=target_path,
        old_commit_sha=old_commit_sha,
        new_commit_sha=new_commit_sha,
        old_line_start=old_line_start,
        old_line_end=old_line_end,
        new_line_start=None,
        new_line_end=None,
        quote_sha256=quote_sha,
        candidate_sha256=mapped_sha,
        strategy="content_changed_or_removed",
        reason="Quoted passage no longer matches in target commit; reference invalidated as stale_ref.",
    )


# ---------------------------------------------------------------------------
# Snapshot & Store integration (Phase 1+2 planning outside lock, Phase 3 CAS write)
# ---------------------------------------------------------------------------

def _relative_source_path(source: dict[str, Any], origin_roots: list[str]) -> str | None:
    """Resolve a file source's project-relative POSIX path without touching foreign host symlinks."""
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
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
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
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
                    s_meta = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
                    old_commit = existing_ref.get("commit_sha") or s_meta.get("git_commit")
                    return (
                        source, rel, norm_quote,
                        existing_ref.get("line_start") if isinstance(existing_ref.get("line_start"), int) else None,
                        existing_ref.get("line_end") if isinstance(existing_ref.get("line_end"), int) else None,
                        old_commit if isinstance(old_commit, str) else None,
                    )

    # Otherwise derive from the cited file source that literally contains the quote.
    pinned_lines = metadata.get("evidence_lines")
    pinned_start = pinned_lines[0] if (isinstance(pinned_lines, list) and len(pinned_lines) == 2
                                       and all(type(x) is int and x >= 1 for x in pinned_lines)) else None
    pinned_end = pinned_lines[1] if pinned_start is not None else None

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
        s_meta = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
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
        if old_commit and commit_exists(resolved_root, old_commit):
            if old_commit != target_sha:
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


def _ensure_healed_file_source(
    store, tx, project_id: str, root: Path, action: PlannedHealAction
) -> dict[str, Any]:
    """Create or reuse the current file source row for a healed quote location inside `tx`."""
    from cairnmind.scip.parsing import analyze_code, parser_identity

    assert action.new_file_content is not None and action.outcome.new_path is not None
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

        audit_doc = {
            "action": "quote_self_heal",
            "outcome": outcome.status,
            "ref_status": outcome.ref_status,
            "stale_ref": outcome.stale_ref,
            "validation_status": outcome.validation_status,
            "requires_intervention": outcome.requires_intervention,
            "record_id": old["id"],
            "project_id": project_id,
            "snapshot_id": plan.snapshot_id,
            "source_id": action.old_source_id,
            "new_source_id": new_source_id,
            "previous_version": old["version"],
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
        audit_source = store.new_source(
            tx,
            project_id,
            f"{HEAL_AUDIT_PREFIX}{uuid4()}",
            json_text(audit_doc),
            "manual",
            {
                "origin": HEAL_AUDIT_ORIGIN,
                "healing_audit": True,
                "record_id": old["id"],
                "outcome": outcome.status,
                "ref_status": outcome.ref_status,
                "snapshot_id": plan.snapshot_id,
                "actor": actor,
            },
        )

        quote_ref: dict[str, Any] = {
            "source_id": new_source_id or action.old_source_id,
            "original_source_id": action.old_source_id,
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
            "audit_source_id": audit_source["id"],
        }
        if outcome.status == "healed":
            quote_ref["previous_commit_sha"] = outcome.old_commit_sha
            quote_ref["previous_path"] = outcome.old_path
            quote_ref["previous_lines"] = (
                [outcome.old_line_start, outcome.old_line_end] if outcome.old_line_start else None
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

        audit_record = {"audit_source_id": audit_source["id"], **audit_doc}
        audit_entries.append(audit_record)
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
    if expected_versions is not None:
        if not isinstance(expected_versions, dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 1 for k, v in expected_versions.items()
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
    from cairnmind.memory import memory as portable

    path, previous_raw, manifest, payload = portable._load_snapshot(snapshot_path)
    snapshot_id = manifest["snapshot_id"]
    if expected_snapshot_id is not None and expected_snapshot_id != snapshot_id:
        raise QuoteCASConflict(
            f"Snapshot CAS conflict: expected snapshot {expected_snapshot_id}, found {snapshot_id}."
        )
    project_id = manifest["project"]["id"]
    plan = plan_snapshot_healing(
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

        audit_doc = {
            "action": "quote_self_heal",
            "outcome": outcome.status,
            "ref_status": outcome.ref_status,
            "stale_ref": outcome.stale_ref,
            "validation_status": outcome.validation_status,
            "requires_intervention": outcome.requires_intervention,
            "record_id": rec["id"],
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "source_id": action.old_source_id,
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
        audit_text = json_text(audit_doc)
        audit_source = {
            "id": str(uuid4()),
            "project_id": project_id,
            "uri": f"{HEAL_AUDIT_PREFIX}{uuid4()}",
            "kind": "manual",
            "content": audit_text,
            "sha256": hashlib.sha256(audit_text.encode("utf-8")).hexdigest(),
            "metadata": {
                "origin": HEAL_AUDIT_ORIGIN,
                "healing_audit": True,
                "record_id": rec["id"],
                "outcome": outcome.status,
                "ref_status": outcome.ref_status,
                "snapshot_id": snapshot_id,
                "actor": actor,
            },
            "is_current": True,
            "created_at": now,
        }
        updated_payload["sources"].append(audit_source)

        quote_ref = {
            "source_id": new_source_id or action.old_source_id,
            "original_source_id": action.old_source_id,
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
            "audit_source_id": audit_source["id"],
        }
        if outcome.status == "healed":
            quote_ref["previous_commit_sha"] = outcome.old_commit_sha
            quote_ref["previous_path"] = outcome.old_path
            quote_ref["previous_lines"] = (
                [outcome.old_line_start, outcome.old_line_end] if outcome.old_line_start else None
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
