"""The repository evaluation's own logic, without models or an engine (issue #104)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from cairnmind.config import Settings
from cairnmind.ollama import OllamaUnavailable
from cairnmind.repository_evaluation import (
    NOT_INDEXED, answer_is_right, case_result, load_questions, run_repository_evaluation, summarize, tracked_files,
)

REPOSITORY = Path(__file__).resolve().parents[1]
QUESTIONS = REPOSITORY / "docs" / "evaluation" / "repository-questions.json"


def write(path, questions):
    path.write_text(json.dumps({"format_version": 1, "questions": questions}), encoding="utf-8")
    return path


def git_repository(root):
    root.mkdir(parents=True, exist_ok=True)
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                                 "-c", "commit.gpgsign=false", "commit", "-qm", "t", "--allow-empty")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def test_the_repository_s_questions_resolve_against_its_own_files():
    cases = load_questions(QUESTIONS, REPOSITORY)
    assert len(cases) == 20 and len({case["id"] for case in cases}) == 20
    assert all(case["accepted"] and all(case["accepted"]) for case in cases)
    schema = next(case for case in cases if case["id"] == "schema-version")
    from cairnmind.store import SCHEMA_VERSION
    assert schema["accepted"][0] == str(SCHEMA_VERSION)


def test_a_question_whose_answer_cannot_be_read_is_refused(tmp_path):
    (tmp_path / "limits.py").write_text("RETRIES = 3\n")
    missing = write(tmp_path / "q.json", [{"id": "gone", "question": "?", "file": "limits.py",
                                           "answer": {"pattern": "^TIMEOUT = ([0-9]+)"}}])
    with pytest.raises(ValueError, match="gone: the pattern finds no value"):
        load_questions(missing, tmp_path)
    twice = write(tmp_path / "q2.json", [{"id": "a", "question": "?", "file": "limits.py", "answer": {"accepted": ["3"]}}] * 2)
    with pytest.raises(ValueError, match="used twice"):
        load_questions(twice, tmp_path)
    found = load_questions(write(tmp_path / "q3.json", [{"id": "r", "question": "?", "file": "limits.py",
                                                         "answer": {"pattern": "^RETRIES = ([0-9]+)", "forms": ["{word}"]}}]),
                           tmp_path)
    assert found[0]["accepted"] == ["3", "three"]


@pytest.mark.parametrize("answer, value, forms, right", [
    ("The retry delay is 45 seconds.", "45.0", [], True),
    ("The retry delay is 45.0 seconds.", "45.0", [], True),
    ("The retry delay is 450 seconds.", "45.0", [], False),
    ("Release 2.31 or newer.", "2.31", [], True),
    ("It requires release 2.310.", "2.31", [], False),
    ("At least 0.75 (75%).", "0.75", [], True),
    ("The model is MODEL-X:1B.", "model-x:1b", [], True),
    # Thousands groups: a value is not part of a larger number.
    ("About 70,000 events.", "70", [], False),
    ("A limit of 1,600 rows.", "600", [], False),
    ("It holds 9,216 tokens.", "9", [], False),
    ("It holds 9,216 tokens.", "9216", [], True),
    # A unit counts only where the question lists the form.
    ("The engine runs with -Xmx2048m.", "2048", [], False),
    ("The engine runs with -Xmx2048m.", "2048", ["-Xmx{}m", "{}m"], True),
    ("It gets 2048m of heap.", "2048", ["{}m"], True),
    ("It waits 45s.", "45.0", ["{}s"], True),
    ("It waits 45s.", "45.0", [], False),
    # A number word only where the question lists it, and never as part of another word.
    ("At most seven retries are made.", "7", ["{word}"], True),
    ("At most seven retries are made.", "7", [], False),
    ("One of the seventy files.", "7", ["{word}"], False),
])
def test_an_answer_is_right_only_with_an_accepted_form_as_a_whole_token(tmp_path, answer, value, forms, right):
    (tmp_path / "v.txt").write_text(f"VALUE = {value}\n")
    question = write(tmp_path / "q.json", [{"id": "v", "question": "?", "file": "v.txt",
                                            "answer": {"pattern": "^VALUE = (.+)$", "forms": forms}}])
    accepted = load_questions(question, tmp_path)[0]["accepted"]
    assert answer_is_right(answer, accepted) is right


def test_fake_answers_give_complete_right_and_wrong_counts(tmp_path):
    cases = [{"id": f"q{index}", "question": "?", "file": "a.py", "accepted": ["7", "seven"]} for index in range(5)]
    results = [
        {"status": "complete", "answer": "7 files.", "sources": [{"uri": str(tmp_path / "a.py"), "line_start": 3,
                                                                  "quote": "LIMIT = 7"}],
         "diagnostics": {"code": "ok", "support_check": "supported"}},
        {"status": "complete", "answer": "seven", "sources": [], "diagnostics": {"code": "ok"}},
        {"status": "complete", "answer": "200 files.", "sources": [], "diagnostics": {"code": "ok"}},
        {"status": "partial", "answer": "7", "sources": [], "diagnostics": {"code": "answer_unsupported"}},
        {"status": "partial", "answer": "unknown", "sources": [], "diagnostics": {"code": "insufficient_evidence"}},
    ]
    rows = [case_result(case, result, tmp_path, 2, 1.25) for case, result in zip(cases, results)]
    assert rows[0]["citations"] == [{"path": "a.py", "line_start": 3, "quote": "LIMIT = 7"}]
    assert [row["right"] for row in rows] == [True, True, False, False, False]  # a partial answer is never right
    assert summarize(rows) == {"asked": 5, "complete": 3, "complete_and_right": 2, "complete_and_wrong": 1,
                               "not_complete": 2, "required_complete_and_right": 4, "status": "failed"}
    assert summarize(rows[:2])["status"] == "passed"


def test_only_files_git_tracks_are_copied_never_links_ignored_files_or_the_evaluation_s_own(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "evaluation").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "code.py").write_text("LIMIT = 7\n")
    (root / ".gitignore").write_text(".env\nreports/\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "reports").mkdir()
    (root / "reports" / "repository-old.json").write_text("{}")
    (root / "docs" / "evaluation" / "repository-questions.json").write_text("{}")
    (root / "docs" / "evaluation" / "RESULTS.md").write_text("results")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (root / "src" / "link.txt").symlink_to(outside)
    git_repository(root)
    (root / "src" / "untracked.py").write_text("X = 1\n")
    files = tracked_files(root.resolve(), list(NOT_INDEXED))
    assert files == [".gitignore", "src/code.py"]
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not the top of a Git worktree"):
        tracked_files(plain.resolve(), [])


def no_server():
    class NoServer:
        async def model_identity(self, name):
            raise OllamaUnavailable("Local Ollama is not running", endpoint="/api/tags")
    return NoServer()


def test_without_a_model_server_nothing_is_copied_or_measured(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("LIMIT = 7\n")
    git_repository(root)
    questions = write(tmp_path / "q.json", [{"id": "l", "question": "?", "file": "a.py", "answer": {"accepted": ["7"]}}])
    settings = Settings(data_dir=tmp_path / "user-data")
    report = run_repository_evaluation(settings, root, questions, report_dir=tmp_path / "reports", ollama=no_server())
    assert report["status"] == "not_run" and report["questions"] == []
    assert "not available" in report["reason"] and report["error"]["type"] == "OllamaUnavailable"
    assert Path(report["report_path"]).parent == (tmp_path / "reports").resolve()
    assert "database" not in report["isolation"]
    assert not (tmp_path / "user-data").exists()  # the user's data directory is not touched


def test_without_a_running_engine_nothing_is_copied(tmp_path, monkeypatch):
    import cairnmind.engine as engine
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("LIMIT = 7\n")
    git_repository(root)
    questions = write(tmp_path / "q.json", [{"id": "l", "question": "?", "file": "a.py", "answer": {"accepted": ["7"]}}])

    class Models:
        async def model_identity(self, name):
            return name + "@digest"

    copied = []
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    monkeypatch.setattr("cairnmind.repository_evaluation._copy", lambda *args: copied.append(args))
    report = run_repository_evaluation(Settings(data_dir=tmp_path / "user-data"), root, questions,
                                       report_dir=tmp_path / "reports", ollama=Models())
    assert report["status"] == "not_run" and "cairnmind engine start" in report["reason"]
    assert copied == [] and "cleanup_error" not in report


def test_a_database_that_cannot_be_dropped_fails_the_run_and_is_named(tmp_path, monkeypatch, capsys):
    import cairnmind.engine as engine
    import cairnmind.repository_evaluation as evaluation
    from cairnmind.arcade import ArcadeError
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("LIMIT = 7\n")
    git_repository(root)
    questions = write(tmp_path / "q.json", [{"id": "l", "question": "?", "file": "a.py", "answer": {"accepted": ["7"]}}])

    class Models:
        async def model_identity(self, name):
            return name + "@digest"

    async def all_right(settings, copy, cases, measured, report):
        report["questions"] = [{"status": "complete", "right": True}] * 5

    class Refusing:
        def __init__(self, settings):
            pass

        def server_command(self, command):
            raise ArcadeError("drop refused")

    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": True, "version_matches": True})
    monkeypatch.setattr(evaluation, "_ask_all", all_right)
    monkeypatch.setattr(evaluation, "ArcadeClient", Refusing)
    report = run_repository_evaluation(Settings(data_dir=tmp_path / "user-data"), root, questions,
                                       report_dir=tmp_path / "reports", ollama=Models())
    name = report["isolation"]["database"]
    assert name.startswith("cairnmind_eval_") and report["isolation"]["database_dropped"] is False
    assert report["status"] == "failed"
    assert name in capsys.readouterr().out
    saved = json.loads(Path(report["report_path"]).read_text())
    assert saved["isolation"]["database"] == name and saved["status"] == "failed"


def test_an_interrupted_run_still_drops_its_database_and_writes_its_report(tmp_path, monkeypatch):
    import cairnmind.engine as engine
    import cairnmind.repository_evaluation as evaluation
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("LIMIT = 7\n")
    git_repository(root)
    questions = write(tmp_path / "q.json", [{"id": "l", "question": "?", "file": "a.py", "answer": {"accepted": ["7"]}}])

    class Models:
        async def model_identity(self, name):
            return name + "@digest"

    async def interrupted(*args):
        raise KeyboardInterrupt

    dropped = []

    class Client:
        def __init__(self, settings):
            self.settings = settings

        def server_command(self, command):
            dropped.append(command)

    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": True, "version_matches": True})
    monkeypatch.setattr(evaluation, "_ask_all", interrupted)
    monkeypatch.setattr(evaluation, "ArcadeClient", Client)
    with pytest.raises(KeyboardInterrupt):
        run_repository_evaluation(Settings(data_dir=tmp_path / "user-data"), root, questions,
                                  report_dir=tmp_path / "reports", ollama=Models())
    assert dropped and dropped[0].startswith("drop database cairnmind_eval_")
    saved = json.loads(next((tmp_path / "reports").glob("repository-*.json")).read_text())
    assert saved["failure_stage"] == "interrupted" and saved["isolation"]["database_dropped"] is True
