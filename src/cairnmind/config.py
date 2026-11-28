"""Shared runtime settings. All persistent state belongs in the WSL filesystem."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cairnmind.pal.file_locks import lock_exclusive
from cairnmind.pal.private_files import ensure_private_dir, write_durable_private

# Environment variables are spelled CAIRNMIND_<NAME>. The earlier spelling DEVBRAIN_<NAME> is
# still read, and still set for child processes, so that sessions, project files and shell
# profiles from earlier versions keep working. The new spelling wins when both are present.
ENV_PREFIXES = ("CAIRNMIND_", "DEVBRAIN_")
SESSION_NAMES = ("SESSION_ID", "PACKET_ID", "PROJECT", "CLIENT", "CONTEXT_FILE")
DATA_DIR_NAMES = ("cairnmind", "devbrain")
# What the knowledge service and the interface broker answer on /health. A process started by an
# earlier version answers with the earlier name and is still recognised, so that it can be stopped.
SERVICE_NAMES = ("cairnmind", "devbrain")
BROKER_NAMES = ("cairnmind-cockpit", "devbrain-cockpit")
# The question budget (not extraction, #266) of a new installation on macOS only (elsewhere Settings.ask_timeout): with an MLX
# build on Apple silicon one discovery call took 43 s, so discovery, a repair and the support check need
# about twice the budget of Linux (#231).
DARWIN_ASK_TIMEOUT = 120.0
# Whole-database backups: the suffix written and the envelope format, each with its earlier form.
BACKUP_SUFFIXES = (".cairnmind.json.gz", ".devbrain.json.gz")
BACKUP_FORMATS = ("cairnmind-arcadedb-backup", "devbrain-arcadedb-backup")


def tls_verify(url: str) -> bool:
    """The `verify` of an HTTP client for this address: False only for plain http (#415).

    httpx builds a TLS context with the whole certificate store for every client, about 20 ms, whether or not the
    address is https. A plain http connection has no TLS and nothing to verify, so skipping the store weakens
    nothing; an https address, such as a model server on another computer, is verified as before. Whatever is not
    exactly `http://` (in any case, as httpx reads the scheme) is verified: a space before it, no address, another scheme.
    """
    return not (isinstance(url, str) and url.lower().startswith("http://"))


def env_names(name: str) -> tuple[str, ...]:
    """Both spellings of one variable, the current one first."""
    return tuple(prefix + name for prefix in ENV_PREFIXES)


def derived_database(root: Path) -> str:
    """The database name of a data directory that names none: the prefix and a hash of its path.

    A new installation takes `cairnmind_`. A directory that already is an installation keeps the earlier
    `devbrain_` (#369): one with a config.json that names no database (written before the name was saved),
    one that names the earlier derived name, or one whose database files already exist. An unreadable
    config.json counts as existing."""
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    prefix = env_value("ARCADE_DB_PREFIX")
    if prefix is not None:
        return prefix + "_" + digest
    earlier = "devbrain_" + digest
    config = root / "config.json"
    try:
        stored = json.loads(config.read_text(encoding="utf-8")).get("arcade_database") if config.exists() else "new"
    except (OSError, ValueError, AttributeError):
        stored = ""
    if not stored or stored == earlier or (root / "arcade" / "databases" / earlier).exists():
        return earlier
    return "cairnmind_" + digest


def is_own_variable(name: str) -> bool:
    return name.startswith(ENV_PREFIXES)


def env_value(name: str, default: str | None = None, environ=None) -> str | None:
    """A configuration variable: the current spelling if it is present, else the earlier one."""
    environ = os.environ if environ is None else environ
    for key in env_names(name):
        if key in environ:
            return environ[key]
    return default


def session_value(name: str, environ=None, *, strict: bool = True) -> str | None:
    """A variable of the session protocol: the first spelling that is not empty.

    A project file can forward a listed variable as an empty string, so presence proves
    nothing here. Two spellings that are both set and disagree mean that two launchers left
    their marks in one environment: that is refused, or None with ``strict=False``.
    """
    environ = os.environ if environ is None else environ
    values = [environ[key] for key in env_names(name) if environ.get(key)]
    if len(set(values)) > 1:
        if strict:
            current, earlier = env_names(name)
            raise ValueError(f"resume_required: {current} and {earlier} name different sessions")
        return None
    return values[0] if values else None


def set_env(environ, name: str, value: str) -> None:
    """Set both spellings, so that a reader of either version sees the same value."""
    for key in env_names(name):
        environ[key] = value


def drop_env(environ, *names: str) -> None:
    for name in names:
        for key in env_names(name):
            environ.pop(key, None)


def _home_override(environ=None) -> str | None:
    """The data directory named by a variable: the first spelling that is not empty, as in the installer."""
    environ = os.environ if environ is None else environ
    return next((environ[key] for key in env_names("HOME") if environ.get(key)), None)


def env_conflicts(environ=None) -> list[str]:
    """Configuration names whose two spellings are both set and differ; the current spelling is the one used."""
    environ = os.environ if environ is None else environ
    found = []
    for key in environ:
        name = key[len(ENV_PREFIXES[0]):]
        earlier = ENV_PREFIXES[1] + name
        if (not key.startswith(ENV_PREFIXES[0]) or name in SESSION_NAMES or earlier not in environ
                or environ[earlier] == environ[key]):
            continue
        if name == "HOME" and not (environ[key] and environ[earlier]):
            continue  # an empty spelling names no directory and is passed over
        found.append(name)
    return sorted(found)


def session_conflicts(environ=None) -> list[str]:
    """Session names whose two spellings name different things; neither is honoured (see session_value)."""
    environ = os.environ if environ is None else environ
    return [name for name in SESSION_NAMES if len({environ[key] for key in env_names(name) if environ.get(key)}) > 1]


def _holds_a_store(path: Path) -> bool:
    """Whether an earlier version left anything there, even a store that lost its configuration file."""
    if path.is_symlink():
        return True  # also a dangling link: stay on it and fail there, as before
    try:
        return any(path.iterdir())
    except OSError:
        return False


def _default_data_base(environ=None) -> Path:
    """Base directory for the default data directory: %LOCALAPPDATA% on Windows, ~/.local/share on POSIX."""
    if sys.platform == "win32":
        env = os.environ if environ is None else environ
        local = env.get("LOCALAPPDATA")
        if local:
            return Path(local)
        return Path.home() / "AppData" / "Local"
    return Path("~/.local/share").expanduser()


def default_data_dir(environ=None) -> Path:
    """Where this installation keeps its data when no --data-dir is given.

    A directory that an earlier version created is used where it is: it is never moved or
    copied, because project files, running services and the database name all refer to its
    path. Only an installation with no data yet starts under the current name. environ={} gives
    the directory of a plain shell, where no CAIRNMIND_HOME is set.
    """
    chosen = _home_override(environ)
    if chosen:
        return Path(chosen).expanduser().resolve()
    base = _default_data_base(environ)
    current, earlier = (base / name for name in DATA_DIR_NAMES)
    if _holds_a_store(earlier):
        return earlier.resolve()
    return current.resolve()


def data_dir_note() -> str | None:
    """What a user should know about the directory chosen by default, or None."""
    if _home_override():
        return None
    base = _default_data_base()
    current, earlier = (base / name for name in DATA_DIR_NAMES)
    if _holds_a_store(earlier) and (current / "config.json").is_file():
        return (f"Both {earlier} and {current} hold data; {earlier} is used. "
                f"Choose one explicitly with --data-dir or {ENV_PREFIXES[0]}HOME.")
    return None


# The engine image of `cairnmind engine start --docker` (#272): deploy/arcadedb, published by the release job for
# linux/amd64 and linux/arm64 and pinned by the digest of its index. It must hold arcade_version's ArcadeDB.
ARCADE_IMAGE = "ghcr.io/piotrwozn/cairnmind-arcadedb"
# Published from 477159d by the workflow "Database image" (docs/RELEASING.md); "sha256:" and 64 zeros means unpublished.
ARCADE_IMAGE_DIGEST = "sha256:25de89aff7ece566802b6eea7e4271b5a8540524c0c2ffc773e9be07c92b609c"


@dataclass
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    cockpit_port: int = 8766
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3.5:9b"
    embedding_model: str = "qwen3-embedding:0.6b"
    num_ctx: int = 16384
    # The knowledge agent's model server for each role; None is the local Ollama at ollama_url.
    # Checked by memory_models.normalize_memory_model. The model names stay in the two fields above.
    memory_chat: dict | None = None
    memory_embedding: dict | None = None
    # The sensitivity classes (sensitivity.CLASSES) a role on another computer may receive (#181). Checked by
    # memory_egress.allowed_classes: a value that is not a list of distinct known classes sends nothing.
    memory_remote_classes: list = field(default_factory=lambda: ["project"])
    answer_tokens: int = 1200
    max_steps: int = 8
    ask_timeout: float = 60.0
    poll_seconds: float = 10.0
    token: str = ""
    # Storage backend selection (ADR-01): "sqlite" (embedded WAL + FTS5 + vectors) or "arcade" (ArcadeDB server).
    # "" preserves existing ArcadeDB behavior unless state.db or db_path selects SQLite.
    storage_backend: str = ""
    # Optional explicit SQLite database path (ADR-01; e.g. ":memory:" or a custom file path).
    db_path: str = ""
    arcade_url: str = "http://127.0.0.1:2480"
    arcade_user: str = "root"
    arcade_password: str = field(default="", repr=False)
    arcade_database: str = ""
    arcade_version: str = "26.11.1"
    java_executable: str = ""
    # Where the managed engine runs (#272): "native" (Java 21 on this machine) or "docker" (Cairnmind's image).
    # "" until `cairnmind init` chooses: every config.json before 0.13.0 has none, and those engines are native.
    engine_mode: str = ""
    # With engine_mode "docker": run the image built on this machine (`engine start --docker --build`), not the
    # published one. Set only by `engine start --docker`.
    engine_docker_build: bool = False
    # Whether the knowledge agent has models (#274): "enabled", or "disabled" (deterministic-only mode). "" until
    # `cairnmind init` chooses, which counts as enabled; a config.json from before 0.13.0 has none and is enabled.
    knowledge_agent: str = ""
    tmux_executable: str = ""
    # Database access outside this machine (#72): a private file holding the password, and the PEM file of
    # a private certificate authority. Both are absolute paths; see database_credentials.
    arcade_password_file: str = ""
    arcade_ca_file: str = ""
    # Connect-only mode (#68): the address of a database this installation only connects to and never
    # installs, starts or stops. When set it is the address used, in place of arcade_url; see database_mode.
    arcade_connect_url: str = ""
    # Set by load and never saved: where the password came from ("password"), why it cannot be used
    # ("problem"), the values load put in place of the stored ones ("kept": field -> (stored, loaded)) and the
    # variable each of them came from ("environment": field -> spelling).
    _origin: dict = field(default_factory=dict, repr=False, compare=False)
    # Keep every answer of `ask` in the project's answer log (answer_log.py); off unless set to true.
    answer_log: bool = False
    # What is kept without end otherwise (#190). At each engine start an engine.log of at least
    # engine_log_bytes is moved to engine.log.1 (older copies shift up, engine_log_copies are kept); a
    # completed job loses its attempt history job_history_days after it last changed (0: never).
    engine_log_bytes: int = 10 * 1024 * 1024
    engine_log_copies: int = 3
    job_history_days: int = 30
    # Automatic SCIP multi-language code indexing: "auto" (enqueue on project add/change),
    # "manual" (only on explicit `cairnmind scip index`/`import`), or "off".
    scip_mode: str = "auto"
    scip_timeout: float = 300.0
    scip_docker_fallback: bool = True
    scip_languages: list = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def load(cls, data_dir: Path | None = None, *, create: bool = False) -> Settings:
        """Read settings without filesystem writes unless initialization is explicit."""
        root = (data_dir or default_data_dir()).expanduser().resolve()
        path = root / "config.json"
        if create:
            if not root.exists():
                ensure_private_dir(root)
            with (root / "config.lock").open("a", encoding="utf-8") as lock:
                lock_exclusive(lock)
                obj = cls.load(root, create=False)
                changed = False
                if not obj.token:
                    obj.token = secrets.token_urlsafe(32)
                    changed = True
                # Only the managed engine on this machine gets a generated password, kept in config.json
                # as before (#72). The password of a database this installation only connects to (#68) is
                # set where that server runs, and comes from the environment or a file.
                from cairnmind.storage.database_mode import connect_only, savable
                # The connect address is saved the first time it is seen in the environment (#68): a command
                # started later without the variable (another shell, a terminal server started before the
                # export) must not find a managed installation here and start an engine with an empty store.
                # Only when config.json names none: a different address in the variable is used by that
                # process, as for every other variable, and config.json keeps what it has.
                seen = obj._origin["kept"].get("arcade_connect_url")
                if (seen and not seen[0] and obj.arcade_connect_url == seen[1] and savable(seen[1])
                        and not obj._origin.get("connect_problem")):
                    del obj._origin["kept"]["arcade_connect_url"]
                    obj._origin["environment"].pop("arcade_connect_url", None)  # config.json holds it from now on
                    obj._origin["connect_saved"] = True
                    changed = True
                if obj._origin.get("password") == "none" and not connect_only(obj):
                    obj.arcade_password = secrets.token_urlsafe(32)
                    obj._origin["password"] = "config"  # noqa: S105 - origin label, not a secret
                    obj._origin["kept"].pop("arcade_password", None)
                    changed = True
                if changed:
                    obj.save()
                return obj
        values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        values.pop("data_dir", None)
        values = {k: v for k, v in values.items() if k in cls.__dataclass_fields__ and not k.startswith("_")}
        if path.exists():
            values.setdefault("knowledge_agent", "enabled")  # written before 0.13.0: its models count as configured
        if sys.platform == "darwin":  # only where config.json names none: a stored budget is the user's
            values.setdefault("ask_timeout", DARWIN_ASK_TIMEOUT)
        stored = dict(values)
        kept = {}
        environment = {}  # field -> the spelling of the variable that supplied it (topology.py names it)
        for name, variable in (("storage_backend", "BACKEND"),
                               ("storage_backend", "STORAGE_BACKEND"),
                               ("db_path", "DB_PATH"),
                               ("arcade_url", "ARCADE_URL"),
                               ("arcade_user", "ARCADE_USER"),
                               ("arcade_database", "ARCADE_DATABASE"),
                               ("arcade_password_file", "ARCADE_PASSWORD_FILE"),
                               ("arcade_ca_file", "ARCADE_CA_FILE"),
                               ("arcade_connect_url", "ARCADE_CONNECT_URL"),
                               ("java_executable", "JAVA_EXECUTABLE"),
                               ("tmux_executable", "TMUX_EXECUTABLE"),
                               ("port", "PORT"),
                               ("cockpit_port", "COCKPIT_PORT"),
                               ("ollama_url", "OLLAMA_URL"),
                               ("scip_mode", "SCIP_MODE"),
                               ("scip_timeout", "SCIP_TIMEOUT"),
                               ("scip_docker_fallback", "SCIP_DOCKER_FALLBACK")):
            spelling = ENV_PREFIXES[0] + variable
            if name in ("port", "cockpit_port", "ollama_url", "arcade_connect_url",
                        "storage_backend", "db_path",
                        "scip_mode", "scip_timeout", "scip_docker_fallback"):
                # an empty variable (a project file can forward one) names nothing: the first spelling that is not empty;
                # for the connect address it must not turn a connect-only installation into a managed one
                spelling = next((key for key in env_names(variable) if os.environ.get(key)), None)
                override = os.environ[spelling] if spelling else None
            else:
                override = env_value(variable)
                spelling = next((key for key in env_names(variable) if key in os.environ), None)
            if override is None:
                continue
            if name in ("port", "cockpit_port"):
                try:
                    override = int(override)
                    if not 0 < override < 65536:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{spelling} must be a port number from 1 to 65535.") from None
            elif name == "scip_mode":
                override = override.strip().lower()
                if override not in {"auto", "manual", "off"}:
                    raise ValueError(f"{spelling} must be one of: auto, manual, off.")
            elif name == "scip_timeout":
                try:
                    override = float(override)
                    if override <= 0:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{spelling} must be a positive number of seconds.") from None
            elif name == "scip_docker_fallback":
                lowered = override.strip().lower()
                if lowered in {"1", "true", "yes", "on"}:
                    override = True
                elif lowered in {"0", "false", "no", "off"}:
                    override = False
                else:
                    raise ValueError(f"{spelling} must be true or false.")
            values[name] = override
            kept[name] = (stored.get(name), override)
            if override != stored.get(name):  # a variable that repeats config.json supplies nothing
                environment[name] = spelling
        # Connect-only (#68): arcade_connect_url is the address. The stored arcade_url stays as it is in
        # config.json, so that removing the setting brings the managed engine back.
        connect, conflict = values.get("arcade_connect_url") or "", None
        if connect and not isinstance(connect, str):
            conflict = "arcade_connect_url must be an address, such as https://db.example:2490."
            values["arcade_connect_url"] = ""
        elif connect:
            named = env_value("ARCADE_URL")
            if named and named.rstrip("/") != connect.rstrip("/"):
                conflict = (f"{ENV_PREFIXES[0]}ARCADE_URL and arcade_connect_url ({ENV_PREFIXES[0]}ARCADE_CONNECT_URL) "
                            "name different database addresses; set only one of them.")
            kept["arcade_url"] = (stored.get("arcade_url"), connect)
            values["arcade_url"] = connect
        # The password: the environment, then arcade_password_file, then config.json for this machine only.
        from cairnmind.storage.database_credentials import resolve_password
        password, source, problem = resolve_password(values)
        problem = conflict or problem
        if password != stored.get("arcade_password", ""):
            kept["arcade_password"] = (stored.get("arcade_password"), password)
        values["arcade_password"] = password
        # An existing installation keeps its name (derived_database, #369).
        prefix = env_value("ARCADE_DB_PREFIX", "cairnmind")
        derived = derived_database(root)
        if not values.get("arcade_database"):
            if not prefix or not prefix.replace("_", "").isalnum():
                raise ValueError("Invalid ArcadeDB database prefix")
            values["arcade_database"] = derived
        # The first save stores the derived name in config.json, so it is recognised by its value.
        named_database = "derived" if values["arcade_database"] == derived else "configured"
        # The same holds for the user: the first save writes the default root, so root read back from
        # config.json counts as the default; only the environment sets it on purpose.
        named_user = "configured" if "arcade_user" in kept or values.get("arcade_user", "root") != "root" else "default"
        obj = cls(data_dir=root, **values, _origin={"password": source, "problem": problem, "kept": kept,
                                                    "environment": environment, "database": named_database, "connect_problem": conflict,
                                                    "user": named_user})
        return obj

    def save(self) -> None:
        if not self.data_dir.exists():
            ensure_private_dir(self.data_dir)
        values = asdict(self)
        values.pop("_origin", None)
        values["data_dir"] = str(self.data_dir)
        # A value load took from the environment or a password file stays out of config.json while it is
        # unchanged: config.json keeps what it had (#72). A value changed since load is saved.
        for name, (stored, loaded) in (self._origin or {}).get("kept", {}).items():
            if values.get(name) == loaded:
                if stored is None:
                    values.pop(name, None)
                else:
                    values[name] = stored
        path = self.data_dir / "config.json"
        # Atomic, owner-readable configuration, including the local bearer token.
        write_durable_private(path, json.dumps(values, indent=2), prefix=".config-")


def resolved_backend(settings: Settings) -> str:
    """Resolve active storage backend ('sqlite' or 'arcade') per ADR-01."""
    chosen = (getattr(settings, "storage_backend", "") or "").strip().lower()
    if chosen in {"sqlite", "arcade"}:
        return chosen
    if chosen:
        raise ValueError(f"Unsupported storage_backend: {chosen!r} (expected 'sqlite' or 'arcade').")
    if getattr(settings, "db_path", ""):
        return "sqlite"
    data_dir = Path(settings.data_dir)
    if (data_dir / "state.db").is_file() and not (data_dir / "arcade" / "databases").exists():
        return "sqlite"
    return "arcade"


def sqlite_db_path(settings: Settings) -> Path | str:
    """Resolve the SQLite database path (``data_dir/state.db`` or ``db_path`` override, ADR-01)."""
    override = (getattr(settings, "db_path", "") or "").strip()
    if override == ":memory:":
        return ":memory:"
    if override:
        return Path(override).expanduser().resolve()
    return Path(settings.data_dir).expanduser().resolve() / "state.db"

