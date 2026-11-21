"""The database engine in a Docker container, so Java 21 on this machine is optional (#272).

The container runs Cairnmind's image (deploy/arcadedb) as the user of this machine, with ``data_dir/arcade``
mounted where the native engine keeps the same four folders, so one data directory serves both engines, never
at the same time. Everything asked of Docker goes through ``run_docker``. Stop and start act only on the
container that has this installation's name and both labels, and refuse when that cannot be established.
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse

from . import __version__, config
from .arcade import ArcadeError
from .database_mode import MODE_INVALID, connect_only, refusal
from .terminal_safe import safe_text

LABEL_INSTALLATION = "io.cairnmind.installation"
LABEL_DATA_DIR = "io.cairnmind.data-dir"
CONTAINER_DATA = "/var/lib/arcadedb"
CONTAINER_PASSWORD = "/run/secrets/arcadedb_root_password"
JAVA_OPTIONS = "ARCADEDB_JAVA_OPTS=-Darcadedb.server.mode=development"  # like the native engine, see docs/DEPLOYMENT.md
WAIT_ROUNDS, WAIT_STEP = 240, 0.25  # a first start of the JVM in a container is slower than the native 30 s
STOP_SECONDS = 60

DOCKER_NOT_FOUND = "docker was not found on PATH."
DOCKER_NO_DAEMON = "docker is installed, but its daemon does not answer: {detail}"
DOCKER_FAILED = "docker {command} could not be run: {detail}"
DOCKER_UNUSABLE = ("The engine mode is docker, but Docker cannot be used: {problem} Start Docker, or run the engine "
                   "with Java 21 on this machine: `cairnmind engine start --native`.")
UNPUBLISHED = ("This release of Cairnmind pins no published engine image yet. Build the image on this machine "
               "instead: `cairnmind engine start --docker --build`.")
IMAGE_MISSING = ("The engine image {image} is not on this machine. Download it with "
                 "`cairnmind engine start --docker`.")
LOCAL_IMAGE_MISSING = ("The engine image {image}, built on this machine, is missing. Build it again with "
                       "`cairnmind engine start --docker --build`.")
IMAGE_FILES_MISSING = ("The files to build the engine image are not part of this installation ({path} is missing). "
                       "Install Cairnmind with scripts/install-wsl.sh, or use the published image: "
                       "`cairnmind engine start --docker`.")
BUILD_FAILED = "docker build of the engine image failed (exit status {code}); its output is above."
PULLING = "Downloading the engine image {image}; this happens once per release."
PULL_FAILED = "docker pull {image} failed (exit status {code}); its output is above."
NOT_OURS = ("A container named {name} exists, but it does not carry this installation's labels; Cairnmind does not "
            "touch it. Remove or rename it yourself if nothing uses it.")
CANNOT_ASK = ("Docker cannot be asked about the engine container {name}: {problem} Nothing was started or "
              "stopped.")
RECORD_NOT_OURS = ("arcade/container.json names the container {recorded}, but this installation's container is "
                   "{name}: the data directory was copied or moved. Cairnmind does not touch {recorded}; stop it "
                   "with docker yourself if it is yours.")
NATIVE_RUNS = ("The native engine (Java, process {pid}) runs on this data directory. Stop it with `cairnmind engine "
               "stop` first: the two engines must not use one data directory at the same time.")
NATIVE_UNKNOWN = ("This data directory records a native engine (process {pid}) whose command line cannot be read, so "
                  "it cannot be told whether it runs here. Nothing was started.")
CONTAINER_RUNS = ("An engine in Docker (container {name}) runs on this data directory. Stop it with `cairnmind "
                  "engine stop` first: the two engines must not use one data directory at the same time.")
OTHER_IMAGE = ("The engine container {name} runs {running}, not {wanted}. Stop it with `cairnmind engine stop`, then "
               "start again.")
RUN_FAILED = "docker run of the engine container failed: {detail}"
EXITED = "The engine container {name} stopped during startup. See: docker logs {name}"
TIMED_OUT = ("The engine container {name} did not answer within {seconds} seconds and was stopped. See: docker logs "
             "{name}")
STOP_FAILED = "The engine container {name} could not be stopped: {detail}"
REMOVE_FAILED = "The engine container {name} could not be removed: {detail}"
NO_CONTAINER = "No engine container {name} for this data directory"
ROOT_REFUSED = ("Run Cairnmind as your own user, not as root: the engine container would make the data directory "
                "belong to another user.")
PATH_UNSAFE = ("Docker cannot mount the data directory {path}: its path contains ':' or a control character. Use a "
               "data directory without them.")
SHORT_PASSWORD = ("The engine image needs a root password of at least 8 characters; the configured one is "
                  "shorter.")
BUILD_INVALID = "engine_docker_build in config.json is {value}; it must be true or false."
MODE_SAVED = "Engine mode: {mode}, saved as engine_mode in {config}."
CHOSE_DOCKER = ("Engine mode: docker. Java 21 was not found and Docker answers, so the database engine runs in a "
                "container. Saved as engine_mode in {config}.")
CHOSE_NATIVE = ("Engine mode: native. Java 21 was found ({java}), so the database engine runs on this machine. "
                "Saved as engine_mode in {config}.")
KEPT_NATIVE = ("Engine mode: native, the engine this data directory already holds. Saved as engine_mode in "
               "{config}. To run it in Docker instead: `cairnmind engine start --docker`.")
NEITHER = ("Neither Java 21 nor Docker can run the database engine here. Java: {java}. Docker: {docker} Install one "
           "of them, then run `cairnmind init` again.")
JAVA_NOT_NEEDED = "The engine runs in Docker; Java is not needed on this machine."
DOCKER_ATTENTION = "Docker does not answer, and the engine runs in Docker: {error}"

# The Polish of every sentence above; the terminal catalog merges it (terminal_i18n).
MESSAGES = {
    DOCKER_NOT_FOUND: "Nie znaleziono polecenia docker w PATH.",
    DOCKER_NO_DAEMON: "docker jest zainstalowany, ale jego demon nie odpowiada: {detail}",
    DOCKER_FAILED: "Nie udało się uruchomić docker {command}: {detail}",
    DOCKER_UNUSABLE: ("Tryb silnika to docker, ale nie można użyć Dockera: {problem} Uruchom Dockera albo uruchom "
                      "silnik z Javą 21 na tym komputerze: `cairnmind engine start --native`."),
    UNPUBLISHED: ("To wydanie Cairnmind nie wskazuje jeszcze opublikowanego obrazu silnika. Zbuduj obraz na tym "
                  "komputerze: `cairnmind engine start --docker --build`."),
    IMAGE_MISSING: ("Obrazu silnika {image} nie ma na tym komputerze. Pobierz go poleceniem "
                    "`cairnmind engine start --docker`."),
    LOCAL_IMAGE_MISSING: ("Brakuje obrazu silnika {image} zbudowanego na tym komputerze. Zbuduj go ponownie "
                          "poleceniem `cairnmind engine start --docker --build`."),
    IMAGE_FILES_MISSING: ("Pliki do zbudowania obrazu silnika nie są częścią tej instalacji (brakuje {path}). "
                          "Zainstaluj Cairnmind skryptem scripts/install-wsl.sh albo użyj opublikowanego obrazu: "
                          "`cairnmind engine start --docker`."),
    BUILD_FAILED: "docker build obrazu silnika nie powiódł się (kod wyjścia {code}); jego wynik jest powyżej.",
    PULLING: "Pobieranie obrazu silnika {image}; dzieje się to raz na wydanie.",
    PULL_FAILED: "docker pull {image} nie powiódł się (kod wyjścia {code}); jego wynik jest powyżej.",
    NOT_OURS: ("Istnieje kontener o nazwie {name}, ale nie ma etykiet tej instalacji; Cairnmind go nie rusza. "
               "Usuń go lub zmień jego nazwę samodzielnie, jeśli nic go nie używa."),
    CANNOT_ASK: ("Nie można zapytać Dockera o kontener silnika {name}: {problem} Nic nie zostało uruchomione ani "
                 "zatrzymane."),
    RECORD_NOT_OURS: ("arcade/container.json wskazuje kontener {recorded}, ale kontener tej instalacji to {name}: "
                      "katalog danych skopiowano lub przeniesiono. Cairnmind nie rusza {recorded}; zatrzymaj go "
                      "samodzielnie dockerem, jeśli jest twój."),
    NATIVE_RUNS: ("Na tym katalogu danych działa natywny silnik (Java, proces {pid}). Najpierw zatrzymaj go "
                  "poleceniem `cairnmind engine stop`: dwa silniki nie mogą jednocześnie używać jednego katalogu "
                  "danych."),
    NATIVE_UNKNOWN: ("Ten katalog danych zapisał natywny silnik (proces {pid}), którego wiersza poleceń nie można "
                     "odczytać, więc nie wiadomo, czy tu działa. Nic nie zostało uruchomione."),
    CONTAINER_RUNS: ("Na tym katalogu danych działa silnik w Dockerze (kontener {name}). Najpierw zatrzymaj go "
                     "poleceniem `cairnmind engine stop`: dwa silniki nie mogą jednocześnie używać jednego "
                     "katalogu danych."),
    OTHER_IMAGE: ("Kontener silnika {name} działa na obrazie {running}, a nie {wanted}. Zatrzymaj go poleceniem "
                  "`cairnmind engine stop` i uruchom ponownie."),
    RUN_FAILED: "docker run kontenera silnika nie powiódł się: {detail}",
    EXITED: "Kontener silnika {name} zatrzymał się podczas uruchamiania. Zobacz: docker logs {name}",
    TIMED_OUT: ("Kontener silnika {name} nie odpowiedział w ciągu {seconds} sekund i został zatrzymany. Zobacz: "
                "docker logs {name}"),
    STOP_FAILED: "Nie udało się zatrzymać kontenera silnika {name}: {detail}",
    REMOVE_FAILED: "Nie udało się usunąć kontenera silnika {name}: {detail}",
    NO_CONTAINER: "Brak kontenera silnika {name} dla tego katalogu danych",
    ROOT_REFUSED: ("Uruchom Cairnmind jako własny użytkownik, nie jako root: kontener silnika przekazałby katalog "
                   "danych innemu użytkownikowi."),
    PATH_UNSAFE: ("Docker nie może zamontować katalogu danych {path}: jego ścieżka zawiera ':' lub znak sterujący. "
                  "Użyj katalogu danych bez nich."),
    SHORT_PASSWORD: ("Obraz silnika wymaga hasła roota o długości co najmniej 8 znaków; skonfigurowane jest "
                     "krótsze."),
    BUILD_INVALID: "engine_docker_build w config.json ma wartość {value}; musi to być true albo false.",
    MODE_INVALID: "engine_mode w config.json ma wartość {value}; musi to być native albo docker.",
    MODE_SAVED: "Tryb silnika: {mode}, zapisany jako engine_mode w {config}.",
    CHOSE_DOCKER: ("Tryb silnika: docker. Nie znaleziono Javy 21, a Docker odpowiada, więc silnik bazy danych "
                   "działa w kontenerze. Zapisano jako engine_mode w {config}."),
    CHOSE_NATIVE: ("Tryb silnika: native. Znaleziono Javę 21 ({java}), więc silnik bazy danych działa na tym "
                   "komputerze. Zapisano jako engine_mode w {config}."),
    KEPT_NATIVE: ("Tryb silnika: native, czyli silnik, który ten katalog danych już ma. Zapisano jako engine_mode "
                  "w {config}. Aby uruchamiać go w Dockerze: `cairnmind engine start --docker`."),
    NEITHER: ("Ani Java 21, ani Docker nie mogą tu uruchomić silnika bazy danych. Java: {java}. Docker: {docker} "
              "Zainstaluj jedno z nich i uruchom ponownie `cairnmind init`."),
    JAVA_NOT_NEEDED: "Silnik działa w Dockerze; Java nie jest potrzebna na tym komputerze.",
    DOCKER_ATTENTION: "Docker nie odpowiada, a silnik działa w Dockerze: {error}",
}


def run_docker(arguments, *, timeout=15, show: bool = False) -> subprocess.CompletedProcess:
    """The one place Docker is run. With show its output goes to this process's stderr (a pull or a build)."""
    shown = {"stdout": 2, "stderr": 2} if show else {}
    try:
        return subprocess.run(["docker", *arguments], stdin=subprocess.DEVNULL, capture_output=not show,
                              text=True, timeout=timeout, **shown)
    except FileNotFoundError:
        raise ArcadeError(DOCKER_NOT_FOUND) from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = "it did not answer in time" if isinstance(exc, subprocess.TimeoutExpired) else (
            exc.strerror or type(exc).__name__)
        raise ArcadeError(DOCKER_FAILED.format(command=arguments[0], detail=detail)) from None


