"""Check that pull requests altering stable contract files declare the change (#248).

A pull request that modifies ``docs/STABLE_CONTRACT.md`` or any file under
``tests/fixtures/contracts/`` must BOTH:
1. carry the ``contract-change`` label, and
2. contain a non-empty ``## Contracts`` section in its description (ignoring
   HTML template comments).
"""
from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
from pathlib import Path
import re
import sys

REQUIRED_LABEL = "contract-change"
CONTRACT_EXACT_PATHS = frozenset({"docs/STABLE_CONTRACT.md"})
CONTRACT_PREFIXES = ("tests/fixtures/contracts/",)

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CONTRACTS_HEADING_RE = re.compile(r"(?im)^##[ \t]+Contracts[ \t]*$")
_NEXT_H2_RE = re.compile(r"(?m)^##[ \t]+")


def normalize_repo_path(path: str) -> str:
    """Normalize a repository-relative path to POSIX form without a leading './'."""
    norm = path.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def is_contract_path(path: str) -> bool:
    """Return True when ``path`` belongs to the stable contract surface guarded on PRs."""
    norm = normalize_repo_path(path)
    if norm in CONTRACT_EXACT_PATHS:
        return True
    return any(norm.startswith(prefix) for prefix in CONTRACT_PREFIXES)


def matched_contract_paths(changed_files: Iterable[str]) -> list[str]:
    """Return sorted deduplicated repository paths matching the contract surface."""
    return sorted({normalize_repo_path(p) for p in changed_files if p and is_contract_path(p)})


def extract_contracts_section(pr_body: str | None) -> str:
    """Extract non-comment text under ``## Contracts`` in a PR description."""
    if not pr_body:
        return ""
    text = pr_body.replace("\r\n", "\n").replace("\r", "\n")
    match = _CONTRACTS_HEADING_RE.search(text)
    if not match:
        return ""
    after = text[match.end() :]
    next_heading = _NEXT_H2_RE.search(after)
    section = after[: next_heading.start()] if next_heading else after
    cleaned = _HTML_COMMENT_RE.sub("", section)
    return cleaned.strip()


def evaluate_pr(
    changed_files: Iterable[str],
    pr_body: str | None,
    labels: Iterable[str],
) -> tuple[bool, list[str], list[str]]:
    """Evaluate a pull request against the #248 contract-declaration rule.

    Returns ``(passed, matched_paths, errors)``.
    """
    matched = matched_contract_paths(changed_files)
    if not matched:
        return True, [], []

    errors: list[str] = []
    section_text = extract_contracts_section(pr_body)
    if not section_text:
        errors.append(
            "Missing contract declaration: fill in the `## Contracts` section of the pull request "
            "description (outside HTML comments) explaining what changed in `docs/STABLE_CONTRACT.md` "
            "or `tests/fixtures/contracts/`."
        )

    normalized_labels = {str(label).strip().casefold() for label in labels if str(label).strip()}
    if REQUIRED_LABEL not in normalized_labels:
        errors.append(
            f"Missing required label `{REQUIRED_LABEL}`: add the `{REQUIRED_LABEL}` label to this "
            "pull request because it modifies stable contract files."
        )

    return not errors, matched, errors


def _read_changed_files(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("Changed files JSON must be an array of file paths.")
        return [str(item) for item in parsed]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that PRs modifying stable contract files declare it in ## Contracts and carry contract-change."
    )
    parser.add_argument(
        "--changed-files-file",
        type=Path,
        required=True,
        help="Path to a newline-separated list (or JSON array) of files modified by the PR.",
    )
    parser.add_argument(
        "--event-path",
        type=Path,
        default=None,
        help="Path to the GitHub Actions GITHUB_EVENT_PATH JSON file.",
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        default=None,
        help="Optional path to a file containing the PR description body.",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="PR label name (repeatable; merged with labels from --event-path if provided).",
    )
    args = parser.parse_args(argv)

    changed_files = _read_changed_files(args.changed_files_file)
    pr_body: str | None = None
    labels: list[str] = list(args.label)

    if args.event_path is not None:
        event = json.loads(args.event_path.read_text(encoding="utf-8"))
        pr = event.get("pull_request") or {}
        pr_body = pr.get("body")
        for lbl in pr.get("labels") or []:
            if isinstance(lbl, dict) and lbl.get("name"):
                labels.append(str(lbl["name"]))
            elif isinstance(lbl, str):
                labels.append(lbl)

    if args.body_file is not None:
        pr_body = args.body_file.read_text(encoding="utf-8")

    passed, matched, errors = evaluate_pr(changed_files, pr_body, labels)
    if not matched:
        print("Contract check: no stable contract files changed (skipped).")
        return 0

    print("Contract check matched stable contract files:")
    for item in matched:
        print(f"  - {item}")

    if passed:
        print(f"Contract check passed (`{REQUIRED_LABEL}` label and `## Contracts` section present).")
        return 0

    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
