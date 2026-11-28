"""Which system this is, and the install hint that fits it (#233).

Linux and WSL keep the sentences they always had (TEXTS has no 'linux' entries: Linux reads the 'wsl'
ones); a Mac gets Homebrew and no mention of WSL or apt. The hint texts are fixed English sentences with a Polish
translation in MESSAGES, which the terminal catalog merges.
"""
from __future__ import annotations

import platform
import subprocess
import sys

SYSTEMS = ("darwin", "windows", "wsl", "linux")


def is_wsl(release: str | None = None) -> bool:
    """True under WSL, where the kernel release names Microsoft (the test `doctor` has always used)."""
    return "microsoft" in (platform.release() if release is None else release).lower()


def host_system(platform_name: str | None = None, release: str | None = None) -> str:
    """'darwin' on a Mac, 'windows' on native Windows, 'wsl' under WSL, otherwise 'linux'."""
    chosen = platform_name or sys.platform
    if chosen == "darwin":
        return "darwin"
    if chosen == "win32":
        return "windows"
    return "wsl" if is_wsl(release) else "linux"


_TMUX_BREW = "Install tmux >= 3.4, for example with: brew install tmux"
_TMUX_BREW_PL = "Zainstaluj tmux >= 3.4, na przykład poleceniem: brew install tmux"
_CODE_BREW = "tmux_missing: install tmux >= 3.4, for example with: brew install tmux"
_TMUX_WIN = "tmux is not available on native Windows; use the standalone terminal UI (`cairnmind`) or CLI commands."
_TMUX_WIN_PL = "tmux nie jest dostępny na natywnym Windowsie; użyj samodzielnego TUI (`cairnmind`) lub poleceń CLI."
_CODE_WIN = "tmux_missing: tmux is not available on native Windows"

