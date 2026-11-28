"""doctor and the install hints know the system they run on (#233).

No Mac is needed: a test selects Darwin, WSL or plain Linux explicitly. The WSL texts are pinned word for word,
because the output on Linux and WSL must not change.
"""
from __future__ import annotations

import json
import re
import subprocess
import types

import httpx
import pytest

from cairnmind.agents.client_runtime import NativeRuntime
from cairnmind.config import Settings
from cairnmind.memory import integrations
from cairnmind.pal import platform_hints
from cairnmind.storage import engine
from cairnmind.terminal import workspace
from cairnmind.terminal.terminal_i18n import tr


@pytest.fixture
def settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return Settings.load(tmp_path / "memory", create=True)


# --- which system -------------------------------------------------------------------------------------------

def test_the_system_is_told_from_the_platform_and_the_kernel_release():
    assert platform_hints.host_system("darwin", "25.0.0") == "darwin"
    assert platform_hints.host_system("linux", "5.15.167.4-microsoft-standard-WSL2") == "wsl"
    assert platform_hints.host_system("linux", "6.8.0-45-generic") == "linux"
    assert platform_hints.is_wsl("5.15-Microsoft-standard") and not platform_hints.is_wsl("6.8.0-generic")


# --- doctor -------------------------------------------------------------------------------------------------

def quiet_doctor(settings, monkeypatch, system):
    monkeypatch.setattr(settings, "java_executable", "")
    monkeypatch.setattr(platform_hints, "host_system", lambda *a, **k: system)
    monkeypatch.setattr(integrations, "_probe_cli", lambda client, *a, **k: integrations_probe(client))
    monkeypatch.setattr(integrations.shutil, "which", lambda *a, **k: None)
    from cairnmind.terminal import runtime_selection
    monkeypatch.setattr(runtime_selection.shutil, "which", lambda *a, **k: None)
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    return integrations.doctor(settings)


def integrations_probe(client):
    return {"installed": False, "notes": [platform_hints.hint("client_note", client=client)]}


def fake_sysctl(monkeypatch, values):
    """Answers of the two reads by name; a missing name fails like a failed sysctl."""
    monkeypatch.setattr(platform_hints, "_sysctl", lambda name: values.get(name))


CHIP = {"machdep.cpu.brand_string": "Apple M3 Pro", "hw.memsize": "38654705664"}


def test_doctor_on_a_mac_names_the_chip_and_its_unified_memory(settings, monkeypatch):
    fake_sysctl(monkeypatch, CHIP)
    gpu = quiet_doctor(settings, monkeypatch, "darwin")["gpu"]
    assert gpu == {"chip": "Apple M3 Pro", "memory_gib": 36,
                   "note": "Apple M3 Pro, 36 GiB unified memory shared with the models"}
    assert tr("{chip}, {gib} GiB unified memory shared with the models", "pl").endswith("z modelami")


@pytest.mark.parametrize("values", [
    {"hw.memsize": "38654705664"}, {"machdep.cpu.brand_string": "Apple M3 Pro"},
    {**CHIP, "hw.memsize": "lots"}, {**CHIP, "hw.memsize": "0"}, {**CHIP, "hw.memsize": "-5"},
    {**CHIP, "machdep.cpu.brand_string": "x" * 200}, {**CHIP, "machdep.cpu.brand_string": "Apple M3\x1b[31m"},
    {**CHIP, "hw.memsize": "9" * 5000}, {**CHIP, "machdep.cpu.brand_string": "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz"},
    {}])
def test_a_failed_or_garbage_sysctl_keeps_the_old_note_on_a_mac(settings, monkeypatch, values):
    fake_sysctl(monkeypatch, values)
    assert quiet_doctor(settings, monkeypatch, "darwin")["gpu"] == {"note": "The GPU is not checked on macOS."}


def test_sysctl_is_run_without_a_shell_from_its_fixed_path(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="Apple M3 Pro\n")
    monkeypatch.setattr(platform_hints.subprocess, "run", run)
    assert platform_hints._sysctl("machdep.cpu.brand_string") == "Apple M3 Pro"
    assert calls[0][0] == ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]
    assert calls[0][1]["timeout"] and not calls[0][1].get("shell")
    monkeypatch.setattr(platform_hints.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="x"))
    assert platform_hints._sysctl("hw.memsize") is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("sysctl", 3)
    monkeypatch.setattr(platform_hints.subprocess, "run", boom)
    assert platform_hints._sysctl("hw.memsize") is None


