"""The opt-in answer log of `ask` and the replay that compares evidence, not wording (issue #15)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind.agents import answer_log
from cairnmind.scip import ingest
from cairnmind.config import Settings
from cairnmind.scip.ingest import forget_index_signature
from cairnmind.service import create_app
from cairnmind.store import Store
from cairnmind.agents.support import NAMES_SYSTEM, SUPPORT_SYSTEM


class LocalModel:
    """The fake model of the ask tests: one complete answer citing the first initial hit."""

    async def models(self):
        return {"models": [{"name": "qwen3.5:9b", "model": "qwen3.5:9b", "digest": "abc123"}]}

    async def model_identity(self, name):
        return f"{name}@abc123"

    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def chat(self, messages, tools=None, **kwargs):
        if messages[0].get("content") == SUPPORT_SYSTEM:
            return {"message": {"content": json.dumps({"supported": True, "reason": "Stated."})}}
        if messages[0].get("content") == NAMES_SYSTEM:  # the names call: nothing unstated
            return {"message": {"content": json.dumps({"names": []})}}
        evidence = json.loads(messages[1]["content"])["initial_evidence"][0]
        return {"message": {"content": json.dumps({
            "answer": "The authenticate function handles login.",
            "citations": [{"source_id": evidence["source_id"], "quote": "def authenticate"}], "unknowns": []})}}


@pytest.fixture(autouse=True)
def trusted_timestamps(monkeypatch):
    monkeypatch.setattr(ingest, "RACY_WINDOW_NS", -10**12)


@pytest.fixture
def workspace(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def authenticate(token):\n    return token == 'valid'\n")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    store = Store(settings.data_dir)
    project = store.add_project("demo", root)
    forget_index_signature(store)
    return settings, store, project, root


def _ask(settings, body):
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModel())) as client:
        response = client.post("/ask", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()


def test_an_answer_asked_with_log_is_kept_with_its_evidence_and_never_becomes_evidence(workspace):
    settings, store, project, root = workspace
    pid = project["id"]
    # Not asked to log: nothing is kept (the log is opt-in).
    assert _ask(settings, {"project": "demo", "question": "Where is authenticate?"})["status"] == "complete"
    assert answer_log.read_entries(settings, pid) == ([], 0)
    question = f"Where is authenticate? token {settings.token}"
    result = _ask(settings, {"project": "demo", "question": question, "log": True})
    assert result["status"] == "complete"
    entries, unreadable = answer_log.read_entries(settings, pid)
    assert unreadable == 0 and len(entries) == 1
    entry = entries[0]
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert entry["project_id"] == pid and entry["status"] == "complete" and entry["diagnostic_code"] == "ok"
    assert entry["support_check"] == "supported" and entry["answer"] == result["answer"]
    assert entry["code"]["head"] == head and entry["code"]["fingerprint"]
    assert entry["model"] == {"chat": settings.chat_model, "digest": f"{settings.chat_model}@abc123",
                              "embedding": settings.embedding_model}
    assert entry["timings"]["elapsed_seconds"] is not None
    [cited] = entry["sources"]
    assert cited["uri"] == str(root / "auth.py") and cited["quote"] == "def authenticate"
    assert cited["id"] == result["sources"][0]["id"] and len(cited["sha256"]) == 64
    # No secret is kept: the service token in the question is replaced.
    raw = answer_log.log_path(settings, pid).read_text()
    assert settings.token not in raw and "[REDACTED]" in entry["question"]
    # Private to the owner, like the rest of the data directory's memory.
    from conftest import assert_private
    assert_private(answer_log.log_path(settings, pid))
    assert_private(answer_log.log_path(settings, pid).parent, directory=True)
    # A stored answer never becomes evidence: no source, chunk or embedding holds it.
    assert not store.query("SELECT id FROM sources WHERE kind IN ['agent_answer','generated_answer']")
    assert not store.query("SELECT id FROM sources WHERE content LIKE :text", {"text": "%handles login%"})
    assert not store.query("SELECT id FROM chunks WHERE text LIKE :text", {"text": "%handles login%"})


def test_the_answer_log_setting_logs_every_answer_unless_a_request_says_no(workspace):
    settings, _, project, _ = workspace
    settings.answer_log = True
    _ask(settings, {"project": "demo", "question": "Where is authenticate?"})
    _ask(settings, {"project": "demo", "question": "Where is authenticate, really?", "log": False})
    entries, _ = answer_log.read_entries(settings, project["id"])
    assert [entry["question"] for entry in entries] == ["Where is authenticate?"]


def _entry(settings, question, status="complete", uris=("a.py",), sha="1" * 64):
    result = {"status": status, "answer": "x", "diagnostics": {"code": "ok"},
              "sources": [{"id": f"s-{uri}", "uri": uri, "sha256": sha, "kind": "code", "quote": "q"} for uri in uris]}
    return answer_log.make_entry(settings, "p1", question, result)


def test_the_log_is_bounded_and_prunable(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(answer_log, "MAX_LOG_BYTES", 20_000)
    for number in range(100):
        assert answer_log.append(settings, _entry(settings, f"Question {number}?"))
    entries, unreadable = answer_log.read_entries(settings, "p1")
    assert unreadable == 0 and entries[-1]["question"] == "Question 99?"
    assert answer_log.log_path(settings, "p1").stat().st_size <= 20_000 and len(entries) < 100
    oversized = _entry(settings, "Q" * (answer_log.MAX_ENTRY_BYTES + 1))
    assert answer_log.append(settings, oversized) is False
    assert answer_log.prune(settings, "p1", 3)["kept"] == 3
    assert answer_log.prune(settings, "p1", 10) == {"project_id": "p1", "kept": 3, "removed": 0}
    assert [entry["question"] for entry in answer_log.read_entries(settings, "p1")[0]] == [
        "Question 97?", "Question 98?", "Question 99?"]
    assert answer_log.prune(settings, "p1", 0) == {"project_id": "p1", "kept": 0, "removed": 3}
    assert answer_log.read_entries(settings, "p1") == ([], 0)
    assert answer_log.forget(settings, "p1") is True and not answer_log.log_path(settings, "p1").parent.exists()
    with pytest.raises(ValueError):
        answer_log.log_path(settings, "../elsewhere")


def test_unreadable_lines_are_counted_not_trusted(tmp_path):
    settings = Settings(data_dir=tmp_path)
    answer_log.append(settings, _entry(settings, "Kept?"))
    with answer_log.log_path(settings, "p1").open("ab") as handle:
        handle.write(b"not json\n" + json.dumps({"format_version": 1, "id": "../x", "question": "?",
                                                 "sources": []}).encode() + b"\n")
    entries, unreadable = answer_log.read_entries(settings, "p1")
    assert [entry["question"] for entry in entries] == ["Kept?"] and unreadable == 2


def test_compare_looks_at_status_and_cited_addresses_not_wording():
    before = {"status": "complete", "answer": "One wording.",
              "sources": [{"uri": "a.py", "sha256": "1"}, {"uri": "b.py", "sha256": "2"}]}
    same = {"status": "complete", "answer": "Another wording entirely.",
            "sources": [{"uri": "b.py", "sha256": "2"}, {"uri": "a.py", "sha256": "1"}]}
    assert answer_log.compare(before, same)["verdict"] == "unchanged"
    assert answer_log.compare(before, {**same, "status": "partial"})["verdict"] == "regressed"
    lost = answer_log.compare(before, {**same, "sources": [{"uri": "a.py", "sha256": "9"}]})
    assert lost == {"verdict": "lost_sources", "lost_sources": ["b.py"], "new_sources": [],
                    "changed_sources": ["a.py"]}
    added = answer_log.compare(before, {**same, "sources": [*same["sources"], {"uri": "c.py", "sha256": "3"}]})
    assert added["verdict"] == "sources_added" and added["new_sources"] == ["c.py"]
    assert answer_log.compare({**before, "status": "partial"}, same)["verdict"] == "improved"


def test_replay_asks_each_distinct_question_once_and_reports_what_changed(tmp_path):
    settings = Settings(data_dir=tmp_path)
    for entry in (_entry(settings, "Stable?"), _entry(settings, "Regresses?"), _entry(settings, "Loses?", uris=("a.py", "b.py")),
                  _entry(settings, "Stable?")):
        answer_log.append(settings, entry)
    asked = []

    def ask(payload):
        asked.append(payload)
        question = payload["question"]
        if question == "Regresses?":
            return {"status": "partial", "diagnostics": {"code": "citation_invalid"}, "sources": []}
        return {"status": "complete", "answer": "Reworded.", "diagnostics": {"code": "ok"},
                "sources": [{"uri": "a.py", "sha256": "1" * 64}]}

    report = answer_log.replay(settings, "p1", ask, report_dir=tmp_path / "reports")
    assert [payload["question"] for payload in asked] == ["Stable?", "Loses?", "Regresses?"]
    assert all(payload["log"] is False and payload["project"] == "p1" for payload in asked)
    verdicts = {row["question"]: row["verdict"] for row in report["answers"]}
    assert verdicts == {"Stable?": "unchanged", "Loses?": "lost_sources", "Regresses?": "regressed"}
    assert report["status"] == "failed" and report["summary"]["regressed"] == 1
    assert json.loads(open(report["report_path"], encoding="utf-8").read())["replayed"] == 3
    empty = answer_log.replay(settings, "p2", ask, report_dir=tmp_path / "reports")
    assert empty["status"] == "not_run" and empty["replayed"] == 0


def test_cli_asks_with_log_lists_shows_prunes_and_replays(tmp_path, monkeypatch):
    calls = []

    def request(settings, method, path, **kwargs):
        calls.append(kwargs["json"])
        return {"status": "complete", "answer": "x", "sources": [{"uri": "a.py", "sha256": "1" * 64}]}

    class Project:
        def resolve_project(self, value):
            return {"id": "p1", "name": "demo"}

    monkeypatch.setattr("cairnmind.cli.request", request)
    monkeypatch.setattr("cairnmind.cli.store_for_read", lambda settings: Project())
    from cairnmind.cli import app
    runner = CliRunner()
    base = ["--data-dir", str(tmp_path / "memory")]
    assert runner.invoke(app, [*base, "ask", "demo", "Q?", "--log"]).exit_code == 0
    assert calls[-1]["log"] is True
    assert runner.invoke(app, [*base, "ask", "demo", "Q?"]).exit_code == 0
    assert "log" not in calls[-1]
    settings = Settings.load(tmp_path / "memory")
    answer_log.append(settings, _entry(settings, "Logged?"))
    listed = runner.invoke(app, [*base, "answers", "list", "demo"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)["answers"]
    assert [row["question"] for row in rows] == ["Logged?"]
    shown = runner.invoke(app, [*base, "answers", "show", "demo", rows[0]["id"]])
    assert shown.exit_code == 0 and json.loads(shown.output)["sources"][0]["uri"] == "a.py"
    assert runner.invoke(app, [*base, "answers", "show", "demo", "f" * 32]).exit_code != 0
    replayed = runner.invoke(app, [*base, "eval", "replay", "demo", "--report-dir", str(tmp_path / "r")])
    assert replayed.exit_code == 0, replayed.output
    assert json.loads(replayed.output)["summary"]["unchanged"] == 1 and calls[-1]["log"] is False
    pruned = runner.invoke(app, [*base, "answers", "prune", "demo", "--keep", "0"])
    assert pruned.exit_code == 0 and json.loads(pruned.output)["removed"] == 1
    assert runner.invoke(app, [*base, "eval", "replay", "demo", "--report-dir", str(tmp_path / "r")]).exit_code == 3


def test_replay_asks_without_the_launched_session_and_keeps_a_refusal_as_its_reason(tmp_path, monkeypatch):
    import httpx
    from cairnmind import cli
    settings = Settings.load(tmp_path / "memory", create=True)
    answer_log.append(settings, {**_entry(settings, "Logged?"), "worktree": "/repo/wt"})
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(400, json={"detail": "Requested read worktree differs from session scope"})

    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(cli, "ensure_service", lambda settings: None)
    monkeypatch.setattr(cli, "session_value", lambda name: "agent-session-1")
    monkeypatch.setattr(cli, "store_for_read", lambda settings: type(
        "P", (), {"resolve_project": lambda self, value: {"id": "p1"}})())
    result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "eval", "replay", "demo",
                                          "--report-dir", str(tmp_path / "r")])
    assert result.exit_code == 1, result.output
    # The logged scope is asked for, not the launched agent's session.
    assert sent == [{"project": "p1", "question": "Logged?", "worktree": "/repo/wt", "log": False}]
    [row] = json.loads(result.output)["answers"]
    assert row["verdict"] == "not_asked" and row["reason"] == "Requested read worktree differs from session scope"
    # Other commands still send the session.
    with pytest.raises(cli.ServiceRefusal):
        cli.request(settings, "POST", "/ask", json={"question": "Q?"}, raise_refusal=True)
    assert sent[-1]["session_id"] == "agent-session-1"


def test_a_cut_off_last_line_does_not_swallow_the_next_entry(tmp_path):
    settings = Settings(data_dir=tmp_path)
    answer_log.append(settings, _entry(settings, "First?"))
    with answer_log.log_path(settings, "p1").open("ab") as handle:
        handle.write(b'{"format_version":1,"id":"cut')
    answer_log.append(settings, _entry(settings, "Second?"))
    entries, unreadable = answer_log.read_entries(settings, "p1")
    assert [entry["question"] for entry in entries] == ["First?", "Second?"] and unreadable == 1


def test_redaction_follows_the_agent_journal_rule(tmp_path):
    settings = Settings(data_dir=tmp_path, token="t" * 40, arcade_password="pw1")
    result = {"status": "complete", "answer": "Ends with a cut token " + "t" * 20,
              "sources": [{"id": "s", "uri": "a.py", "sha256": "1" * 64, "quote": "password pw1 here"}]}
    entry = answer_log.make_entry(settings, "p1", "Q?", result)
    assert entry["answer"] == "Ends with a cut token [REDACTED]"
    assert entry["sources"][0]["quote"] == "password [REDACTED] here"


def test_a_queue_timeout_is_logged(workspace, monkeypatch):
    settings, _, project, _ = workspace
    settings.ask_timeout = -4.9  # the service waits ask_timeout + 5 seconds for the inference queue

    async def busy_queue(self, operation, priority=0):
        await asyncio.sleep(30)  # other work holds the model: the answer never leaves the queue

    monkeypatch.setattr("cairnmind.service.InferenceQueue.submit", busy_queue)
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, start_worker=False, ollama=LocalModel())) as client:
        response = client.post("/ask", json={"project": "demo", "question": "Where?", "log": True}, headers=headers)
        assert response.status_code == 200 and response.json()["diagnostics"]["code"] == "queue_timeout"
    [entry] = answer_log.read_entries(settings, project["id"])[0]
    assert entry["diagnostic_code"] == "queue_timeout" and entry["status"] == "partial" and entry["code"]["head"]


def test_an_unexpected_log_error_is_only_a_warning(workspace, monkeypatch, caplog):
    settings = workspace[0]

    def broken(*args, **kwargs):
        raise TypeError("not JSON")

    monkeypatch.setattr(answer_log, "make_entry", broken)
    with caplog.at_level("WARNING"):
        assert _ask(settings, {"project": "demo", "question": "Where is authenticate?", "log": True})["status"] == "complete"
    assert "answer log could not be written" in caplog.text


def test_forgetting_a_project_deletes_its_answer_log(workspace):
    settings, store, project, _ = workspace
    answer_log.append(settings, {**_entry(settings, "Logged?"), "project_id": project["id"]})
    from cairnmind.memory import project_forget  # the command line refuses since #347; the workspace's route calls this
    result = project_forget.delete(settings, store, project["id"])
    assert result["answer_log_deleted"] is True
    assert not answer_log.log_path(settings, project["id"]).parent.exists()
    assert os.path.isdir(settings.data_dir / answer_log.DIRECTORY)
