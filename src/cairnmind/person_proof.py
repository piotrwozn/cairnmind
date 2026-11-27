"""A person's act needs the key the open workspace keeps in its tmux server (#214).

The workspace puts a random key into its own tmux server when it creates it (a user option, written through
standard input: never a file, a command line, an environment variable or a pane). The inspector and the
finding form read it from their server when they send a person's request, the broker passes it on, and the
service reads the same server before it lets that request count as a person's. The service token alone
reaches no person's route. `cairnmind restore` asks the same server (restore_proof) to decide whether a backup's
confirmations stay a person's.

This is a bar against every documented door (a route, a command, a tool), not against a program of the same
user that uses the workspace's tmux server: docs/MEMORY_CONTRACTS.md says what remains.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys

from .runtime_selection import resolve_tmux
from .runtime_sockets import MAX_SOCKET_PATH, socket_base, socket_directory, workspace_namespace

OPTION = "@cairnmind_person_key"
KEY_HEADER, SOCKET_HEADER = "X-Cairnmind-Person", "X-Cairnmind-Workspace"
# The paths a panel sends to the broker for a person's act (BrokerClient adds the proof to these POSTs only).
PERSON_BROKER_PATH = re.compile(r"/api/(inspector/(records/[^/]+/(review|release|replace)|conflicts/resolve"
                                r"|plans/[^/]+/confirm|impact/verdict|record-kinds/(accept|withdraw)"
                                r"|legacy-groups/adopt|projects/forget)"
                                r"|findings|memory/import(-published|-commit)?)")
_KEY = re.compile(r"[0-9a-f]{64}")
_HEADER_VALUE = re.compile(r"[\x20-\x7e]+")
# Set by `require` only, in the task of the request that passed the check.
_PROVEN: ContextVar[bool] = ContextVar("cairnmind_person_proven", default=False)

PROOF_MISSING = (
    "A person's act is made in the workspace, and this request did not come from it. Nothing was saved. The person "
    "reviews and decides in the inspector of the workspace (open it with `cairnmind`); a session, a script or a "
    "command stores what it found as proposed.")
PROOF_REFUSED = (
    "This request did not come from the open workspace of this installation, or that workspace was closed. Nothing "
    "was saved. Close the view and open the workspace again with `cairnmind`.")
NO_WORKSPACE_KEY = (
    "The open workspace was started by an earlier version and holds no key for a person's acts. Nothing was saved. "
    "Stop it with `cairnmind workspace stop` and open it again with `cairnmind`.")
TMUX_MISSING = (
    "The service cannot run tmux, so it cannot check that this request comes from the open workspace. Nothing was "
    "saved. Set tmux_executable in the configuration, or start the service again from a terminal where tmux runs "
    "(`cairnmind stop`, then `cairnmind`).")
IMPORT_REPLACE = (
    "Replacing what a person confirmed or disputed on this machine needs the person's workspace, and this import did "
    "not come from it. Nothing was imported. Import it in the memory view of the workspace (`cairnmind`): "
    "Import for an archive, Team memory for a published folder or a commit.")
FORGET_REFUSED = (
    "Deleting a project's memory is a person's act made in the workspace, and this request did not come from it. "
    "Nothing was deleted. The person deletes it in the workspace (`cairnmind`): More, then Delete project memory.")


class PersonProofRefused(Exception):
    """The request carries no valid proof of the open workspace; `message` is the sentence to show."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def new_key() -> str:
    return secrets.token_hex(32)


def set_key_input(key: str) -> str:
    """What the workspace sends to `tmux source-file -` on standard input; the key is in no command line."""
    if not _KEY.fullmatch(key):
        raise ValueError("The key must be 64 lowercase hexadecimal characters.")
    return f"set-option -g {OPTION} {key}\n"


def workspace_socket(settings, value) -> Path | None:
    """The socket of this installation's workspace, or None. All checks must hold: the shape of the path, a
    private directory of the user (not a link), and a socket of the user."""
    if sys.platform == "win32" or not hasattr(os, "getuid"):
        return None
    if (not isinstance(value, str) or not value or "\0" in value or not os.path.isabs(value)
            or len(os.fsencode(value)) > MAX_SOCKET_PATH):
        return None
    socket = Path(value)
    expected = socket_directory("ui", workspace_namespace(settings.data_dir)).name
    if socket.name != "tmux.sock" or socket.parent.name != expected:
        return None
    if sys.platform == "darwin" and socket.parent.parent != socket_base():
        return None
    try:
        directory, node = os.lstat(socket.parent), os.lstat(socket)
    except OSError:
        return None
    uid = os.getuid()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != uid or stat.S_IMODE(directory.st_mode) != 0o700
            or not stat.S_ISSOCK(node.st_mode) or node.st_uid != uid):
        return None
    return socket