def _detail(done) -> str:
    """The last line Docker wrote on stderr, as inert text; its exit status when it wrote none."""
    lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
    return safe_text(lines[-1].strip(), limit=300) if lines else f"exit status {done.returncode}"


def docker_answers() -> tuple[str | None, str | None]:
    """(the server's version, None) when Docker answers, or (None, why not)."""
    try:
        done = run_docker(["version", "--format", "{{.Server.Version}}"])
    except ArcadeError as exc:
        return None, str(exc)
    version = (done.stdout or "").strip()
    if done.returncode != 0 or not version:
        return None, DOCKER_NO_DAEMON.format(detail=_detail(done))
    return safe_text(version, limit=40), None


def _state(settings) -> Path:
    return settings.data_dir / "arcade"


def container_name(settings) -> str:
    from .store_writer import installation
    return "cairnmind-arcadedb-" + installation(settings.data_dir).id[:12]


def labels(settings) -> dict[str, str]:
    from .store_writer import installation
    return {LABEL_INSTALLATION: installation(settings.data_dir).id, LABEL_DATA_DIR: str(settings.data_dir)}


def inspect_container(settings) -> dict | None:
    """The container of this installation's name as Docker describes it; None when there is none.

    Fails closed: Docker not answering, a timeout or a reply of another shape raise CANNOT_ASK."""
    name = container_name(settings)
    try:
        done = run_docker(["container", "inspect", name])
    except ArcadeError as exc:
        raise ArcadeError(CANNOT_ASK.format(name=name, problem=str(exc))) from None
    if done.returncode != 0:
        if "no such container" in (done.stderr or "").casefold():
            return None
        raise ArcadeError(CANNOT_ASK.format(name=name, problem=_detail(done)))
    try:
        found = json.loads(done.stdout)
    except ValueError:
        found = None
    if not (isinstance(found, list) and len(found) == 1 and isinstance(found[0], dict)):
        raise ArcadeError(CANNOT_ASK.format(name=name, problem="its answer was not what was asked for."))
    return found[0]


