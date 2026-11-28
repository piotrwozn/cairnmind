"""Phase 1C + Phase 2 quote locator, ambiguity guard, partial-edit detector, and SHA-256 validator."""
from __future__ import annotations

import difflib

from cairnmind.memory.quote_healing_git import (
    CONTEXT_WINDOW,
    DiffHunk,
    HealOutcome,
    _line_tokens,
    _meaningful_tokens,
    ast_enclosing_scope,
    compute_line_hunks,
    normalize_newlines,
    project_line_range,
    sha256_quote,
    tokenize_code,
    validate_quote_checksum,
)


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
    exact_aligned = sum(1 for a, b in zip(left, right, strict=False) if a == b)
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
        exact_lines = sum(1 for a, b in zip(quote_lines, window, strict=True) if a == b)
        partial_lines = sum(1 for a, b in zip(quote_lines, window, strict=True) if _is_partial_line_edit(a, b))
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
