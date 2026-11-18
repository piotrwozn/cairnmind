"""Which system this is, the install hint that fits it, and the GPU an Apple Mac reports (#233).

Linux and WSL keep the sentences they always had; a Mac gets Homebrew and no mention of WSL or apt, and a
plain Linux machine gets apt without WSL. The hint texts are fixed English sentences with a Polish
translation in MESSAGES, which the terminal catalog merges. Everything read from the machine is data: the
Darwin probes run without a shell, with a timeout and from the system's own folders, and fail soft.
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import sys

SYSTEMS = ("darwin", "wsl", "linux")


def is_wsl(release: str | None = None) -> bool:
    """True under WSL, where the kernel release names Microsoft (the test `doctor` has always used)."""
    return "microsoft" in (platform.release() if release is None else release).lower()


def host_system(platform_name: str | None = None, release: str | None = None) -> str:
    """'darwin' on a Mac, 'wsl' under WSL, otherwise 'linux' (the code's default, as before)."""
    if (platform_name or sys.platform) == "darwin":
        return "darwin"
    return "wsl" if is_wsl(release) else "linux"


_TMUX_APT = "Install tmux >= 3.4, for example with: sudo apt install tmux"
_TMUX_BREW = "Install tmux >= 3.4, for example with: brew install tmux"
_TMUX_APT_PL = "Zainstaluj tmux >= 3.4, na przykład poleceniem: sudo apt install tmux"
_TMUX_BREW_PL = "Zainstaluj tmux >= 3.4, na przykład poleceniem: brew install tmux"
_CODE_APT = "tmux_missing: install tmux >= 3.4, for example with: sudo apt install tmux"
_CODE_BREW = "tmux_missing: install tmux >= 3.4, for example with: brew install tmux"

# topic -> system -> (English, Polish). The WSL texts are the ones the code had before #233, word for word.
TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "tmux_workspace": {
        "wsl": ("Install tmux >= 3.4 in the same WSL distribution as Cairnmind.",
                "Zainstaluj tmux >= 3.4 w tej samej dystrybucji WSL co Cairnmind."),
        "linux": (_TMUX_APT, _TMUX_APT_PL), "darwin": (_TMUX_BREW, _TMUX_BREW_PL)},
    "tmux_launch": {
        "wsl": ("Install tmux >= 3.4 in Ubuntu: sudo apt install tmux",
                "Zainstaluj tmux >= 3.4 w Ubuntu: sudo apt install tmux"),
        "linux": (_TMUX_APT, _TMUX_APT_PL), "darwin": (_TMUX_BREW, _TMUX_BREW_PL)},
    "tmux_action": {
        "wsl": ("Install tmux >= 3.4 in the same WSL distribution",
                "Zainstaluj tmux >= 3.4 w tej samej dystrybucji WSL"),
        "linux": (_TMUX_APT, _TMUX_APT_PL), "darwin": (_TMUX_BREW, _TMUX_BREW_PL)},
    "tmux_code": {
        "wsl": ("tmux_missing: install tmux >= 3.4 in this WSL distribution",
                "tmux_missing: zainstaluj tmux >= 3.4 w tej dystrybucji WSL"),
        "linux": (_CODE_APT, "tmux_missing: zainstaluj tmux >= 3.4, na przykład poleceniem: sudo apt install tmux"),
        "darwin": (_CODE_BREW, "tmux_missing: zainstaluj tmux >= 3.4, na przykład poleceniem: brew install tmux")},
    "client_missing": {
        "wsl": ("client_missing: install the native CLI in this WSL distribution",
                "client_missing: zainstaluj natywne CLI w tej dystrybucji WSL"),
        "linux": ("client_missing: install the native CLI on this machine",
                  "client_missing: zainstaluj natywne CLI na tym komputerze"),
        "darwin": ("client_missing: install the native CLI on this machine",
                   "client_missing: zainstaluj natywne CLI na tym komputerze")},
    "client_note": {
        "wsl": ("Install {client} in the same WSL distribution as Cairnmind.",
                "Zainstaluj {client} w tej samej dystrybucji WSL co Cairnmind."),
        "linux": ("Install {client} on the same machine as Cairnmind.",
                  "Zainstaluj {client} na tym samym komputerze co Cairnmind."),
        "darwin": ("Install {client} on the same machine as Cairnmind.",
                   "Zainstaluj {client} na tym samym komputerze co Cairnmind.")},
    "client_not_installed": {
        "wsl": ("{client} is not installed in this WSL distribution",
                "{client} nie jest zainstalowany w tej dystrybucji WSL"),
        "linux": ("{client} is not installed on this machine", "{client} nie jest zainstalowany na tym komputerze"),
        "darwin": ("{client} is not installed on this machine", "{client} nie jest zainstalowany na tym komputerze")},
    "java": {
        "wsl": ("Install Java 21 or newer in WSL (openjdk-21-jre-headless)",
                "Zainstaluj Javę 21 lub nowszą w WSL (openjdk-21-jre-headless)"),
        "linux": ("Install Java 21 or newer, for example with: sudo apt install openjdk-21-jre-headless",
                  "Zainstaluj Javę 21 lub nowszą, na przykład poleceniem: sudo apt install openjdk-21-jre-headless"),
        "darwin": ("Install Java 21 or newer, for example with: brew install openjdk@21",
                   "Zainstaluj Javę 21 lub nowszą, na przykład poleceniem: brew install openjdk@21")},
    "gpu_missing": {
        "wsl": ("nvidia-smi not found; verify Windows NVIDIA driver and WSL GPU access.",
                "Nie znaleziono nvidia-smi; sprawdź sterownik NVIDIA w Windows i dostęp WSL do GPU."),
        "linux": ("nvidia-smi not found; verify that the NVIDIA driver is installed.",
                  "Nie znaleziono nvidia-smi; sprawdź, czy sterownik NVIDIA jest zainstalowany.")},
    "gpu_unreadable": {
        "darwin": ("The GPU could not be read from system_profiler or sysctl.",
                   "Nie udało się odczytać GPU przez system_profiler ani sysctl.")},
    "gpu_unavailable": {
        "wsl": ("nvidia-smi unavailable", "nvidia-smi niedostępne"),
        "linux": ("nvidia-smi unavailable", "nvidia-smi niedostępne"),
        "darwin": ("GPU unavailable", "GPU niedostępne")},
}

