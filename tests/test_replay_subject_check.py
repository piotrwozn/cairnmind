"""The replay of the checks of names over kept reports (issue #245): model-free, it reads Git only.

On macOS it behaves as here: it needs only `git` and the installed package, no engine and no model.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_subject_check.py"
CITE = {"path": "src/example/config.py", "line_start": 1, "quote": "port: int = 8765"}


def _row(id, question, answer, right, absent=False):
    return {"id": id, "question": question, "answer": answer, "status": "complete", "right": right,
            "citations": [CITE], **({"absent": True} if absent else {})}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@localhost",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def _replay(*args):
    done = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout


def _repo(tmp_path, rows):
    repo = tmp_path / "repo"
    (repo / "src" / "example").mkdir(parents=True)
    (repo / "src" / "example" / "config.py").write_text("class Settings:\n    port: int = 8765\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "tree")
    report = tmp_path / "repository-test.json"
    report.write_text(json.dumps({"format_version": 1, "questions": rows}), encoding="utf-8")
    return repo, report


def test_the_replay_names_the_right_answers_each_check_would_withhold_and_the_wrong_ones_it_would_stop(tmp_path):
    repo, report = _repo(tmp_path, [
        _row("plain", "On which port does the service listen by default?", "8765", True),
        _row("context", "On which port does the service listen on Fedora?", "8765", True),
        _row("introduced", "On which port does the service listen?", "Zookeeper uses 8765.", True),
        _row("absent", "What is the default Redis port?", "8765", False, absent=True),
    ])
    code, out = _replay("--repo", str(repo), "--commit", "HEAD", str(report))
    assert code == 1
    assert "4 complete, 3 right" in out
    assert "subject check: withholds 1 right, stops 1 wrong" in out
    assert "right answer withheld: context: Fedora" in out and "wrong answer stopped: absent: Redis" in out
    assert "names check: withholds 1 right, stops 0 wrong" in out
    assert "right answer withheld: introduced: Zookeeper" in out


def test_the_replay_counts_only_a_quote_the_report_did_not_cut_as_quoted(tmp_path):
    # The file has "Zookeeper" after the short quote; the agent never kept that text, so the names check withholds.
    repo, report = _repo(tmp_path, [_row("tail", "On which port does the service listen?", "Zookeeper uses 8765.", True)])
    (repo / "src" / "example" / "config.py").write_text(
        "class Settings:\n    port: int = 8765\n    # Zookeeper mirrors this port\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "tail")
    code, out = _replay("--repo", str(repo), "--commit", "HEAD", str(report))
    assert code == 1 and "right answer withheld: tail: Zookeeper" in out


def test_the_replay_exits_zero_when_no_right_answer_is_withheld_and_2_when_it_cannot_read_what_it_needs(tmp_path):
    repo, report = _repo(tmp_path, [_row("plain", "On which port does the service listen by default?", "8765", True)])
    assert _replay("--repo", str(repo), str(report))[0] == 0
    code, out = _replay("--repo", str(repo), "--commit", "no-such-commit", str(report))
    assert code == 2 and "not replayed" in out
    broken = tmp_path / "repository-broken.json"
    broken.write_text("not json", encoding="utf-8")
    assert _replay("--repo", str(repo), str(broken))[0] == 2
    # A cited file that is not in the tree is never read as "nothing withheld".
    moved = json.loads(report.read_text(encoding="utf-8"))
    moved["questions"][0]["citations"] = [{**CITE, "path": "src/example/gone.py"}]
    report.write_text(json.dumps(moved), encoding="utf-8")
    code, out = _replay("--repo", str(repo), str(report))
    assert code == 2 and "not read from Git: plain: src/example/gone.py" in out
