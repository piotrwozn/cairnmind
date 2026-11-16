"""What the installer prints of `cairnmind doctor`: only the parts that need attention (#244).

Run as ``cairnmind doctor | python -I -m cairnmind.doctor_attention``. It reads the doctor report as JSON on
standard input and prints one line for each thing to look at, or one line saying nothing needs attention.
The full report stays one command away.
"""
from __future__ import annotations

import json
import sys

from .terminal_safe import safe_text

FULL_REPORT = "The full report: cairnmind doctor"
# Keys every doctor report has; an object without all of them (`cairnmind init` prints one with data_dir alone)
# is not a report that found nothing.
REPORT_KEYS = frozenset({"environment_notes", "data_dir", "store_writer"})


def _line(text: object) -> str:
    return safe_text(" ".join(str(text).split()), limit=300)


def _address(url: object) -> str:
    return str(url or "").strip().rstrip("/").lower()


def attention(report: dict) -> list[str]:
    """One plain line per part of the report that is missing, unusable or noted."""
    lines = []
    notes = report.get("environment_notes") or []
    if not isinstance(notes, list):  # a string would be read one character at a time
        raise ValueError("environment_notes is not a list")
    for note in notes:
        lines.append(_line(note))
    java = report.get("java") or {}
    if java and java.get("needed") is not False:
        if java.get("compatible") is False or not (java.get("executable") or java.get("on_path")):
            found = java.get("error") or java.get("version") or "not found"
            lines.append(_line(f"Java 21 or newer is needed for the engine: {found}"))
        elif java.get("error"):
            # A -version that timed out or failed: Java is there, it did not answer, as after a first install.
            lines.append(_line(f"Java did not answer: {java['error']}"))
    tmux = report.get("tmux") or {}
    if tmux and tmux.get("code") != "ready":
        lines.append(_line(f"tmux is not ready ({tmux.get('code') or 'unknown'}); the workspace needs tmux "
                           f"{tmux.get('minimum_version') or ''}".rstrip()))
    arcade = report.get("arcadedb") or {}
    if arcade.get("available") is False:
        lines.append(_line(f"The database engine does not answer at {arcade.get('url') or 'its address'}: "
                           f"{arcade.get('error') or arcade.get('reason') or 'start it with cairnmind engine start'}"))
    elif arcade.get("error"):
        lines.append(_line(f"The database server cannot be used: {arcade['error']}"))
    elif arcade.get("database_available") is False:
        lines.append(_line(f"The database {arcade.get('database') or ''} cannot be read; see cairnmind doctor."))
    ollama = report.get("ollama") or {}
    ollama_down = ollama.get("available") is False
    if ollama_down:
        lines.append("Ollama does not answer; local models need `ollama serve`.")
    else:
        for role in ("chat", "embedding"):
            if ollama.get(f"{role}_model_installed") is False:
                lines.append(_line(f"The {role} model is not downloaded; run cairnmind models pull."))
    for role, model in sorted((report.get("memory_models") or {}).items()):
        if not isinstance(model, dict) or model.get("reachable") is not False:
            continue
        if ollama_down and ollama.get("url") and _address(model.get("base_url")) == _address(ollama["url"]):
            continue  # the same server as the Ollama line above: one cause, one line
        lines.append(_line(f"The {role} model server at {model.get('base_url') or 'its address'} does not answer."))
    for name, client in sorted((report.get("clients") or {}).items()):
        if isinstance(client, dict) and client.get("installed") and not client.get("verified_resume"):
            lines.append(_line(f"{name} is installed but its contract checks fail; see cairnmind doctor --contract."))
    writer = report.get("store_writer") or {}
    if writer.get("role") in {"reader", "lost", "unknown"} or writer.get("problem"):
        lines.append(_line(f"Store writer: {writer.get('role')}. {writer.get('problem') or writer.get('reason') or ''}"))
    return lines


def main() -> int:
    try:
        report = json.loads(sys.stdin.read())
        if not isinstance(report, dict) or not REPORT_KEYS <= report.keys():
            # Any other object, even {}, is not a report that found nothing (#244).
            raise ValueError("not a doctor report")
        lines = attention(report)
    except (ValueError, AttributeError, TypeError):  # a report of another shape: a sentence, never a traceback
        print(f"The doctor report could not be read. {FULL_REPORT}")
        return 0
    if lines:
        print("Needs attention:")
        for line in lines:
            print(f"  - {line}")
    else:
        print("Nothing needs attention.")
    print(FULL_REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
