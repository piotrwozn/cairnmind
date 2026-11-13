"""The repository evaluation's own logic, without models or an engine (issue #104)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairnmind.config import Settings
from cairnmind.ollama import OllamaUnavailable
from cairnmind.repository_evaluation import (
    answer_is_right, case_result, load_questions, run_repository_evaluation, summarize,
)

REPOSITORY = Path(__file__).resolve().parents[1]
QUESTIONS = REPOSITORY / "docs" / "evaluation" / "repository-questions.json"


def write(path, questions):
    path.write_text(json.dumps({"format_version": 1, "questions": questions}), encoding="utf-8")
    return path


def test_the_repository_s_questions_resolve_against_its_own_files():
    cases = load_questions(QUESTIONS, REPOSITORY)
    assert len(cases) == 20 and len({case["id"] for case in cases}) == 20
    assert all(case["accepted"] and all(case["accepted"]) for case in cases)
    # A value read at run time is the one the code holds now.
    schema = next(case for case in cases if case["id"] == "schema-version")
    from cairnmind.store import SCHEMA_VERSION
    assert schema["accepted"] == [str(SCHEMA_VERSION)]


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
                                                         "answer": {"pattern": "^RETRIES = ([0-9]+)"}}]), tmp_path)
    assert found[0]["accepted"] == ["3"]


@pytest.mark.parametrize("answer, accepted, right", [
    ("The retry delay is 45 seconds.", ["45.0"], True),
    ("The retry delay is 45.0 seconds.", ["45.0"], True),
    ("The retry delay is 450 seconds.", ["45.0"], False),
    ("At most seven retries are made.", ["7"], True),
    ("At most 17 retries.", ["7"], False),
    ("Release 2.31 or newer.", ["2.31"], True),
    ("It requires release 2.310.", ["2.31"], False),
    ("At least 0.75 (75%).", ["0.75"], True),
    ("The model is MODEL-X:1B.", ["model-x:1b"], True),
    ("Use `toolname cache show`.", ["cache show", "cache inspect"], True),
])
def test_an_answer_is_right_only_with_an_accepted_value_as_a_whole_token(answer, accepted, right):
    assert answer_is_right(answer, accepted) is right


def test_fake_answers_give_complete_right_and_wrong_counts(tmp_path):
    cases = [{"id": f"q{index}", "question": "?", "file": "a.py", "accepted": ["8"]} for index in range(5)]
    results = [
        {"status": "complete", "answer": "8 files.", "sources": [{"uri": str(tmp_path / "a.py"), "line_start": 3,
                                                                  "quote": "LIMIT = 8"}],
         "diagnostics": {"code": "ok", "support_check": "supported"}},
        {"status": "complete", "answer": "eight", "sources": [], "diagnostics": {"code": "ok"}},
        {"status": "complete", "answer": "200 files.", "sources": [], "diagnostics": {"code": "ok"}},
        {"status": "partial", "answer": "8", "sources": [], "diagnostics": {"code": "answer_unsupported"}},
        {"status": "partial", "answer": "unknown", "sources": [], "diagnostics": {"code": "insufficient_evidence"}},
    ]
    rows = [case_result(case, result, tmp_path, 2, 1.25) for case, result in zip(cases, results)]
    assert rows[0]["citations"] == [{"path": "a.py", "line_start": 3, "quote": "LIMIT = 8"}]
    assert [row["right"] for row in rows] == [True, True, False, False, False]  # a partial answer is never right
    assert summarize(rows) == {"asked": 5, "complete": 3, "complete_and_right": 2, "complete_and_wrong": 1,
                               "not_complete": 2, "required_complete_and_right": 4, "status": "failed"}
    assert summarize(rows[:2])["status"] == "passed"


def test_without_a_model_server_nothing_is_copied_or_measured(tmp_path):
    class NoServer:
        async def model_identity(self, name):
            raise OllamaUnavailable("Local Ollama is not running", endpoint="/api/tags")

    settings = Settings(data_dir=tmp_path / "user-data")
    report = run_repository_evaluation(settings, REPOSITORY, QUESTIONS, report_dir=tmp_path / "reports",
                                       ollama=NoServer())
    assert report["status"] == "not_run" and report["questions"] == []
    assert "not available" in report["reason"] and report["error"]["type"] == "OllamaUnavailable"
    assert Path(report["report_path"]).parent == (tmp_path / "reports").resolve()
    assert not (tmp_path / "user-data").exists()  # the user's data directory is not touched
