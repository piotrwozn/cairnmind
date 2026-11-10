"""Explicit Java selection stays local and diagnostics describe the actual binary."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from devbrain.arcade import ArcadeError
from devbrain.config import Settings
from devbrain import engine, integrations
from devbrain import runtime_selection
from devbrain.client_runtime import NativeRuntime


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
    from devbrain import native_worker
    import sys
    monkeypatch.setattr(sys, "argv", ["worker", "--attach", "--tmux-bin", "/missing/tmux",
                                      "--socket", "/private/socket", "--session", "private"])
    monkeypatch.setattr(native_worker.fcntl, "ioctl", lambda *args: pytest.fail("TTY touched"))
    monkeypatch.setattr(native_worker.os, "execve", lambda *args: pytest.fail("Process started"))
    with pytest.raises(ValueError):
        native_worker.main()
