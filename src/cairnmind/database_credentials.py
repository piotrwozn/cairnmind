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

import errno
import os
from pathlib import Path
import re
import ssl
import stat
from typing import Any

import httpx

from .config import env_names
from .private_files import IS_WINDOWS, is_private_stat, is_reparse_point, open_no_follow

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


_READ = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
         | getattr(os, "O_NONBLOCK", 0))
_FOLDER = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
           | getattr(os, "O_CLOEXEC", 0))
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
    target = Path(path)
    try:
        if IS_WINDOWS:
            if is_reparse_point(target):
                raise ValueError(f"{setting} names {path}, a symbolic link: {LINK_RULE}.")
            if target.is_dir():
                raise ValueError(f"{setting} names {path}, which must be a regular file of this user.")
            mounted = False
            fd = open_no_follow(target, os.O_RDONLY, private_on_create=False)
        else:
            mounted = stat.S_ISLNK(os.lstat(path).st_mode)
            fd = _open_mounted_secret(path, setting) if mounted else os.open(path, _READ)
    except ValueError:
        raise
    except FileNotFoundError:
        raise ValueError(f"{setting} names {path}, which does not exist.") from None
    except OSError as error:
        if error.errno == errno.ELOOP:  # it became a link between the check and the open
            raise ValueError(f"{setting} names {path}, a symbolic link: {LINK_RULE}.") from None
        reason = os.strerror(error.errno) if error.errno else "unknown error"
        raise ValueError(f"{setting} names {path}, which cannot be opened ({reason}).") from None
    # The checks run on the descriptor before it becomes a file object: os.fdopen refuses a folder with
    # "[Errno 21] Is a directory", which would name neither the setting nor the rule (#242).
    try:
        info = os.fstat(fd)
        if mounted:
            if not is_private_stat(info, target, directory=False, strict=False) or info.st_size > FILE_LIMIT:
                raise ValueError(f"{setting} names {path}, a mounted secret whose file must be a regular file of "
                                 f"this user that group and others cannot read (defaultMode 0400 in Kubernetes) "
                                 f"and at most {FILE_LIMIT} bytes.")
        elif not is_private_stat(info, target, directory=False, strict=True) or info.st_size > FILE_LIMIT:
            raise ValueError(f"{setting} names {path}, which must be a regular file of this user with mode 0600 "
                             f"(chmod 600) and at most {FILE_LIMIT} bytes.")
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
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
    # The setting a person wrote the address in: arcade_connect_url when that one supplied it (#68).
    named = "arcade_connect_url" if getattr(settings, "arcade_connect_url", "") else "arcade_url"
    try:
        parsed = httpx.URL(settings.arcade_url)
    except (httpx.InvalidURL, TypeError):
        return f"{named} is not a valid address."
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return f"{named} must start with https:// (or http:// for this machine)."
    if parsed.userinfo or parsed.query or parsed.fragment:
        return (f"{named} must not carry a user, a password, a query or a fragment: the user is arcade_user "
                f"and the password comes from outside the address ({PASSWORD_HELP}).")
    origin = getattr(settings, "_origin", None) or {}
    if origin.get("problem"):
        return origin["problem"]
    if parsed.host in LOOPBACK:
        if getattr(settings, "arcade_connect_url", "") and not settings.arcade_password:
            # Connect-only (#68): no engine is started here, so no password is generated for one either.
            from .database_mode import address
            return (f"ArcadeDB at {address(settings)}, which this installation only connects to, needs the "
                    f"password set where that server runs: {PASSWORD_HELP}.{_unset(settings)}")
        return None
    if parsed.scheme != "https":
        return (f"ArcadeDB on {parsed.host}, a host other than this machine, must be reached with https://; "
                "plain http:// is allowed only for 127.0.0.1, localhost and ::1.")
    if settings.arcade_user == "root":
        refusal = (f"ArcadeDB user root is refused for {parsed.host}, a host other than this machine. Create a "
                   "user with rights on this database only (cairnmind db create-user) and set arcade_user.")
        if not settings.arcade_password:
            # Everything the command lacks at once (#242): the password and the database too, not root alone.
            refusal += " " + _no_password(parsed.host, origin) + _unset(settings, user=False)
        return refusal
    if not USER_NAME.fullmatch(settings.arcade_user or ""):
        return "arcade_user must be a user name of letters, digits, _ and -, starting with a letter."
    if not settings.arcade_password:
        return _no_password(parsed.host, origin) + _unset(settings)
    return None


