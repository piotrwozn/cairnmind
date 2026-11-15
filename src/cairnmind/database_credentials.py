"""The database password, and the rules for reaching ArcadeDB on a host other than this one (#72).

The password is taken from the first of these that is set:

1. the variable CAIRNMIND_ARCADE_PASSWORD (or its earlier spelling DEVBRAIN_ARCADE_PASSWORD);
2. the file named by ``arcade_password_file``: an absolute path to a regular file of this user with
   mode 0600, not a link, as for the keys of the credential store (api_credentials.CredentialStore);
   or a Kubernetes secret mounted as a volume, whose file is a link in exactly the kubelet's layout to a
   regular file of this user that group and others cannot read (read_private_file, #211);
3. ``config.json``, and only for an engine on this machine (a loopback address). That is where the
   managed local engine's password has always been, so an installation from an earlier version keeps
   working unchanged. For any other host it is not used.

A password from the environment or a file is never written back to ``config.json`` (Settings.save).
No function here returns, logs or puts the password in a message: messages name the rule and the path.
"""
from __future__ import annotations

import os
import re
import ssl
import stat
from typing import Any

import httpx

from .config import env_names

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# The longest password file read: a password is one line, and a larger file is not one.
FILE_LIMIT = 4096
USER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
PASSWORD_HELP = ("set CAIRNMIND_ARCADE_PASSWORD, or name a private file (mode 0600) with "
                 "arcade_password_file in config.json")


def is_loopback(url: str) -> bool:
    try:
        return httpx.URL(url).host in LOOPBACK
    except (httpx.InvalidURL, TypeError):
        return False


def valid_password(value: str) -> bool:
    """One line of printable ASCII without spaces: the rule CredentialStore applies to a key."""
    return bool(value) and len(value) <= FILE_LIMIT and all(33 <= ord(char) <= 126 for char in value)


_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LINK_RULE = ("a link is followed only as Kubernetes mounts a secret: NAME -> ..data/NAME, and ..data -> a "
             "folder beside it whose name starts with ..; no other link is followed")


def _open_mounted_secret(path: str, setting: str) -> int:
    """The file behind a link laid out exactly as the kubelet mounts a secret, opened without following
    any further link: ``folder/NAME -> ..data/NAME``, ``folder/..data -> ..<time>``, and the regular file
    ``folder/..<time>/NAME``. The kubelet replaces ``..data`` when the secret changes and then removes
    the old folder, so a folder that vanished between the two steps is looked up once more."""
    refused = ValueError(f"{setting} names {path}, a symbolic link: {LINK_RULE}.")
    folder, name = os.path.split(path)
    for attempt in (1, 2):
        try:
            if os.readlink(path) != f"..data/{name}":
                raise refused
            version = os.readlink(os.path.join(folder, "..data"))
        except OSError:
            raise refused from None
        if "/" in version or not version.startswith("..") or version in ("..", "..data"):
            raise refused
        try:
            parent = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            raise refused from None
        try:
            inner = os.open(version, _FOLDER, dir_fd=parent)
            try:
                fd = os.open(name, _READ, dir_fd=inner)
            finally:
                os.close(inner)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise refused
            return fd
        except FileNotFoundError:
            if attempt == 2:
                raise refused from None
        except OSError:  # a link where a folder or the file must be, or a folder that cannot be read
            raise refused from None
        finally:
            os.close(parent)
    raise refused


def read_private_file(path: str, setting: str) -> str:
    """The one line of a private file of this user. ``setting`` names the setting in messages.

    The file itself is never a link, except in the layout of a mounted Kubernetes secret (#211); its
    file must then be a regular file of this user that group and others cannot read.
    """
    if not path or not os.path.isabs(path):
        raise ValueError(f"{setting} must be an absolute path.")
    try:
        mounted = stat.S_ISLNK(os.lstat(path).st_mode)
        fd = _open_mounted_secret(path, setting) if mounted else os.open(path, _READ)
    except FileNotFoundError:
        raise ValueError(f"{setting} names {path}, which does not exist.") from None
    except OSError:
        raise ValueError(f"{setting} names {path}, which cannot be read safely ({LINK_RULE}).") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if mounted:
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > FILE_LIMIT:
                raise ValueError(f"{setting} names {path}, a mounted secret whose file must be a regular file of "
                                 f"this user that group and others cannot read (defaultMode 0400 in Kubernetes) "
                                 f"and at most {FILE_LIMIT} bytes.")
        elif (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > FILE_LIMIT):
            raise ValueError(f"{setting} names {path}, which must be a regular file of this user with mode 0600 "
                             f"(chmod 600) and at most {FILE_LIMIT} bytes.")
        raw = handle.read(FILE_LIMIT + 1)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    value = text[:-1] if text.endswith("\n") else text
    value = value[:-1] if value.endswith("\r") else value
    if not valid_password(value):
        raise ValueError(f"{setting} names {path}, which must hold one line of printable characters without spaces.")
    return value