# topic -> system -> (English, Polish). The WSL texts are the ones the code had before #233, word for word.
TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "tmux_workspace": {
        "wsl": ("Install tmux >= 3.4 in the same WSL distribution as Cairnmind.",
                "Zainstaluj tmux >= 3.4 w tej samej dystrybucji WSL co Cairnmind."),
        "darwin": (_TMUX_BREW, _TMUX_BREW_PL),
        "windows": (_TMUX_WIN, _TMUX_WIN_PL)},
    "tmux_launch": {
        "wsl": ("Install tmux >= 3.4 in Ubuntu: sudo apt install tmux",
                "Zainstaluj tmux >= 3.4 w Ubuntu: sudo apt install tmux"),
        "darwin": (_TMUX_BREW, _TMUX_BREW_PL),
        "windows": (_TMUX_WIN, _TMUX_WIN_PL)},
    "tmux_action": {
        "wsl": ("Install tmux >= 3.4 in the same WSL distribution",
                "Zainstaluj tmux >= 3.4 w tej samej dystrybucji WSL"),
        "darwin": (_TMUX_BREW, _TMUX_BREW_PL),
        "windows": (_TMUX_WIN, _TMUX_WIN_PL)},
    "tmux_code": {
        "wsl": ("tmux_missing: install tmux >= 3.4 in this WSL distribution",
                "tmux_missing: zainstaluj tmux >= 3.4 w tej dystrybucji WSL"),
        "darwin": (_CODE_BREW, "tmux_missing: zainstaluj tmux >= 3.4, na przykład poleceniem: brew install tmux"),
        "windows": (_CODE_WIN, "tmux_missing: tmux nie jest dostępny na natywnym Windowsie")},
    "client_missing": {
        "wsl": ("client_missing: install the native CLI in this WSL distribution",
                "client_missing: zainstaluj natywne CLI w tej dystrybucji WSL"),
        "darwin": ("client_missing: install the native CLI on this machine",
                   "client_missing: zainstaluj natywne CLI na tym komputerze"),
        "windows": ("client_missing: install the native CLI on this machine",
                    "client_missing: zainstaluj natywne CLI na tym komputerze")},
    "client_note": {
        "wsl": ("Install {client} in the same WSL distribution as Cairnmind.",
                "Zainstaluj {client} w tej samej dystrybucji WSL co Cairnmind."),
        "darwin": ("Install {client} on the same machine as Cairnmind.",
                   "Zainstaluj {client} na tym samym komputerze co Cairnmind."),
        "windows": ("Install {client} on the same machine as Cairnmind.",
                    "Zainstaluj {client} na tym samym komputerze co Cairnmind.")},
    "client_not_installed": {
        "wsl": ("{client} is not installed in this WSL distribution",
                "{client} nie jest zainstalowany w tej dystrybucji WSL"),
        "darwin": ("{client} is not installed on this machine", "{client} nie jest zainstalowany na tym komputerze"),
        "windows": ("{client} is not installed on this machine", "{client} nie jest zainstalowany na tym komputerze")},
    "java": {
        "wsl": ("Install Java 21 or newer in WSL: sudo apt install -y openjdk-21-jre-headless",
                "Zainstaluj Javę 21 lub nowszą w WSL: sudo apt install -y openjdk-21-jre-headless"),
        "darwin": ("Install Java 21 or newer, for example with: brew install openjdk@21",
                   "Zainstaluj Javę 21 lub nowszą, na przykład poleceniem: brew install openjdk@21"),
        "windows": ("Install Java 21 or newer, for example with: winget install EclipseAdoptium.Temurin.21.JRE",
                    "Zainstaluj Javę 21 lub nowszą, na przykład poleceniem: winget install EclipseAdoptium.Temurin.21.JRE")},
    "gpu_missing": {
        "wsl": ("nvidia-smi not found; verify Windows NVIDIA driver and WSL GPU access.",
                "Nie znaleziono nvidia-smi; sprawdź sterownik NVIDIA w Windows i dostęp WSL do GPU."),
        "darwin": ("The GPU is not checked on macOS.", "Na macOS GPU nie jest sprawdzane."),
        "windows": ("nvidia-smi not found; verify the NVIDIA driver on Windows.",
                    "Nie znaleziono nvidia-smi; sprawdź sterownik NVIDIA w Windows.")},
    "gpu_chip": {
        "darwin": ("{chip}, {gib} GiB unified memory shared with the models",
                   "{chip}, {gib} GiB pamięci współdzielonej z modelami")},
}

# English -> Polish, for the terminal catalog. Texts with {client} are formatted by the caller's translate.
MESSAGES = {english: polish for by_system in TEXTS.values() for english, polish in by_system.values()}


def _sysctl(name: str) -> str | None:
    """`sysctl -n name` run without a shell from its fixed path; None when it fails or prints nothing."""
    try:
        done = subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def mac_chip() -> dict | None:
    """{'chip', 'memory_gib', 'note'} of a Mac from two sysctl reads (unified memory); None if either is unusable."""
    chip, size = _sysctl("machdep.cpu.brand_string"), _sysctl("hw.memsize")
    if not chip or not size or len(size) > 20 or not size.isascii() or not size.isdigit() or int(size) <= 0:
        return None  # 20 digits hold any 64-bit byte count; longer would hit int()'s digit limit
    chip = " ".join(chip.split())  # the output is untrusted text: printable and short, or not used
    if len(chip) > 64 or not chip.isprintable() or not chip.startswith("Apple "):  # Intel: memory is not unified
        return None
    gib = round(int(size) / 2**30, 1)
    gib = int(gib) if gib == int(gib) else gib
    return {"chip": chip, "memory_gib": gib, "note": hint("gpu_chip", "darwin", chip=chip, gib=gib)}


def hint(topic: str, system: str | None = None, **values: str | int | float) -> str:
    """The English sentence of a topic for this system (or the given one), with the values filled in."""
    by_system = TEXTS[topic]
    key = host_system() if system is None else system
    text = (by_system.get(key) or by_system.get("wsl") or next(iter(by_system.values())))[0]
    return text.format(**values) if values else text
