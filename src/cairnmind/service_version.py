"""A knowledge service that runs another version of Cairnmind than the client that reaches it (#319).

/health reports the service's version. The command line (cli.ensure_service and the diagnostics in cli.request),
the workspace (through ensure_service) and an MCP server (before every tool call) ask it before they send anything.
A service of this data directory that reports another version, or none that can be read, gets nothing, read or
write: it holds the rules of its own version (the ACK gate, citation checks, a person's key), not this one's.
`cairnmind stop` and `cairnmind doctor` still reach it. Version strings are compared, so a reinstall of the same
version, or a development checkout with the installed version's number, is not told apart.
The English sentences are the source; MESSAGES is their Polish for the terminal catalog.
"""
from __future__ import annotations

import hashlib
import re
import shlex

import httpx

from . import __version__
from .config import SERVICE_NAMES

_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")

OTHER_VERSION = ("The knowledge service of this data directory runs Cairnmind {service}, and this is Cairnmind "
                 "{client}, so nothing is sent to it: the two versions may not agree on what the store allows. Stop it "
                 "with `{command}`; the next command or tool call that needs the service starts it in version {client}.")
VERSION_UNKNOWN = ("The knowledge service at {url} did not say which version of Cairnmind it runs, so nothing is sent "
                   "to it. Try again; if this keeps happening, stop it with `{command}`.")

MESSAGES = {
    OTHER_VERSION: ("Usługa wiedzy tego katalogu danych działa w wersji Cairnmind {service}, a to jest Cairnmind "
                    "{client}, więc nic nie jest do niej wysyłane: obie wersje mogą się nie zgadzać co do tego, na co "
                    "pozwala baza. Zatrzymaj ją poleceniem `{command}`; następne polecenie albo wywołanie narzędzia, "
                    "które potrzebuje usługi, uruchomi ją w wersji {client}."),
    VERSION_UNKNOWN: ("Usługa wiedzy pod adresem {url} nie podała, w której wersji Cairnmind działa, więc nic nie jest "
                      "do niej wysyłane. Spróbuj ponownie; jeśli to się powtarza, zatrzymaj ją poleceniem `{command}`."),
}


def instance_id(settings) -> str:
    """The data directory a service answers for on /health (service.py re-exports it)."""
    return hashlib.sha256(str(settings.data_dir).encode()).hexdigest()[:16]


def _stop_command(settings) -> str:
    from .cli import _data_dir_arguments  # only when refusing: an MCP server does not import the command line otherwise
    return shlex.join(["cairnmind", *_data_dir_arguments(settings.data_dir), "stop"])


def refusal(settings) -> str | None:
    """Why nothing may be sent to this data directory's knowledge service, None when it may (or nothing answers)."""
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.request("GET", settings.url + "/health")
    except httpx.ConnectError:
        return None  # nothing listens: the request that follows fails, or starts the service, on its own
    except httpx.HTTPError:
        return VERSION_UNKNOWN.format(url=settings.url, command=_stop_command(settings))
    try:
        health = response.json()
    except ValueError:
        return None  # not a Cairnmind service: what follows is refused for that reason
    if not (isinstance(health, dict) and health.get("service") in SERVICE_NAMES
            and health.get("instance") == instance_id(settings)):
        return None  # another program, or another data directory's service, whose refusal of the token says so
    version = health.get("version")
    if version == __version__:
        return None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):  # printed: never raw text from a socket
        return VERSION_UNKNOWN.format(url=settings.url, command=_stop_command(settings))
    return OTHER_VERSION.format(service=version, client=__version__, command=_stop_command(settings))