def is_ours(info: dict, settings) -> bool:
    """The container has this installation's name and both its labels with exactly these values."""
    held = (info.get("Config") or {}).get("Labels") or {}
    return (str(info.get("Name", "")).lstrip("/") == container_name(settings)
            and all(held.get(key) == value for key, value in labels(settings).items()))


def _running(info: dict) -> bool:
    return (info.get("State") or {}).get("Running") is True


def container_state(settings) -> dict:
    name = container_name(settings)
    try:
        info = inspect_container(settings)
    except ArcadeError as exc:
        return {"name": name, "state": "unknown", "error": str(exc)}
    if info is None:
        return {"name": name, "state": "absent"}
    if not is_ours(info, settings):
        return {"name": name, "state": "not ours", "error": NOT_OURS.format(name=name)}
    return {"name": name, "state": safe_text((info.get("State") or {}).get("Status") or "unknown", limit=40)}


def local_image_tag() -> str:
    return f"cairnmind-arcadedb:{__version__}-local"


def image_reference(settings, local: bool | None = None) -> str:
    """The image to run: the one built here (local, or the stored engine_docker_build) or the pinned published one."""
    if local is None:
        local = settings.engine_docker_build
        if type(local) is not bool:
            raise ValueError(BUILD_INVALID.format(value=repr(local)[:80]))
    return local_image_tag() if local else f"{config.ARCADE_IMAGE}@{config.ARCADE_IMAGE_DIGEST}"


