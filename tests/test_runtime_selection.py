"""Explicit Java selection stays local and diagnostics describe the actual binary."""
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest

from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind import engine, integrations
from cairnmind import runtime_selection
from cairnmind.client_runtime import NativeRuntime


def executable(tmp_path):
    path = tmp_path / "private runtime" / "bin" / "java"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def test_explicit_java_is_resolved_without_path_fallback(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    alias = tmp_path / "java-alias"
    alias.symlink_to(binary)
    settings = Settings(tmp_path, java_executable=str(alias))
    monkeypatch.setattr(engine.shutil, "which", lambda name: pytest.fail("Unexpected PATH fallback"))
    assert engine.resolve_java(settings) == str(binary.resolve())


@pytest.mark.parametrize("value", ["java", "relative/bin/java", "/missing/devbrain/java", 21, 0, None])
def test_invalid_explicit_java_does_not_fall_back(tmp_path, monkeypatch, value):
    monkeypatch.setattr(engine.shutil, "which", lambda name: pytest.fail("Unexpected PATH fallback"))
    with pytest.raises(ArcadeError):
        engine.resolve_java(Settings(tmp_path, java_executable=value))


def test_non_executable_java_is_rejected(tmp_path):
    binary = executable(tmp_path)
    binary.chmod(0o600)
    with pytest.raises(ArcadeError):
        engine.resolve_java(Settings(tmp_path, java_executable=str(binary)))


def test_empty_java_setting_keeps_path_behavior(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    monkeypatch.setattr(engine.shutil, "which", lambda name: str(binary))
    assert engine.resolve_java(Settings(tmp_path)) == str(binary)
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)
    assert engine.resolve_java(Settings(tmp_path)) is None


def test_java_config_roundtrip_and_environment_override_are_local(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_JAVA_EXECUTABLE", raising=False)
    settings = Settings(tmp_path, java_executable="/private/jdk/bin/java")
    settings.save()
    original = (tmp_path / "config.json").read_bytes()
    assert Settings.load(tmp_path).java_executable == "/private/jdk/bin/java"
    monkeypatch.setenv("DEVBRAIN_JAVA_EXECUTABLE", "/second/jdk/bin/java")
    assert Settings.load(tmp_path).java_executable == "/second/jdk/bin/java"
    assert (tmp_path / "config.json").read_bytes() == original


@pytest.mark.parametrize("returncode,version", [(1, 'openjdk version "21.0.13"'),
                                                (0, 'openjdk version "17.0.1"'),
                                                (0, "unknown")])
def test_failed_or_old_java_probe_never_launches_server(tmp_path, monkeypatch, returncode, version):
    settings = Settings(tmp_path, java_executable=str(executable(tmp_path)))
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, returncode, "", version))
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: pytest.fail("Invalid Java launched"))
    with pytest.raises(ArcadeError, match="Java 21"):
        engine.start_engine(settings)


def fake_java(tmp_path, body, shebang="#!/bin/sh\n"):
    """A shell script standing in for Java. ``exec sleep`` so that no orphan keeps the output pipe open."""
    path = tmp_path / "fake-jdk" / "bin" / "java"
    path.parent.mkdir(parents=True)
    path.write_text(shebang + body)
    path.chmod(0o700)
    return path


def prepare_start(settings, monkeypatch):
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})

    real_popen = subprocess.Popen

    def launched(argv, *a, **k):
        # subprocess.run is built on Popen, so only the server launch is replaced, not the version probe.
        if "-version" in argv:
            return real_popen(argv, *a, **k)
        raise RuntimeError("probe accepted, server launch reached")
    monkeypatch.setattr(engine.subprocess, "Popen", launched)


def test_a_slow_first_java_start_is_retried_and_accepted(tmp_path, monkeypatch):
    marker = tmp_path / "first-call-done"
    binary = fake_java(tmp_path, f'if [ ! -e "{marker}" ]; then touch "{marker}"; exec sleep 5; fi\n'
                                 'echo \'openjdk version "21.0.13"\' >&2\n')
    settings = Settings(tmp_path, java_executable=str(binary))
    prepare_start(settings, monkeypatch)
    # The first call (5 s) always runs into the limit; the second answers at once, so a slow runner
    # would need two seconds just to start a shell before this test could fail.
    monkeypatch.setattr(engine, "JAVA_PROBE_TIMEOUT", 2)
    with pytest.raises(RuntimeError, match="server launch reached"):
        engine.start_engine(settings)
    assert marker.exists()


@pytest.mark.parametrize("shebang", ["#!/nonexistent/interpreter\n", ""])
def test_java_that_cannot_be_run_is_an_arcade_error(tmp_path, monkeypatch, shebang):
    # Both a missing interpreter and a file that is not a program make the operating system refuse to run it.
    binary = fake_java(tmp_path, "echo hello\n", shebang=shebang)
    settings = Settings(tmp_path, java_executable=str(binary))
    prepare_start(settings, monkeypatch)
    with pytest.raises(ArcadeError, match=r"^Java could not be run \(.*java\): "):
        engine.start_engine(settings)