def _no_password(host: str, origin: dict) -> str:
    if origin.get("password") == "config_refused":
        return (f"The password stored in config.json is used only for the engine on this machine, not for "
                f"{host}: {PASSWORD_HELP}.")
    return f"ArcadeDB on {host} needs a password: {PASSWORD_HELP}."


def _unset(settings, *, user: bool = True) -> str:
    """The other settings a command without a password also lacks, and what it would use in their place.

    Only named, not refused (#242): root on this machine and a database name derived from the data directory
    are what the managed engine has always used, and #72 refuses root only for another host. Named together
    with the password, a command started from a shell without the variables says everything it lacks at once.
    """
    origin = getattr(settings, "_origin", None) or {}
    missing = []
    if user and origin.get("user") == "default":
        missing.append(f"arcade_user ({settings.arcade_user}, the default, would be used)")
    if origin.get("database") == "derived":
        missing.append(f"arcade_database (the name {settings.arcade_database}, derived from this data directory's "
                       "path, would be used)")
    if not missing:
        return ""
    return (" Not set either, or left at the default: " + " and ".join(missing) + ". Set them in config.json, or as CAIRNMIND_ARCADE_USER and "
            "CAIRNMIND_ARCADE_DATABASE, to the user and the database created for this installation.")


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
    from .database_mode import address, describe, missing_database, supported_versions, version_problem
    origin = getattr(settings, "_origin", None) or {}
    source = origin.get("password", "config" if settings.arcade_password else "none")
    # Where storage runs (#68): mode "managed" or "connect", and the host.
    place = describe(settings)
    connect = place["mode"] == "connect"
    result: dict[str, Any] = {**place, "available": False, "url": address(settings),
                              "database": settings.arcade_database,
                              "configured_version": settings.arcade_version, "user": settings.arcade_user,
                              "transport": transport_label(settings), "password_source": SOURCE_LABELS.get(source, source),
                              "notes": []}
    if place.get("engine") == "native":
        from .engine import runtime_dir
        result["installed"] = (runtime_dir(settings) / "lib").is_dir()  # a stopped engine is not a missing one (#287)
    if getattr(settings, "arcade_password_file", ""):
        result["password_file"] = settings.arcade_password_file
    if getattr(settings, "arcade_ca_file", ""):
        result["ca_file"] = settings.arcade_ca_file
    if connect:
        result["supported_versions"] = list(supported_versions(settings))
        if origin.get("database") == "derived":
            result["notes"].append("The database name is derived from this data directory's path. Another "
                                   "installation that should use this database must set arcade_database "
                                   f"(CAIRNMIND_ARCADE_DATABASE) to {settings.arcade_database}.")
    if source == "config":
        result["notes"].append("The database password is stored in config.json. That is allowed for the engine on "
                               "this machine only; for a database on another host, " + PASSWORD_HELP + ".")
    try:
        client = ArcadeClient(settings)
    except ValueError as exc:
        result["error"] = str(exc)
        if connect:
            # Nothing was sent, so whether the server answers is not known: None, as in `engine status` (#242).
            result["available"] = None
        return result
    try:
        info = client._request("GET", "/api/v1/server?mode=basic", timeout=timeout).json()
        result.update({"available": True, "version": info.get("version"), "server_name": info.get("serverName"),
                       "version_matches": str(info.get("version", "")).startswith(settings.arcade_version)})
        if connect:
            refused = version_problem(settings, info)
            result["version_matches"] = refused is None
            if refused:
                # Refused, not only reported: no command of this installation uses this server.
                result["error"] = refused
                return result
        try:
            client.query("SELECT 1 AS ok")
            result["database_available"] = True
        except ArcadeError as exc:
            result["database_available"] = False
            result["database_status"] = exc.status_code
            if connect:
                try:
                    if not client.database_exists():
                        result["error"] = missing_database(settings)
                except ArcadeError:
                    pass
    except (ArcadeError, ValueError, AttributeError) as exc:
        result["error"] = str(exc) if isinstance(exc, ArcadeError) else type(exc).__name__
        if isinstance(exc, ArcadeError) and exc.status_code == 401:
            result["sign_in_refused"] = True  # a server answers and does not accept this sign-in (#313)
    finally:
        client.close()
    return result


def admin_password(path: str) -> str:
    return read_private_file(path, "--admin-password-file")


