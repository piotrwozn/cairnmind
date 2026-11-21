"""What a person reads when confirming or publishing is refused (#239): the condition that failed, and a way out that works."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairnmind.inspector import Inspector
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_i18n_messages import broker_error_text

from test_published_memory_acts import workspace  # noqa: F401 (a fixture)


def test_confirming_a_record_already_confirmed_here_says_so_and_not_version_conflict(workspace):  # noqa: F811
    store, pid, _, _ = workspace
    source = store.snapshot(pid, "note://twice", "Use cookies.", "note")
    record = store.remember(pid, {"kind": "decision", "content": "Use cookies."}, [source["id"]])
    inspector = Inspector(store)
    confirmed = inspector.review(pid, record["id"], "confirm", 1)["record"]
    with pytest.raises(ValueError, match="^Review version conflict"):
        inspector.review(pid, record["id"], "confirm", 1)  # A stale version is still a version conflict.
    with pytest.raises(ValueError, match="^Already confirmed by a person here at this version; nothing to do.$"):
        inspector.review(pid, record["id"], "confirm", confirmed["version"])
    disputed = inspector.review(pid, record["id"], "dispute", confirmed["version"], reason="Wrong")["record"]
    with pytest.raises(ValueError, match="^Already disputed at this version; nothing to do.$"):
        inspector.review(pid, record["id"], "dispute", disputed["version"], reason="Still wrong")
    other = store.snapshot(pid, "note://other", "Use tokens.", "note")
    replacement = store.remember(pid, {"kind": "decision", "key": "auth-2", "content": "Use tokens.",
                                       "supersedes": record["id"]}, [other["id"]])
    historical = next(r for r in store.list_records(pid, include_history=True) if r["id"] == record["id"])
    assert replacement["supersedes"] == record["id"] and historical["status"] == "superseded"
    with pytest.raises(ValueError, match="^A historical version cannot be reviewed.$"):
        inspector.review(pid, record["id"], "confirm", historical["version"])
    # Over HTTP it is a refusal (400), not a conflict to read again and retry (409); the panel shows it in Polish.
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    response = TestClient(app).post(f"/inspector/records/{replacement['id']}/review",
                                    headers={"Authorization": "Bearer " + store.settings.token},
                                    json={"project": pid, "action": "confirm", "expected_version": 1})
    assert response.status_code == 200
    again = TestClient(app).post(f"/inspector/records/{replacement['id']}/review",
                                 headers={"Authorization": "Bearer " + store.settings.token},
                                 json={"project": pid, "action": "confirm", "expected_version": 2})
    assert again.status_code == 400 and again.json()["detail"].startswith("Already confirmed by a person here")
    error = SimpleNamespace(detail=again.json()["detail"], status_code=400)
    polish = broker_error_text(error, lambda text: tr(text, "pl"))
    assert polish == "Broker 400: Już potwierdzone przez osobę na tym komputerze w tej wersji; nie ma nic do zrobienia."
    for sentence in ("A historical version cannot be reviewed.", "Already disputed at this version; nothing to do."):
        assert tr(sentence, "pl") != sentence


def test_a_refusal_for_the_command_line_says_where_a_person_confirms_now(workspace, monkeypatch):  # noqa: F811
    from typer.testing import CliRunner

    from cairnmind import cli, confirmation, record_kinds
    store, pid, _, _ = workspace
    for name in ("CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID", *confirmation.CLIENT_MARKERS):
        monkeypatch.delenv(name, raising=False)
    # The command line confirms nothing (#214), with a terminal or without: each refusal says where a person does.
    run = CliRunner()
    plan = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "confirm-plan", pid,
                                "00000000-0000-4000-8000-000000000000", "--expected-version", "1"])
    remember = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "remember", pid, "x", "--confirmed"])
    kinds = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "record-kind", "accept", pid, "claude", "x"])
    for result, sentence in ((plan, confirmation.CLI_PLAN_REFUSED), (remember, confirmation.CLI_CONFIRM_REFUSED),
                             (kinds, record_kinds.CLI_REFUSED)):
        words = " ".join(result.output.split())
        assert result.exit_code == 2 and sentence in words and "inspector of the workspace" in words, result.output
    # The one command that used to store a confirmed task stores a proposed one and says so (#239, #214).
    task = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "task", "add", pid, "Write the guide"])
    assert task.exit_code == 0 and confirmation.CLI_TASK_PROPOSED in " ".join(task.output.split()), task.output
    assert '"status": "proposed"' in task.output
    prune = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "store", "prune-backups", "--keep", "1"])
    assert prune.exit_code == 2 and "piped or redirected" in " ".join(prune.output.split()), prune.output


def test_a_plan_save_that_changes_a_confirmed_plan_says_where_a_person_changes_it(workspace, tmp_path,  # noqa: F811
                                                                                   monkeypatch):
    import json

    from typer.testing import CliRunner

    from cairnmind import cli, confirmation
    from cairnmind.memory_acts import confirm_plan
    from cairnmind.plans import PlanManager
    store, pid, _, _ = workspace
    for name in ("CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID", *confirmation.CLIENT_MARKERS):
        monkeypatch.delenv(name, raising=False)
    run, document = CliRunner(), tmp_path / "plan.json"
    steps = [{"key": "read", "content": "Read the package"}]
    document.write_text(json.dumps({"plan": {"title": "Importer", "goal": "Import memory", "constraints": []},
                                    "steps": steps}), encoding="utf-8")
    first = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "plan", "save", pid, str(document),
                                 "--idempotency-key", "plan-1", "--activate"])
    assert first.exit_code == 0, first.output
    plan_id = json.loads(first.output)["plan"]["id"]
    confirm_plan(store, pid, plan_id, PlanManager(store).get(pid, plan_id)["plan"]["version"])
    current = PlanManager(store).get(pid, plan_id)
    for key, plan, saved in (("plan-2", {"id": plan_id, "title": "Importer, second part"}, steps),
                             ("plan-3", {"id": plan_id}, [{**steps[0], "content": "Read it twice"}])):
        document.write_text(json.dumps({"plan": plan, "steps": saved, "expected_version": current["plan"]["version"],
                                        "task_versions": {t["id"]: t["version"] for t in current["tasks"]}}),
                            encoding="utf-8")
        # No plan save is a person's act (#214), in a terminal or not: the refusal names the inspector.
        refused = run.invoke(cli.app, ["--data-dir", str(store.data_dir), "plan", "save", pid, str(document),
                                       "--idempotency-key", key])
        words = " ".join((refused.output + str(refused.exception or "")).split())
        assert refused.exit_code != 0 and "Nothing was saved" in words, words
        assert "in the inspector of the workspace" in words and "own terminal" not in words
        assert "piped or redirected" not in words and "no plan save is" in words
    assert PlanManager(store).get(pid, plan_id) == current


def test_adopting_a_confirmed_legacy_task_names_it_by_its_text_and_its_id_once(workspace):  # noqa: F811
    from cairnmind.confirmation import ConfirmationRefused
    from cairnmind.plans import PlanManager
    from conftest import as_person
    store, pid, _, _ = workspace
    source = store.snapshot(pid, "note://legacy", "Ship the importer", "note")
    task = as_person(store, pid, {"kind": "task", "content": "Ship the importer",
                                  "metadata": {"state": "todo", "plan_id": "old-group", "step_index": 1}},
                     [source["id"]])
    with pytest.raises(ConfirmationRefused) as refused:
        PlanManager(store).adopt_legacy(pid, "old-group", idempotency_key="adopt-1")
    words = " ".join(str(refused.value).split())
    assert f"Task 'Ship the importer' (task {task['id']})" in words, words
    assert words.count(task["id"]) == 1, words


def test_an_unknown_revision_is_named_as_unknown_and_other_git_failures_keep_their_reason(workspace, tmp_path):  # noqa: F811
    from cairnmind.memory_published_git import GitReadError, resolve
    _, _, root, commit = workspace
    assert resolve(root, "HEAD") == commit
    with pytest.raises(GitReadError, match=r"^Unknown revision nope-rev: no commit of that name in "):
        resolve(root, "nope-rev")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitReadError, match="git rev-parse failed in .*: fatal: not a git repository"):
        resolve(plain, "HEAD")


def test_check_published_exits_2_for_a_path_that_is_no_directory_and_names_what_is_wrong(workspace, tmp_path):  # noqa: F811
    from typer.testing import CliRunner

    from cairnmind import cli
    from cairnmind.memory_published import Publisher
    store, pid, root, _ = workspace
    source = store.snapshot(pid, "note://check", "Use cookies.", "note")
    record = store.remember(pid, {"kind": "decision", "content": "Use cookies."}, [source["id"]])
    Inspector(store).review(pid, record["id"], "confirm", 1)
    Publisher(store).publish(pid)
    folder = root / ".memory" / "cairnmind-published"

    def check(path):
        result = CliRunner().invoke(cli.app, ["memory", "check-published", str(path)])
        return result.exit_code, " ".join(result.output.split())

    assert check(folder)[0] == 0
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    for path in (tmp_path / "nonexistent", a_file):
        code, words = check(path)
        # The message box wraps a long path, so only the start of the error is compared.
        assert code == 2 and "Invalid value for PATH" in words, words
    path = folder / "decision" / f"{record['id']}.md"
    original = path.read_text()
    path.write_text("")
    code, words = check(folder)
    assert code == 1 and "the file is empty" in words and "line break" not in words, words
    created = next(line for line in original.split("\n") if line.startswith("created_at: "))
    path.write_text(original.replace(created, 'created_at: "2026-11-15T10:00:00"'))
    code, words = check(folder)
    assert code == 1 and "created_at: it must be an ISO 8601 time with a time zone" in words, words
    kind = next(line for line in original.split("\n") if line.startswith("kind: "))
    path.write_text(original.replace(kind, kind.replace("decision", "opinion"), 1))
    code, words = check(folder)
    assert code == 1 and "invalid kind" in words and "created_at" not in words, words


def test_importing_the_stores_own_snapshot_is_unchanged_and_a_refusal_says_which_reason(workspace, tmp_path):  # noqa: F811
    from cairnmind.memory import MemoryConflict, MemoryManager
    from cairnmind.store import Store
    store, pid, root, _ = workspace
    source = store.snapshot(pid, "note://snap", "Use cookies.", "note")
    store.remember(pid, {"kind": "decision", "content": "Use cookies."}, [source["id"]])
    manager = MemoryManager(store)
    first = manager.snapshot(pid)
    for dry_run in (True, False):
        assert manager.import_snapshot(first["path"], root, dry_run=dry_run)["status"] == "unchanged"
    store.remember(pid, {"kind": "fact", "content": "The service listens on localhost."}, [source["id"]])
    with pytest.raises(MemoryConflict) as changed:
        manager.import_snapshot(first["path"], root, dry_run=True)
    words = str(changed.value)
    assert f"changed since its last snapshot {first['snapshot_id']}" in words and "memory snapshot acts" in words
    assert "diverged" not in words
    # Two histories from one snapshot: another machine and this store each continue it.
    second = manager.snapshot(pid)
    other = Store(tmp_path / "other-database")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    MemoryManager(other).import_snapshot(second["path"], elsewhere)
    other_source = other.snapshot(pid, "note://other", "Use tokens.", "note")
    other.remember(pid, {"kind": "constraint", "content": "Use tokens."}, [other_source["id"]])
    package = MemoryManager(other).snapshot(pid)
    store.remember(pid, {"kind": "fact", "content": "Only this store knows this."}, [source["id"]])
    third = manager.snapshot(pid)
    with pytest.raises(MemoryConflict) as diverged:
        manager.import_snapshot(package["path"], root, dry_run=True)
    words = str(diverged.value)
    assert f"does not continue this store's last snapshot {third['snapshot_id']}" in words and "diverged" in words


def test_the_refusal_for_a_confirmed_checkpoint_record_points_to_the_checkpoint_tool(workspace):  # noqa: F811
    from cairnmind.confirmation import ConfirmationRefused
    store, pid, _, _ = workspace
    saved = store.checkpoint(pid, {"goal": "Ship", "next": "Write tests"})
    confirmed = Inspector(store).review(pid, saved["id"], "confirm", saved["version"])["record"]
    with pytest.raises(ConfirmationRefused) as refused:
        store.remember(pid, {"id": saved["id"], "expected_version": confirmed["version"], "content": "{}"},
                       confirmed["source_ids"])
    words = " ".join(str(refused.value).split())
    assert "checkpoint tool" in words and "under another key" not in words, words
