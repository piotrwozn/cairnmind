#!/usr/bin/env python3
"""Install DevBrain 0.8.0 from verified 0.7.2 sources with guarded backups."""
from __future__ import annotations

import argparse
import ast
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import zlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

PAYLOAD = "__PAYLOAD__"
GATE_CHECKS = {"ssl_candidate", "unit", "native_evaluation", "c2_repetitions", "native_children", "engines_stopped"}
PACKAGE_PROBE = r'''
import hashlib,importlib.metadata,importlib.util,json,sys
from pathlib import Path
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
prefix=Path(sys.prefix).resolve();base=Path(sys._base_executable).resolve()
spec=importlib.util.find_spec('devbrain')
if spec is None or not spec.origin:raise ValueError('DevBrain is not installed')
origin=Path(spec.origin).resolve();package=origin.parent
if prefix==Path(sys.base_prefix).resolve() or not package.is_relative_to(prefix):
    raise ValueError('Use an isolated wheel installation, not a system/editable package')
distribution=importlib.metadata.distribution('devbrain')
direct=json.loads(distribution.read_text('direct_url.json') or '{}')
if direct.get('dir_info',{}).get('editable'):raise ValueError('Editable backup package is not accepted')
files={}
for path in package.rglob('*'):
    if '__pycache__' in path.parts or path.suffix=='.pyc':continue
    if path.is_symlink():raise ValueError('Linked package file')
    if path.is_file():files[path.relative_to(package).as_posix()]=sha(path)
versions={}
for item in importlib.metadata.distributions():
    name=item.metadata['Name'].lower().replace('_','-').replace('.','-')
    if name in versions:raise ValueError('Duplicate installed distribution')
    versions[name]=item.version
print(json.dumps({'version':distribution.version,'base_executable':str(base),'base_sha256':sha(base),
 'prefix':str(prefix),'origin':str(origin),'package_sha256':files,'dependencies':versions,
 'python_version':list(sys.version_info[:3])}))
'''


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def terminal_open(command, environment=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Aktualizacja zakonczona. Otworz interaktywny terminal i uruchom devbrain.")
    return subprocess.run(command, check=False, env=environment).returncode


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_environment(runtime=None, data_dir=None):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE", "VIRTUAL_ENV", "LD_PRELOAD", "LD_LIBRARY_PATH",
                                  "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "DEVBRAIN_HOME",
                                  "DEVBRAIN_ARCADE_URL", "DEVBRAIN_ARCADE_USER", "DEVBRAIN_ARCADE_PASSWORD",
                                  "DEVBRAIN_ARCADE_DATABASE", "DEVBRAIN_ARCADE_DB_PREFIX"}}
    environment["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
    if runtime:
        environment["DEVBRAIN_JAVA_EXECUTABLE"] = runtime["java_executable"]
        environment["DEVBRAIN_TMUX_EXECUTABLE"] = runtime["tmux_executable"]
        environment["JAVA_HOME"] = str(Path(runtime["java_executable"]).parent.parent)
        environment["PATH"] = os.pathsep.join([str(Path(runtime["java_executable"]).parent),
                                               str(Path(runtime["tmux_executable"]).parent), environment.get("PATH", "")])
    if data_dir is not None:
        environment["DEVBRAIN_HOME"] = str(data_dir)
    return environment


def verify_java_environment(environment, runtime):
    """0.7.2 resolves Java through PATH; check that exact resolution without running it."""
    resolved = shutil.which("java", path=environment.get("PATH", ""))
    expected = Path(runtime["java_executable"])
    if (not resolved or Path(resolved).resolve() != expected
            or file_digest(Path(resolved)) != runtime["java_binary_sha256"]
            or environment.get("JAVA_HOME") != str(expected.parent.parent)
            or {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"} & environment.keys()):
        raise ValueError("Legacy 0.7.2 Java resolution does not match the qualified JDK")


def verify_runtime_gate(path, python_executable):
    path = path.expanduser().resolve(strict=True)
    gate = json.loads(path.read_text())
    checks = gate.get("checks", {})
    if (gate.get("schema_version") != 1 or gate.get("scope") != "bounded_runtime_qualification"
            or gate.get("status") != "pass" or not isinstance(checks, dict)
            or not GATE_CHECKS <= checks.keys()
            or any(not isinstance(item, dict) or item.get("pass") is not True for item in checks.values())):
        raise ValueError("A passed runtime gate with every mandatory check is required before accessing data")
    runtime = gate.get("runtime", {})
    for name in ("python", "java", "tmux"):
        executable, expected = runtime.get(name + "_executable"), runtime.get(name + "_binary_sha256")
        if (not isinstance(executable, str) or not Path(executable).is_absolute()
                or str(Path(executable).resolve(strict=True)) != executable
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or file_digest(Path(executable)) != expected or not os.access(executable, os.X_OK)):
            raise ValueError("Runtime binary differs from the passed gate: " + name)
    if (python_executable.expanduser().resolve(strict=True) != Path(runtime["python_executable"])
            or Path(sys._base_executable).resolve() != Path(runtime["python_executable"])):
        raise ValueError("Run the updater and --python using the exact qualified base Python")
    return runtime, file_digest(path)


def verify_package(python, runtime, version, expected_files, dependency_pins):
    # Preserve a venv's literal launcher. Resolving it for Popen loses its prefix.
    python = python.expanduser().absolute()
    if python.resolve(strict=True) != Path(runtime["python_executable"]) or file_digest(python) != runtime["python_binary_sha256"]:
        raise ValueError("The package interpreter does not use the qualified base Python")
    result = subprocess.run([str(python), "-I", "-B", "-c", PACKAGE_PROBE], capture_output=True,
                            text=True, timeout=20, check=True, env=runtime_environment(runtime))
    observed = json.loads(result.stdout)
    prefix, origin = Path(observed.get("prefix", "")), Path(observed.get("origin", ""))
    if (observed.get("version") != version or observed.get("base_executable") != runtime["python_executable"]
            or observed.get("base_sha256") != runtime["python_binary_sha256"]
            or not prefix.is_absolute() or not origin.is_absolute() or not origin.is_relative_to(prefix)
            or observed.get("python_version", []) < [3, 12] or observed.get("package_sha256") != expected_files):
        raise ValueError("Package version, origin, base interpreter or complete package manifest does not match")
    dependencies = observed.get("dependencies", {})
    if dependencies.get("devbrain") != version or not set(dependency_pins.get("__required__", [])) <= dependencies.keys() or any(
            name != "devbrain" and (name not in dependency_pins or value not in dependency_pins[name])
            for name, value in dependencies.items()):
        raise ValueError("Installed dependency versions differ from the immutable lockfile")
    observed["python_launcher"] = str(python)
    return observed


def validate_backup(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("The backup was not created as a private regular file")
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        envelope = json.load(stream)
    payload = envelope.get("payload", {})
    if (envelope.get("format") != "devbrain-arcadedb-backup" or envelope.get("version") != 1
            or payload.get("format_version") != 5
            or envelope.get("sha256") != digest(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode())):
        raise ValueError("Backup format or payload checksum is invalid; source changes were refused")


def tool_environment(uv, environment):
    result = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=10, check=True,
                            env=environment)
    root = Path(result.stdout.strip())
    if not root.is_absolute():
        raise ValueError("uv did not return an absolute tool directory")
    return (root / "devbrain").resolve()


def installed_target(uv, runtime, package):
    python = tool_environment(uv, runtime_environment(runtime)) / "bin/python"
    if not python.is_file() or python.resolve() != Path(runtime["python_executable"]):
        return None
    try:
        return verify_package(python, runtime, package["version"], package["target_package_sha256"], package["target_dependency_pins"])
    except (ValueError, subprocess.SubprocessError):
        return None


def stop_if_supported(command, name, environment):
    status = subprocess.run([*command, name, "status"], capture_output=True, text=True, timeout=15, check=False, env=environment)
    if status.returncode == 0:
        info = json.loads(status.stdout)
        subprocess.run([*command, name, "stop"], check=True, timeout=30, env=environment)
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


def apply(source, check_only=False, open_terminal=False, python_executable=None, backup_python=None, runtime_gate=None, data_dir=None):
    source = source.expanduser().resolve()
    if not check_only and any(value is None for value in (python_executable, backup_python, runtime_gate, data_dir)):
        raise ValueError("Apply requires --python, --backup-python, --runtime-gate and --data-dir; --check-only stays file-only")
    if not (source / "scripts/install-wsl.sh").is_file():
        raise ValueError(f"Brak projektu DevBrain w {source}")
    package = json.loads(zlib.decompress(base64.b64decode(PAYLOAD)))
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    if version not in {*package["supported_versions"], package["version"]}:
        raise ValueError(f"Nieobslugiwana wersja zrodel {version}; wymagana wersja 0.7.2.")
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
    # No old console script, service or private data is accessed before these
    # independent runtime/package checks. The backup wheel is never upgraded.
    runtime, gate_hash = verify_runtime_gate(runtime_gate, python_executable)
    python_executable = Path(runtime["python_executable"])
    backup_python = backup_python.expanduser().absolute()
    backup_identity = verify_package(backup_python, runtime, package["backup_version"],
                                     package["backup_package_sha256"], package["backup_dependency_pins"])
    data_dir = data_dir.expanduser().absolute()
    if (data_dir.resolve() != data_dir or not data_dir.is_dir() or not (data_dir / "config.json").is_file()
            or Path(backup_identity["prefix"]).is_relative_to(source)
            or Path(backup_identity["prefix"]).is_relative_to(data_dir)):
        raise ValueError("Use an existing explicit data directory and a backup venv outside source/data")
    environment = runtime_environment(runtime, data_dir)
    verify_java_environment(environment, runtime)
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("Uruchom skrypt w Ubuntu z uv dostepnym w PATH.")
    target_environment = tool_environment(uv, environment)
    backup_environment = Path(backup_identity["prefix"])
    if backup_environment.is_relative_to(target_environment) or target_environment.is_relative_to(backup_environment):
        raise ValueError("The backup venv must be independent of the uv tool environment that installation replaces")
    tmux = runtime["tmux_executable"]
    probe = subprocess.run([tmux, "-V"], capture_output=True, text=True, timeout=5, check=False, env=environment)
    matched = re.search(r"tmux (\d+)\.(\d+)", probe.stdout)
    if probe.returncode or not matched or tuple(map(int, matched.groups())) < (3, 4):
        raise ValueError("Wymagany tmux 3.4+. Zaktualizuj tmux przed aktualizacja DevBrain.")
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "devbrain-updates"
    receipt_path = state_root / ("installed-" + digest(str(source).encode())[:20] + ".json")
    receipt = {"format_version": 2, "source": str(source), "data_dir": str(data_dir), "version": package["version"],
               "payload_sha256": digest(PAYLOAD.encode()), "runtime_gate_sha256": gate_hash,
               "python_executable": str(python_executable), "python_binary_sha256": runtime["python_binary_sha256"],
               "backup_python": str(backup_python), "backup_identity": backup_identity,
               "target_environment": str(target_environment),
               "backup_baseline_sha256": package["backup_baseline_sha256"]}
    if receipt_path.is_symlink():
        raise ValueError("Potwierdzenie instalacji nie moze byc dowiazaniem.")
    try:
        saved_receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        saved_receipt = None
    target = installed_target(uv, runtime, package) if not staged and saved_receipt == receipt else None
    if target is not None:
        print("DevBrain jest juz aktualny; pliki i uslugi pozostaly bez zmian.", flush=True)
        command = [target["python_launcher"], "-I", "-B", "-m", "devbrain", "--data-dir", str(data_dir)]
        return terminal_open(command, environment) if open_terminal else 0
    # These commands stop view/service hosts, never the native client sessions.
    command = [str(backup_python), "-I", "-B", "-m", "devbrain", "--data-dir", str(data_dir)]
    stop_if_supported(command, "workspace", environment)
    stop_if_supported(command, "cockpit", environment)
    subprocess.run([*command, "stop"], check=True, timeout=30, env=environment)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
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
    if verify_runtime_gate(runtime_gate, python_executable)[1] != gate_hash or verify_package(
            backup_python, runtime, package["backup_version"], package["backup_package_sha256"], package["backup_dependency_pins"]) != backup_identity:
        raise ValueError("Qualified runtime or immutable backup package changed before backup")
    verify_java_environment(environment, runtime)
    subprocess.run([*command, "backup", str(backup / "database.devbrain.json.gz")], check=True, timeout=180, env=environment)
    validate_backup(backup / "database.devbrain.json.gz")
    backup_runtime = backup / "backup-runtime.json"
    backup_runtime.write_text(json.dumps(receipt, indent=2))
    backup_runtime.chmod(0o600)
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
    print("Instalowanie DevBrain 0.8.0 z zachowaniem danych, modeli, logowan i uprawnien...", flush=True)
    install_environment = dict(environment)
    install_environment["DEVBRAIN_INSTALL_PYTHON"] = str(python_executable)
    # The installer's Java/tmux preflight also uses the exact gated binaries.
    install_environment["PATH"] = os.pathsep.join([str(Path(runtime["java_executable"]).parent), str(Path(tmux).parent), environment.get("PATH", "")])
    result = subprocess.run(["bash", str(source / "scripts/install-wsl.sh")], cwd=source, env=install_environment, check=False).returncode
    if result:
        return result
    target = installed_target(uv, runtime, package)
    if target is None:
        raise ValueError("Installer completed without a verified target package on the qualified Python")
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
    command = [target["python_launcher"], "-I", "-B", "-m", "devbrain", "--data-dir", str(data_dir)]
    return terminal_open(command, environment) if open_terminal else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path.home() / "src/devbrain")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--open", action="store_true", help="Po instalacji otworz srodowisko w terminalu")
    parser.add_argument("--python", type=Path, help="Use a specific, previously validated Python interpreter")
    parser.add_argument("--backup-python", type=Path, help="Required for apply: isolated immutable 0.7.2 wheel venv using the qualified base")
    parser.add_argument("--runtime-gate", type=Path, help="Required for apply: passed runtime qualification JSON")
    parser.add_argument("--data-dir", type=Path, help="Required for apply: explicit existing DevBrain data directory")
    args = parser.parse_args()
    try:
        raise SystemExit(apply(args.source_dir, args.check_only, args.open, args.python, args.backup_python, args.runtime_gate, args.data_dir))
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"Aktualizacja DevBrain: {error}", file=sys.stderr)
        raise SystemExit(1) from None
