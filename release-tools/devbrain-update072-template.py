#!/usr/bin/env python3
"""Install DevBrain 0.7.2 from verified 0.7.1 sources with guarded backups."""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from uuid import uuid4
import zlib

PAYLOAD = "__PAYLOAD__"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def terminal_open(binary):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Aktualizacja zakonczona. Otworz interaktywny terminal i uruchom devbrain.")
    return subprocess.run([binary], check=False).returncode


def installed_version(binary):
    result = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=10)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout).get("version")
    except (ValueError, AttributeError):
        return None


def stop_if_supported(binary, name):
    status = subprocess.run([binary, name, "status"], capture_output=True, text=True, timeout=15)
    if status.returncode == 0:
        info = json.loads(status.stdout)
        subprocess.run([binary, name, "stop"], check=True, timeout=30)
        if name == "cockpit" and info.get("available"):
            wait_for_broker_exit(info)


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19], fields[0]
    except (OSError, ValueError, IndexError):
        return None


def wait_for_broker_exit(info):
    """Stop only a positively identified old broker if HTTP shutdown leaves it alive."""
    pid, birth = info.get("pid"), info.get("birth")
    if type(pid) is not int or pid <= 1 or not isinstance(birth, str):
        return
    def alive():
        identity = process_identity(pid)
        return identity is not None and identity[0] == birth and identity[1] != "Z"
    deadline = time.monotonic() + 8
    while alive() and time.monotonic() < deadline:
        time.sleep(.1)
    if not alive():
        return
    # A fixture/embedded HTTP host is not a standalone daemon we may signal.
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        module = args.index(b"-m")
        data = args.index(b"--data-dir")
        cli = args.index(b"cockpit")
        own = (pid not in {os.getpid(), os.getppid()} and Path(f"/proc/{pid}").stat().st_uid == os.getuid()
               and args[module + 1] == b"devbrain" and args[cli + 1] == b"serve"
               and hashlib.sha256(str(Path(os.fsdecode(args[data + 1])).resolve()).encode()).hexdigest()[:24] == info.get("instance"))
    except (OSError, ValueError, IndexError):
        own = False
    if not own:
        raise ValueError("Poprzedni broker nadal dziala. Zakoncz jego interfejs i ponow aktualizacje; pliki nie zostaly zmienione.")
    try:
        pid_fd = os.pidfd_open(pid)
    except (AttributeError, OSError):
        if not alive():
            return
        raise ValueError("Zamknij poprzedni interfejs przed aktualizacja; system nie udostepnil bezpiecznego uchwytu procesu.") from None
    print("Zamykanie starego hosta interfejsu; procesy klientow pozostaja w tmux.", flush=True)
    try:
        for number, seconds in ((signal.SIGINT, 3), (signal.SIGKILL, 2)):
            if not alive():
                return
            try:
                signal.pidfd_send_signal(pid_fd, number)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + seconds
            while alive() and time.monotonic() < deadline:
                time.sleep(.1)
        if alive():
            raise ValueError("Nie udalo sie zatrzymac poprzedniego brokera; aktualizacja plikow zostala wstrzymana.")
    finally:
        os.close(pid_fd)