@pytest.mark.parametrize("name", ["linux", "wsl"])
def test_linux_and_wsl_never_read_sysctl(settings, monkeypatch, name):
    monkeypatch.setattr(platform_hints, "_sysctl", lambda name: pytest.fail("sysctl read off a Mac"))
    assert "chip" not in quiet_doctor(settings, monkeypatch, name)["gpu"]


def test_doctor_on_a_mac_has_no_wsl_apt_or_nvidia_text(settings, monkeypatch):
    fake_sysctl(monkeypatch, {})
    result = quiet_doctor(settings, monkeypatch, "darwin")
    assert result["gpu"] == {"note": "The GPU is not checked on macOS."}
    assert result["tmux"]["action"] == "Install tmux >= 3.4, for example with: brew install tmux"
    assert "wsl" not in result
    shown = json.dumps({k: v for k, v in result.items() if k not in {"data_dir", "python_executable"}})
    assert not re.search(r"wsl|\bapt\b|nvidia|ubuntu|distribution", shown, re.I), shown


def test_doctor_on_wsl_says_what_it_always_said(settings, monkeypatch):
    result = quiet_doctor(settings, monkeypatch, "wsl")
    assert result["gpu"] == {"available": False,
                             "note": "nvidia-smi not found; verify Windows NVIDIA driver and WSL GPU access."}
    assert result["tmux"]["action"] == "Install tmux >= 3.4 in the same WSL distribution"
    assert result["clients"]["codex"]["notes"] == ["Install codex in the same WSL distribution as Cairnmind."]
    assert "wsl" in result


def test_doctor_on_plain_linux_says_what_it_always_said(settings, monkeypatch):
    result = quiet_doctor(settings, monkeypatch, "linux")
    assert result["gpu"]["note"] == "nvidia-smi not found; verify Windows NVIDIA driver and WSL GPU access."
    assert result["tmux"]["action"] == "Install tmux >= 3.4 in the same WSL distribution"
    assert result["clients"]["codex"]["notes"] == ["Install codex in the same WSL distribution as Cairnmind."]
    assert isinstance(result["wsl"], bool)  # the key stays on Linux; its value is the kernel's, not the test's


