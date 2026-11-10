from __future__ import annotations

import fcntl
import json
import subprocess
import sys

from typer.testing import CliRunner

from devbrain.cli import app
from devbrain.config import Settings
from devbrain.store import Store


def test_cli_task_plan_and_evidence_workflow(tmp_path, monkeypatch):
    from devbrain import ingest
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    runner = CliRunner()
    data = tmp_path / "memory"
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (root / "auth.py").write_text("def authenticate(token):\n    return token == 'valid'\n")
    (root / ".devbrain").mkdir()
    check = [sys.executable, "-c", "from auth import authenticate; assert authenticate('valid'); "
             "assert not authenticate('bad'); print('2 assertions passed')"]
    (root / ".devbrain/checks.toml").write_text("[checks.unit]\nargv = " + json.dumps(check) + "\n")
    for arguments in (("init",), ("config", "user.email", "fixture@example.invalid"),
                      ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
    base = ["--data-dir", str(data)]

    def run(*args):
        result = runner.invoke(app, [*base, *args])
        assert result.exit_code == 0, result.output or repr(result.exception)
        return json.loads(result.output)

    run("init")
    run("project", "add", "demo", str(root))
    note = run("remember", "demo", "Plan logowania", "--kind", "decision", "--confirmed")
    assert note["status"] == "confirmed"
    task = run("task", "add", "demo", "Test logowania", "--plan", "login", "--step", "1",
               "--requires-test", "--requires-review", "--criterion", "Działa poprawne i błędne hasło")
    run("task", "update", "demo", task["id"], "needs_test")
    failed = runner.invoke(app, [*base, "task", "update", "demo", task["id"], "done"])
    assert failed.exit_code != 0
    report = tmp_path / "verification.txt"
    report.write_text("pytest: 2 passed. Review: accepted. Acceptance scenarios: passed.")
    reported = runner.invoke(app, [*base, "task", "update", "demo", task["id"], "done", "--test-passed",
                                  "--review-passed", "--acceptance-passed", "--evidence", str(report)])
    assert reported.exit_code != 0  # A supplied text report is not a machine run.
    verified = run("check", "run", "demo", "unit", "--task", task["id"])
    assert verified["passed"]
    done = run("task", "update", "demo", task["id"], "done", "--check-run", verified["id"],
               "--review-passed", "--acceptance-passed", "--evidence", str(report))
    assert done["metadata"]["state"] == "done"
    assert done["metadata"]["check_run_ids"] == [verified["id"]]
    export = tmp_path / "memory.json"
    run("export", str(export), "--project", "demo")
    assert len(json.loads(export.read_text())["records"]) == 2
    run("backup", str(tmp_path / "backup.devbrain.json.gz"))


def test_restore_requires_service_lock_even_if_health_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr("devbrain.cli.service_alive", lambda settings: False)
    settings = Settings.load(tmp_path / "memory", create=True)
    backup = Store(settings.data_dir).backup(tmp_path / "backup.devbrain.json.gz")
    with (settings.data_dir / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(app, ["--data-dir", str(settings.data_dir), "restore", str(backup)])
    assert result.exit_code != 0
    assert "still holds the store" in " ".join(result.output.split())


def test_unacked_cli_evidence_write_is_atomic(tmp_path, monkeypatch):
    from devbrain.sessions import SessionManager
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    root = tmp_path / "empty"
    root.mkdir()
    project = store.add_project("demo", root)
    source = store.snapshot(project["id"], "manual://task", "Review login", "manual")
    task = store.remember(project["id"], {"kind": "task", "content": "Review login",
        "metadata": {"state": "todo", "requires_review": True}}, [source["id"]])
    session = SessionManager(store, settings).prepare(project["id"], "codex")
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", session["session_id"])
    evidence = tmp_path / "review.txt"
    evidence.write_text("Reviewed and accepted.")
    before = store.query("SELECT id FROM sources")
    result = CliRunner().invoke(app, ["--data-dir", str(settings.data_dir), "task", "update", "demo",
        task["id"], "done", "--review-passed", "--evidence", str(evidence)])
    assert result.exit_code != 0
    assert "resume_required" in str(result.exception)
    assert store.query("SELECT id FROM sources") == before
    assert store.list_records(project["id"])[0]["metadata"]["state"] == "todo"


def test_cli_ask_accepts_bounded_answer_contract_and_keeps_default_payload(tmp_path, monkeypatch):
    calls = []
    def request(settings, method, path, **kwargs):
        calls.append((method, path, kwargs["json"]))
        return {"status": "complete", "answer": "{}"}
    monkeypatch.setattr("devbrain.cli.request", request)
    schema = {"type": "object", "properties": {"version": {"type": "integer"}},
              "required": ["version"], "additionalProperties": False}
    file = tmp_path / "answer.json"
    file.write_text(json.dumps(schema))
    base = ["--data-dir", str(tmp_path / "memory"), "ask", "demo", "Version?"]
    runner = CliRunner()
    result = runner.invoke(app, [*base, "--answer-schema", str(file)])
    assert result.exit_code == 0, result.output or str(result.exception)
    assert calls[-1][2]["answer_schema"] == schema
    result = runner.invoke(app, base)
    assert result.exit_code == 0
    assert "answer_schema" not in calls[-1][2]
    file.write_text('{"type":"object","$ref":"file:///etc/passwd"}')
    assert runner.invoke(app, [*base, "--answer-schema", str(file)]).exit_code != 0
    assert len(calls) == 2


def test_cli_evaluation_skip_claude_is_explicit_and_remains_not_run(tmp_path, monkeypatch):
    calls = []
    def evaluate(settings, **kwargs):
        calls.append(kwargs)
        return {"status": "not_run", "native_mode": "codex_only_partial"}
    monkeypatch.setattr("devbrain.evaluation.run_evaluation", evaluate)
    base = ["--data-dir", str(tmp_path / "memory"), "eval", "run"]
    runner = CliRunner()
    assert runner.invoke(app, [*base, "--skip-claude"]).exit_code != 0
    assert calls == []
    result = runner.invoke(app, [*base, "--clients", "--skip-claude"])
    assert result.exit_code == 3
    assert calls[0]["clients"] is True and calls[0]["skip_claude"] is True
    assert json.loads(result.output)["status"] == "not_run"