def test_a_server_that_does_not_come_up_is_gone_before_start_engine_fails(tmp_path, monkeypatch):
    pid_file = tmp_path / "server-pid"
    binary = fake_java(tmp_path, 'case "$1" in -version) echo \'openjdk version "21.0.13"\' >&2; exit 0;; esac\n'
                                 f'echo $$ > "{pid_file}"\ntrap \'\' TERM\nsleep 1.5\n')
    settings = Settings(tmp_path, java_executable=str(binary), arcade_database="runtime_test")
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    monkeypatch.setattr(engine, "engine_status", lambda settings: {"available": False})
    real_sleep = time.sleep
    monkeypatch.setattr(engine.time, "sleep", lambda seconds: real_sleep(0.001))
    with pytest.raises(ArcadeError, match="startup timed out"):
        engine.start_engine(settings)
    # The server ignored the first SIGTERM for 1.5 s; start_engine must have waited for it.
    assert not Path(f"/proc/{int(pid_file.read_text())}").exists()
    assert not (tmp_path / "arcade" / "process.json").exists()


def test_doctor_says_when_java_timed_out_instead_of_naming_the_exception(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    settings = Settings(tmp_path, java_executable=str(binary), arcade_database="runtime_test")
    monkeypatch.setattr(integrations, "_probe_cli", lambda name: {})
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)

    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 4)
    monkeypatch.setattr(integrations.subprocess, "run", timed_out)
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    error = integrations.doctor(settings)["java"]["error"]
    assert "did not answer -version within 4 seconds" in error and "engine start waits longer" in error


def test_java_that_never_answers_is_an_arcade_error_not_a_timeout(tmp_path, monkeypatch):
    binary = fake_java(tmp_path, "exec sleep 5\n")
    settings = Settings(tmp_path, java_executable=str(binary))
    prepare_start(settings, monkeypatch)
    monkeypatch.setattr(engine, "JAVA_PROBE_TIMEOUT", 0.3)
    with pytest.raises(ArcadeError, match=r"Java did not answer .* -version within 0\.3 seconds, 2 times"):
        engine.start_engine(settings)


def test_java_older_than_21_is_still_refused_with_the_existing_message(tmp_path, monkeypatch):
    binary = fake_java(tmp_path, 'echo \'openjdk version "17.0.1"\' >&2\n')
    settings = Settings(tmp_path, java_executable=str(binary))
    prepare_start(settings, monkeypatch)
    with pytest.raises(ArcadeError, match="^ArcadeDB requires Java 21 or newer$"):
        engine.start_engine(settings)


def test_command_line_shows_a_message_when_java_never_answers(tmp_path, monkeypatch, capsys):
    from cairnmind import cli
    binary = fake_java(tmp_path, "exec sleep 5\n")
    data = tmp_path / "data"
    monkeypatch.setenv("CAIRNMIND_JAVA_EXECUTABLE", str(binary))
    settings = Settings.load(data, create=True)
    prepare_start(settings, monkeypatch)
    monkeypatch.setattr(engine, "JAVA_PROBE_TIMEOUT", 0.3)
    monkeypatch.setattr("sys.argv", ["cairnmind", "--data-dir", str(data), "engine", "start"])
    with pytest.raises(SystemExit) as stopped:
        cli.entrypoint()
    assert stopped.value.code == 1
    assert "Cairnmind: Java did not answer" in capsys.readouterr().err


