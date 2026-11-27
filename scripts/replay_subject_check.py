"""Replay the checks of names over kept repository reports, without a model (issue #245).

    python scripts/replay_subject_check.py --repo . --commit 478535d  repository-20261115T*.json
    python scripts/replay_subject_check.py --repo . report-a.json@46095ba report-b.json@478535d

`cairnmind eval repository` keeps a report with the question, the answer (cut at 600 characters),
its citations (path, line, quote cut at 300) and whether the answer was right. This reads each
cited file from Git at the commit the report was measured on (the report names none: give it with
--commit, or per report as REPORT@COMMIT) and prints, for every report, how many answers that were
complete and right each check would have withheld, and which complete and wrong answers it would
have stopped:

- the subject check (subject.py, in the answer path): a name of the question that no quoted source has;
- the check of the names an answer introduces (subject.py, switched on by CHECK_ANSWER_NAMES only
  when this shows no right answer lost): a name of the answer that no quote or path has.

It calls no model and no engine, reads Git only through `git show` and `git ls-tree`, and writes only
to the output. Exit 0: no right answer would be withheld. Exit 1: one would. Exit 2: a report or a
cited file could not be read, so nothing is shown for it (never read as "none withheld"). The
project's own names are approximated by the evaluation's project names and the directories of the
tree at the commit. A quote is read from the file at its place, up to the length the agent keeps,
because a report cuts it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

from cairnmind.agents.agent import EXCERPT_CHARS
from cairnmind.agents.subject import missing_answer_names, missing_subject

REPORT_QUOTE_CHARS = 300  # where repository_evaluation.case_result cuts a quote
REPORT_ANSWER_CHARS = 600  # where case_result cuts an answer; one this long may be cut (a quote of exactly 300 likewise)
PROJECT_NAMES = ("repository", "evaluation")  # what the evaluation calls the copy it indexes


def git(repo: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=False)
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


def directories(repo: Path, commit: str) -> set[str] | None:
    listed = git(repo, "ls-tree", "-r", "--name-only", commit)
    if listed is None:
        return None
    return {part for line in listed.splitlines() for part in PurePosixPath(line).parts[:-1]}


def quote_in(text: str, quote: str) -> str:
    """The quote as the agent kept it: the text of the report, extended in the file only if the report cut it."""
    if len(quote) < REPORT_QUOTE_CHARS:  # an uncut quote is the whole quote; the file text after it was not quoted
        return quote
    at = text.find(quote) if quote else -1
    return text[at:at + EXCERPT_CHARS] if at >= 0 else quote


def replay_report(repo: Path, path: Path, commit: str) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = report["questions"]
    folders = directories(repo, commit)
    if folders is None:
        raise ValueError(f"commit {commit} is not in {repo}")
    own = [*PROJECT_NAMES, *folders]
    out = {"report": path.name, "commit": commit, "complete": 0, "right": 0, "unreadable": [],
           "subject": {"right_withheld": [], "wrong_stopped": []}, "names": {"right_withheld": [], "wrong_stopped": []}}
    for row in rows:
        if row.get("status") != "complete":
            continue
        out["complete"] += 1
        right = bool(row.get("right")) and not row.get("absent")
        out["right"] += right
        answer = str(row.get("answer", ""))
        if len(answer) >= REPORT_ANSWER_CHARS:  # a name after the cut is invisible: never read as "none withheld"
            out["unreadable"].append(f"{row['id']}: the report may have cut the answer at {REPORT_ANSWER_CHARS} characters")
        texts, passages = [], []
        for cite in row.get("citations") or []:
            text = git(repo, "show", f"{commit}:{cite['path']}")
            if text is None:
                out["unreadable"].append(f"{row['id']}: {cite['path']}")
                continue
            texts.append(text)
            passages += [quote_in(text, str(cite.get("quote", ""))), cite["path"]]
        if not passages:
            continue
        found = {"subject": missing_subject(row["question"], [*passages, *texts], own),
                 "names": missing_answer_names(row["question"], answer, passages, own)}
        for check, missing in found.items():
            if missing:
                out[check]["right_withheld" if right else "wrong_stopped"].append({"id": row["id"], "names": missing})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the checks of names over kept repository reports.")
    parser.add_argument("--repo", type=Path, default=Path("."), help="the clone that has the commits")
    parser.add_argument("--commit", default="HEAD", help="the commit of reports given without @COMMIT")
    parser.add_argument("reports", nargs="+", help="repository-*.json, or REPORT@COMMIT")
    args = parser.parse_args(argv)
    code = 0
    for item in args.reports:
        name, _, commit = item.partition("@")
        try:
            result = replay_report(args.repo, Path(name), commit or args.commit)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"{name}: not replayed: {exc}")
            code = max(code, 2)
            continue
        print(f"{result['report']} at {result['commit']}: {result['complete']} complete, {result['right']} right")
        for check in ("subject", "names"):
            lost, stopped = result[check]["right_withheld"], result[check]["wrong_stopped"]
            print(f"  {check} check: withholds {len(lost)} right, stops {len(stopped)} wrong")
            for entry in lost:
                print(f"    right answer withheld: {entry['id']}: {', '.join(entry['names'])}")
            for entry in stopped:
                print(f"    wrong answer stopped: {entry['id']}: {', '.join(entry['names'])}")
            code = max(code, 1 if lost else 0)
        for missing in result["unreadable"]:
            print(f"  not read from Git: {missing}")
            code = max(code, 2)
    return code


if __name__ == "__main__":
    sys.exit(main())
