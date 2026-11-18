"""doctor, benchmark and the install hints know the system they run on (#233).

No Mac is needed: a test selects Darwin, WSL or plain Linux explicitly and fakes what system_profiler and
sysctl print. The WSL texts are pinned word for word, because the output on Linux and WSL must not change.
"""
from __future__ import annotations

import json
import re
import subprocess
import types

import httpx
import pytest

from cairnmind import engine, integrations, platform_hints, workspace
from cairnmind.client_runtime import NativeRuntime
from cairnmind.config import Settings
from cairnmind.terminal_i18n import tr


@pytest.fixture
def settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return Settings.load(tmp_path / "memory", create=True)


PROFILE = json.dumps({"SPDisplaysDataType": [{
    "_name": "kHW_AppleM3ProItem", "spdisplays_mtlgpufamilysupport": "spdisplays_metal4",
    "spdisplays_vendor": "sppci_vendor_Apple", "sppci_bus": "spdisplays_builtin",
    "sppci_cores": "18", "sppci_model": "Apple M3 Pro"}]})


def fake_mac(profile=PROFILE, brand="Apple M3 Pro", memory=str(36 * 2**30), fail=()):
    """A run() that answers like system_profiler and sysctl on a Mac; commands in `fail` break."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert isinstance(argv, list) and "shell" not in kwargs and kwargs["timeout"] > 0
        name = argv[0].rsplit("/", 1)[-1]
        key = name if name == "system_profiler" else argv[-1]
        if key in fail:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        out = {"system_profiler": profile, "machdep.cpu.brand_string": brand, "hw.memsize": memory}[key]
        return types.SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
    run.calls = calls
    return run


# --- which system -------------------------------------------------------------------------------------------

def test_the_system_is_told_from_the_platform_and_the_kernel_release():
    assert platform_hints.host_system("darwin", "25.0.0") == "darwin"
    assert platform_hints.host_system("linux", "5.15.167.4-microsoft-standard-WSL2") == "wsl"
    assert platform_hints.host_system("linux", "6.8.0-45-generic") == "linux"
    assert platform_hints.is_wsl("5.15-Microsoft-standard") and not platform_hints.is_wsl("6.8.0-generic")


# --- the Mac's GPU ------------------------------------------------------------------------------------------

def test_the_gpu_of_a_mac_is_the_chip_with_its_unified_memory_and_metal():
    run = fake_mac()
    report = platform_hints.apple_gpu(run)
    assert report == {"available": True, "api": "Metal", "unified_memory_bytes": 36 * 2**30,
                      "devices": ["Apple M3 Pro, 18 GPU cores, 36 GiB unified memory, Metal 4"]}
    assert all(isinstance(argv, list) for argv, _ in run.calls)
    assert platform_hints.apple_gpu_line(run) == report["devices"][0]


def test_without_a_profile_the_cpu_brand_stands_in_for_the_apple_gpu():
    report = platform_hints.apple_gpu(fake_mac(profile="not json"))
    assert report["available"] and report["devices"] == ["Apple M3 Pro, 36 GiB unified memory"]


def test_an_intel_mac_has_video_memory_not_unified_memory():
    profile = json.dumps({"SPDisplaysDataType": [{"sppci_model": "AMD Radeon Pro 5500M", "spdisplays_vram": "8 GB",
                                                  "spdisplays_mtlgpufamilysupport": "spdisplays_metal3"}]})
    report = platform_hints.apple_gpu(fake_mac(profile=profile, brand="Intel(R) Core(TM) i9"))
    assert report["devices"] == ["AMD Radeon Pro 5500M, 8 GB video memory, Metal 3"]
    assert "unified_memory_bytes" not in report


@pytest.mark.parametrize("fail", [("system_profiler", "machdep.cpu.brand_string"),
                                  ("system_profiler", "machdep.cpu.brand_string", "hw.memsize")])
def test_a_mac_that_answers_nothing_reports_no_gpu_and_does_not_raise(fail):
    report = platform_hints.apple_gpu(fake_mac(fail=fail))
    assert report == {"available": False, "note": "The GPU could not be read from system_profiler or sysctl."}
    assert platform_hints.apple_gpu_line(fake_mac(fail=fail)) == "GPU unavailable"


def test_a_missing_tool_and_odd_output_are_not_trusted():
    def gone(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    assert platform_hints.apple_gpu(gone)["available"] is False
    odd = json.dumps({"SPDisplaysDataType": [None, 3, {"sppci_model": "Evil\x1b[31m\nname" + "x" * 500}]})
    device = platform_hints.apple_gpu(fake_mac(profile=odd, brand="", memory="garbage"))["devices"][0]
    assert "\x1b" not in device and "\n" not in device and len(device) <= 80


# --- doctor -------------------------------------------------------------------------------------------------

def quiet_doctor(settings, monkeypatch, system, run=None):
    monkeypatch.setattr(settings, "java_executable", "")
    monkeypatch.setattr(platform_hints, "host_system", lambda *a, **k: system)
    monkeypatch.setattr(integrations, "_probe_cli", lambda client, *a, **k: integrations_probe(client))
    monkeypatch.setattr(integrations.shutil, "which", lambda *a, **k: None)
    from cairnmind import runtime_selection
    monkeypatch.setattr(runtime_selection.shutil, "which", lambda *a, **k: None)
    if run is not None:
        monkeypatch.setattr(subprocess, "run", run)
    real_client = httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    return integrations.doctor(settings)


def integrations_probe(client):
    return {"installed": False, "notes": [platform_hints.hint("client_note", client=client)]}


def test_doctor_on_a_mac_names_the_gpu_and_has_no_wsl_or_apt_text(settings, monkeypatch):
    result = quiet_doctor(settings, monkeypatch, "darwin", fake_mac())
    assert result["gpu"]["available"] and result["gpu"]["devices"] == [
        "Apple M3 Pro, 18 GPU cores, 36 GiB unified memory, Metal 4"]
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


# --- benchmark ----------------------------------------------------------------------------------------------

def test_benchmark_names_the_apple_gpu_on_a_mac_and_nvidia_smi_elsewhere():
    assert platform_hints.benchmark_gpu("darwin", fake_mac()) == "Apple M3 Pro, 18 GPU cores, 36 GiB unified memory, Metal 4"

    def nvidia(argv, **kwargs):
        assert argv[0] == "nvidia-smi"
        return types.SimpleNamespace(returncode=0, stdout="RTX 4090, 1 MiB, 24564 MiB\n")

    def absent(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    assert platform_hints.benchmark_gpu("wsl", nvidia) == "RTX 4090, 1 MiB, 24564 MiB"
    assert platform_hints.benchmark_gpu("linux", absent) == "nvidia-smi unavailable"


# --- the hints, at the places that print them ---------------------------------------------------------------

WSL_WORDS = {
    "tmux_workspace": "Install tmux >= 3.4 in the same WSL distribution as Cairnmind.",
    "tmux_launch": "Install tmux >= 3.4 in Ubuntu: sudo apt install tmux",
    "tmux_action": "Install tmux >= 3.4 in the same WSL distribution",
    "tmux_code": "tmux_missing: install tmux >= 3.4 in this WSL distribution",
    "client_missing": "client_missing: install the native CLI in this WSL distribution",
    "java": "Install Java 21 or newer in WSL (openjdk-21-jre-headless)",
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
    from cairnmind import runtime_selection
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
    monkeypatch.setattr(workspace.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(workspace.sys, "stdout", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(workspace, "WorkspaceManager", lambda s: types.SimpleNamespace(
        diagnostics=lambda: {"available": False}))
    with pytest.raises(ValueError) as caught:
        workspace.launch_workspace(settings)
    assert str(caught.value) == platform_hints.hint("tmux_launch", name)


def test_a_missing_client_and_a_missing_java_ask_for_the_system_they_run_on(settings, monkeypatch, tmp_path):
    from cairnmind import client_auth
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
        assert str(caught.value) == platform_hints.hint("java", name)
        with pytest.raises(ValueError) as caught:
            NativeRuntime(settings).start({"root": str(tmp_path)}, "codex")
        assert str(caught.value) == platform_hints.hint("client_missing", name)
