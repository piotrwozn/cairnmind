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


def default_data_dir() -> Path:
    return Path(os.environ.get("DEVBRAIN_HOME", "~/.local/share/devbrain")).expanduser().resolve()


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
        for field, name in (("arcade_url", "DEVBRAIN_ARCADE_URL"),
                            ("arcade_user", "DEVBRAIN_ARCADE_USER"),
                            ("arcade_password", "DEVBRAIN_ARCADE_PASSWORD"),
                            ("arcade_database", "DEVBRAIN_ARCADE_DATABASE"),
                            ("java_executable", "DEVBRAIN_JAVA_EXECUTABLE"),
                            ("tmux_executable", "DEVBRAIN_TMUX_EXECUTABLE")):
            if name in os.environ:
                values[field] = os.environ[name]
        if not values.get("arcade_database"):
            prefix = os.environ.get("DEVBRAIN_ARCADE_DB_PREFIX", "devbrain")
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