def _tmux(settings) -> str | None:
    try:
        return resolve_tmux(settings)
    except ValueError:
        return None


def read_key(settings, socket, binary=None) -> tuple[str, str | None]:
    """Ask the tmux server on `socket` for the key: ("key", key), ("no_key", None) or ("unavailable", None).

    Only a server that answers decides: no server, no tmux, a timeout or an answer of another shape is
    `unavailable`, and nothing starts a server. `binary` is the tmux that started it, when the caller knows.
    """
    binary = binary or _tmux(settings)
    if not binary:
        return "unavailable", None
    environment = {name: value for name, value in os.environ.items() if name not in ("TMUX", "TMUX_PANE")}
    try:
        answer = subprocess.run([binary, "-S", str(socket), "show-options", "-gqv", OPTION], capture_output=True,
                                text=True, timeout=3, env=environment)
    except (OSError, subprocess.SubprocessError):
        return "unavailable", None
    if answer.returncode != 0:
        return "unavailable", None
    found = answer.stdout.strip()
    if not found:
        return "no_key", None
    return ("key", found) if _KEY.fullmatch(found) else ("unavailable", None)


def restore_proof(settings) -> str:
    """What `cairnmind restore` finds of this installation's open workspace: "key", "no_key" or "unavailable" (#214).

    The socket is the one the workspace makes for this data directory, checked as the socket of a person's
    request is (workspace_socket), and its tmux server is asked as the service asks it (read_key). Only "key"
    lets a restore keep a person's confirmations; nothing a caller passes (an option, a file) decides it. The
    environment does: on Linux and WSL TMPDIR decides where the socket directory is, and any tmux server of the
    user's own at that place that holds a well-formed key answers "key" (docs/MEMORY_CONTRACTS.md).
    """
    value = str(socket_directory("ui", workspace_namespace(settings.data_dir)) / "tmux.sock")
    socket = workspace_socket(settings, value)
    return "unavailable" if socket is None else read_key(settings, socket)[0]


def workspace_headers(settings, environ=None) -> dict:
    """The proof a panel of the open workspace sends with a person's request, read for this request only.

    Nothing outside this installation's workspace (a coding session's shell has the native server's socket or
    none) sends anything; a workspace without a key sends only its socket, so that the service can say so.
    """
    environ = os.environ if environ is None else environ
    socket = workspace_socket(settings, str(environ.get("TMUX", "")).split(",")[0])
    if socket is None:
        return {}
    kind, key = read_key(settings, socket)
    if kind == "key":
        return {KEY_HEADER: key, SOCKET_HEADER: str(socket)}
    return {SOCKET_HEADER: str(socket)} if kind == "no_key" else {}


def forwardable(headers) -> dict:
    """What the broker passes on: the two headers when they are well formed. It checks nothing else."""
    found = {}
    for name, limit in ((KEY_HEADER, 200), (SOCKET_HEADER, 4096)):
        value = headers.get(name)
        if isinstance(value, str) and len(value) <= limit and _HEADER_VALUE.fullmatch(value):
            found[name] = value
    return found


def check(settings, headers, refusal=PROOF_MISSING) -> None:
    """Raise PersonProofRefused unless the headers prove the open workspace of this installation."""
    value, given = headers.get(SOCKET_HEADER), headers.get(KEY_HEADER)
    if value is None and given is None:
        raise PersonProofRefused(refusal)
    socket = workspace_socket(settings, value)
    if socket is None:
        raise PersonProofRefused(PROOF_REFUSED)
    if not _tmux(settings):
        raise PersonProofRefused(TMUX_MISSING)
    kind, key = read_key(settings, socket)
    if kind == "no_key":
        raise PersonProofRefused(NO_WORKSPACE_KEY)
    if kind != "key" or not isinstance(given, str) or not hmac.compare_digest(key.encode(), given.encode()):
        raise PersonProofRefused(PROOF_REFUSED)


async def require(request, settings, refusal=PROOF_MISSING) -> None:
    """The check of a person's route: 403 with the sentence when it fails, otherwise the request counts as a
    person's for the store (the task of this request, and the worker threads it starts)."""
    from fastapi import HTTPException
    try:
        await asyncio.to_thread(check, settings, request.headers, refusal)
    except PersonProofRefused as exc:
        raise HTTPException(status_code=403, detail=exc.message) from None
    _PROVEN.set(True)


def present() -> bool:
    """Whether the request being served passed `require`."""
    return _PROVEN.get()