def resolve_password(values: dict, environ=None) -> tuple[str, str, str | None]:
    """(password, source, problem) for the settings ``values`` that Settings.load has gathered.

    source is environment, file, config, config_refused (a stored password that is not used because
    the host is not this machine) or none. problem is a message, without the password, or None.
    """
    environ = os.environ if environ is None else environ
    # The first spelling that is not empty, the current one first: an empty variable names no password,
    # so it does not hide the earlier spelling, and with neither the next source is used.
    for name in env_names("ARCADE_PASSWORD"):
        if environ.get(name):
            return environ[name], "environment", None
    path = values.get("arcade_password_file") or ""
    if path:
        try:
            return read_private_file(path, "arcade_password_file"), "file", None
        except ValueError as exc:
            return "", "file", str(exc)
    stored = values.get("arcade_password") or ""
    if not isinstance(stored, str):
        return "", "none", "arcade_password in config.json must be text."
    if is_loopback(values.get("arcade_url") or "http://127.0.0.1"):
        return stored, "config" if stored else "none", None
    return "", "config_refused" if stored else "none", None


def problem(settings) -> str | None:
    """Why ``settings`` must not be used to reach the database, or None. Checked by ArcadeClient."""
    try:
        parsed = httpx.URL(settings.arcade_url)
    except (httpx.InvalidURL, TypeError):
        return "arcade_url is not a valid address."
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return "arcade_url must start with https:// (or http:// for this machine)."
    if parsed.userinfo or parsed.query or parsed.fragment:
        return "arcade_url must not carry a user, a password, a query or a fragment."
    origin = getattr(settings, "_origin", None) or {}
    if origin.get("problem"):
        return origin["problem"]
    if parsed.host in LOOPBACK:
        return None
    if parsed.scheme != "https":
        return (f"ArcadeDB on {parsed.host}, a host other than this machine, must be reached with https://; "
                "plain http:// is allowed only for 127.0.0.1, localhost and ::1.")
    if settings.arcade_user == "root":
        return (f"ArcadeDB user root is refused for {parsed.host}, a host other than this machine. Create a "
                "user with rights on this database only (cairnmind db create-user) and set arcade_user.")
    if not USER_NAME.fullmatch(settings.arcade_user or ""):
        return "arcade_user must be a user name of letters, digits, _ and -, starting with a letter."
    if not settings.arcade_password:
        if origin.get("password") == "config_refused":
            return (f"The password stored in config.json is used only for the engine on this machine, not for "
                    f"{parsed.host}: {PASSWORD_HELP}.")
        return f"ArcadeDB on {parsed.host} needs a password: {PASSWORD_HELP}."
    return None


def tls_context(settings) -> ssl.SSLContext:
    """Certificate and host name verification, always on: the system's authorities, or the file named.

    Without ``arcade_ca_file`` the system's authorities are OpenSSL's default ones, and OpenSSL lets
    the variables SSL_CERT_FILE and SSL_CERT_DIR replace those. That is an operator's setting like
    CAIRNMIND_ARCADE_CA_FILE: verification stays on whichever authorities it names.
    """
    authority = getattr(settings, "arcade_ca_file", "") or ""
    if authority and not os.path.isabs(authority):
        raise ValueError("arcade_ca_file must be an absolute path.")
    try:
        context = ssl.create_default_context(cafile=authority or None)
    except (OSError, ssl.SSLError):
        raise ValueError(f"arcade_ca_file names {authority}, which cannot be loaded as PEM certificates.") from None
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    return context


