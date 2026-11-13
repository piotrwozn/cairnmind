"""Local credentials, separate from project memory and public profile metadata."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from .api_profiles import ENV_NAME, PROFILE_ID


def credential_environment_names():
    """Known credential variables to exclude from child command environments."""
    return ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
            "CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_ADMIN_KEY", "AZURE_OPENAI_API_KEY")


class CredentialStore:
    """Ephemeral keys live in this broker instance; persisted keys are opt-in.

    ``status`` never returns key text and does not create files. ``resolve`` is
    a private worker operation, not a public profile or UI endpoint.
    """
    def __init__(self, settings, *, environ=None):
        self.root = Path(settings.data_dir) / "credentials"
        self.path = self.root / "api-keys.json"
        self._ephemeral = {}
        self._mutex = threading.RLock()
        self._environ = os.environ if environ is None else environ

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not PROFILE_ID.fullmatch(value):
            raise ValueError("Niepoprawny identyfikator poświadczeń.")
        return value

    @staticmethod
    def _key(value):
        if (not isinstance(value, str) or not value or len(value) > 16384
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError("Niepoprawny klucz API.")
        return value

    def _directory(self, create=False):
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.exists() and not self.root.is_symlink():
            return False
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("Magazyn poświadczeń musi być prywatnym katalogiem użytkownika.")
        return True

    @contextmanager
    def _lock(self):
        self._directory(create=True)
        fd = os.open(self.root / "keys.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Blokada poświadczeń musi być prywatna.")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        if not self._directory():
            return {}
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {}
        except OSError:
            raise ValueError("Nie można bezpiecznie odczytać magazynu poświadczeń.") from None
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024 * 1024):
                raise ValueError("Plik poświadczeń musi być prywatnym plikiem użytkownika.")
            try:
                value = json.load(handle)
            except (ValueError, UnicodeError):
                raise ValueError("Niepoprawny magazyn poświadczeń.") from None
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("keys"), dict):
            raise ValueError("Nieobsługiwany magazyn poświadczeń.")
        for name, key in value["keys"].items():
            self._id(name)
            self._key(key)
        return value["keys"]

    def _write(self, keys):
        raw = json.dumps({"version": 1, "keys": keys}, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Magazyn poświadczeń przekroczył limit rozmiaru.")
        fd, temporary = tempfile.mkstemp(prefix=".api-keys-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def set(self, profile_id, key, persist=False):
        profile_id, key = self._id(profile_id), self._key(key)
        if type(persist) is not bool:
            raise ValueError("Wybierz sposób przechowywania klucza.")
        with self._mutex:
            if persist:
                with self._lock():
                    keys = self._read()
                    keys[profile_id] = key
                    self._write(keys)
                self._ephemeral.pop(profile_id, None)
            else:
                # Explicitly replacing a saved key with a session key must not
                # resurrect the old saved key after a broker restart.
                if self.path.exists() or self.path.is_symlink():
                    with self._lock():
                        keys = self._read()
                        if profile_id in keys:
                            keys.pop(profile_id)
                            self._write(keys)
                self._ephemeral[profile_id] = key
        return {"configured": True, "storage": "local" if persist else "session", "code": "configured_unchecked"}

    def delete(self, profile_id):
        profile_id = self._id(profile_id)
        with self._mutex:
            self._ephemeral.pop(profile_id, None)
            if self.path.exists() or self.path.is_symlink():
                with self._lock():
                    keys = self._read()
                    if profile_id in keys:
                        keys.pop(profile_id)
                        self._write(keys)
        return {"configured": False, "storage": "none", "code": "credential_missing"}

    @staticmethod
    def _reference(profile):
        if not isinstance(profile, dict):
            raise ValueError("Niepoprawny profil poświadczeń.")
        reference = profile.get("credential_ref")
        if reference is None:
            return None, None
        if not isinstance(reference, str):
            raise ValueError("Niepoprawne odwołanie do poświadczeń.")
        kind, _, name = reference.partition(":")
        if kind == "env" and ENV_NAME.fullmatch(name):
            return kind, name
        if kind == "profile" and PROFILE_ID.fullmatch(name):
            return kind, name
        raise ValueError("Niepoprawne odwołanie do poświadczeń.")

    def resolve(self, profile):
        kind, name = self._reference(profile)
        with self._mutex:
            if kind is None:
                return None
            if kind == "env":
                value = self._environ.get(name)
                return self._key(value) if value else None
            if name in self._ephemeral:
                return self._ephemeral[name]
            return self._read().get(name)

    def status(self, profile):
        kind, name = self._reference(profile)
        with self._mutex:
            if kind is None:
                return {"configured": True, "storage": "none", "code": "not_required"}
            if kind == "env":
                value = self._environ.get(name)
                configured = bool(value)
                storage = "environment" if configured else "none"
                if configured:
                    # The same check resolve() applies, so a status never says "configured" for a
                    # value the launcher refuses. The value is neither returned nor logged.
                    try:
                        self._key(value)
                    except ValueError:
                        return {"configured": False, "storage": storage, "code": "credential_invalid"}
            else:
                configured = name in self._ephemeral
                storage = "session" if configured else "local" if name in self._read() else "none"
                configured = storage != "none"
            return {"configured": configured, "storage": storage,
                    "code": "configured_unchecked" if configured else "credential_missing"}

    def clear_session(self):
        with self._mutex:
            self._ephemeral.clear()