def build_context() -> Path:
    """The files of the image, shipped inside the package (pyproject.toml force-include of deploy/arcadedb)."""
    context = Path(__file__).resolve().parent / "arcadedb_image"
    if not (context / "Dockerfile").is_file():
        raise ArcadeError(IMAGE_FILES_MISSING.format(path=context))
    return context


def container_recorded(settings) -> bool:
    """Whether this data directory holds the record of a container it started (arcade/container.json)."""
    return (_state(settings) / "container.json").exists()


def _check_record(settings, name: str) -> None:
    path = _state(settings) / "container.json"
    try:
        recorded = json.loads(path.read_text()).get("name")
    except (OSError, ValueError, AttributeError):
        recorded = None
    if recorded != name:
        raise ArcadeError(RECORD_NOT_OURS.format(recorded=safe_text(recorded or "unknown", limit=80), name=name))


def refuse_if_container_runs(settings) -> None:
    """The native start's check (#272): no record, no Docker call. A recorded container that runs, or that cannot
    be asked about, stops the start; one that is gone or stopped loses its record."""
    if not container_recorded(settings):
        return
    name = container_name(settings)
    _check_record(settings, name)
    info = inspect_container(settings)
    if info is not None:
        if not is_ours(info, settings):
            raise ArcadeError(NOT_OURS.format(name=name))
        if _running(info):
            raise ArcadeError(CONTAINER_RUNS.format(name=name))
    (_state(settings) / "container.json").unlink(missing_ok=True)


