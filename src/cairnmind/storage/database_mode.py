"""Whether this installation manages its own ArcadeDB engine or only connects to one (#68).

Managed: the address is plain http on this machine and nothing says otherwise. Cairnmind installs the
pinned engine into the data directory, starts it when a command needs it and stops it on request.

Connect-only: the database runs somewhere this installation does not control, a container, a server
or a cluster. The installation never installs, starts or stops an engine and never creates a
database; it signs in, checks the server's version and uses the database it was given. That mode is
in force when

- ``arcade_connect_url`` (the variable CAIRNMIND_ARCADE_CONNECT_URL) names the address, on any host,
  this machine included (a container on 127.0.0.1); or
- ``arcade_url`` names an address the managed engine could never serve: another host, or https.

``arcade_connect_url`` replaces ``arcade_url`` as the address (Settings.load), so every rule of #72
applies to it unchanged: https with verification, a dedicated user and a password from outside
config.json for a host other than this machine. A loopback ``arcade_url`` without
``arcade_connect_url`` stays managed, as in every earlier release.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
CONNECT_SETTING = "arcade_connect_url"
DEPLOYMENT_GUIDE = "docs/DEPLOYMENT.md"


def is_managed_url(url: str) -> bool:
    """Whether ``url`` is one the managed engine can serve: plain http on this machine (#72)."""
    try:
        parsed = urlparse(url)
        return parsed.scheme == "http" and parsed.hostname in LOOPBACK
    except (ValueError, TypeError, AttributeError):
        return False


def connect_only(settings) -> bool:
    return bool(getattr(settings, CONNECT_SETTING, "")) or not is_managed_url(settings.arcade_url)


def mode(settings) -> str:
    return "connect" if connect_only(settings) else "managed"


def host(settings) -> str:
    try:
        return urlparse(settings.arcade_url).hostname or "unknown"
    except (ValueError, TypeError, AttributeError):
        return "unknown"


def on_this_machine(settings) -> bool:
    return host(settings) in LOOPBACK


def address(settings) -> str:
    """The database address as a message or a report may show it: never with a user, a password, a query
    or a fragment. ArcadeClient refuses such an address and sends nothing, but what a person typed there
    by mistake must not come back in `doctor`, `engine status` or a refusal that gets pasted elsewhere."""
    return shown(settings.arcade_url)


def shown(url: Any) -> str:
    """Any address (the database's, a model server's) as a message may show it: see address."""
    if isinstance(url, str) and not any(mark in url for mark in "@?#"):
        return url
    parsed_path = ""
    try:
        parsed = urlparse(url)
        name, port, scheme = parsed.hostname, parsed.port, parsed.scheme
        parsed_path = parsed.path.rstrip("/")
    except (ValueError, TypeError, AttributeError):
        name = port = scheme = None
    if not name or scheme not in ("http", "https"):
        return "an address that is not valid"
    name = f"[{name}]" if ":" in name else name
    return f"{scheme}://{name}" + (f":{port}" if port else "") + parsed_path


def savable(url) -> bool:
    """Whether ``url`` may be written to config.json as the connect address: an address and nothing else."""
    try:
        parsed = urlparse(url)
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and not any(mark in url for mark in "@?#"))
    except (ValueError, TypeError, AttributeError):
        return False


def saved_note(settings) -> str:
    """Said once, when the address from the variable is written to config.json (Settings.load)."""
    return (f"Saved {CONNECT_SETTING} = {address(settings)} in {settings.data_dir / 'config.json'}: this data "
            "directory is connect-only from now on, also for a command started without the variable. To go back "
            f"to the engine Cairnmind manages, remove {CONNECT_SETTING} from that file and unset the variable.")


def why(settings) -> str:
    """One sentence: what puts this installation in connect-only mode."""
    if getattr(settings, CONNECT_SETTING, ""):
        return f"{CONNECT_SETTING} names that address"
    return "that address is not on this machine" if not on_this_machine(settings) else \
        "that address is https, which the engine Cairnmind manages never serves"


def refusal(settings, action: str) -> str:
    """Why an engine command does nothing here, for ``action`` such as "start an engine"."""
    return (f"This installation only connects to ArcadeDB at {address(settings)} ({why(settings)}); it does not "
            f"{action}. The server is installed, started and stopped where it runs (see {DEPLOYMENT_GUIDE}). "
            "`cairnmind engine status` shows whether it answers and which version it runs.")