def transport_label(settings) -> str:
    try:
        parsed = httpx.URL(settings.arcade_url)
    except (httpx.InvalidURL, TypeError):
        return "invalid"
    if parsed.scheme == "https":
        return "https (certificate verification on)"
    return "http (loopback, not encrypted)" if parsed.host in LOOPBACK else "http (not encrypted, refused)"


SOURCE_LABELS = {"environment": "environment", "file": "file", "config": "config.json",
                 "config_refused": "config.json (not used for this host)", "none": "none"}


def doctor_block(settings, *, timeout: float = 1.5) -> dict[str, Any]:
    """The database part of ``cairnmind doctor``: read-only, and through ArcadeClient, so its rules apply."""
    from .arcade import ArcadeClient, ArcadeError
    origin = getattr(settings, "_origin", None) or {}
    source = origin.get("password", "config" if settings.arcade_password else "none")
    result: dict[str, Any] = {"available": False, "database": settings.arcade_database,
                              "configured_version": settings.arcade_version, "user": settings.arcade_user,
                              "transport": transport_label(settings), "password_source": SOURCE_LABELS.get(source, source),
                              "notes": []}
    if getattr(settings, "arcade_password_file", ""):
        result["password_file"] = settings.arcade_password_file
    if getattr(settings, "arcade_ca_file", ""):
        result["ca_file"] = settings.arcade_ca_file
    if source == "config":
        result["notes"].append("The database password is stored in config.json. That is allowed for the engine on "
                               "this machine only; for a database on another host, " + PASSWORD_HELP + ".")
    try:
        client = ArcadeClient(settings)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    try:
        info = client._request("GET", "/api/v1/server?mode=basic", timeout=timeout).json()
        result.update({"available": True, "version": info.get("version"), "server_name": info.get("serverName"),
                       "version_matches": str(info.get("version", "")).startswith(settings.arcade_version)})
        try:
            client.query("SELECT 1 AS ok")
            result["database_available"] = True
        except ArcadeError as exc:
            result["database_available"] = False
            result["database_status"] = exc.status_code
    except (ArcadeError, ValueError, AttributeError) as exc:
        result["error"] = str(exc) if isinstance(exc, ArcadeError) else type(exc).__name__
    finally:
        client.close()
    return result


def admin_password(path: str) -> str:
    return read_private_file(path, "--admin-password-file")


def create_user(settings, admin_secret: str, *, group: str = "admin") -> dict[str, Any]:
    """Create the database and a user with rights on it only, signing in once as root (cairnmind db create-user).

    The user is ``arcade_user`` and the password the one resolved for it (environment or file); both
    must already be configured, so that the password never passes through this command's arguments.
    """
    from dataclasses import replace
    from .arcade import ArcadeClient, ArcadeError
    import json
    name, secret = settings.arcade_user, settings.arcade_password
    if name == "root" or not USER_NAME.fullmatch(name or ""):
        raise ValueError("Set arcade_user to the new user's name first (letters, digits, _ and -; not root).")
    source = (getattr(settings, "_origin", None) or {}).get("password")
    if source not in ("environment", "file") or not secret:
        problem_text = (getattr(settings, "_origin", None) or {}).get("problem")
        raise ValueError(problem_text or ("The new user's password must come from outside config.json: "
                                          + PASSWORD_HELP + "."))
    if not valid_password(secret):
        raise ValueError("The new user's password must be one line of printable characters without spaces.")
    admin = ArcadeClient(replace(settings, arcade_user="root", arcade_password=admin_secret), administrator=True)
    payload = {"name": name, "password": secret, "databases": {settings.arcade_database: [group]}}
    try:
        admin.ensure_database()
        admin.server_command("create user " + json.dumps(payload))
    except ArcadeError as exc:
        message = str(exc).replace(secret, "[redacted]").replace(admin_secret, "[redacted]")
        raise ArcadeError(f"The user {name} was not created: {message}", status_code=exc.status_code) from None
    finally:
        admin.close()
    check = ArcadeClient(settings)
    try:
        check.query("SELECT 1 AS ok")
    finally:
        check.close()
    return {"user": name, "database": settings.arcade_database, "group": group, "signed_in": True}

