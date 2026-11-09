#!/usr/bin/env python3
"""Real isolated uv-tool upgrade from archived 0.7.2 to guarded 0.8.0.

Starts one fresh, scope-owned ArcadeDB engine, stops its verified process before
late native observation, and never starts model inference. Evidence is retained.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
import pwd
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from uuid import uuid4

BASELINE_SHA256 = "6b65e7315895b8c44b84bafc9146dfcdae732c3890f959d0e5e58cbf4fd9aa2c"
PYTHON_IDENTITY = "import json,sys;from pathlib import Path;print(json.dumps({'version':sys.version,'base_executable':str(Path(sys._base_executable).resolve()),'major_minor':list(sys.version_info[:2])}))"
CLEANUP = r'''
import json,sys
from pathlib import Path
from urllib.parse import quote
from devbrain.config import Settings
from devbrain.arcade import ArcadeClient
settings=Settings.load(Path(sys.argv[1]));prefix=sys.argv[2]
assert settings.arcade_database.startswith(prefix+'_')
client=ArcadeClient(settings)
exists=client._request('GET','/api/v1/exists/'+quote(settings.arcade_database)).json().get('result')
if exists is True:
    client.server_command('drop database '+settings.arcade_database)
elif exists is not False:
    raise RuntimeError('Could not establish whether the fixture database exists')
assert client.server_info()
print(json.dumps({'only_owned_database_removed_or_absent':True,'owned_engine_available_before_shutdown':True}))
'''
RESTORE_PROBE = r'''
import gzip,json,sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

def verify_restored_export(expected, actual, journal):
    # Restore intentionally rotates the journal; every other collection, including
    # all vector namespaces and rows, must still match the backup exactly.
    assert set(actual) == set(expected), 'restore_collection_keys'
    for key, value in expected.items():
        if key not in {'format_version', 'exported_at', 'journal_state', 'journal_events'}:
            assert actual[key] == value, key
    assert len(actual['projects']) == 2, 'restore_projects'
    old_states, states, events = expected['journal_state'], actual['journal_state'], actual['journal_events']
    assert len(old_states) == len(states) == 1, 'restore_journal_state_count'
    old, state = old_states[0], states[0]
    assert old['id'] == 'current' and str(UUID(old['epoch'])) == old['epoch'], 'backup_journal_epoch'
    assert type(old['seq']) is int and old['seq'] >= 0, 'backup_journal_sequence'
    epoch = state['epoch']
    assert str(UUID(epoch)) == epoch and epoch != old['epoch'], 'restore_journal_epoch'
    assert state == {'id': 'current', 'epoch': epoch, 'seq': 1, 'first_seq': 1}, 'restore_journal_state'
    assert type(state['seq']) is type(state['first_seq']) is int, 'restore_journal_sequence'
    assert len(events) == 1, 'restore_journal_event_count'
    event = events[0]
    assert event == {'id': f'{epoch}:1', 'epoch': epoch, 'seq': 1, 'project_id': None,
                     'entity_type': 'reset', 'entity_id': None, 'op': 'reset', 'at': event['at']}, 'restore_journal_reset'
    assert type(event['seq']) is int and isinstance(event['at'], str), 'restore_journal_event_types'
    at = datetime.fromisoformat(event['at'])
    assert at.utcoffset() == timezone.utc.utcoffset(None), 'restore_journal_timestamp'
    result = journal.read(after={'epoch': old['epoch'], 'seq': old['seq']})
    assert result == {'epoch': epoch, 'cursor': {'epoch': epoch, 'seq': 1},
                      'events': [], 'reset_required': True}, 'restore_old_cursor'
    assert result['reset_required'] is True and type(result['cursor']['seq']) is int, 'restore_old_cursor_types'
    return {'real_restore_verified': True, 'projects': 2, 'collections': len(expected),
            'journal_reset_verified': True, 'old_cursor_reset_required': True}

if __name__ == '__main__':
    from devbrain.store import Store
    from devbrain.journal import Journal
    data, backup = map(Path, sys.argv[1:3])
    with gzip.open(backup, 'rt') as stream:
        expected = json.load(stream)['payload']
    store = Store(data)
    print(json.dumps(verify_restored_export(expected, store.export(), Journal(store))))
'''


def load_monitor():
    path = Path(__file__).with_name("validate-stability-080.py")
    spec = importlib.util.spec_from_file_location("upgrade080_native_monitor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def qualified_java_environment(environment, runtime):
    """Cover the immutable 0.7.2 engine's shutil.which('java') resolver too."""
    environment = {key: value for key, value in environment.items()
                   if key not in {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"}}
    java = Path(runtime["java_executable"])
    environment["JAVA_HOME"] = str(java.parent.parent)
    environment["DEVBRAIN_JAVA_EXECUTABLE"] = str(java)
    environment["PATH"] = os.pathsep.join([str(java.parent), str(Path(runtime["tmux_executable"]).parent),
                                          environment.get("PATH", "")])
    resolved = shutil.which("java", path=environment["PATH"])
    if not resolved or Path(resolved).resolve() != java:
        raise ValueError("Legacy Java PATH resolution differs from the qualified JDK")
    with Path(resolved).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != runtime["java_binary_sha256"]:
            raise ValueError("The qualified Java binary changed")
    return environment


def fixture_processes(directory, monitor, command=None):
    """Metadata only for children carrying this unique fixture marker."""
    marker = ("DEVBRAIN_UPGRADE_FIXTURE=" + str(directory)).encode()
    command_marker = ("DEVBRAIN_UPGRADE_COMMAND=" + command).encode() if command else None
    rows = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit() and int(path.name) != os.getpid():
            try:
                # Bind ownership evidence to one identity, before and after
                # reading /proc. A reused PID cannot inherit the old marker.
                row = monitor.process(int(path.name))
                if not row or row["state"] in {"Z", "X"} or path.stat().st_uid != os.getuid():
                    continue
                environment = (path / "environ").read_bytes().split(b"\0")
                if marker not in environment or (command_marker and command_marker not in environment):
                    continue
                if monitor.same_process(row):
                    rows.append(row)
            except (FileNotFoundError, ProcessLookupError):
                pass
            except PermissionError as exc:
                raise RuntimeError("Cannot inspect fixture process ownership") from exc
    return rows


def stop_fixture_processes(directory, monitor, command=None):
    """Signal stable pidfd handles, never numeric PIDs or process-group IDs."""
    rows = fixture_processes(directory, monitor, command)
    handles = []
    try:
        for row in rows:
            if not monitor.same_process(row):
                continue
            try:
                handle = os.pidfd_open(row["pid"])
            except ProcessLookupError:
                continue
            handles.append((handle, row))
        for number in (signal.SIGTERM, signal.SIGKILL):
            for handle, row in handles:
                if monitor.same_process(row):
                    try:
                        signal.pidfd_send_signal(handle, number)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 3
            while any(monitor.same_process(row) for _, row in handles) and time.monotonic() < deadline:
                time.sleep(.05)
    finally:
        for handle, _ in handles:
            os.close(handle)
    remaining = fixture_processes(directory, monitor, command)
    if remaining:
        raise RuntimeError("Fixture-owned detached processes survived cleanup")
    return len(rows)


def release_bundle(release_dir, baseline_zip):
    """Read one self-consistent candidate before creating a fixture or executing it."""
    manifest_raw = (release_dir / "devbrain-update-0.8.0-manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    updater = (release_dir / "devbrain-update-0.8.0.py").read_bytes()
    archive = (release_dir / "devbrain-0.8.0.zip").read_bytes()
    def digest(value):
        return hashlib.sha256(value).hexdigest()
    if (manifest.get("version") != "0.8.0" or manifest.get("supported_versions") != ["0.7.2"]
            or manifest.get("stage") != "candidate" or manifest.get("qualified") is not False):
        raise ValueError("Expected an unqualified 0.8.0 candidate manifest")
    if digest(updater) != manifest.get("updater_sha256") or digest(archive) != manifest.get("zip_sha256"):
        raise ValueError("Candidate changed or artifact checksum does not match its manifest")
    if (digest(baseline_zip.read_bytes()) != BASELINE_SHA256
            or manifest["baselines"]["0.7.2"]["sha256"] != BASELINE_SHA256):
        raise ValueError("The immutable 0.7.2 baseline ZIP changed")
    return manifest, manifest_raw, updater, archive


def baseline_uv_shim(uv, python):
    """Only the isolated fixture overrides the old installer's hardcoded 3.12.

    No archived source is edited. All real uv operations and arguments remain
    unchanged except that one interpreter selector. The shim is removed before
    running the candidate updater, whose --python contract is tested directly.
    """
    return (f"#!{python}\nimport os,sys\nargs=sys.argv[1:]\n"
            "if args[:2] == ['tool', 'install']:\n"
            "    position=args.index('--python')+1\n"
            "    assert args[position]=='3.12', 'Unexpected archived Python selector'\n"
            f"    args[position]={str(python)!r}\n"
            f"os.execv({str(uv)!r}, [{str(uv)!r}]+args)\n")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def claim_fresh_engine_root(private_root, engine_root, cache_root):
    """Reject adoption before reading configuration or creating any engine state."""
    if (not private_root.is_absolute() or private_root.resolve(strict=True) != private_root
            or private_root == cache_root or not private_root.is_relative_to(cache_root)):
        raise ValueError("An existing canonical private Linux cache root is required")
    info = private_root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("The private fixture root must be owned and mode 0700")
    if (not engine_root.is_absolute() or engine_root.parent != private_root
            or engine_root.resolve() != engine_root or os.path.lexists(engine_root)):
        raise ValueError("--shared-engine must be a NEW direct child of --root; existing engines cannot be adopted")
    # Exclusive mkdir also refuses another process creating this path after the check.
    engine_root.mkdir(mode=0o700)
    return engine_root


def finish_owned_resources(engine, stop_processes, watch, grace):
    """Even failed startup must attempt own cleanup, then complete late observation."""
    result = {"cleanup_complete": False, "forced_cleanup": False, "errors": [], "detached_processes": 0}
    try:
        stopped = engine.stop()
        if (stopped is not True or engine.report.get("actual_exit_verified") is not True
                or engine.report.get("cleanup_complete") is not True
                or engine.report.get("forced_cleanup") is not False):
            raise RuntimeError("Owned engine exit was not verified")
    except Exception as exc:  # Never skip process cleanup or late watch after engine failure.
        result["errors"].append("owned_engine_cleanup:" + type(exc).__name__)
    finally:
        result["engine"] = dict(engine.report)
        result["forced_cleanup"] = bool(engine.report.get("forced_cleanup"))
    try:
        result["detached_processes"] = stop_processes()
        if result["detached_processes"]:
            result["forced_cleanup"] = True
            result["errors"].append("unexpected_detached_fixture_processes")
    except Exception as exc:
        result["errors"].append("fixture_process_cleanup:" + type(exc).__name__)
    try:
        result["native"] = watch.finish(grace)
    except Exception as exc:
        result["errors"].append("late_native_observation:" + type(exc).__name__)
    result["cleanup_complete"] = not result["errors"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--shared-engine", type=Path, required=True, help="NEW engine home directly under --root; existing engines are refused")
    parser.add_argument("--arcade-dist", type=Path, required=True)
    parser.add_argument("--baseline-zip", type=Path, required=True, help="Exact immutable 0.7.2 ZIP; SHA-256 is pinned")
    parser.add_argument("--release-dir", type=Path, default=Path(__file__).resolve().parents[1] / "releases/candidates/0.8.0")
    parser.add_argument("--python", type=Path, required=True, help="Exact previously qualified Python executable for both installations")
    parser.add_argument("--java", type=Path, help="Exact previously qualified Java executable exposed to the isolated installers")
    parser.add_argument("--tmux", type=Path, help="Exact previously qualified tmux executable exposed to the isolated installers")
    parser.add_argument("--wsl-crash-dir", type=Path, required=True, help="Metadata-only WSL native crash observation")
    parser.add_argument("--cleanup-grace", type=int, default=30)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"execute": False, "status": "prepared_only", "message": "Use the external qualified launcher."}))
        return 0
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.run_id) or os.environ.get("DEVBRAIN_QUALIFICATION_RUN_ID") != args.run_id:
        parser.error("An external supervisor with the same run marker is required")
    artifact_root = args.artifact_root
    if (not artifact_root.is_absolute() or artifact_root.resolve() != artifact_root
            or not str(artifact_root).startswith("/mnt/c/") or not artifact_root.is_dir()
            or (artifact_root / "controller-report.json").exists()):
        parser.error("Use a fresh mapped /mnt/c artifact root")
    supervision_spec = importlib.util.spec_from_file_location("upgrade080_supervision", Path(__file__).with_name("supervise-080.py"))
    supervision = importlib.util.module_from_spec(supervision_spec)
    supervision_spec.loader.exec_module(supervision)
    gate = supervision.read_json(artifact_root / "gate.json")
    runtime = supervision.qualified_runtime(gate)
    common_spec = importlib.util.spec_from_file_location("upgrade080_common", Path(__file__).with_name("validate-native-account080.py"))
    common = importlib.util.module_from_spec(common_spec)
    common_spec.loader.exec_module(common)
    common.require(sys.flags.isolated == 1 and sys.prefix != sys.base_prefix
                   and str(Path(sys._base_executable).resolve()) == runtime["python_executable"],
                   "literal_qualified_venv_isolated_python_required")
    proof = supervision.read_json(artifact_root / "mapping-proof.json")
    common.require(proof.get("run_id") == args.run_id, "scope_proof_run_id_mismatch")
    scope = supervision.ScopeOwnership(proof.get("scope_unit"))
    parent = supervision.identity(os.getppid())
    common.require(parent is not None and scope.owns(parent), "supervisor_parent_required")
    for name, path in (("python", args.python), ("java", args.java), ("tmux", args.tmux)):
        if path is None or str(path.expanduser().resolve()) != runtime[name + "_executable"]:
            parser.error("Smoke runtime arguments must match the passed gate: " + name)
    if args.cleanup_grace < 30:
        parser.error("At least 30 seconds of post-cleanup observation is required")
    if not __debug__:
        parser.error("This validation must run with Python assertions enabled")
    spec = importlib.util.spec_from_file_location("archived_smoke_helpers",
                                                Path(__file__).with_name("test_devbrain_update071_smoke.py"))
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    digest, write_json = helpers.digest, helpers.write_json
    release_dir = args.release_dir.expanduser().resolve()
    selected_python = args.python.expanduser().resolve()
    if not selected_python.is_file() or not os.access(selected_python, os.X_OK):
        raise ValueError("The selected Python executable is unavailable")
    baseline_zip = args.baseline_zip.resolve(strict=True)
    manifest, manifest_raw, updater_raw, archive_raw = release_bundle(release_dir, baseline_zip)
    dist = args.arcade_dist.resolve(strict=True)
    common.require(args.arcade_dist == dist and (dist / "lib").is_dir(), "canonical_arcade_distribution_required")
    monitor = load_monitor()
    watch = monitor.CrashWatch(args.wsl_crash_dir)
    engine_home = claim_fresh_engine_root(args.root, args.shared_engine,
                                         Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache")
    directory = args.root / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    # Execute an immutable local snapshot even if a subsequent release build
    # refreshes the workspace artifacts while this validation is running.
    updater = directory / "devbrain-update-0.8.0.py"
    updater.write_bytes(updater_raw)
    updater.chmod(0o600)
    source, data, state, isolated_home, tools, bins = (directory / name for name in
                                                     ("source", "data", "state", "home", "tools", "bin"))
    backup_venv, restore_data = directory / "backup-072-venv", directory / "restore-data"
    backup_python = backup_venv / "bin/python"
    bins.mkdir()
    isolated_home.mkdir()
    prefix = "devbrain_upgrade080_" + uuid4().hex[:10]
    from devbrain.config import Settings
    settings = Settings(data_dir=engine_home, arcade_url=f"http://127.0.0.1:{free_port()}",
                        arcade_password=secrets.token_urlsafe(32), token=secrets.token_urlsafe(32),
                        arcade_database=prefix + "_engine", java_executable=runtime["java_executable"],
                        tmux_executable=runtime["tmux_executable"], ollama_url="http://127.0.0.1:1")
    settings.save()
    engine = common.OwnedEngine(settings, dist, runtime, scope, supervision, directory)
    inherited_home = Path.home()
    uv = Path(shutil.which("uv") or inherited_home / ".local/bin/uv").resolve()
    tmux, java = args.tmux, args.java
    assert uv.is_file() and tmux and java
    tmux, java = Path(tmux).expanduser().resolve(), Path(java).expanduser().resolve()
    for executable in (tmux, java):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("Selected runtime executable is unavailable: " + str(executable))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("DEVBRAIN_") and key not in
           {"UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "TMUX", "TMUX_PANE"}}
    env.update(HOME=str(isolated_home), DEVBRAIN_HOME=str(data), UV_TOOL_DIR=str(tools),
               DEVBRAIN_QUALIFICATION_RUN_ID=args.run_id,
               DEVBRAIN_UPGRADE_FIXTURE=str(directory),
               UV_TOOL_BIN_DIR=str(bins), UV_CACHE_DIR=str(inherited_home / ".cache/uv"), UV_OFFLINE="true",
               XDG_STATE_HOME=str(state), DEVBRAIN_ARCADE_DIST=str(dist), DEVBRAIN_ARCADE_DB_PREFIX=prefix,
               DEVBRAIN_ARCADE_URL=settings.arcade_url, DEVBRAIN_ARCADE_USER=settings.arcade_user,
               DEVBRAIN_ARCADE_PASSWORD=settings.arcade_password,
               DEVBRAIN_JAVA_EXECUTABLE=str(java), DEVBRAIN_TMUX_EXECUTABLE=str(tmux),
               COPILOT_SKIP_CLI_DOWNLOAD="1",
               PATH=str(bins) + os.pathsep + str(uv.parent) + os.pathsep + os.environ["PATH"],
               LANG="C.UTF-8", FIXTURE_071_NATIVE_TRACE=str(directory / "native-trace.jsonl"))
    for name in ("codex", "claude"):
        fake = bins / name
        fake.write_text("#!" + str(selected_python) + "\n" + helpers.FAKE_NATIVE)
        fake.chmod(0o700)
    for name, executable in (("java", java), ("tmux", tmux)):
        (bins / name).symlink_to(executable)
    binary, python = bins / "devbrain", tools / "devbrain/bin/python"
    checks = {}
    report = {"schema_version": 1, "run_id": args.run_id, "runtime": runtime, "cleanup_complete": False,
              "forced_cleanup": False,
              "python_launcher": sys.executable, "python_prefix": str(Path(sys.prefix).resolve()),
              "status": "running", "from_version": "0.7.2", "to_version": "0.8.0",
              "fixture_directory": str(directory), "baseline_sha256": digest(baseline_zip.read_bytes()),
              "engine_home": str(engine_home), "engine_ownership": "fresh_controller_child_in_qualified_uuid_scope",
              "release_dir": str(release_dir), "candidate_only": True,
              "updater_sha256": digest(updater_raw), "manifest_sha256": digest(manifest_raw),
              "release_zip_sha256": digest(archive_raw),
              "baseline_install_mode": "unchanged_archived_installer_with_fixture_uv_python_selector_override",
              "baseline_original_python_selector": "3.12", "selected_python": str(selected_python),
              "selected_java": str(java), "selected_tmux": str(tmux),
              "initial_boot_id": watch.initial_boot_id,
              "native_monitor_sha256": digest(Path(monitor.__file__).read_bytes()),
              "external_controller_finalization_required": True,
              "controller_exit_verified": False,
              "scope": "devbrain_upgrade_smoke_controller",
              "model_inference": False, "checks": checks}
    count = 0

    native_errors = []
    forced_cleanup_count = 0

    def observe(*, cleanup=False):
        try:
            observed = watch.poll()
            if observed["native_failure"] and not cleanup:
                raise RuntimeError("A new native failure invalidated the candidate smoke")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            native_errors.append(str(exc))
            if not cleanup:
                raise

    def run(command, label, timeout=300, expected=0, *, cleanup=False):
        nonlocal count, forced_cleanup_count
        count += 1
        log = directory / f"{count:02d}-{label}.log"
        print(json.dumps({"phase": label, "log": str(log)}), flush=True)
        report["current_phase"] = label
        write_json(directory / "report.json", report)
        observe(cleanup=cleanup)
        command_id = uuid4().hex
        child_environment = qualified_java_environment(env, runtime)
        process = subprocess.Popen(list(map(str, command)), cwd=directory,
                                   env={**child_environment, "DEVBRAIN_UPGRADE_COMMAND": command_id},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        deadline = time.monotonic() + timeout
        stdout = stderr = ""
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=max(.01, min(1, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    observe(cleanup=cleanup)
                    if time.monotonic() >= deadline:
                        raise
            if fixture_processes(directory, monitor, command_id):
                raise RuntimeError("Command exited with live fixture descendants")
            observe(cleanup=cleanup)
        finally:
            forced_cleanup_count += stop_fixture_processes(directory, monitor, command_id)
            final_stdout, final_stderr = process.communicate(timeout=5)
            stdout, stderr = final_stdout or stdout, final_stderr or stderr
            log.write_text(stdout + stderr)
            log.chmod(0o600)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        assert result.returncode == expected, (label, result.returncode, str(log))
        return result

    installation_started = False
    try:
        previous_environment = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(qualified_java_environment(env, runtime))
            engine.start()
        finally:
            os.environ.clear()
            os.environ.update(previous_environment)
        report["engine"] = dict(engine.report)
        checks["fresh_owned_engine_ready"] = engine.report.get("ready") is True
        observe()
        identity = json.loads(run([selected_python, "-I", "-B", "-c", PYTHON_IDENTITY], "selected-python").stdout)
        assert identity["major_minor"] >= [3, 12]
        assert Path(identity["base_executable"]).samefile(selected_python)
        report["selected_python_identity"] = identity
        helpers.extract(baseline_zip, source)
        # This wheel/venv is independent of both the mutable source checkout and
        # uv's devbrain tool venv, which the candidate installer replaces.
        constraints = directory / "backup-072-constraints.txt"
        run([uv, "export", "--directory", source, "--locked", "--no-dev", "--no-emit-project",
             "--format", "requirements-txt", "--output-file", constraints], "backup-072-locked-constraints")
        run([uv, "venv", "--python", selected_python, backup_venv], "backup-072-venv")
        run([uv, "pip", "install", "--python", backup_python, "--constraints", constraints, source],
            "backup-072-immutable-wheel", timeout=600)
        write_json(data / "config.json", {"port": free_port(), "cockpit_port": free_port(),
                                         "ollama_url": "http://127.0.0.1:1", "poll_seconds": 3600})
        shim = bins / "uv"
        shim.write_text(baseline_uv_shim(uv, selected_python))
        shim.chmod(0o700)
        try:
            installation_started = True
            run(["bash", source / "scripts/install-wsl.sh"], "baseline-install-real-uv-explicit-python", timeout=600)
        finally:
            shim.unlink(missing_ok=True)
        installed_identity = json.loads(run([python, "-I", "-B", "-c", PYTHON_IDENTITY], "baseline-python-identity").stdout)
        assert Path(installed_identity["base_executable"]).samefile(selected_python)
        checks["baseline_uses_selected_python"] = installed_identity
        project, neighbor = directory / "project", directory / "neighbor"
        project.mkdir()
        neighbor.mkdir()
        baseline = directory / "baseline.json"
        run([python, "-I", "-B", "-c", helpers.SEED, data, project, neighbor, baseline, "0.7.2", free_port()],
            "seed-owned-database")
        protected = {
            data / "config.json": (data / "config.json").read_bytes(),
            data / "models.lock.json": b'{"canary":"existing model metadata"}\n',
            project / ".codex/config.toml": b'approval_policy="on-request"\nsandbox_mode="workspace-write"\n',
            project / ".claude/settings.local.json": b'{"permissions":{"deny":["Bash(rm:*)"]}}\n',
            isolated_home / ".codex/auth.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
            isolated_home / ".claude/.credentials.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
            data / "runtime/copilot/config.json": b'{"fixture_auth":"PRIVATE_AUTH_CANARY_071"}\n',
        }
        for path, raw in protected.items():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(raw)
            path.chmod(0o600)
        keys = data / "credentials/api-keys.json"
        write_json(keys, {"version": 1, "keys": {"upgrade-key": "API_PRIVATE_CANARY_071"}})
        keys.parent.chmod(0o700)
        protected[keys] = keys.read_bytes()
        preferences = data / "cockpit/preferences.json"
        write_json(preferences, {"version": 2, "profiles": [
            {"id": "codex", "client": "codex", "label": "Custom Codex", "model": "native-fixture"},
            {"id": "claude", "client": "claude", "label": "Custom Claude"},
            {"id": "upgrade-key", "client": "api", "label": "Preserve API", "provider": "openai",
             "model": "fixture-model", "credential_ref": "profile:upgrade-key"},
            {"id": "copilot", "client": "copilot", "label": "Preserve Copilot", "model": "fixture-model"}],
            "scan_roots": [], "default_slots": [{"id": "codex", "profile_id": "codex"},
                                                {"id": "claude", "profile_id": "claude"}],
            "project_slots": {}, "last_project": None})
        protected[preferences] = preferences.read_bytes()
        before_source = helpers.inventory(source)
        old_binary_trace = directory / "old-console-script-invoked.txt"
        binary.write_text(f"#!{selected_python}\nfrom pathlib import Path\nPath({str(old_binary_trace)!r}).write_text('unexpected old console invocation')\nraise SystemExit(91)\n")
        binary.chmod(0o700)
        update_command = [selected_python, updater, "--source-dir", source, "--python", selected_python,
                          "--backup-python", backup_python, "--runtime-gate", artifact_root / "gate.json", "--data-dir", data]
        run([*update_command, "--check-only"], "check-only")
        assert helpers.inventory(source) == before_source and not state.exists()
        checks["check_only_nonmutating"] = True
        run(update_command, "apply-exact-updater", timeout=600)
        assert not old_binary_trace.exists(), "Updater executed the unqualified old console script"
        checks["old_console_script_never_executed"] = True
        installed_identity = json.loads(run([python, "-I", "-B", "-c", PYTHON_IDENTITY], "candidate-python-identity").stdout)
        assert Path(installed_identity["base_executable"]).samefile(selected_python)
        checks["candidate_uses_selected_python"] = installed_identity
        probe_script = helpers.PROBE.replace("=='0.7.1'", "=='0.8.0'").replace("'version':'0.7.1'", "'version':'0.8.0'")
        probe = json.loads(run([python, "-I", "-B", "-c", probe_script, data, baseline, source],
                               "probe-installed-package-and-memory").stdout)
        checks.update({key: value for key, value in probe.items() if key != "database"})
        for path, raw in protected.items():
            assert path.read_bytes() == raw and path.stat().st_mode & 0o777 == 0o600, str(path)
        checks["profiles_keys_auth_config_models_permissions_preserved"] = len(protected)
        for row in manifest["files"]:
            assert digest((source / row["path"]).read_bytes()) == row["after"], row["path"]
        checks["verified_updated_source_files"] = len(manifest["files"])
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as archive:
            for name in archive.namelist():
                if not name.endswith("/"):
                    assert (source / name.removeprefix("devbrain/")).read_bytes() == archive.read(name), name
        checks["all_sources_match_candidate_zip"] = True
        backups = list((state / "devbrain-updates").glob("*/database.devbrain.json.gz"))
        assert len(backups) == 1
        with gzip.open(backups[0], "rt") as handle:
            backup = json.load(handle)
        assert backup["payload"]["format_version"] == 5
        assert backup["sha256"] == digest(json.dumps(backup["payload"], ensure_ascii=False,
            separators=(",", ":"), sort_keys=True).encode())
        old = json.loads(baseline.read_text())["export"]
        for key, rows in old.items():
            if key not in {"format_version", "exported_at"}:
                assert backup["payload"][key] == rows, key
        assert "API_PRIVATE_CANARY_071" not in json.dumps(backup)
        assert "PRIVATE_AUTH_CANARY_071" not in json.dumps(backup)
        checks["database_backup_exact_private_and_excludes_credentials"] = True
        backup_runtime = json.loads((backups[0].parent / "backup-runtime.json").read_text())
        assert backup_runtime["backup_python"] == str(backup_python)
        assert backup_runtime["backup_identity"]["version"] == "0.7.2"
        assert backup_runtime["backup_identity"]["base_executable"] == str(selected_python)
        assert backup_runtime["backup_identity"]["base_sha256"] == runtime["python_binary_sha256"]
        assert Path(backup_runtime["backup_identity"]["origin"]).is_relative_to(backup_venv)
        checks["backup_uses_independent_072_wheel_on_qualified_base"] = True
        restore_config = json.loads((data / "config.json").read_text())
        restore_config.update(data_dir=str(restore_data), arcade_database=prefix + "_restore", port=free_port(), cockpit_port=free_port())
        write_json(restore_data / "config.json", restore_config)
        run([backup_python, "-I", "-B", "-m", "devbrain", "--data-dir", restore_data,
             "restore", backups[0]], "real-restore-through-qualified-072", timeout=180)
        restored = json.loads(run([backup_python, "-I", "-B", "-c", RESTORE_PROBE, restore_data, backups[0]],
                                  "verify-real-072-restore").stdout)
        assert restored["real_restore_verified"]
        current_restored = json.loads(run([python, "-I", "-B", "-c", RESTORE_PROBE, restore_data, backups[0]],
                                          "verify-080-reads-restored-072-data").stdout)
        assert current_restored["real_restore_verified"]
        checks["real_072_backup_restore_and_080_readback"] = True
        assert backups[0].stat().st_mode & 0o777 == 0o600
        saved = json.loads((backups[0].parent / "manifest.json").read_text())
        with zipfile.ZipFile(baseline_zip) as archive:
            for entry in saved:
                path = backups[0].parent / entry["path"]
                if entry["existed"]:
                    assert path.read_bytes() == archive.read("devbrain/" + entry["path"])
                    assert path.stat().st_mode & 0o777 == 0o600
        checks["original_source_backup_exact_and_private"] = len(saved)
        paths = [*source.rglob("*"), *state.rglob("*"), *protected]
        before = helpers.file_signatures(paths)
        reapplied = run(update_command, "idempotent-reapply")
        assert "pliki i uslugi pozostaly bez zmian" in reapplied.stdout
        assert helpers.file_signatures(paths) == before
        checks["reapply_preserves_bytes_modes_mtimes_inodes"] = len(before)
        assert json.loads(run([binary, "version"], "installed-version").stdout)["version"] == "0.8.0"
        assert not (data / "arcade/process.json").exists(), "Installer replaced the controller-owned test engine"
        assert not (restore_data / "arcade/process.json").exists(), "Restore replaced the controller-owned test engine"
        checks["only_controller_owned_engine_was_used"] = True
        report["status"] = "checks_complete_pending_cleanup"
    except Exception as exc:  # noqa: BLE001 — preserve any smoke failure as evidence before cleanup
        import traceback
        report.update(status="failed", failure_type=type(exc).__name__, failure=str(exc))
        (directory / "traceback.txt").write_text(traceback.format_exc())
    finally:
        if installation_started and (backup_python.exists() or python.exists()) and (data / "config.json").exists():
            try:
                cleaner = backup_python if backup_python.exists() else python
                cleaned = run([cleaner, "-I", "-B", "-c", CLEANUP, data, prefix], "cleanup-owned-database", cleanup=True)
                checks.update(json.loads(cleaned.stdout))
            except Exception as exc:  # noqa: BLE001 — cleanup failure must fail the report
                report.update(status="failed", cleanup_error=str(exc))
        if backup_python.exists() and (restore_data / "config.json").exists():
            try:
                run([backup_python, "-I", "-B", "-c", CLEANUP, restore_data, prefix], "cleanup-restored-owned-database", cleanup=True)
                checks["restored_database_removed"] = True
            except Exception as exc:  # noqa: BLE001 — isolated restore failure must not leak its database
                report.update(status="failed", cleanup_error=str(exc))
        resources = finish_owned_resources(engine, lambda: stop_fixture_processes(directory, monitor),
                                            watch, args.cleanup_grace)
        report["engine"] = resources["engine"]
        report["resource_cleanup_errors"] = resources["errors"]
        forced_cleanup_count += resources["detached_processes"]
        report["forced_cleanup"] = forced_cleanup_count > 0 or resources["forced_cleanup"]
        checks["owned_process_cleanup_complete"] = resources["cleanup_complete"]
        checks["detached_fixture_processes_stopped"] = resources["detached_processes"]
        try:
            final = resources["native"]
            native = {"initial_boot_id": watch.initial_boot_id, "final_boot_id": final["boot_id"],
                      "native_failure": final["native_failure"], "new_wsl_dumps": final.get("new_wsl_dumps", []),
                      "new_kernel_fatals": final.get("new_kernel_fatals", []), "errors": native_errors,
                      "cleanup_complete": (resources["cleanup_complete"] and "cleanup_error" not in report)}
            native["passed"] = (not native["native_failure"] and not native_errors and native["cleanup_complete"]
                                and bool(native["initial_boot_id"]) and native["initial_boot_id"] == native["final_boot_id"])
            report["native_fatal_qualification"] = native
            report["cleanup_complete"] = native["cleanup_complete"]
            report["status"] = ("upgrade_smoke_completed_pending_attestation"
                if report["status"] == "checks_complete_pending_cleanup" and native["passed"] and not report["forced_cleanup"] else "failed")
        except Exception as exc:  # noqa: BLE001 — failed observation cannot qualify a run
            report.update(status="failed", native_observation_error=str(exc))
        write_json(directory / "report.json", report)
        supervision.atomic_json(artifact_root / "controller-report.json", report)
        print(json.dumps({"status": report["status"], "report": str(directory / "report.json")}), flush=True)
    return 0 if report["status"] == "upgrade_smoke_completed_pending_attestation" else 1


if __name__ == "__main__":
    raise SystemExit(main())
