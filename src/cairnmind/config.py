"""Shared runtime settings. All persistent state belongs in the WSL filesystem."""
from __future__ import annotations

import json
import os
import secrets
import fcntl
import tempfile
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path


# Environment variables are spelled CAIRNMIND_<NAME>. The earlier spelling DEVBRAIN_<NAME> is
# still read, and still set for child processes, so that sessions, project files and shell
# profiles from earlier versions keep working. The new spelling wins when both are present.
ENV_PREFIXES = ("CAIRNMIND_", "DEVBRAIN_")
SESSION_NAMES = ("SESSION_ID", "PACKET_ID", "PROJECT", "CLIENT", "CONTEXT_FILE")
DATA_DIR_NAMES = ("cairnmind", "devbrain")


def env_names(name: str) -> tuple[str, ...]:
    """Both spellings of one variable, the current one first."""
    return tuple(prefix + name for prefix in ENV_PREFIXES)


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


def env_conflicts(environ=None) -> list[str]:
    """Names whose two spellings are both present with different values; the current spelling is the one used."""
    environ = os.environ if environ is None else environ
    return sorted(key[len(ENV_PREFIXES[0]):] for key in environ
                  if key.startswith(ENV_PREFIXES[0])
                  and ENV_PREFIXES[1] + key[len(ENV_PREFIXES[0]):] in environ
                  and environ[ENV_PREFIXES[1] + key[len(ENV_PREFIXES[0]):]] != environ[key])


def default_data_dir() -> Path:
    """Where this installation keeps its data when no --data-dir is given.

    A directory that an earlier version created is used where it is: it is never moved or
    copied, because project files, running services and the database name all refer to its
    path. Only an installation with no data yet starts under the current name.
    """
    chosen = env_value("HOME")
    if chosen:
        return Path(chosen).expanduser().resolve()
    base = Path("~/.local/share").expanduser()
    current, earlier = (base / name for name in DATA_DIR_NAMES)
    if (earlier / "config.json").is_file():
        return earlier.resolve()
    return current.resolve()


def data_dir_note() -> str | None:
    """What a user should know about the directory chosen by default, or None."""
    if env_value("HOME"):
        return None
    base = Path("~/.local/share").expanduser()
    current, earlier = (base / name for name in DATA_DIR_NAMES)
    if (earlier / "config.json").is_file() and (current / "config.json").is_file():
        return (f"Both {earlier} and {current} hold a configuration; {earlier} is used. "
                f"Choose one explicitly with --data-dir or {ENV_PREFIXES[0]}HOME.")
    return None


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
    answer_tokens: int = 1200
    max_steps: int = 8
    ask_timeout: float = 60.0
    poll_seconds: float = 10.0
    token: str = ""
    arcade_url: str = "http://127.0.0.1:2480"
    arcade_user: str = "root"
    arcade_password: str = ""
    arcade_database: str = ""
    arcade_version: str = "26.11.1"
    java_executable: str = ""
    tmux_executable: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def load(cls, data_dir: Path | None = None, *, create: bool = False) -> Settings:
        """Read settings without filesystem writes unless initialization is explicit."""
        root = (data_dir or default_data_dir()).expanduser().resolve()
        path = root / "config.json"
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (root / "config.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                obj = cls.load(root, create=False)
                changed = False
                if not obj.token:
                    obj.token = secrets.token_urlsafe(32)
                    changed = True
                if not obj.arcade_password:
                    obj.arcade_password = secrets.token_urlsafe(32)
                    changed = True
                if changed:
                    obj.save()
                return obj
        values = json.loads(path.read_text()) if path.exists() else {}
        values.pop("data_dir", None)
        values = {k: v for k, v in values.items() if k in cls.__dataclass_fields__}
        for field, name in (("arcade_url", "ARCADE_URL"),
                            ("arcade_user", "ARCADE_USER"),
                            ("arcade_password", "ARCADE_PASSWORD"),
                            ("arcade_database", "ARCADE_DATABASE"),
                            ("java_executable", "JAVA_EXECUTABLE"),
                            ("tmux_executable", "TMUX_EXECUTABLE")):
            override = env_value(name)
            if override is not None:
                values[field] = override
        if not values.get("arcade_database"):
            # The default prefix is part of the name of every existing database: it does not change.
            prefix = env_value("ARCADE_DB_PREFIX", "devbrain")
            if not prefix.replace("_", "").isalnum():
                raise ValueError("Invalid ArcadeDB database prefix")
            values["arcade_database"] = prefix + "_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
        obj = cls(data_dir=root, **values)
        return obj

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        values = asdict(self)
        values["data_dir"] = str(self.data_dir)
        path = self.data_dir / "config.json"
        # Atomic, owner-readable configuration, including the local bearer token.
        fd, name = tempfile.mkstemp(prefix=".config-", dir=self.data_dir)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(values, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