def plan_user(settings, admin_secret: str) -> dict[str, Any]:
    """What ``create_user`` would meet, found without creating anything (the dry run of ``setup remote-db``).

    Signs in as root once (a refusal is ArcadeError with its status) to read the version and whether the
    database exists, then once as the user. ArcadeDB cannot list users: it answers 401 both for a user that does
    not exist and for a wrong password, and 403 for a user it accepts that has no rights on the database (measured
    on 26.11.1). ``sign_in`` is None when the user signs in, else the status of the refusal.
    """
    from dataclasses import replace
    from .arcade import ArcadeClient, ArcadeError
    from .database_mode import server_version, version_problem
    _new_user(settings)  # what --yes refuses before it signs in, the dry run refuses too
    admin = ArcadeClient(replace(settings, arcade_user="root", arcade_password=admin_secret), administrator=True)
    try:
        info = admin.server_info()
        refused = version_problem(settings, info)
        if refused:
            raise ArcadeError(refused)
        exists = admin.database_exists()
    except ArcadeError as exc:
        raise ArcadeError(str(exc).replace(admin_secret, "[redacted]"), status_code=exc.status_code) from None
    finally:
        admin.close()
    user = ArcadeClient(settings)
    try:
        user.query("SELECT 1 AS ok")
        sign_in = None
    except ArcadeError as exc:
        # 404 with no database is the answer of a user that has rights on a database that is not there yet
        if exc.status_code not in (401, 403) and not (exc.status_code == 404 and not exists):
            raise  # no answer about the user (a timeout, invalid JSON): not "the user signs in"
        sign_in = exc.status_code
    finally:
        user.close()
    return {"version": server_version(info), "database_exists": exists, "sign_in": sign_in}


def _new_user(settings) -> tuple[str, str]:
    """The name and password ``create_user`` would use, or ValueError: what it refuses before it signs in as root."""
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
    return name, secret


def create_user(settings, admin_secret: str, *, group: str = "admin") -> dict[str, Any]:
    """Create the database and a user with rights on it only, signing in once as root (cairnmind db create-user).

    The user is ``arcade_user`` and the password the one resolved for it (environment or file); both
    must already be configured, so that the password never passes through this command's arguments.
    """
    from contextlib import suppress
    from dataclasses import replace
    from .arcade import ArcadeClient, ArcadeError
    import json
    name, secret = _new_user(settings)
    admin = ArcadeClient(replace(settings, arcade_user="root", arcade_password=admin_secret), administrator=True)
    payload = {"name": name, "password": secret, "databases": {settings.arcade_database: [group]}}
    created = database_created = False
    try:
        try:
            # The version first (#68): root creates nothing on a server this release does not support.
            from .database_mode import version_problem
            refused = version_problem(settings, admin.server_info())
            if refused:
                raise ArcadeError(refused)
            # The user before the database (#242): ArcadeDB accepts a user for a database that does not exist
            # yet, and answers 409 for a user that exists. That user is checked below.
            try:
                admin.server_command("create user " + json.dumps(payload))
                created = True
            except ArcadeError as exc:
                if exc.status_code != 409:
                    raise
            if created:
                try:
                    database_created = not admin.database_exists()
                    admin.ensure_database()
                except ArcadeError:
                    with suppress(ArcadeError):  # the user made a moment ago, for a database that is not there
                        admin.server_command(f"drop user {name}")
                    raise
            elif not admin.database_exists():
                # A user left without its database: a rollback above that failed too, or a database deleted on
                # the server. The database is made, and dropped again below unless this user can use it.
                admin.ensure_database()
                database_created = True
        except ArcadeError as exc:
            message = str(exc).replace(secret, "[redacted]").replace(admin_secret, "[redacted]")
            raise ArcadeError(f"The user {name} was not created: {message}", status_code=exc.status_code) from None
        check = ArcadeClient(settings)
        try:
            check.query("SELECT 1 AS ok")
        except ArcadeError as exc:
            if created:
                raise
            # An existing user is used as it is only when it already signs in to this database: a run again
            # for the same pair. Rights on a second database are never added to a user of another one.
            if database_created:
                with suppress(ArcadeError):  # empty, made a moment ago by this run
                    admin.server_command(f"drop database {settings.arcade_database}")
            raise ArcadeError(
                f"The user {name} already exists, and it cannot use the database {settings.arcade_database} with "
                f"the password this installation reads. Each database gets its own user: set arcade_user to a new "
                f"name and its password ({PASSWORD_HELP}), then run `cairnmind db create-user` again. If this "
                "user was created for this database with another password, set that password instead. Nothing "
                "was created.", status_code=exc.status_code) from None
        finally:
            check.close()
    finally:
        admin.close()
    return {"user": name, "database": settings.arcade_database, "group": group, "created": created,
            "database_created": database_created, "signed_in": True}