def test_doctor_reads_nvidia_smi_as_before_on_linux(settings, monkeypatch):
    monkeypatch.setattr(settings, "java_executable", "")
    monkeypatch.setattr(platform_hints, "host_system", lambda *a, **k: "linux")
    monkeypatch.setattr(integrations, "_probe_cli", lambda *a, **k: {"installed": False})
    monkeypatch.setattr(integrations.shutil, "which", lambda name, **k: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    real_run = subprocess.run
    monkeypatch.setattr(integrations.subprocess, "run", lambda argv, **k: types.SimpleNamespace(
        returncode=0, stdout="RTX 4090, 24564 MiB, 555.42\n", stderr="") if "nvidia-smi" in argv[0] else real_run(argv, **k))
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    assert integrations.doctor(settings)["gpu"] == {"available": True, "devices": ["RTX 4090, 24564 MiB, 555.42"]}


# --- the hints, at the places that print them ---------------------------------------------------------------

WSL_WORDS = {
    "tmux_workspace": "Install tmux >= 3.4 in the same WSL distribution as Cairnmind.",
    "tmux_launch": "Install tmux >= 3.4 in Ubuntu: sudo apt install tmux",
    "tmux_action": "Install tmux >= 3.4 in the same WSL distribution",
    "tmux_code": "tmux_missing: install tmux >= 3.4 in this WSL distribution",
    "client_missing": "client_missing: install the native CLI in this WSL distribution",
    "java": "Install Java 21 or newer in WSL: sudo apt install -y openjdk-21-jre-headless",
}


@pytest.mark.parametrize("topic", sorted(WSL_WORDS))
def test_the_wsl_wording_is_what_the_code_said_before(topic):
    assert platform_hints.hint(topic, "wsl") == WSL_WORDS[topic]


@pytest.mark.parametrize("topic", sorted(platform_hints.TEXTS))
def test_every_hint_has_a_polish_translation_in_the_catalog(topic):
    for system, (english, polish) in platform_hints.TEXTS[topic].items():
        assert english != polish and polish.strip()
        assert tr(english, "pl") == polish, (topic, system)
        assert tr(english, "en") == english


@pytest.mark.parametrize("topic", sorted(platform_hints.TEXTS))
def test_only_the_wsl_texts_mention_wsl_and_only_the_mac_ones_brew(topic):
    assert "linux" not in platform_hints.TEXTS[topic]  # plain Linux reads the 'wsl' texts, unchanged
    if "wsl" in platform_hints.TEXTS[topic]:
        assert platform_hints.hint(topic, "linux") == platform_hints.hint(topic, "wsl")
    for system, (english, polish) in platform_hints.TEXTS[topic].items():
        if system != "wsl":
            assert not re.search(r"wsl|ubuntu|distribution|dystrybuc|apt install", english + polish, re.I), (topic, system)
        installs = topic.startswith("tmux") or topic == "java"
        assert ("brew install" in english) == (installs and system == "darwin")


@pytest.mark.parametrize("name", ["wsl", "linux", "darwin"])
def test_the_places_that_print_a_hint_ask_for_the_system_they_run_on(settings, monkeypatch, tmp_path, name):
    from cairnmind.terminal import runtime_selection
    monkeypatch.setattr(platform_hints, "host_system", lambda *a, **k: name)
    monkeypatch.setattr(integrations.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(runtime_selection.shutil, "which", lambda *a, **k: None)
    assert NativeRuntime(settings).diagnostics()["action"] == platform_hints.hint("tmux_action", name)
    with pytest.raises(ValueError) as caught:
        NativeRuntime(settings)._tmux("list-sessions")
    assert str(caught.value) == platform_hints.hint("tmux_code", name)
    with pytest.raises(ValueError) as caught:
        integrations.prepare_client_launch(settings, {"root": str(tmp_path)}, "codex")
    assert str(caught.value) == platform_hints.hint("client_not_installed", name, client="codex")
    assert integrations._probe_cli("codex")["notes"] == [platform_hints.hint("client_note", name, client="codex")]
    with pytest.raises(ValueError) as caught:
        workspace.WorkspaceManager._initialize(types.SimpleNamespace(diagnostics=lambda: {"available": False}))
    assert str(caught.value) == platform_hints.hint("tmux_workspace", name)
    monkeypatch.setattr(workspace.sys, "platform", "darwin" if name == "darwin" else "linux")
    monkeypatch.setattr(workspace.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(workspace.sys, "stdout", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(workspace, "WorkspaceManager", lambda s: types.SimpleNamespace(
        diagnostics=lambda: {"available": False}))
    with pytest.raises(ValueError) as caught:
        workspace.launch_workspace(settings)
    assert platform_hints.hint("tmux_launch", name).rstrip(".") + "." in str(caught.value)


def test_a_missing_client_and_a_missing_java_ask_for_the_system_they_run_on(settings, monkeypatch, tmp_path):
    from cairnmind.agents import client_auth
    (tmp_path / "lib").mkdir()
    monkeypatch.setattr(engine, "engine_status", lambda s: {"available": False})
    monkeypatch.setattr(engine, "runtime_dir", lambda s: tmp_path)
    monkeypatch.setattr(engine, "resolve_java", lambda s: None)
    monkeypatch.setattr(client_auth, "probe_client", lambda client: {"installation": "missing"})
    monkeypatch.setattr(NativeRuntime, "diagnostics", lambda self: {"available": True})
    for name in ("wsl", "linux", "darwin"):
        monkeypatch.setattr(platform_hints, "host_system", lambda *a, _n=name, **k: _n)
        with pytest.raises(engine.ArcadeError) as caught:
            engine.start_engine(settings)
        # The system's own hint, inside the sentence that also names Docker (#313).
        assert platform_hints.hint("java", name) in str(caught.value)
        assert "cairnmind engine start --docker" in str(caught.value)
        with pytest.raises(ValueError) as caught:
            NativeRuntime(settings).start({"root": str(tmp_path)}, "codex")
        assert str(caught.value) == platform_hints.hint("client_missing", name)