def apply(source, check_only=False, open_terminal=False):
    source = source.expanduser().resolve()
    if not (source / "scripts/install-wsl.sh").is_file():
        raise ValueError(f"Brak projektu DevBrain w {source}")
    package = json.loads(zlib.decompress(base64.b64decode(PAYLOAD)))
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    if version not in {*package["supported_versions"], package["version"]}:
        raise ValueError(f"Nieobslugiwana wersja zrodel {version}; wymagana wersja 0.7.1.")
    staged = []
    for item in package["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Nieprawidlowa sciezka w poprawce")
        path = source / relative
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError(f"Sciezka prowadzi poza projekt: {relative}")
        if path.exists() and not path.is_file():
            raise ValueError(f"Sciezka nie jest plikiem: {relative}")
        raw = (base64.b64decode(item["content"], validate=True)
               if item["encoding"] == "base64" else item["content"].encode("utf-8"))
        if digest(raw) != item["after"]:
            raise ValueError(f"Uszkodzona poprawka: {relative}")
        current = path.read_bytes() if path.is_file() else None
        current_hash = digest(current) if current is not None else None
        if current_hash == item["after"]:
            continue
        allowed = ([item["before_by_version"][version]] if version in package["supported_versions"]
                   else item["before"])
        if current_hash not in allowed:
            raise ValueError(f"Plik zmieniony lokalnie; nie nadpisano danych: {relative}")
        if relative.suffix == ".py":
            ast.parse(raw.decode("utf-8"), filename=str(relative))
        elif relative.suffix == ".toml" or relative.name == "uv.lock":
            tomllib.loads(raw.decode("utf-8"))
        staged.append((relative, path, raw, current))
    print(f"Sprawdzone pliki. Do aktualizacji: {len(staged)}. Wersja docelowa: {package['version']}", flush=True)
    if check_only:
        return 0
    binary = shutil.which("devbrain")
    if not binary or not shutil.which("uv"):
        raise ValueError("Uruchom skrypt w Ubuntu z devbrain i uv dostepnymi w PATH.")
    tmux = shutil.which("tmux")
    if not tmux:
        raise ValueError("Brak tmux 3.4+. Najpierw: sudo apt install tmux. Pliki aplikacji nie zostaly zmienione.")
    probe = subprocess.run([tmux, "-V"], capture_output=True, text=True, timeout=5)
    matched = re.search(r"tmux (\d+)\.(\d+)", probe.stdout)
    if probe.returncode or not matched or tuple(map(int, matched.groups())) < (3, 4):
        raise ValueError("Wymagany tmux 3.4+. Zaktualizuj tmux przed aktualizacja DevBrain.")
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "devbrain-updates"
    receipt_path = state_root / ("installed-" + digest(str(source).encode())[:20] + ".json")
    receipt = {"format_version": 1, "source": str(source), "version": package["version"],
               "payload_sha256": digest(PAYLOAD.encode())}
    if receipt_path.is_symlink():
        raise ValueError("Potwierdzenie instalacji nie moze byc dowiazaniem.")
    try:
        saved_receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        saved_receipt = None
    if not staged and saved_receipt == receipt and installed_version(binary) == package["version"]:
        print("DevBrain jest juz aktualny; pliki i uslugi pozostaly bez zmian.", flush=True)
        return terminal_open(binary) if open_terminal else 0
    # These commands stop view/service hosts, never the native client sessions.
    stop_if_supported(binary, "workspace")
    stop_if_supported(binary, "cockpit")
    subprocess.run([binary, "stop"], check=True, timeout=30)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    backup = state_root / stamp
    backup.mkdir(parents=True, mode=0o700)
    inventory = []
    for relative, path, raw, current in staged:
        inventory.append({"path": str(relative), "existed": current is not None})
        if current is not None:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(current)
            saved.chmod(0o600)
    inventory_path = backup / "manifest.json"
    inventory_path.write_text(json.dumps(inventory, indent=2))
    inventory_path.chmod(0o600)
    subprocess.run([binary, "backup", str(backup / "database.devbrain.json.gz")], check=True, timeout=180)
    for relative, path, raw, current in staged:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".devbrain-update-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(path.stat().st_mode & 0o777 if current is not None else 0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Kopia plikow i bazy: {backup}", flush=True)
    print("Instalowanie DevBrain 0.7.2 z zachowaniem danych, modeli, logowan i uprawnien...", flush=True)
    install_environment = dict(os.environ)
    install_environment["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
    result = subprocess.run(["bash", str(source / "scripts/install-wsl.sh")], cwd=source, env=install_environment).returncode
    if result:
        return result
    fd, temporary_name = tempfile.mkstemp(prefix=".installed-", dir=state_root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(receipt, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(receipt_path)
    finally:
        temporary.unlink(missing_ok=True)
    print("Gotowe. Terminalowe srodowisko uruchomisz komenda: devbrain", flush=True)
    return terminal_open(binary) if open_terminal else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path.home() / "src/devbrain")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--open", action="store_true", help="Po instalacji otworz srodowisko w terminalu")
    args = parser.parse_args()
    try:
        raise SystemExit(apply(args.source_dir, args.check_only, args.open))
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"Aktualizacja DevBrain: {error}", file=sys.stderr)
        raise SystemExit(1) from None