def test_start_uses_selected_binary_and_records_it(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    settings = Settings(tmp_path, java_executable=str(binary), arcade_database="runtime_test")
    (engine.runtime_dir(settings) / "lib").mkdir(parents=True)
    statuses = iter([{"available": False}, {"available": True, "version_matches": True}])
    monkeypatch.setattr(engine, "engine_status", lambda settings: next(statuses))
    probes, launches = [], []
    def probe(argv, **kwargs):
        probes.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", 'openjdk version "21.0.13"')
    def launch(argv, **kwargs):
        launches.append(argv)
        return SimpleNamespace(pid=4321, poll=lambda: None)
    monkeypatch.setattr(engine.subprocess, "run", probe)
    monkeypatch.setattr(engine.subprocess, "Popen", launch)
    assert engine.start_engine(settings)["available"]
    assert probes == [[str(binary), "-version"]]
    assert launches[0][0] == str(binary)
    assert not any("TieredStopAtLevel" in arg for arg in launches[0])
    record = json.loads((tmp_path / "arcade/process.json").read_text())
    assert record["java_executable"] == str(binary)


@pytest.mark.parametrize("owned", [True, False])
def test_managed_java_requires_process_identity(tmp_path, monkeypatch, owned):
    settings = Settings(tmp_path)
    state = tmp_path / "arcade"
    state.mkdir()
    (state / "process.json").write_text(json.dumps({"pid": 4321}))
    read = Path.read_bytes
    resolve = Path.resolve
    def read_bytes(path):
        if str(path) == "/proc/4321/cmdline":
            return (f"java\0-Darcadedb.server.rootPath={state}\0com.arcadedb.server.ArcadeDBServer\0".encode()
                    if owned else b"unrelated\0")
        return read(path)
    def resolved(path, **kwargs):
        return Path("/actual/jdk/bin/java") if str(path) == "/proc/4321/exe" else resolve(path, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "resolve", resolved)
    assert engine.managed_java_executable(settings) == ("/actual/jdk/bin/java" if owned else None)


def test_doctor_reports_selected_and_running_binary_without_starting_engine(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    settings = Settings(tmp_path, java_executable=str(binary), arcade_database="runtime_test")
    monkeypatch.setattr(integrations, "_probe_cli", lambda name: {})
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)
    monkeypatch.setattr(engine, "managed_java_executable", lambda settings: "/running/jdk/bin/java")
    monkeypatch.setattr(engine, "start_engine", lambda *a: pytest.fail("Doctor started Java"))
    probes = []
    def probe(argv, **kwargs):
        probes.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", 'openjdk version "21.0.13"')
    monkeypatch.setattr(integrations.subprocess, "run", probe)
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    result = integrations.doctor(settings)
    assert probes == [[str(binary), "-version"]]
    assert result["java"]["executable"] == str(binary)
    assert result["java"]["managed_executable"] == "/running/jdk/bin/java"
    assert result["java"]["compatible"] is True
    assert result["java"]["on_path"] is False


@pytest.mark.parametrize("value", ["tmux", "relative/bin/tmux", "/missing/devbrain/tmux", 21, 0, None])
def test_invalid_tmux_setting_never_uses_path(tmp_path, monkeypatch, value):
    monkeypatch.setattr(runtime_selection.shutil, "which", lambda name: pytest.fail("Unexpected fallback"))
    settings = Settings(tmp_path, tmux_executable=value)
    with pytest.raises(ValueError):
        runtime_selection.resolve_tmux(settings)
    runtime = NativeRuntime(settings)
    assert runtime.binary is None
    assert runtime.diagnostics()["code"] == "tmux_invalid_executable"
    with pytest.raises(ValueError, match="tmux_invalid_executable"):
        runtime._tmux("start-server")
    assert not runtime.directory.exists()


def test_tmux_setting_roundtrip_and_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVBRAIN_TMUX_EXECUTABLE", raising=False)
    Settings(tmp_path, tmux_executable="/private/tmux").save()
    original = (tmp_path / "config.json").read_bytes()
    assert Settings.load(tmp_path).tmux_executable == "/private/tmux"
    monkeypatch.setenv("DEVBRAIN_TMUX_EXECUTABLE", "/other/tmux")
    assert Settings.load(tmp_path).tmux_executable == "/other/tmux"
    assert (tmp_path / "config.json").read_bytes() == original


def test_tmux_selection_canonicalizes_and_explicit_override_wins(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    alias = tmp_path / "tmux-alias"
    alias.symlink_to(binary)
    monkeypatch.setattr(runtime_selection.shutil, "which", lambda name: pytest.fail("Unexpected fallback"))
    settings = Settings(tmp_path, tmux_executable="/missing/configured")
    assert runtime_selection.resolve_tmux(settings, override=str(alias)) == str(binary.resolve())
    with pytest.raises(ValueError):
        runtime_selection.resolve_tmux(settings, override="")
    binary.chmod(0o600)
    with pytest.raises(ValueError):
        runtime_selection.resolve_tmux(settings, override=str(alias))


def test_empty_tmux_setting_uses_path_or_reports_missing(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    monkeypatch.setattr(runtime_selection.shutil, "which", lambda name: str(binary))
    assert runtime_selection.resolve_tmux(Settings(tmp_path)) == str(binary.resolve())
    monkeypatch.setattr(runtime_selection.shutil, "which", lambda name: None)
    runtime = NativeRuntime(Settings(tmp_path))
    assert runtime.binary is None
    assert runtime.diagnostics()["code"] == "tmux_missing"


def test_tmux_diagnostics_and_operations_use_same_selected_binary(tmp_path, monkeypatch):
    binary = executable(tmp_path)
    settings = Settings(tmp_path, tmux_executable=str(binary))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "tmux 3.8", "")
    monkeypatch.setattr(subprocess, "run", run)
    runtime = NativeRuntime(settings)
    assert runtime.diagnostics() == {"available": True, "minimum_version": "3.4", "backend": "tmux",
                                     "version": "tmux 3.8", "executable": str(binary), "code": "ready"}
    runtime._tmux("list-sessions")
    assert calls[0] == [str(binary), "-V"]
    assert calls[1][0] == str(binary)
    assert not runtime.directory.exists()


def test_native_attach_rejects_invalid_binary_before_tty_or_exec(tmp_path, monkeypatch):
    from cairnmind import native_worker
    import sys
    monkeypatch.setattr(sys, "argv", ["worker", "--attach", "--tmux-bin", "/missing/tmux",
                                      "--socket", "/private/socket", "--session", "private"])
    monkeypatch.setattr(native_worker.fcntl, "ioctl", lambda *args: pytest.fail("TTY touched"))
    monkeypatch.setattr(native_worker.os, "execve", lambda *args: pytest.fail("Process started"))
    with pytest.raises(ValueError):
        native_worker.main()