def status_fields(settings) -> dict:
    """What `engine status` says of an engine in Docker, before the server is asked; no Java is looked for."""
    return {"mode": "managed", "engine": "docker", "available": False, "url": settings.arcade_url,
            "expected_version": settings.arcade_version, "image": image_reference(settings),
            "container": container_state(settings)}


def doctor_block(settings) -> dict:
    version, problem = docker_answers()
    if problem:
        return {"available": False, "error": problem}
    return {"available": True, "server_version": version, "container": container_state(settings)}


def _remove(name: str) -> None:
    done = run_docker(["rm", name], timeout=60)
    if done.returncode != 0:
        raise ArcadeError(REMOVE_FAILED.format(name=name, detail=_detail(done)))


def _publish_address(settings) -> tuple[str, int]:
    parsed = urlparse(settings.arcade_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.scheme != "http":
        raise ValueError("ArcadeDB must bind locally")
    return ("[::1]" if parsed.hostname == "::1" else "127.0.0.1"), parsed.port or 2480


def start_docker_engine(settings, *, local: bool | None = None, build: bool = False, pull: bool = False) -> dict:
    """Start this installation's engine container, or return the engine that already answers.

    local None takes the stored engine_docker_build; build builds the image first (and implies local); pull lets
    a published image that is missing be downloaded, which only `engine start` and `init` allow."""
    from .engine import ConnectOnly
    if connect_only(settings):
        raise ConnectOnly(refusal(settings, "start an engine"))
    settings = replace(settings, engine_mode="docker")
    state = _state(settings)
    if os.getuid() == 0:
        raise ArcadeError(ROOT_REFUSED)
    if ":" in str(state) or any(ord(char) < 32 or ord(char) == 127 for char in str(state)):
        raise ArcadeError(PATH_UNSAFE.format(path=safe_text(state, limit=200)))
    if len(settings.arcade_password) < 8:
        raise ArcadeError(SHORT_PASSWORD)
    use_local = True if build else local
    image = image_reference(settings, use_local)
    local_image = image == local_image_tag()
    if not local_image and config.ARCADE_IMAGE_DIGEST == "sha256:" + "0" * 64:
        raise ArcadeError(UNPUBLISHED)
    context = build_context() if build else None
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.data_dir / "engine.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _start_locked(settings, image, local_image, context, pull)


def _wait(settings, name: str) -> dict:
    from .engine import engine_status
    for _ in range(WAIT_ROUNDS):
        status = engine_status(settings)
        if status["available"]:
            if not status.get("version_matches"):
                raise ArcadeError(f"ArcadeDB release does not match the pinned {settings.arcade_version}")
            return status
        if (status.get("container") or {}).get("state") not in ("running", "created", "restarting", "unknown"):
            raise ArcadeError(EXITED.format(name=name))
        time.sleep(WAIT_STEP)
    stopped = run_docker(["stop", "--time", str(STOP_SECONDS), name], timeout=STOP_SECONDS + 30)
    if stopped.returncode != 0:
        raise ArcadeError(STOP_FAILED.format(name=name, detail=_detail(stopped)))
    raise ArcadeError(TIMED_OUT.format(name=name, seconds=int(WAIT_ROUNDS * WAIT_STEP)))


def _start_locked(settings, image: str, local_image: bool, context: Path | None, pull: bool) -> dict:
    from .engine import engine_status, native_engine_pid, write_root_password
    pid = native_engine_pid(settings)
    if pid is not None:
        raise ArcadeError(NATIVE_RUNS.format(pid=pid))
    version, problem = docker_answers()
    if problem:
        raise ArcadeError(DOCKER_UNUSABLE.format(problem=problem))
    if context is not None:
        done = run_docker(["build", "--tag", image, str(context)], timeout=None, show=True)
        if done.returncode != 0:
            raise ArcadeError(BUILD_FAILED.format(code=done.returncode))
    name = container_name(settings)
    info = inspect_container(settings)
    if info is not None:
        if not is_ours(info, settings):
            raise ArcadeError(NOT_OURS.format(name=name))
        if _running(info):
            running = str((info.get("Config") or {}).get("Image", ""))
            if running != image:
                raise ArcadeError(OTHER_IMAGE.format(name=name, running=safe_text(running, limit=200), wanted=image))
            return _wait(settings, name)
        _remove(name)
    if run_docker(["image", "inspect", "--format", "{{.Id}}", image]).returncode != 0:
        if local_image:
            raise ArcadeError(LOCAL_IMAGE_MISSING.format(image=image))
        if not pull:
            raise ArcadeError(IMAGE_MISSING.format(image=image))
        print(f"cairnmind: {PULLING.format(image=image)}", file=sys.stderr)
        done = run_docker(["pull", image], timeout=None, show=True)
        if done.returncode != 0:
            raise ArcadeError(PULL_FAILED.format(image=image, code=done.returncode))
    state = _state(settings)
    state.mkdir(exist_ok=True, mode=0o700)  # they must belong to this user before the mount, or Docker makes them root's
    for folder in ("config", "databases", "backups", "log"):
        (state / folder).mkdir(exist_ok=True, mode=0o700)
    password_file = write_root_password(settings)
    host, port = _publish_address(settings)
    held = labels(settings)
    record = state / "container.json"
    fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"name": name, "installation": held[LABEL_INSTALLATION], "data_dir": str(settings.data_dir),
                   "image": image, "started_at": time.time()}, handle)
    os.chmod(record, 0o600)
    done = run_docker(["run", "--detach", "--name", name,
                       "--label", f"{LABEL_INSTALLATION}={held[LABEL_INSTALLATION]}",
                       "--label", f"{LABEL_DATA_DIR}={held[LABEL_DATA_DIR]}",
                       "--user", f"{os.getuid()}:{os.getgid()}",
                       "--publish", f"{host}:{port}:2480",
                       "--volume", f"{state}:{CONTAINER_DATA}",
                       "--volume", f"{password_file}:{CONTAINER_PASSWORD}:ro",
                       "--env", JAVA_OPTIONS, "--pull", "never", image], timeout=120)
    if done.returncode != 0:
        raise ArcadeError(RUN_FAILED.format(detail=_detail(done)))
    return _wait(settings, name)


