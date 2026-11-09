"""Guard checks for an explicitly selected candidate; never an installation.

pytest: DEVBRAIN_TEST_UPDATER_PATH=/absolute/candidate.py python -m pytest ...
CLI:    python test_devbrain_update080_guards.py --updater /absolute/candidate.py
The ownership tests use short harmless processes, never DevBrain services/JVM.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest


@pytest.fixture
def updater():
    default = Path(__file__).resolve().parents[1] / "releases/candidates/0.8.0/devbrain-update-0.8.0.py"
    path = Path(os.environ.get("DEVBRAIN_TEST_UPDATER_PATH", default)).expanduser().resolve()
    if not path.is_file():
        pytest.fail("Build a candidate first or set DEVBRAIN_TEST_UPDATER_PATH: " + str(path))
    # __name__ is not __main__; no CLI code, service calls or pycache writes.
    module = types.ModuleType("update080_guards")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)  # noqa: S102 — explicitly selected local test module
    return module


def accelerate(monkeypatch, module):
    original_sleep = time.sleep
    ticks = itertools.count(0, 0.25)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda _: original_sleep(0.001))


def test_dead_and_reused_processes_are_never_signalled(updater, monkeypatch):
    monkeypatch.setattr(updater.os, "pidfd_open", lambda *a: pytest.fail("Foreign process handle opened"))
    monkeypatch.setattr(updater, "process_identity", lambda pid: None)
    updater.wait_for_broker_exit({"pid": 1234, "birth": "old"})
    monkeypatch.setattr(updater, "process_identity", lambda pid: ("new", "S"))
    updater.wait_for_broker_exit({"pid": 1234, "birth": "old"})


def test_embedded_http_host_cannot_be_killed(updater, monkeypatch):
    birth, _ = updater.process_identity(os.getpid())
    monkeypatch.setattr(
        updater.os, "pidfd_open", lambda *a: pytest.fail("Own test host opened for signalling")
    )
    accelerate(monkeypatch, updater)
    with pytest.raises(ValueError, match="Poprzedni broker nadal"):
        updater.wait_for_broker_exit({"pid": os.getpid(), "birth": birth, "instance": "not-a-daemon"})


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"), reason="Linux pidfd required"
)
def test_stalled_owned_daemon_is_stopped_without_killing_unrelated_process(updater, monkeypatch, tmp_path):
    package = tmp_path / "devbrain"
    package.mkdir()
    (package / "__init__.py").write_text("")
    ready = tmp_path / "ready.json"
    (package / "__main__.py").write_text(
        "import json,os,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        f'Path({str(ready)!r}).write_text(json.dumps({{"pid":os.getpid()}}))\n'
        "while True:time.sleep(.1)\n"
    )
    data = tmp_path / "isolated-data"
    owned = subprocess.Popen(
        [sys.executable, "-m", "devbrain", "--data-dir", str(data), "cockpit", "serve"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(ready.read_text())["pid"] == owned.pid
        birth, _ = updater.process_identity(owned.pid)
        info = {
            "pid": owned.pid,
            "birth": birth,
            "instance": hashlib.sha256(str(data.resolve()).encode()).hexdigest()[:24],
        }
        with monkeypatch.context() as patch:
            accelerate(patch, updater)
            updater.wait_for_broker_exit(info)
        assert owned.wait(timeout=3) == -signal.SIGKILL
        assert unrelated.poll() is None
    finally:
        for child in (owned, unrelated):
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)


@pytest.fixture
def isolated_apply(updater, tmp_path, monkeypatch):
    import base64
    import gzip
    import zlib
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/install-wsl.sh").write_text("exit 0\n")
    (root / "pyproject.toml").write_text('[project]\nversion="0.8.0"\n')
    payload = {"version": "0.8.0", "supported_versions": ["0.7.2"], "files": [], "backup_version": "0.7.2",
               "backup_baseline_sha256": "baseline", "backup_package_sha256": {}, "backup_dependency_pins": {},
               "target_package_sha256": {}, "target_dependency_pins": {}}
    monkeypatch.setattr(updater, "PAYLOAD", base64.b64encode(zlib.compress(json.dumps(payload).encode())).decode())
    state, data = tmp_path / "state", tmp_path / "private-data"
    data.mkdir()
    (data / "config.json").write_text('{"canary":"preserve"}')
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/bin/uv" if name == "uv" else pytest.fail("Old console lookup"))
    selected = Path(sys._base_executable).resolve()
    runtime = {"python_executable": str(selected), "python_binary_sha256": updater.file_digest(selected),
               "java_executable": "/qualified/bin/java", "tmux_executable": "/qualified/bin/tmux"}
    backup = tmp_path / "backup-env/bin/python"
    identity = {"version": "0.7.2", "prefix": str(tmp_path / "backup-env"), "python_launcher": str(backup)}
    monkeypatch.setattr(updater, "verify_runtime_gate", lambda *args: (runtime, "gate-sha"))
    monkeypatch.setattr(updater, "verify_package", lambda *args: identity)
    monkeypatch.setattr(updater, "verify_java_environment", lambda *args: None)
    calls, installed = [], {"value": False}
    monkeypatch.setattr(updater, "installed_target", lambda *args: {"python_launcher": "/new/bin/python"} if installed["value"] else None)
    def run(args, **kwargs):
        args = list(map(str, args))
        calls.append((args, kwargs))
        if args == ["/bin/uv", "tool", "dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=str(tmp_path / "tools"))
        if args[0] == "/qualified/bin/tmux":
            return subprocess.CompletedProcess(args, 0, stdout="tmux 3.4\n")
        if args[-1] == "status":
            return subprocess.CompletedProcess(args, 2, stdout="")
        if len(args) >= 2 and args[-2] == "backup":
            content = {"format_version": 5, "projects": []}
            envelope = {"format": "devbrain-arcadedb-backup", "version": 1, "payload": content,
                        "sha256": updater.digest(json.dumps(content, separators=(",", ":"), sort_keys=True).encode())}
            with gzip.open(args[-1], "wt") as stream:
                json.dump(envelope, stream)
            Path(args[-1]).chmod(0o600)
        if args[0] == "bash":
            installed["value"] = True
        return subprocess.CompletedProcess(args, 0, stdout="")
    monkeypatch.setattr(updater.subprocess, "run", run)
    return types.SimpleNamespace(source=root, state=state, data=data, backup=backup, selected=selected,
        calls=calls, runtime=runtime, identity=identity, installed=installed,
        args={"python_executable": selected, "backup_python": backup, "runtime_gate": tmp_path / "gate.json", "data_dir": data})


def test_successful_installed_receipt_is_required_for_noop(updater, isolated_apply):
    fixture = isolated_apply
    assert updater.apply(fixture.source, **fixture.args) == 0
    calls, state = fixture.calls, fixture.state
    assert any(row[0][0] == "bash" for row in calls)
    assert list((state / "devbrain-updates").glob("installed-*.json"))
    calls.clear()
    assert updater.apply(fixture.source, **fixture.args) == 0
    assert [row[0] for row in calls] == [["/bin/uv", "tool", "dir"], ["/qualified/bin/tmux", "-V"]]
    next((state / "devbrain-updates").glob("installed-*.json")).unlink()
    calls.clear()
    assert updater.apply(fixture.source, **fixture.args) == 0
    assert any(row[0][0] == "bash" for row in calls)


def test_known_version_file_deletion_is_not_mistaken_for_an_older_baseline(updater, tmp_path, monkeypatch):
    import base64
    import zlib

    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/install-wsl.sh").write_text("exit 0\n")
    (root / "pyproject.toml").write_text('[project]\nversion="0.5.0"\n')
    old, new = b"known optional file from 0.5", b"updated file"
    row = {
        "path": "optional.js",
        "before": [None, updater.digest(old)],
        "before_by_version": {"0.4.0": None, "0.5.0": updater.digest(old)},
        "after": updater.digest(new),
        "encoding": "utf-8",
        "content": new.decode(),
    }
    package = {"version": "0.6.0", "supported_versions": ["0.4.0", "0.5.0"], "files": [row]}
    monkeypatch.setattr(
        updater, "PAYLOAD", base64.b64encode(zlib.compress(json.dumps(package).encode())).decode()
    )
    with pytest.raises(ValueError, match="zmieniony lokalnie"):
        updater.apply(root, check_only=True)
    (root / "optional.js").write_bytes(old)
    assert updater.apply(root, check_only=True) == 0
    assert (root / "optional.js").read_bytes() == old


def test_removed_target_file_is_deleted_only_when_it_matches_the_baseline(updater, tmp_path, monkeypatch):
    import base64
    import zlib

    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/install-wsl.sh").write_text("exit 0\n")
    (root / "pyproject.toml").write_text('[project]\nversion="0.7.2"\n')
    old = b"retired module from 0.7.2"
    row = {"path": "src/devbrain/tui.py", "before": [updater.digest(old)],
           "before_by_version": {"0.7.2": updater.digest(old)}, "after": None, "encoding": "delete", "content": ""}
    package = {"version": "0.8.0", "supported_versions": ["0.7.2"], "files": [row]}
    monkeypatch.setattr(
        updater, "PAYLOAD", base64.b64encode(zlib.compress(json.dumps(package).encode())).decode()
    )
    assert updater.apply(root, check_only=True) == 0  # already absent: nothing to delete
    target = root / "src/devbrain/tui.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"locally edited")
    with pytest.raises(ValueError, match="nie usunieto"):
        updater.apply(root, check_only=True)
    target.write_bytes(old)
    assert updater.apply(root, check_only=True) == 0
    assert target.read_bytes() == old  # check-only never deletes


def test_check_only_with_explicit_python_never_runs_install_or_services(updater, tmp_path, monkeypatch):
    import base64
    import zlib

    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/install-wsl.sh").write_text("exit 0\n")
    project = root / "pyproject.toml"
    project.write_text('[project]\nversion="0.7.2"\n')
    package = {"version": "0.8.0", "supported_versions": ["0.7.2"], "files": []}
    monkeypatch.setattr(updater, "PAYLOAD", base64.b64encode(zlib.compress(json.dumps(package).encode())).decode())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(updater.shutil, "which", lambda *args: pytest.fail("check-only searched for installer/service tools"))
    calls = []

    def probe(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(updater.subprocess, "run", probe)
    before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
    assert updater.apply(root, check_only=True, python_executable=Path(sys.executable)) == 0
    assert calls == []
    assert before == {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}
    assert not (tmp_path / "state").exists()


def test_backup_and_stops_use_qualified_072_prefix_and_explicit_data(updater, isolated_apply, monkeypatch):
    fixture = isolated_apply
    monkeypatch.setenv("DEVBRAIN_HOME", "/unrelated/private")
    monkeypatch.setenv("DEVBRAIN_ARCADE_DATABASE", "unrelated_database")
    monkeypatch.setenv("PYTHONPATH", "/unqualified/site-packages")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-XX:TieredStopAtLevel=1")
    monkeypatch.setenv("JDK_JAVA_OPTIONS", "-Xint")
    monkeypatch.setenv("_JAVA_OPTIONS", "-XX:-TieredCompilation")
    monkeypatch.setenv("JAVA_HOME", "/unqualified/jdk")
    assert updater.apply(fixture.source, **fixture.args) == 0
    expected = [str(fixture.backup), "-I", "-B", "-m", "devbrain", "--data-dir", str(fixture.data)]
    commands = [row for row in fixture.calls if "devbrain" in row[0]]
    assert commands and all(argv[:len(expected)] == expected for argv, _ in commands)
    assert any(argv[-2] == "backup" for argv, _ in commands)
    for _, kwargs in commands:
        assert kwargs["env"]["DEVBRAIN_HOME"] == str(fixture.data)
        assert not {"DEVBRAIN_ARCADE_DATABASE", "PYTHONPATH", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"} & kwargs["env"].keys()
        assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(Path(fixture.runtime["java_executable"]).parent)
        assert kwargs["env"]["JAVA_HOME"] == str(Path(fixture.runtime["java_executable"]).parent.parent)
    install = next(kwargs for argv, kwargs in fixture.calls if argv[0] == "bash")
    assert install["env"]["DEVBRAIN_INSTALL_PYTHON"] == str(fixture.selected)
    assert install["env"]["DEVBRAIN_HOME"] == str(fixture.data)
    receipt = json.loads(next((fixture.state / "devbrain-updates").glob("installed-*.json")).read_text())
    assert receipt["backup_python"] == str(fixture.backup) and receipt["runtime_gate_sha256"] == "gate-sha"
    assert receipt["data_dir"] == str(fixture.data) and receipt["format_version"] == 2


@pytest.mark.parametrize("field", ["python_executable", "backup_python", "runtime_gate", "data_dir"])
def test_apply_requires_all_backup_safety_inputs_before_any_process(updater, isolated_apply, field):
    values = dict(isolated_apply.args)
    values[field] = None
    with pytest.raises(ValueError, match="Apply requires"):
        updater.apply(isolated_apply.source, **values)
    assert isolated_apply.calls == [] and not isolated_apply.state.exists()


@pytest.mark.parametrize("barrier", ["verify_runtime_gate", "verify_package", "verify_java_environment"])
def test_failed_qualification_never_reaches_status_stop_backup_or_install(updater, isolated_apply, monkeypatch, barrier):
    def reject(*args):
        raise ValueError("qualification rejected")
    monkeypatch.setattr(updater, barrier, reject)
    with pytest.raises(ValueError, match="qualification rejected"):
        updater.apply(isolated_apply.source, **isolated_apply.args)
    assert isolated_apply.calls == [] and not isolated_apply.state.exists()
    assert (isolated_apply.data / "config.json").read_text() == '{"canary":"preserve"}'


@pytest.mark.parametrize("nested", [False, True])
def test_backup_venv_cannot_be_the_replaced_tool_environment(updater, isolated_apply, monkeypatch, nested):
    fixture = isolated_apply
    target = fixture.state.parent / "tools/devbrain"
    fixture.identity["prefix"] = str(target / "nested" if nested else target)
    with pytest.raises(ValueError, match="independent of the uv tool"):
        updater.apply(fixture.source, **fixture.args)
    assert [argv for argv, _ in fixture.calls] == [["/bin/uv", "tool", "dir"]]
    assert not fixture.state.exists()


def test_corrupt_backup_prevents_install(updater, isolated_apply, monkeypatch):
    import base64
    import zlib
    path = isolated_apply.source / "app.txt"
    path.write_bytes(b"original source")
    package = json.loads(zlib.decompress(base64.b64decode(updater.PAYLOAD)))
    package["files"] = [{"path": "app.txt", "before": [updater.digest(b"original source")],
                         "after": updater.digest(b"changed source"), "encoding": "utf-8", "content": "changed source"}]
    monkeypatch.setattr(updater, "PAYLOAD", base64.b64encode(zlib.compress(json.dumps(package).encode())).decode())
    def reject(path):
        raise ValueError("corrupt backup")
    monkeypatch.setattr(updater, "validate_backup", reject)
    with pytest.raises(ValueError, match="corrupt backup"):
        updater.apply(isolated_apply.source, **isolated_apply.args)
    assert not any(argv[0] == "bash" for argv, _ in isolated_apply.calls)
    assert not list((isolated_apply.state / "devbrain-updates").glob("installed-*.json"))
    assert path.read_bytes() == b"original source"


@pytest.mark.parametrize("changed", [None, "version", "base_sha256", "origin", "package_sha256", "dependencies", "missing_dependency"])
def test_backup_package_identity_accepts_only_expected_package(updater, tmp_path, monkeypatch, changed):
    selected = Path(sys._base_executable).resolve()
    runtime = {"python_executable": str(selected), "python_binary_sha256": updater.file_digest(selected),
               "java_executable": "/qualified/bin/java", "tmux_executable": "/qualified/bin/tmux"}
    files = {"__init__.py": "expected"}
    result = {"version": "0.7.2", "base_executable": str(selected), "base_sha256": runtime["python_binary_sha256"],
              "python_version": [3, 13, 18], "prefix": str(tmp_path / "venv"),
              "origin": str(tmp_path / "venv/lib/devbrain/__init__.py"), "package_sha256": files,
              "dependencies": {"devbrain": "0.7.2", "httpx": "0.28.1"}}
    bad = {"version": "0.8.0", "base_sha256": "wrong", "origin": str(tmp_path / "unrelated/__init__.py"),
           "package_sha256": {"__init__.py": "tampered"}, "dependencies": {"devbrain": "0.7.2", "httpx": "0.1"}}
    if changed == "missing_dependency":
        result["dependencies"] = {"devbrain": "0.7.2"}
    elif changed:
        result[changed] = bad[changed]
    calls = []
    def probe(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps(result))
    monkeypatch.setattr(updater.subprocess, "run", probe)
    if changed:
        with pytest.raises(ValueError):
            updater.verify_package(selected, runtime, "0.7.2", files, {"httpx": ["0.28.1"], "__required__": ["httpx"]})
    else:
        observed = updater.verify_package(selected, runtime, "0.7.2", files, {"httpx": ["0.28.1"], "__required__": ["httpx"]})
        assert observed["python_launcher"] == str(selected)
        assert calls[0][0][:4] == [str(selected), "-I", "-B", "-c"]
        assert "--data-dir" not in calls[0][0] and "DEVBRAIN_HOME" not in calls[0][1]["env"]


@pytest.mark.parametrize("mutation", [None, "blocked", "missing_check", "failed_check", "wrong_hash"])
def test_runtime_gate_accepts_only_complete_unchanged_runtime_without_processes(updater, tmp_path, monkeypatch, mutation):
    selected = Path(sys._base_executable).resolve()
    runtime = {f"{name}_{suffix}": str(selected) if suffix == "executable" else updater.file_digest(selected)
               for name in ("python", "java", "tmux") for suffix in ("executable", "binary_sha256")}
    gate = {"schema_version": 1, "scope": "bounded_runtime_qualification", "status": "pass", "runtime": runtime,
            "checks": {name: {"pass": True} for name in updater.GATE_CHECKS}}
    if mutation == "blocked":
        gate["status"] = "blocked"
    elif mutation == "missing_check":
        gate["checks"].pop("native_children")
    elif mutation == "failed_check":
        gate["checks"]["native_children"]["pass"] = False
    elif mutation == "wrong_hash":
        runtime["python_binary_sha256"] = "0" * 64
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(gate))
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **kw: pytest.fail("Gate started a process"))
    if mutation:
        with pytest.raises(ValueError):
            updater.verify_runtime_gate(path, selected)
    else:
        observed, gate_sha = updater.verify_runtime_gate(path, selected)
        assert observed == runtime and gate_sha == updater.file_digest(path)


def test_unqualified_backup_python_is_never_started(updater, tmp_path, monkeypatch):
    selected = Path(sys._base_executable).resolve()
    foreign = tmp_path / "foreign-python"
    foreign.write_bytes(b"not the approved interpreter")
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **kw: pytest.fail("Unqualified interpreter started"))
    with pytest.raises(ValueError, match="qualified base"):
        updater.verify_package(foreign, {"python_executable": str(selected), "python_binary_sha256": updater.file_digest(selected)}, "0.7.2", {}, {})


@pytest.mark.parametrize("checksum_ok", [True, False])
def test_backup_checksum_is_verified_before_source_update(updater, tmp_path, checksum_ok):
    import gzip
    content = {"format_version": 5, "projects": []}
    envelope = {"format": "devbrain-arcadedb-backup", "version": 1, "payload": content,
                "sha256": updater.digest(json.dumps(content, separators=(",", ":"), sort_keys=True).encode()) if checksum_ok else "0" * 64}
    path = tmp_path / "backup.json.gz"
    with gzip.open(path, "wt") as stream:
        json.dump(envelope, stream)
    path.chmod(0o600)
    if checksum_ok:
        updater.validate_backup(path)
    else:
        with pytest.raises(ValueError, match="checksum"):
            updater.validate_backup(path)


def load_smoke_contract():
    path = Path(__file__).with_name("test_devbrain_update080_smoke.py")
    module = types.ModuleType("upgrade080_smoke_contract")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)  # noqa: S102 — local test module, not its CLI
    return module


@pytest.mark.parametrize("mode", ["apply", "smoke"])
@pytest.mark.parametrize("mutation", [None, "wrong_resolution", "changed_bytes"])
def test_legacy_java_uses_qualified_path_and_rejects_other_jdk(updater, tmp_path, monkeypatch, mode, mutation):
    java = tmp_path / "qualified-jdk/bin/java"
    java.parent.mkdir(parents=True)
    java.write_bytes(b"qualified-java")
    java.chmod(0o700)
    other = tmp_path / "system-jdk/bin/java"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"unqualified-java")
    other.chmod(0o700)
    runtime = {"java_executable": str(java), "java_binary_sha256": updater.file_digest(java),
               "tmux_executable": str(tmp_path / "tmux/bin/tmux")}
    environment = {"PATH": str(other.parent), "JAVA_HOME": str(other.parent.parent),
                   "JAVA_TOOL_OPTIONS": "-Xint", "JDK_JAVA_OPTIONS": "-XX:TieredStopAtLevel=1", "_JAVA_OPTIONS": "-Xcomp"}
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **kw: pytest.fail("Java resolution test started a process"))
    if mutation == "changed_bytes":
        java.write_bytes(b"changed-java")
    elif mutation == "wrong_resolution":
        monkeypatch.setattr(updater.shutil, "which", lambda *a, **kw: str(other))
    if mode == "apply":
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        observed = updater.runtime_environment(runtime)
        def operation():
            return updater.verify_java_environment(observed, runtime)
    else:
        smoke = load_smoke_contract()
        def operation():
            return smoke.qualified_java_environment(environment, runtime)
    if mutation:
        with pytest.raises(ValueError, match="Java"):
            operation()
    else:
        result = operation()
        if mode == "smoke":
            observed = result
        assert observed["PATH"].split(os.pathsep)[0] == str(java.parent)
        assert observed["JAVA_HOME"] == str(java.parent.parent)
        assert not {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"} & observed.keys()


def test_fixture_marker_cannot_authorize_a_reused_process(tmp_path, monkeypatch):
    smoke = load_smoke_contract()
    proc = tmp_path / "proc"
    pid = max(999_999, os.getpid() + 1)
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    directory = tmp_path / "fixture"
    (entry / "environ").write_bytes(("DEVBRAIN_UPGRADE_FIXTURE=" + str(directory)).encode() + b"\0")
    original_path = Path
    monkeypatch.setattr(smoke, "Path", lambda value: proc if value == "/proc" else original_path(value))
    identity = {"pid": pid, "birth": "original", "executable": "/fixture/python", "state": "S"}
    monitor = types.SimpleNamespace(process=lambda value: identity, same_process=lambda row: False)
    assert smoke.fixture_processes(directory, monitor) == []


def test_reused_pid_after_pidfd_open_is_not_signalled(tmp_path, monkeypatch):
    smoke = load_smoke_contract()
    identity = {"pid": 999_999, "birth": "original", "executable": "/fixture/python", "state": "S"}
    scans = iter([[identity], []])
    monkeypatch.setattr(smoke, "fixture_processes", lambda *args: next(scans))
    signals, closed = [], []
    monkeypatch.setattr(smoke, "os", types.SimpleNamespace(pidfd_open=lambda pid: 42, close=closed.append))
    monkeypatch.setattr(smoke, "signal", types.SimpleNamespace(
        SIGTERM=15, SIGKILL=9, pidfd_send_signal=lambda *args: signals.append(args)))
    checks = iter([True, False, False, False, False])
    monitor = types.SimpleNamespace(same_process=lambda row: next(checks))
    assert smoke.stop_fixture_processes(tmp_path, monitor) == 1
    assert signals == [] and closed == [42]


def test_smoke_claims_only_new_private_engine_home(tmp_path):
    smoke = load_smoke_contract()
    cache = tmp_path / "cache"
    root = cache / "upgrade-fixtures"
    root.mkdir(parents=True, mode=0o700)
    engine = root / "fresh-engine"
    assert smoke.claim_fresh_engine_root(root, engine, cache) == engine
    assert list(engine.iterdir()) == []
    assert engine.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["empty", "config", "file", "symlink", "dangling_symlink", "outside", "nested", "relative"])
def test_smoke_refuses_existing_or_unbounded_engine_without_reading_it(tmp_path, monkeypatch, kind):
    smoke = load_smoke_contract()
    cache = tmp_path / "cache"
    root = cache / "upgrade-fixtures"
    root.mkdir(parents=True, mode=0o700)
    engine = root / "engine"
    canary = root / "unrelated-canary"
    canary.write_bytes(b"untouched")
    if kind in {"empty", "config"}:
        engine.mkdir(mode=0o700)
        if kind == "config":
            (engine / "config.json").write_bytes(b"never-read-existing-credentials")
    elif kind == "file":
        engine.write_bytes(b"existing")
    elif kind in {"symlink", "dangling_symlink"}:
        engine.symlink_to(canary if kind == "symlink" else root / "missing")
    elif kind == "outside":
        engine = cache / "outside-engine"
    elif kind == "nested":
        engine = root / "new-parent/engine"
    else:
        engine = Path("relative-engine")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("Existing configuration was read"))
    with pytest.raises(ValueError, match="NEW direct child"):
        smoke.claim_fresh_engine_root(root, engine, cache)
    assert canary.read_bytes() == b"untouched"
    if kind == "config":
        assert (engine / "config.json").read_bytes() == b"never-read-existing-credentials"
    if kind in {"outside", "nested", "relative"}:
        assert not engine.exists()


@pytest.mark.parametrize("mutation", ["public_mode", "foreign_uid", "root_outside_cache", "symlink_root"])
def test_smoke_refuses_untrusted_fixture_parent(tmp_path, monkeypatch, mutation):
    smoke = load_smoke_contract()
    cache = tmp_path / "cache"
    root = cache / "upgrade-fixtures"
    root.mkdir(parents=True, mode=0o700)
    if mutation == "public_mode":
        root.chmod(0o755)
    elif mutation == "foreign_uid":
        monkeypatch.setattr(smoke.os, "getuid", lambda: root.stat().st_uid + 1)
    elif mutation == "root_outside_cache":
        cache = tmp_path / "other-cache"
    else:
        link = cache / "alias"
        link.symlink_to(root, target_is_directory=True)
        root = link
    with pytest.raises(ValueError):
        smoke.claim_fresh_engine_root(root, root / "fresh", cache)
    assert not (root / "fresh").exists()


@pytest.mark.parametrize("failure", [None, "stop_false", "exit_unverified", "forced", "stop_error", "process_error", "descendant", "watch_error"])
def test_smoke_always_stops_engine_before_descendants_and_late_watch(failure):
    smoke = load_smoke_contract()
    order = []
    engine = types.SimpleNamespace(report={"started": True, "actual_exit_verified": False,
                                           "cleanup_complete": False, "forced_cleanup": False})

    def stop():
        order.append("engine-stop-actual-exit")
        if failure == "stop_error":
            raise RuntimeError("PRIVATE_FIXTURE_ERROR_MUST_NOT_ENTER_REPORT")
        engine.report.update(actual_exit_verified=failure != "exit_unverified",
                             cleanup_complete=failure != "stop_false", forced_cleanup=failure == "forced")
        return failure != "stop_false"

    def processes():
        order.append("fixture-process-cleanup")
        if failure == "process_error":
            raise RuntimeError("PRIVATE_FIXTURE_ERROR_MUST_NOT_ENTER_REPORT")
        return 1 if failure == "descendant" else 0

    def late_watch(grace):
        assert grace == 30
        order.append("late-watch")
        if failure == "watch_error":
            raise RuntimeError("PRIVATE_FIXTURE_ERROR_MUST_NOT_ENTER_REPORT")
        return {"native_failure": False, "boot_id": "same"}

    engine.stop = stop
    result = smoke.finish_owned_resources(engine, processes, types.SimpleNamespace(finish=late_watch), 30)
    assert order == ["engine-stop-actual-exit", "fixture-process-cleanup", "late-watch"]
    assert result["cleanup_complete"] is (failure is None)
    assert result["forced_cleanup"] is (failure in {"forced", "descendant"})
    assert "PRIVATE_FIXTURE_ERROR_MUST_NOT_ENTER_REPORT" not in json.dumps(result)
    if failure is None:
        assert result["engine"]["actual_exit_verified"] is True
        assert result["native"]["native_failure"] is False


@pytest.fixture
def restored_snapshot():
    from copy import deepcopy

    old_epoch = "12345678-1234-4234-8234-123456789abc"
    new_epoch = "87654321-4321-4321-8321-cba987654321"
    expected = {
        "format_version": 5, "backend": "arcadedb", "exported_at": "before",
        "projects": [{"id": "project-a"}, {"id": "project-b"}],
        "sources": [{"id": "source-a", "content": "preserve-source"}],
        "records": [{"id": "record-a", "content": "preserve-record"}],
        "vector_indexes": [{"id": "namespace-a", "type_name": "vector_a", "dimension": 2}],
        "vector_data": {"vector_a": [{"id": "embedding-a", "vector": [0.25, 0.75]}]},
        "journal_state": [{"id": "current", "epoch": old_epoch, "seq": 7, "first_seq": 1}],
        "journal_events": [{"id": f"{old_epoch}:7", "epoch": old_epoch, "seq": 7}],
    }
    actual = deepcopy(expected)
    actual["exported_at"] = "after"
    actual["journal_state"] = [{"id": "current", "epoch": new_epoch, "seq": 1, "first_seq": 1}]
    actual["journal_events"] = [{"id": f"{new_epoch}:1", "epoch": new_epoch, "seq": 1,
                                 "project_id": None, "entity_type": "reset", "entity_id": None,
                                 "op": "reset", "at": "2026-11-08T09:00:00.000001+00:00"}]
    response = {"epoch": new_epoch, "cursor": {"epoch": new_epoch, "seq": 1},
                "events": [], "reset_required": True}
    calls = []

    def read(*, after):
        calls.append(deepcopy(after))
        return response

    namespace = {"__name__": "restore_probe_guards"}
    smoke = load_smoke_contract()
    exec(compile(smoke.RESTORE_PROBE, "<restore-probe>", "exec"), namespace)
    return types.SimpleNamespace(expected=expected, actual=actual, response=response, calls=calls,
                                 journal=types.SimpleNamespace(read=read),
                                 verify=namespace["verify_restored_export"], probe=smoke.RESTORE_PROBE)


def test_restore_probe_accepts_preserved_data_only_with_rotated_journal(restored_snapshot):
    value = restored_snapshot
    report = value.verify(value.expected, value.actual, value.journal)
    assert report == {"real_restore_verified": True, "projects": 2, "collections": len(value.expected),
                      "journal_reset_verified": True, "old_cursor_reset_required": True}
    old = value.expected["journal_state"][0]
    assert value.calls == [{"epoch": old["epoch"], "seq": 7}]


@pytest.mark.parametrize("mutation", ["projects", "sources", "records", "vector_indexes", "vector_rows",
                                      "vector_values", "missing_collection", "extra_collection"])
def test_restore_probe_rejects_domain_or_vector_loss(restored_snapshot, mutation):
    value = restored_snapshot
    if mutation in {"projects", "sources", "records", "vector_indexes"}:
        value.actual[mutation].pop()
    elif mutation == "vector_rows":
        value.actual["vector_data"]["vector_a"].clear()
    elif mutation == "vector_values":
        value.actual["vector_data"]["vector_a"][0]["vector"][0] = 0.5
    elif mutation == "missing_collection":
        value.actual.pop("records")
    else:
        value.actual["unexpected_collection"] = []
    with pytest.raises(AssertionError):
        value.verify(value.expected, value.actual, value.journal)
    assert value.calls == []  # A valid reset cannot excuse lost application data.


@pytest.mark.parametrize("mutation", ["old_epoch", "invalid_epoch", "state_count", "state_id", "state_seq",
                                      "first_seq", "boolean_seq", "missing_reset", "duplicate_reset",
                                      "event_id", "event_epoch", "event_seq", "project_reset", "entity_type",
                                      "entity_id", "operation", "naive_timestamp", "invalid_timestamp"])
def test_restore_probe_rejects_incomplete_or_wrong_journal_reset(restored_snapshot, mutation):
    value = restored_snapshot
    state, event = value.actual["journal_state"][0], value.actual["journal_events"][0]
    if mutation == "old_epoch":
        state["epoch"] = value.expected["journal_state"][0]["epoch"]
    elif mutation == "invalid_epoch":
        state["epoch"] = "not-an-epoch"
    elif mutation == "state_count":
        value.actual["journal_state"].append(dict(state))
    elif mutation == "state_id":
        state["id"] = "not-current"
    elif mutation == "state_seq":
        state["seq"] = 2
    elif mutation == "first_seq":
        state["first_seq"] = 0
    elif mutation == "boolean_seq":
        state["seq"] = True
    elif mutation == "missing_reset":
        value.actual["journal_events"].clear()
    elif mutation == "duplicate_reset":
        value.actual["journal_events"].append(dict(event))
    elif mutation == "event_id":
        event["id"] = "wrong:1"
    elif mutation == "event_epoch":
        event["epoch"] = value.expected["journal_state"][0]["epoch"]
    elif mutation == "event_seq":
        event["seq"] = 2
    elif mutation == "project_reset":
        event["project_id"] = "project-a"
    elif mutation == "entity_type":
        event["entity_type"] = "record"
    elif mutation == "entity_id":
        event["entity_id"] = "record-a"
    elif mutation == "operation":
        event["op"] = "upsert"
    elif mutation == "naive_timestamp":
        event["at"] = "2026-11-08T09:00:00"
    else:
        event["at"] = "invalid"
    with pytest.raises((AssertionError, ValueError)):
        value.verify(value.expected, value.actual, value.journal)
    assert value.calls == []


@pytest.mark.parametrize("mutation", ["accepted_old_cursor", "truthy_reset", "replayed_events", "old_cursor",
                                      "wrong_cursor_sequence", "wrong_response_epoch"])
def test_restore_probe_requires_old_cursor_invalidation(restored_snapshot, mutation):
    value = restored_snapshot
    if mutation == "accepted_old_cursor":
        value.response["reset_required"] = False
    elif mutation == "truthy_reset":
        value.response["reset_required"] = 1
    elif mutation == "replayed_events":
        value.response["events"] = value.expected["journal_events"]
    elif mutation == "old_cursor":
        value.response["cursor"]["epoch"] = value.expected["journal_state"][0]["epoch"]
    elif mutation == "wrong_cursor_sequence":
        value.response["cursor"]["seq"] = 7
    else:
        value.response["epoch"] = value.expected["journal_state"][0]["epoch"]
    with pytest.raises(AssertionError):
        value.verify(value.expected, value.actual, value.journal)
    assert len(value.calls) == 1


def test_restore_probe_entrypoint_reads_the_restored_store_journal(restored_snapshot, tmp_path, monkeypatch, capsys):
    import gzip

    value = restored_snapshot
    backup, data = tmp_path / "backup.json.gz", tmp_path / "restored"
    with gzip.open(backup, "wt") as stream:
        json.dump({"payload": value.expected}, stream)
    store = types.SimpleNamespace(export=lambda: value.actual)
    opened, bound = [], []

    def open_store(path):
        opened.append(path)
        return store

    def open_journal(actual_store):
        bound.append(actual_store)
        return value.journal

    monkeypatch.setitem(sys.modules, "devbrain.store", types.SimpleNamespace(Store=open_store))
    monkeypatch.setitem(sys.modules, "devbrain.journal", types.SimpleNamespace(Journal=open_journal))
    monkeypatch.setattr(sys, "argv", ["-c", str(data), str(backup)])
    exec(compile(value.probe, "<restore-probe>", "exec"), {"__name__": "__main__"})
    assert opened == [data] and bound == [store]
    assert value.calls == [{"epoch": value.expected["journal_state"][0]["epoch"], "seq": 7}]
    assert json.loads(capsys.readouterr().out)["old_cursor_reset_required"] is True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--updater", type=Path, help="Candidate updater; alternative to DEVBRAIN_TEST_UPDATER_PATH")
    arguments, pytest_arguments = parser.parse_known_args()
    if arguments.updater is not None:
        os.environ["DEVBRAIN_TEST_UPDATER_PATH"] = str(arguments.updater.expanduser().resolve())
    raise SystemExit(pytest.main(["--noconftest", str(Path(__file__).resolve()), *pytest_arguments]))