# English -> Polish, for the terminal catalog. Texts with {client} are formatted by the caller's translate.
MESSAGES = {english: polish for by_system in TEXTS.values() for english, polish in by_system.values()}


def hint(topic: str, system: str | None = None, **values: str) -> str:
    """The English sentence of a topic for this system (or the given one), with the values filled in."""
    by_system = TEXTS[topic]
    key = host_system() if system is None else system
    text = (by_system.get(key) or by_system.get("linux") or next(iter(by_system.values())))[0]
    return text.format(**values) if values else text


# --- the GPU of a Mac -------------------------------------------------------------------------------------

SYSTEM_PROFILER = "/usr/sbin/system_profiler"
SYSCTL = "/usr/sbin/sysctl"
PROBE_SECONDS = 8
OUTPUT_LIMIT = 2_000_000


def _text(value: object, limit: int = 80) -> str | None:
    """A short printable label from something the machine reported; anything else is dropped."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()[:limit]
    return text or None


def _probe(argv: list[str], run) -> str | None:
    """One read-only command, no shell, bounded in time and size; None when it fails in any way."""
    try:
        done = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=PROBE_SECONDS, check=False,
                                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    out = done.stdout if isinstance(done.stdout, str) else ""
    return out[:OUTPUT_LIMIT] if done.returncode == 0 else None


def _metal(value: object) -> str | None:
    """'spdisplays_metal4' -> 'Metal 4'; a plain 'supported' -> 'Metal'; unsupported or unknown -> None."""
    if not isinstance(value, str):
        return None
    word = value.removeprefix("spdisplays_").lower()
    numbered = re.fullmatch(r"metal(\d+)", word)
    if numbered:
        return f"Metal {numbered.group(1)}"
    return "Metal" if word in {"supported", "metal"} else None


def _gib(size: int) -> str:
    value = size / 2**30
    return str(round(value)) if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def _profiled(text: str | None) -> list[dict]:
    try:
        rows = json.loads(text or "").get("SPDisplaysDataType")
    except (ValueError, AttributeError):
        return []
    found = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("sppci_model")) or _text(row.get("_name"))
        if name:
            found.append({"name": name, "cores": _text(row.get("sppci_cores"), 8),
                          "metal": _metal(row.get("spdisplays_mtlgpufamilysupport")
                                          or row.get("spdisplays_metal")),
                          "vram": _text(row.get("spdisplays_vram"), 24)})
    return found


def apple_gpu(run=None) -> dict:
    """What `doctor` reports for the GPU on a Mac: the chipset, Metal support and the unified memory.

    From `system_profiler SPDisplaysDataType -json`; when that gives nothing, from the CPU brand string of
    `sysctl` (Apple silicon is one chip with the GPU). Memory is `hw.memsize`, shared with the GPU on Apple
    silicon only. Never raises: a machine that answers nothing yields available False and a note.
    """
    devices = _profiled(_probe([SYSTEM_PROFILER, "SPDisplaysDataType", "-json"], run))
    brand = _text(_probe([SYSCTL, "-n", "machdep.cpu.brand_string"], run))
    if not devices and brand and brand.startswith("Apple"):
        devices = [{"name": brand, "cores": None, "metal": None, "vram": None}]
    memory = _probe([SYSCTL, "-n", "hw.memsize"], run)
    size = int(memory.strip()) if memory and memory.strip().isdigit() else None
    if not devices:
        return {"available": False, "note": hint("gpu_unreadable", "darwin")}
    lines = []
    for device in devices:
        unified = device["name"].startswith("Apple")
        parts = [device["name"]]
        if device["cores"]:
            parts.append(f"{device['cores']} GPU cores")
        if unified and size:
            parts.append(f"{_gib(size)} GiB unified memory")
        elif device["vram"]:
            parts.append(f"{device['vram']} video memory")
        if device["metal"]:
            parts.append(device["metal"])
        lines.append(", ".join(parts))
    report = {"available": True, "api": "Metal", "devices": lines}
    if size and any(d["name"].startswith("Apple") for d in devices):
        report["unified_memory_bytes"] = size
    return report


def apple_gpu_line(run=None) -> str:
    """The same, as the one line `benchmark` prints."""
    report = apple_gpu(run)
    return "; ".join(report["devices"]) if report["available"] else hint("gpu_unavailable", "darwin")


def benchmark_gpu(system: str | None = None, run=None) -> str:
    """The GPU line of `benchmark`: the Mac's chip, or what nvidia-smi says (unchanged on Linux and WSL)."""
    if (host_system() if system is None else system) == "darwin":
        return apple_gpu_line(run)
    try:
        return (run or subprocess.run)(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return hint("gpu_unavailable", "linux")