def stop_docker_engine(settings) -> dict:
    """Stop and remove this installation's container; nothing else is touched."""
    name = container_name(settings)
    record = _state(settings) / "container.json"
    if record.exists():
        _check_record(settings, name)
    info = inspect_container(settings)
    if info is None:
        record.unlink(missing_ok=True)
        return {"stopping": False, "reason": NO_CONTAINER.format(name=name)}
    if not is_ours(info, settings):
        raise ArcadeError(NOT_OURS.format(name=name))
    if _running(info):
        done = run_docker(["stop", "--time", str(STOP_SECONDS), name], timeout=STOP_SECONDS + 30)
        if done.returncode != 0:
            raise ArcadeError(STOP_FAILED.format(name=name, detail=_detail(done)))
    _remove(name)
    record.unlink(missing_ok=True)
    return {"stopping": True, "container": name}


def store_engine_mode(settings, mode: str, *, build: bool) -> bool:
    """Save the engine mode (and whether the image is built here) in config.json; True when that changed it."""
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.data_dir / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = config.Settings.load(settings.data_dir)
        changed = stored.engine_mode != mode or stored.engine_docker_build != build
        stored.engine_mode, stored.engine_docker_build = mode, build
        if changed:
            stored.save()
    settings.engine_mode, settings.engine_docker_build = mode, build
    return changed


def choose_engine_mode(settings) -> str | None:
    """The first choice of `cairnmind init`: the sentence saying which mode was chosen and saved, or None when
    nothing is chosen (connect-only, or a mode already stored)."""
    from .engine import java_major, resolve_java
    if connect_only(settings) or settings.engine_mode:
        return None
    where = settings.data_dir / "config.json"
    if _state(settings).exists():  # an installation from before 0.13.0: its engine is native
        store_engine_mode(settings, "native", build=False)
        return KEPT_NATIVE.format(config=where)
    java = resolve_java(settings)  # a configured java_executable that cannot be used raises: never passed over
    major = java_major(java) if java else None
    if major is not None and major >= 21:
        store_engine_mode(settings, "native", build=False)
        return CHOSE_NATIVE.format(java=java, config=where)
    version, problem = docker_answers()
    if problem:
        found = "not found" if java is None else f"Java {major}, older than 21" if major else "it did not run"
        raise ArcadeError(NEITHER.format(java=found, docker=problem))
    store_engine_mode(settings, "docker", build=False)
    return CHOSE_DOCKER.format(config=where)
