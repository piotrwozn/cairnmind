"""Measure file-level vs raw-symbol vs normalised-symbol anchoring on Cairnmind's Git history (#411).

For the last N non-merge commits touching supported code files (`src/`), simulates one
anchored record per function, class, or method in each modified file at the commit's
parent and compares how many `(record, commit)` pairs each rule flags:
1. File-level SHA-256 rule: any edit in the file flags every record anchored in that file.
2. Raw symbol span rule: any byte change inside the symbol's line span (including comments
   and formatting) flags that symbol's record.
3. Normalised symbol body hash rule (#411): flags only semantic body changes (`symbol_changed`)
   or removals (`symbol_gone`), ignoring edits elsewhere in the file, comments, and formatting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

# Allow running directly as `python scripts/measure_symbol_anchoring.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from cairnmind.scip.parsing import analyze_code  # noqa: E402
from cairnmind.memory.symbol_anchor import SYMBOL_ANCHOR_KINDS, find_symbol_in_analysis  # noqa: E402

_CODE_SUFFIXES = frozenset({".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})


def _git(repo: Path, *args: str) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=30,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _blob(repo: Path, rev: str, rel_path: str) -> str | None:
    raw = _git(repo, "show", f"{rev}:./{rel_path}")
    if raw is None or b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _raw_span(text: str, sym: dict[str, Any]) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = max(0, int(sym.get("decorator_line_start", sym["line_start"])) - 1)
    end = min(len(lines), int(sym["line_end"]))
    return "\n".join(lines[start:end])


def measure_history(repo: Path, *, commits: int = 60, path_prefix: str = "src/") -> dict[str, Any]:
    raw_log = _git(repo, "log", "--no-merges", f"--max-count={commits}", "--format=%H %P", "--", path_prefix)
    if raw_log is None:
        raise RuntimeError(f"Unable to read Git log in {repo}")

    commit_pairs: list[tuple[str, str]] = []
    for line in raw_log.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            commit_pairs.append((parts[1], parts[0]))  # (parent, child)

    commits_examined = 0
    files_examined = 0
    total_records = 0
    file_rule_flagged = 0
    raw_symbol_flagged = 0
    norm_symbol_changed = 0
    norm_symbol_gone = 0
    norm_symbol_unchanged = 0
    formatting_or_comment_only = 0

    for parent, child in commit_pairs:
        diff_raw = _git(repo, "diff", "--name-status", "-M", "-z", parent, child, "--", path_prefix)
        if not diff_raw:
            continue
        parts = [p for p in diff_raw.decode("utf-8", errors="replace").split("\0") if p]
        touched: list[tuple[str, str, str]] = []
        idx = 0
        while idx < len(parts):
            code = parts[idx][:1]
            if code in ("R", "C") and idx + 2 < len(parts):
                touched.append((code, parts[idx + 1], parts[idx + 2]))
                idx += 3
            elif idx + 1 < len(parts):
                touched.append((code, parts[idx + 1], parts[idx + 1]))
                idx += 2
            else:
                break

        commit_counted = False
        for status, old_path, new_path in touched:
            if status == "A" or Path(old_path).suffix.lower() not in _CODE_SUFFIXES:
                continue
            old_text = _blob(repo, parent, old_path)
            if old_text is None:
                continue
            new_text = None if status == "D" else _blob(repo, child, new_path)
            old_analysis = analyze_code(Path(old_path), old_text)
            old_symbols = [
                sym for sym in old_analysis.get("symbols", [])
                if sym.get("kind") in SYMBOL_ANCHOR_KINDS and isinstance(sym.get("body_hash"), str)
            ]
            if not old_symbols:
                continue

            if not commit_counted:
                commits_examined += 1
                commit_counted = True
            files_examined += 1

            old_file_sha = hashlib.sha256(old_text.encode("utf-8")).hexdigest()
            new_file_sha = None if new_text is None else hashlib.sha256(new_text.encode("utf-8")).hexdigest()
            file_changed = old_file_sha != new_file_sha
            new_analysis = None if new_text is None else analyze_code(Path(new_path), new_text)

            for sym in old_symbols:
                total_records += 1
                if file_changed:
                    file_rule_flagged += 1
                sym_name = sym.get("qualified_name") or sym["name"]
                if new_analysis is None:
                    raw_symbol_flagged += 1
                    norm_symbol_gone += 1
                    continue
                new_sym = find_symbol_in_analysis(new_analysis, sym_name, sym["kind"])
                if new_sym is None:
                    raw_symbol_flagged += 1
                    norm_symbol_gone += 1
                    continue
                raw_changed = _raw_span(old_text, sym) != _raw_span(new_text or "", new_sym)
                if raw_changed:
                    raw_symbol_flagged += 1
                if new_sym.get("body_hash") == sym["body_hash"]:
                    norm_symbol_unchanged += 1
                    if raw_changed:
                        formatting_or_comment_only += 1
                else:
                    norm_symbol_changed += 1

    norm_flagged = norm_symbol_changed + norm_symbol_gone
    reduction_vs_file = (
        round((1.0 - (norm_flagged / file_rule_flagged)) * 100.0, 2) if file_rule_flagged else 0.0
    )
    reduction_vs_raw = (
        round((1.0 - (norm_flagged / raw_symbol_flagged)) * 100.0, 2) if raw_symbol_flagged else 0.0
    )
    return {
        "repo": str(repo.resolve()),
        "path_prefix": path_prefix,
        "commits_requested": commits,
        "commits_examined": commits_examined,
        "files_examined": files_examined,
        "anchored_symbol_evaluations": total_records,
        "file_level_flagged": file_rule_flagged,
        "raw_symbol_flagged": raw_symbol_flagged,
        "normalized_symbol_flagged": norm_flagged,
        "normalized_symbol_changed": norm_symbol_changed,
        "normalized_symbol_gone": norm_symbol_gone,
        "normalized_symbol_unchanged": norm_symbol_unchanged,
        "formatting_or_comment_only_avoided": formatting_or_comment_only,
        "noise_reduction_vs_file_pct": reduction_vs_file,
        "noise_reduction_vs_raw_symbol_pct": reduction_vs_raw,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure file-level vs symbol-level record flagging on Git history.")
    parser.add_argument("--repo", type=Path, default=_REPO_ROOT, help="Git repository path (default: this repo).")
    parser.add_argument("--commits", type=int, default=60, help="Number of recent commits touching code to examine.")
    parser.add_argument("--prefix", default="src/", help="Path prefix to inspect (default: src/).")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON output.")
    args = parser.parse_args(argv)

    result = measure_history(args.repo, commits=args.commits, path_prefix=args.prefix)
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Repository: {result['repo']} ({result['path_prefix']})")
        print(f"Commits examined:            {result['commits_examined']} (of {result['commits_requested']} requested)")
        print(f"Modified code files:         {result['files_examined']}")
        print(f"Symbol evaluations:          {result['anchored_symbol_evaluations']}")
        print(f"File-level rule flagged:     {result['file_level_flagged']}")
        print(f"Raw symbol span flagged:     {result['raw_symbol_flagged']}")
        print(
            f"Normalised symbol flagged:   {result['normalized_symbol_flagged']} "
            f"({result['normalized_symbol_changed']} changed, {result['normalized_symbol_gone']} gone)"
        )
        print(f"Kept unchanged:              {result['normalized_symbol_unchanged']}")
        print(f"Formatting/comment avoided:  {result['formatting_or_comment_only_avoided']}")
        print(f"Reduction vs file-level:     {result['noise_reduction_vs_file_pct']}%")
        print(f"Reduction vs raw symbol:     {result['noise_reduction_vs_raw_symbol_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
