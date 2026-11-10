"""Real Git + ArcadeDB; subprocess fixtures use only the current Python runtime."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from cairnmind.config import Settings
from cairnmind.store import Store
from cairnmind.verification import (MAX_OUTPUT_BYTES, capture_code_state, load_profiles,
                                  recover_interrupted_runs, run_check, task_definition_hash,
                                  validate_completion, verification_status)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Cairnmind fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / ".gitignore").write_text("cache/\nignored/\n__pycache__/\n")
    (root / "app.py").write_text("value = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    return store, project, root, Settings.load(store.data_dir)


def profile(root, code="print('actual check passed')\n", *, name="unit", timeout=5,
            limit=MAX_OUTPUT_BYTES, extra_inputs=None, args=None):
    script = root / f"check_{name}.py"
    script.write_text(code)
    config = root / ".devbrain" / "checks.toml"
    config.parent.mkdir(exist_ok=True)
    block = (f"[checks.{name}]\nargv = {json.dumps([sys.executable, str(script), *(args or [])])}\n"
             f"cwd = \".\"\ntimeout_seconds = {timeout}\noutput_limit_bytes = {limit}\n"
             f"extra_inputs = {json.dumps(extra_inputs or [])}\n")
    config.write_text((config.read_text() if config.exists() else "") + block)
    return script


def task(store, project, *, profiles=None, reported=False):
    source = store.snapshot(project["id"], "fixture://task", "Run the check", "manual")
    metadata = {"state": "needs_test", "requires_test": True, "required_profiles": profiles or []}
    if reported:
        metadata.update(test_passed=True, test_source_ids=[source["id"]])
    return store.remember(project["id"], {"kind": "task", "content": "Implement value", "metadata": metadata}, [source["id"]])


def test_content_fingerprint_full_files_ignored_inputs_and_modes(workspace):
    store, project, root, _ = workspace
    (root / "large.bin").write_bytes(b"a" * (3 * 1024 * 1024))
    (root / "cache").mkdir()
    (root / "cache" / "out.log").write_text("first")
    first = capture_code_state(store, project["id"])
    assert first["status"] == "fresh"
    (root / "cache" / "out.log").write_text("generated output")
    assert capture_code_state(store, project["id"])["fingerprint"] == first["fingerprint"]
    with (root / "large.bin").open("r+b") as stream:
        stream.seek(2 * 1024 * 1024 + 50)
        stream.write(b"b")
    changed = capture_code_state(store, project["id"])
    assert changed["fingerprint"] != first["fingerprint"]
    (root / "app.py").chmod(0o755)
    executable = capture_code_state(store, project["id"])
    assert executable["fingerprint"] != changed["fingerprint"]
    (root / "app.py").unlink()
    assert capture_code_state(store, project["id"])["fingerprint"] != executable["fingerprint"]
    extra = capture_code_state(store, project["id"], extra_inputs=["cache/*.log"])
    (root / "cache" / "out.log").write_text("new ignored input")
    assert capture_code_state(store, project["id"], extra_inputs=["cache/*.log"])["fingerprint"] != extra["fingerprint"]


def test_unknown_external_symlink_submodule_missing_and_unstable_inputs(workspace, tmp_path, monkeypatch):
    store, project, root, _ = workspace
    outside = tmp_path / "external.py"
    outside.write_text("outside")
    (root / "link.py").symlink_to(outside)
    unknown = capture_code_state(store, project["id"])
    assert unknown["status"] == "unknown"
    assert any("external symlink" in reason for reason in unknown["unknowns"])
    (root / "link.py").unlink()
    missing = capture_code_state(store, project["id"], extra_inputs=["ignored/missing"])
    assert missing["status"] == "unknown"
    with pytest.raises(ValueError, match="inside"):
        capture_code_state(store, project["id"], extra_inputs=["../external.py"])
    head = git(root, "rev-parse", "HEAD").decode().strip()
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{head},module")
    assert capture_code_state(store, project["id"])["status"] == "unknown"
    git(root, "update-index", "--force-remove", "module")
    import cairnmind.verification as verification
    original = verification._manifest
    calls = 0

    def mutating_manifest(*args):
        nonlocal calls
        value = original(*args)
        calls += 1
        if calls == 1:
            (root / "app.py").write_text("changed during capture")
        return value

    monkeypatch.setattr(verification, "_manifest", mutating_manifest)
    assert capture_code_state(store, project["id"])["status"] == "unknown"


def test_internal_symlink_targets_and_commit_identity_are_hashed(workspace):
    store, project, root, _ = workspace
    (root / "ignored").mkdir()
    target = root / "ignored/input"
    target.write_text("input one")
    (root / "link").symlink_to("ignored/input")
    first = capture_code_state(store, project["id"])
    assert first["status"] == "fresh"
    target.write_text("input two")
    changed = capture_code_state(store, project["id"])
    assert changed["fingerprint"] != first["fingerprint"]
    target.chmod(0o755)
    executable = capture_code_state(store, project["id"])
    assert executable["fingerprint"] != changed["fingerprint"]
    git(root, "add", "link")
    git(root, "commit", "-qm", "track link")
    assert capture_code_state(store, project["id"])["head"] != first["head"]


def test_profile_arrays_scope_and_literals(workspace):
    store, project, root, settings = workspace
    profile(root, "import sys\nprint(sys.argv[1])\n", args=["$(echo unsafe); echo nope"])
    check = load_profiles(store, project["id"])["unit"]
    assert check["argv"][-1] == "$(echo unsafe); echo nope"
    record = task(store, project)
    run = run_check(store, settings, project["id"], "unit", record["id"])
    assert run["passed"]
    assert store.get_source(run["source_ids"]["stdout"])["content"].strip() == "$(echo unsafe); echo nope"
    config = root / ".devbrain/checks.toml"
    config.write_text('[checks.unit]\nargv="pytest -q"\n')
    with pytest.raises(ValueError, match="argument array"):
        load_profiles(store, project["id"])
    config.write_text('[checks.unit]\nargv=["pytest"]\ncwd="../"\n')
    with pytest.raises(ValueError, match="cwd"):
        load_profiles(store, project["id"])
    with pytest.raises(ValueError, match="registered"):
        capture_code_state(store, project["id"], root / "child")
    config.write_text('[checks.unit]\nargv=["/missing-devbrain-fixture-executable"]\n')
    failed = run_check(store, settings, project["id"], "unit", record["id"])
    assert failed["outcome"] == "error" and not failed["passed"]
    assert [event["state"] for event in failed["events"]] == ["started", "error"]
    alternate = Settings(data_dir=root.parent / "alternate-memory")
    alternate.data_dir.mkdir()
    (root / "cache").mkdir()
    (alternate.data_dir / "checks").symlink_to(root / "cache")
    with pytest.raises(ValueError, match="outside the working tree"):
        run_check(store, alternate, project["id"], "unit", record["id"])


def test_real_check_evidence_completion_and_stale_state(workspace):
    store, project, root, settings = workspace
    profile(root, "import sys\nprint('two assertions checked')\nprint('stderr evidence', file=sys.stderr)\n")
    record = task(store, project, profiles=["unit"], reported=True)
    assert verification_status(store, record)["status"] == "reported"
    with store.transaction() as tx, pytest.raises(ValueError, match="fresh machine"):
        validate_completion(store, tx, project["id"], record)
    run = run_check(store, settings, project["id"], "unit", record["id"])
    assert run["passed"] and run["returncode"] == 0
    assert run["events"][-1]["state"] == "completed"
    assert store.get_source(run["profile_source_id"])["kind"] == "check_profile"
    assert store.get_source(run["source_ids"]["stderr"])["content"] == "stderr evidence\n"
    assert not (settings.data_dir / "checks" / run["id"] / "active.json").exists()
    assert verification_status(store, record) == {"status": "fresh", "reasons": [], "run_ids": [run["id"]]}
    done = store.remember(project["id"], {"id": record["id"], "expected_version": record["version"],
                          "metadata": {"state": "done", "check_run_ids": [run["id"]]}}, record["source_ids"])
    assert task_definition_hash(done) == task_definition_hash(record)
    assert verification_status(store, done)["status"] == "fresh"
    (root / "app.py").write_text("value = 2\n")
    assert verification_status(store, done)["status"] == "stale"
    assert store.list_records(project["id"])[0]["metadata"]["state"] == "done"
    with store.transaction() as tx, pytest.raises(ValueError, match="fresh machine"):
        validate_completion(store, tx, project["id"], done)


def test_all_required_profiles_and_definition_changes(workspace):
    store, project, root, settings = workspace
    profile(root, name="unit")
    profile(root, name="integration")
    record = task(store, project, profiles=["unit", "integration"])
    run_check(store, settings, project["id"], "unit", record["id"])
    assert verification_status(store, record)["status"] == "stale"
    run_check(store, settings, project["id"], "integration", record["id"])
    assert verification_status(store, record)["status"] == "fresh"
    changed = {**record, "metadata": {**record["metadata"], "acceptance_criteria": ["an additional scenario"]}}
    assert verification_status(store, changed)["status"] == "stale"
    config = root / ".devbrain/checks.toml"
    config.write_text(config.read_text().replace("timeout_seconds = 5", "timeout_seconds = 6"))
    assert verification_status(store, record)["status"] == "stale"


def test_new_failed_attempt_cannot_reuse_old_success(workspace, monkeypatch):
    store, project, root, settings = workspace
    profile(root, "import os\nraise SystemExit(int(os.environ.get('DEVBRAIN_FIXTURE_EXIT', '0')))\n")
    record = task(store, project)
    successful = run_check(store, settings, project["id"], "unit", record["id"])
    assert successful["passed"]
    monkeypatch.setenv("DEVBRAIN_FIXTURE_EXIT", "3")
    failed = run_check(store, settings, project["id"], "unit", record["id"])
    assert failed["returncode"] == 3
    assert failed["code_before"]["fingerprint"] == successful["code_before"]["fingerprint"]
    assert verification_status(store, record)["status"] == "stale"


def test_required_profiles_cannot_mix_different_observed_code_horizons(workspace, monkeypatch):
    store, project, root, settings = workspace
    profile(root, name="unit")
    profile(root, name="integration")
    record = task(store, project, profiles=["unit", "integration"])
    first_content = (root / "app.py").read_text()
    run_check(store, settings, project["id"], "unit", record["id"])
    (root / "app.py").write_text("a different code horizon")
    run_check(store, settings, project["id"], "integration", record["id"])
    (root / "app.py").write_text(first_content)
    import cairnmind.verification as verification
    original_capture = verification.capture_code_state
    calls = 0

    def mutate_between_profile_reads(*args, **kwargs):
        nonlocal calls
        result = original_capture(*args, **kwargs)
        calls += 1
        if calls == 2:  # Horizon-before and unit evidence were both read on A.
            (root / "app.py").write_text("a different code horizon")
        return result

    monkeypatch.setattr(verification, "capture_code_state", mutate_between_profile_reads)
    result = verification_status(store, record)
    assert result["status"] == "stale"
    assert "inputs changed while checking required evidence" in result["reasons"]


def test_profile_change_and_sigterm_cancellation_are_ineligible(workspace):
    store, project, root, settings = workspace
    profile(root, "from pathlib import Path\np = Path('.devbrain/checks.toml')\n"
                  "p.write_text(p.read_text().replace('timeout_seconds = 5', 'timeout_seconds = 6'))\n")
    record = task(store, project)
    changed = run_check(store, settings, project["id"], "unit", record["id"])
    assert not changed["passed"] and "profile changed during check" in changed["reasons"]
    (root / ".devbrain/checks.toml").unlink()
    profile(root, "import os, signal, time\nos.kill(os.getppid(), signal.SIGTERM)\ntime.sleep(10)\n")
    cancelled = run_check(store, settings, project["id"], "unit", record["id"])
    assert cancelled["outcome"] == "cancelled" and not cancelled["passed"]


def test_code_and_task_changes_during_check_prevent_success(workspace, tmp_path):
    store, project, root, settings = workspace
    profile(root, "from pathlib import Path\nPath('app.py').write_text('updated by check')\n")
    record = task(store, project)
    run = run_check(store, settings, project["id"], "unit", record["id"])
    assert run["returncode"] == 0 and not run["passed"]
    assert "code changed during check" in run["reasons"]
    (root / ".devbrain/checks.toml").unlink()
    marker, release = tmp_path / "started", tmp_path / "release"
    profile(root, "from pathlib import Path\nimport time, sys\nPath(sys.argv[1]).touch()\n"
                  "while not Path(sys.argv[2]).exists(): time.sleep(0.01)\n", args=[str(marker), str(release)])
    with ThreadPoolExecutor() as pool:
        future = pool.submit(run_check, store, settings, project["id"], "unit", record["id"])
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        store.remember(project["id"], {"id": record["id"], "expected_version": record["version"],
                                       "content": "Changed requirement"}, record["source_ids"])
        release.touch()
        changed = future.result(timeout=10)
    assert not changed["passed"] and "task definition changed during check" in changed["reasons"]


@pytest.mark.parametrize("case", ["timeout", "output_limit", "binary_limit", "nonzero"])
def test_nonpassing_process_outcomes_and_bounded_output(workspace, case):
    store, project, root, settings = workspace
    script = {"timeout": "import time\nprint('started', flush=True)\ntime.sleep(10)\n",
              "output_limit": "import os\nos.write(1, b'x' * 65536)\n",
              "binary_limit": "import os\nos.write(1, bytes([255]) * 65536)\n",
              "nonzero": "raise SystemExit(7)\n"}[case]
    profile(root, script, timeout=0.2 if case == "timeout" else 5, limit=1000)
    record = task(store, project)
    run = run_check(store, settings, project["id"], "unit", record["id"])
    assert not run["passed"]
    assert isinstance(run["returncode"], int)
    assert run["outcome"] == ("completed" if case == "nonzero" else "output_limit" if case == "binary_limit" else case)
    assert run["output_bytes"] <= 1000
    assert sum((settings.data_dir / "checks" / run["id"] / stream).stat().st_size
               for stream in ("stdout", "stderr")) <= 1000
    assert sum(len(store.get_source(source_id)["content"].encode("utf-8"))
               for source_id in run["source_ids"].values()) <= 1000
    assert verification_status(store, record)["status"] == "stale"


def test_timeout_terminates_process_group(workspace, tmp_path):
    store, project, root, settings = workspace
    child = root / "child.py"
    child.write_text("import time\ntime.sleep(20)\n")
    child_pid = tmp_path / "child.pid"
    profile(root, "import subprocess, sys, time\nfrom pathlib import Path\n"
                  "child = subprocess.Popen([sys.executable, sys.argv[1]])\n"
                  "Path(sys.argv[2]).write_text(str(child.pid))\ntime.sleep(20)\n",
            timeout=0.3, args=[str(child), str(child_pid)])
    record = task(store, project)
    run = run_check(store, settings, project["id"], "unit", record["id"])
    assert run["outcome"] == "timeout"
    from cairnmind.verification import _process_identity
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 3
    while _process_identity(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _process_identity(pid) is None


def test_crashed_cli_spool_recovers_once_and_cannot_qualify(workspace, monkeypatch):
    store, project, root, settings = workspace
    profile(root)
    record = task(store, project)
    import cairnmind.verification as verification
    original_insert = verification._insert_run

    def crash(*args):
        raise RuntimeError("simulated CLI failure before final insert")

    monkeypatch.setattr(verification, "_insert_run", crash)
    with pytest.raises(RuntimeError, match="CLI failure"):
        run_check(store, settings, project["id"], "unit", record["id"])
    active = next((settings.data_dir / "checks").glob("*/active.json"))
    assert recover_interrupted_runs(store, settings) == []  # The owning CLI is still alive.
    descriptor = json.loads(active.read_text())
    descriptor["runner_identity"] = "different process birth token"
    active.write_text(json.dumps(descriptor))
    monkeypatch.setattr(verification, "_insert_run", original_insert)
    recovered = recover_interrupted_runs(store, settings)
    assert len(recovered) == 1 and recovered[0]["outcome"] == "interrupted"
    assert not recovered[0]["passed"] and recovered[0]["returncode"] is None
    assert not active.exists() and recover_interrupted_runs(store, settings) == []
    assert verification_status(store, record)["status"] == "stale"


def test_runs_are_scoped_and_historical_flags_never_machine_proof(workspace, tmp_path):
    store, project, root, settings = workspace
    profile(root)
    record = task(store, project, reported=True)
    run_check(store, settings, project["id"], "unit", record["id"])
    another = tmp_path / "worktree"
    git(root, "worktree", "add", "--detach", str(another), "HEAD")
    store.add_project_root(project["id"], another)
    assert verification_status(store, record, another)["status"] == "reported"
    profile(another)
    alternate_run = run_check(store, settings, project["id"], "unit", record["id"], another)
    assert alternate_run["passed"]
    (root / "app.py").write_text("main worktree changed")
    assert verification_status(store, record)["status"] == "stale"
    assert verification_status(store, record, another)["status"] == "fresh"
    with store.transaction() as tx:
        validate_completion(store, tx, project["id"], {**record, "_completion_worktree": str(another)})
    with store.transaction() as tx, pytest.raises(ValueError, match="fresh machine"):
        validate_completion(store, tx, project["id"], record)
    with pytest.raises(ValueError, match="another project"):
        validate_completion(store, store, "foreign-project", record)


def test_forget_after_last_output_source_cannot_resurrect_orphan_run(workspace, monkeypatch, tmp_path):
    store, project, root, settings = workspace
    profile(root)
    record = task(store, project)
    import cairnmind.verification as verification
    original_insert = verification._insert_run

    def forget_immediately_before_insert(store, run):
        # The exact race: all immutable evidence exists, then forget wins before
        # the final run's transaction starts.
        assert store.get_source(run["profile_source_id"])
        assert all(store.get_source(source_id) for source_id in run["source_ids"].values())
        assert run["passed"] is True
        store.forget_project(project["id"])
        return original_insert(store, run)

    monkeypatch.setattr(verification, "_insert_run", forget_immediately_before_insert)
    run = run_check(store, settings, project["id"], "unit", record["id"])
    assert run["outcome"] == "cancelled" and not run["passed"] and not run["recorded"]
    assert run["source_ids"] == {} and run["profile_source_id"] is None
    assert "Project removed" in run["error"]
    assert store.query("SELECT FROM check_runs") == []
    assert store.query("SELECT FROM sources") == []
    assert not (settings.data_dir / "checks" / run["id"]).exists()
    assert (root / "app.py").read_text() == "value = 1\n"
    payload = Store._validated_backup(store.backup(tmp_path / "after-forget.devbrain.json.gz"))
    assert payload["projects"] == [] and payload["check_runs"] == []


@pytest.mark.parametrize("changed", ["task", "profile_source", "output_source"])
def test_final_insert_rejects_foreign_task_and_source_references(workspace, tmp_path, changed):
    store, project, root, settings = workspace
    profile(root)
    record = task(store, project)
    run = run_check(store, settings, project["id"], "unit", record["id"])
    foreign = store.add_project("foreign", tmp_path / "foreign")
    foreign_source = store.snapshot(foreign["id"], "fixture://foreign", "foreign source")
    foreign_task = task(store, foreign)
    invalid = {**run, "id": str(uuid4()), "source_ids": dict(run["source_ids"])}
    if changed == "task":
        invalid["task_id"] = foreign_task["id"]
    elif changed == "profile_source":
        invalid["profile_source_id"] = foreign_source["id"]
    else:
        invalid["source_ids"]["stderr"] = foreign_source["id"]
    from cairnmind.verification import _insert_run
    with pytest.raises(ValueError, match="Task removed|Source removed"):
        _insert_run(store, invalid)
    assert store.query("SELECT id FROM check_runs") == [{"id": run["id"]}]
    Store._validated_backup(store.backup(tmp_path / "still-valid.devbrain.json.gz"))


def test_deleted_project_recovery_cleans_own_logs_without_killing_or_deleting_user_files(
        workspace, monkeypatch, tmp_path):
    store, project, root, settings = workspace
    profile(root)
    record = task(store, project)
    import cairnmind.verification as verification

    def crash(*args):
        raise RuntimeError("simulate dead CLI after execution")

    monkeypatch.setattr(verification, "_insert_run", crash)
    with pytest.raises(RuntimeError, match="dead CLI"):
        run_check(store, settings, project["id"], "unit", record["id"])
    active_path = next((settings.data_dir / "checks").glob("*/active.json"))
    active = json.loads(active_path.read_text())
    active.update(runner_identity="previous process birth", process_pid=os.getpid(),
                  process_identity=verification._process_identity(os.getpid()))
    active_path.write_text(json.dumps(active))
    user_directory = active_path.parent / "user-worktree"
    user_directory.mkdir()
    user_file = user_directory / "README"
    user_file.write_text("preserve user files")
    store.forget_project(project["id"])

    def no_arbitrary_kill(*args):
        raise AssertionError("deleted-project recovery must not signal arbitrary PIDs")

    monkeypatch.setattr(verification.os, "killpg", no_arbitrary_kill)
    recovered = recover_interrupted_runs(store, settings)
    assert len(recovered) == 1 and recovered[0]["outcome"] == "cancelled"
    assert not recovered[0]["passed"] and not recovered[0]["recorded"]
    assert recovered[0]["source_ids"] == {}
    assert not active_path.exists()
    assert not (active_path.parent / "stdout").exists()
    assert not (active_path.parent / "stderr").exists()
    assert user_file.read_text() == "preserve user files"
    assert (root / "app.py").read_text() == "value = 1\n"
    assert store.query("SELECT FROM check_runs") == []
    assert recover_interrupted_runs(store, settings) == []
    Store._validated_backup(store.backup(tmp_path / "after-recovery.devbrain.json.gz"))
