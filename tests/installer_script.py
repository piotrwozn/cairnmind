"""The installer's functions for a test, cut out of scripts/install-wsl.sh, with a socket folder of the test's own (#364).

The script looks for the terminal servers of an installed version in ${TMPDIR:-/tmp} and, for macOS, in the fixed
folder /private/tmp/cairnmind-<uid>. The script has no setting for the second one, and must not get one: a person's
environment must never hide a running workspace from the installer. So a test gets the text with that folder
moved under a directory of its own, and a test that took the text any other way would read the machine's folder.
"""
from __future__ import annotations

import os
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh"
MACOS_SOCKET_ROOT = "/private/tmp"


def installer_functions(socket_root: Path) -> str:
    """The functions between the "left running" and "end" markers; the macOS socket root is `socket_root`.

    Keep the path short when the test binds sockets there: a socket path holds about a hundred bytes.
    """
    if os.path.realpath(socket_root) in ("/tmp", "/private/tmp"):
        raise AssertionError(f"{socket_root} is the machine's own socket folder: give the test a folder of its own.")
    script = SCRIPT.read_text()
    start = script.index("# --- what an installed version left running")
    text = script[start:script.index("# --- end ---", start)]
    if MACOS_SOCKET_ROOT not in text:  # the rule moved: this redirect would silently do nothing
        raise AssertionError(f"The installer no longer names {MACOS_SOCKET_ROOT}; redirect its new socket folder here.")
    text = text.replace("${TMPDIR:-/tmp}", "${TMPDIR:?a test sets TMPDIR to a folder of its own}")  # no TMPDIR: not /tmp
    return text.replace(MACOS_SOCKET_ROOT, str(socket_root))
