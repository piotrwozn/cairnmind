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
import time
from typing import Any

import httpx

from cairnmind import __version__
from cairnmind.config import SERVICE_NAMES, tls_verify

_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")

OTHER_VERSION = ("The knowledge service of this data directory runs Cairnmind {service}, and this is Cairnmind "
                 "{client}, so nothing is sent to it: the two versions may not agree on what the store allows. Stop it "
                 "with `{command}`; the next command that needs the service starts it in version {client}.")
# An answer that is this data directory's service, with a version that cannot be read: `stop` reaches it.
VERSION_UNKNOWN = ("The knowledge service at {url} did not say which version of Cairnmind it runs, so nothing is sent "
                   "to it. Try again; if this keeps happening, stop it with `{command}`.")
# No answer at all: nothing is known of the program, `stop` would say it is not running and `doctor` cannot name it.
NO_HEALTH = ("The program at {url} did not answer within 5 seconds, so nothing is sent to it. Try again; if this keeps "
             "happening, the program on that port hangs or is not the knowledge service of this data directory: end "
             "it, or give this data directory its own port: `port` in {data_dir}/config.json, or CAIRNMIND_PORT for "
             "one command.")

MESSAGES = {
    OTHER_VERSION: ("Usługa wiedzy tego katalogu danych działa w wersji Cairnmind {service}, a to jest Cairnmind "
                    "{client}, więc nic nie jest do niej wysyłane: obie wersje mogą się nie zgadzać co do tego, na co "
                    "pozwala baza. Zatrzymaj ją poleceniem `{command}`; następne polecenie, "
                    "które potrzebuje usługi, uruchomi ją w wersji {client}."),
    NO_HEALTH: ("Program pod adresem {url} nie odpowiedział w ciągu 5 sekund, więc nic nie jest do niego wysyłane. "
                "Spróbuj ponownie; jeśli to się powtarza, program na tym porcie zawiesił się albo nie jest usługą "
                "wiedzy tego katalogu danych: zakończ go albo daj temu katalogowi danych własny port: `port` w "
                "{data_dir}/config.json albo CAIRNMIND_PORT dla jednego polecenia."),
    VERSION_UNKNOWN: ("Usługa wiedzy pod adresem {url} nie podała, w której wersji Cairnmind działa, więc nic nie jest "
                      "do niej wysyłane. Spróbuj ponownie; jeśli to się powtarza, zatrzymaj ją poleceniem `{command}`."),
}


def instance_id(settings) -> str:
    """The data directory a service answers for on /health (service.py re-exports it)."""
    return hashlib.sha256(str(settings.data_dir).encode()).hexdigest()[:16]


class Sentence(str):
    """A formatted sentence that remembers its template and values: the workspace translates the template first and
    puts the values in after (terminal_i18n_messages.broker_error_text), as it does for FOREIGN_SERVICE."""

    template: str
    values: dict[str, Any]

    def __new__(cls, template: str, **values: Any):
        text = super().__new__(cls, template.format(**values))
        text.template, text.values = template, values
        return text


def _command(settings, verb: str) -> str:
    from cairnmind.cli_cmds.io import (
        _data_dir_arguments,  # only when refusing: an MCP server does not import the command line otherwise
    )
    from cairnmind.pal.process_control import format_command
    return format_command(["cairnmind", *_data_dir_arguments(settings.data_dir), verb])


_SEEN: dict = {}  # (url, data directory) -> (when, /health answer) of the answer cli.service_alive just took


def remember(settings, health: dict) -> None:
    _SEEN[settings.url, str(settings.data_dir)] = (time.monotonic(), health)


def refusal(settings) -> Sentence | None:
    """Why nothing may be sent to this data directory's knowledge service, None when it may (or nothing answers).
    The answer cli.service_alive took a moment ago (ensure_service asks both) is used once instead of a second
    request; any other call asks again, so the bridge's check before every request stays a live one (#405)."""
    seen = _SEEN.pop((settings.url, str(settings.data_dir)), None)
    if seen and time.monotonic() - seen[0] < 1:
        health = seen[1]
    else:
        try:
            with httpx.Client(trust_env=False, timeout=5, verify=tls_verify(settings.url)) as client:
                response = client.request("GET", settings.url + "/health")
        except httpx.ConnectError:
            return None  # nothing listens: the request that follows fails, or starts the service, on its own
        except httpx.HTTPError:
            # Nothing is known of the program: `stop` would call it "not running" (it waits 0.7 s), so it is not advised.
            return Sentence(NO_HEALTH, url=settings.url, data_dir=settings.data_dir)
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
        return Sentence(VERSION_UNKNOWN, url=settings.url, command=_command(settings, "stop"))
    return Sentence(OTHER_VERSION, service=version, client=__version__, command=_command(settings, "stop"))