def supported_versions(settings) -> tuple[str, ...]:
    """The ArcadeDB releases this release of Cairnmind works with: the one it pins."""
    return (settings.arcade_version,)


def server_version(info) -> str:
    return str((info or {}).get("version", "")).split(" ", 1)[0]


def version_problem(settings, info) -> str | None:
    """Why the server that answered with ``info`` must not be used, or None."""
    actual, supported = server_version(info), supported_versions(settings)
    if actual in supported:
        return None
    if not connect_only(settings):
        return f"ArcadeDB release does not match the pinned {settings.arcade_version}"
    return (f"ArcadeDB at {address(settings)} runs version {actual or 'unknown'}, which this release of Cairnmind "
            f"does not support. Supported: {', '.join(supported)}. Run the server from the image of this release "
            f"({DEPLOYMENT_GUIDE}, \"When the pinned ArcadeDB version changes\"), or use the Cairnmind release "
            "that matches the server. Nothing was read or written.")


def unavailable(settings, detail: str | None = None) -> str:
    """The server did not answer, in connect-only mode: say where it was looked for and that nothing starts here."""
    reason = f" ({detail})" if detail else ""
    return (f"ArcadeDB at {address(settings)} is unavailable{reason}. This installation only connects to it "
            f"({why(settings)}) and starts no engine of its own: start the server where it runs, or check the "
            "address, the network and a port-forward or tunnel in between.")


def missing_database(settings) -> str:
    """The database does not exist, in connect-only mode, where none is ever created by an ordinary command."""
    derived = "" if (getattr(settings, "_origin", None) or {}).get("database") != "derived" else (
        " That name was derived from this data directory's path: to use a database another installation "
        "created, set arcade_database (CAIRNMIND_ARCADE_DATABASE) to its name.")
    # ArcadeDB answers {"result": false} to a user without rights on a database that exists, as to one that
    # does not exist (measured on 26.11.1, #242): both causes are named, or create-user is suggested in a loop.
    user = settings.arcade_user
    rights = "" if user == "root" else (
        f" If it exists, the user {user} has no rights on it: each database gets its own user, so set arcade_user "
        "to the user created with it, or to a new name and its password and run create-user for that one.")
    whose = "" if user == "root" else f", or the user {user} has no rights on it"
    return (f"The database {settings.arcade_database} does not exist on ArcadeDB at {address(settings)}{whose}, and "
            f"a connect-only installation creates none.{derived} Create it once, with its user: `cairnmind db "
            f"create-user --admin-password-file FILE`.{rights}")


def sign_in_refused(settings) -> str:
    """What to add when the server does not accept the user of a connect-only installation."""
    return (f". ArcadeDB at {address(settings)} did not accept the user {settings.arcade_user}. If the database "
            "and this user were not created yet, create them once: `cairnmind db create-user "
            "--admin-password-file FILE`. Otherwise check arcade_user and the password this installation reads. "
            # The same answer, measured on 26.11.1 (#242): accepted again 31 s after seven wrong passwords.
            "ArcadeDB also refuses a user for about 30 seconds after several failed sign-ins, even with the right "
            "password: if this worked before, wait and try again.")


ENGINE_MODES = ("native", "docker")
MODE_INVALID = "engine_mode in config.json is {value}; it must be native or docker."


def engine_mode(settings) -> str:
    """'native' or 'docker' for a managed installation (#272). Empty, as every config.json before 0.13.0 has it,
    is native: those installations run the native engine. Any other value is refused, never guessed."""
    value = getattr(settings, "engine_mode", "")
    if value == "":
        return "native"
    if value in ENGINE_MODES:
        return value
    raise ValueError(MODE_INVALID.format(value=repr(value)[:80]))


def describe(settings) -> dict:
    """What `engine status`, `status` and `doctor` say about where storage runs."""
    result = {"mode": mode(settings), "host": host(settings)}
    if result["mode"] == "connect":
        result["reason"] = why(settings)
        result["engine"] = "not managed by this installation: never installed, started or stopped here"
    else:
        try:
            result["engine"] = engine_mode(settings)
        except ValueError as exc:
            result["engine"], result["engine_problem"] = "unknown", str(exc)
    return result
