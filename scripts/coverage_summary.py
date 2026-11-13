"""Report total and patch coverage from a coverage XML report (Cobertura format).

    uv run pytest -n 4 --cov --cov-report=xml
    uv run python scripts/coverage_summary.py                 # patch coverage against origin/main
    uv run python scripts/coverage_summary.py --compare-branch origin/some-branch
    uv run python scripts/coverage_summary.py --no-patch      # total only (no pull request)

Total: line and branch coverage of src/cairnmind. Patch: the lines added or changed since the merge
base with the compare branch (diff-cover) that the tests executed. Printed as plain lines; when
GITHUB_STEP_SUMMARY is set, also written there as a Markdown table, and when GITHUB_OUTPUT is set,
the total and a shields.io endpoint badge are written as step outputs. Nothing is sent anywhere,
and no threshold fails the run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

MAX_FILES = 30  # uncovered changed lines are listed for at most this many files
BADGE_COLOURS = ((80, "brightgreen"), (60, "yellow"), (0, "red"))  # from 80 %, from 60 %, below 60 %


def percent(part: int, whole: int) -> float | None:
    return 100.0 * part / whole if whole else None


def shown(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def totals(xml_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    numbers = {name: int(root.get(name, 0)) for name in
               ("lines-covered", "lines-valid", "branches-covered", "branches-valid")}
    return {"lines_covered": numbers["lines-covered"], "lines": numbers["lines-valid"],
            "branches_covered": numbers["branches-covered"], "branches": numbers["branches-valid"],
            "line_percent": percent(numbers["lines-covered"], numbers["lines-valid"]),
            "branch_percent": percent(numbers["branches-covered"], numbers["branches-valid"])}


def patch(xml_path: Path, compare_branch: str) -> dict:
    """Changed-line coverage against the merge base with compare_branch, or the reason it is missing."""
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "diff-cover.json"
        tool = Path(sys.executable).with_name("diff-cover")  # installed with this Python (dev extra)
        try:
            result = subprocess.run([str(tool if tool.exists() else "diff-cover"), str(xml_path),
                                     f"--compare-branch={compare_branch}", f"--format=json:{report}"],
                                    capture_output=True, text=True)
        except OSError as exc:
            return {"error": f"diff-cover could not be run: {exc.strerror or exc}"}
        if result.returncode != 0 or not report.exists():
            reason = (result.stderr or result.stdout).strip().splitlines()
            return {"error": reason[-1] if reason else f"diff-cover exited with {result.returncode}"}
        data = json.loads(report.read_text())
    changed = int(data.get("total_num_lines", 0))
    missed = int(data.get("total_num_violations", 0))
    files = {name: sorted(stats.get("violation_lines", [])) for name, stats in data.get("src_stats", {}).items()
             if stats.get("violation_lines")}
    return {"compare_branch": compare_branch, "changed": changed, "covered": changed - missed, "missed": missed,
            "percent": percent(changed - missed, changed), "uncovered": files}


def ranges(lines: list[int]) -> str:
    parts, start, previous = [], None, None
    for line in lines:
        if start is None:
            start = previous = line
        elif line == previous + 1:
            previous = line
        else:
            parts.append(f"{start}" if start == previous else f"{start}-{previous}")
            start = previous = line
    if start is not None:
        parts.append(f"{start}" if start == previous else f"{start}-{previous}")
    return ", ".join(parts)


def badge(line_percent: float | None) -> dict:
    if line_percent is None:
        return {"schemaVersion": 1, "label": "coverage", "message": "unknown", "color": "lightgrey"}
    whole = math.floor(line_percent)
    colour = next(colour for limit, colour in BADGE_COLOURS if whole >= limit)
    return {"schemaVersion": 1, "label": "coverage", "message": f"{whole}%", "color": colour}


def text_lines(total: dict, change: dict | None) -> list[str]:
    lines = [f"Total line coverage: {shown(total['line_percent'])} "
             f"({total['lines_covered']} of {total['lines']} lines)",
             f"Total branch coverage: {shown(total['branch_percent'])} "
             f"({total['branches_covered']} of {total['branches']} branches)"]
    if change is None:
        return lines
    if "error" in change:
        return [*lines, f"Patch coverage: not computed ({change['error']})"]
    if not change["changed"]:
        return [*lines, f"Patch coverage against {change['compare_branch']}: no measured line of src/cairnmind changed"]
    lines.append(f"Patch coverage against {change['compare_branch']}: {shown(change['percent'])} "
                 f"({change['covered']} of {change['changed']} changed lines covered, {change['missed']} missed)")
    for name, missed in list(change["uncovered"].items())[:MAX_FILES]:
        lines.append(f"  not covered: {name}: {ranges(missed)}")
    if len(change["uncovered"]) > MAX_FILES:
        lines.append(f"  and {len(change['uncovered']) - MAX_FILES} more files")
    return lines


def markdown(total: dict, change: dict | None) -> str:
    rows = ["## Coverage", "", "| Measure | Coverage | Covered | Missed |", "| --- | ---: | ---: | ---: |",
            f"| Total lines (src/cairnmind) | {shown(total['line_percent'])} | {total['lines_covered']} "
            f"| {total['lines'] - total['lines_covered']} |",
            f"| Total branches | {shown(total['branch_percent'])} | {total['branches_covered']} "
            f"| {total['branches'] - total['branches_covered']} |"]
    if change is not None and "error" not in change:
        rows.append(f"| Changed lines (against `{change['compare_branch']}`) | {shown(change['percent'])} "
                    f"| {change['covered']} | {change['missed']} |")
    rows.append("")
    if change is None:
        rows.append("No pull request: total only.")
    elif "error" in change:
        rows.append(f"Patch coverage was not computed: {change['error']}")
    elif change["uncovered"]:
        rows += ["Changed lines not executed by the tests:", ""]
        rows += [f"- `{name}`: {ranges(missed)}" for name, missed in list(change["uncovered"].items())[:MAX_FILES]]
        if len(change["uncovered"]) > MAX_FILES:
            rows.append(f"- and {len(change['uncovered']) - MAX_FILES} more files")
    elif change["changed"]:
        rows.append("Every changed line was executed by the tests.")
    else:
        rows.append("The change has no measured lines in src/cairnmind.")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--xml", type=Path, default=Path("coverage.xml"))
    parser.add_argument("--compare-branch", default="origin/main")
    parser.add_argument("--no-patch", action="store_true", help="report the total only")
    parser.add_argument("--badge", type=Path, help="also write a shields.io endpoint JSON file here")
    args = parser.parse_args(argv)
    if not args.xml.is_file():
        print(f"No coverage report at {args.xml}; run the tests with --cov --cov-report=xml first.", file=sys.stderr)
        return 2
    total = totals(args.xml)
    change = None if args.no_patch else patch(args.xml, args.compare_branch)
    print("\n".join(text_lines(total, change)))
    endpoint = badge(total["line_percent"])
    if args.badge:
        args.badge.write_text(json.dumps(endpoint) + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as handle:
            handle.write(markdown(total, change))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as handle:
            handle.write(f"line_percent={shown(total['line_percent'])}\n")
            handle.write(f"badge={json.dumps(endpoint)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
