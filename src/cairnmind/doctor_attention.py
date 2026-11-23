"""What `cairnmind doctor` says in a terminal, and what the installer prints of it (#244, #287).

Run as ``cairnmind doctor | python -I -m cairnmind.doctor_attention``. It reads the doctor report as JSON on
standard input and prints the summary: where memory is kept, what is required and present, what is required and
missing, what is optional, and the next step. The installer prints the same summary as `cairnmind doctor` in a
terminal; the full report is `cairnmind doctor --json`. The exit code of `cairnmind doctor` is 1 exactly when
required_missing is not empty (#313). An installation that only reads a store another one writes is not missing
anything (#373): the summary says so and who writes.
"""
from __future__ import annotations

import json
import sys

from . import platform_hints
from .engine_docker import DOCKER_ATTENTION
from .first_start import (
    CLIENT_NAMES, CLIENT_OFF, CLIENT_ON, DATA_DIR, ENGINE_DOWN, ENGINE_NOT_INSTALLED, ENGINE_ON_DEMAND, ENGINE_READY,
    ENGINE_RELEASE, FULL_REPORT, JAVA_NEEDED, KNOWLEDGE_DISABLED, MODELS_OFF, MODELS_ON, NEXT_FIX, NEXT_PROJECTS,
    NEXT_START, NOTES, NOTHING_MISSING, OPTIONAL_BROKEN, OPTIONAL_OFF, OPTIONAL_ON, READER_ROLE, REPORT_UNREADABLE, REQUIRED_MISSING,
    REQUIRED_PRESENT, SERVER_DOWN_CONNECT, SERVICE_ON_DEMAND, SERVICE_RUNNING, THIS_MAC, WORKSPACE_OFF,
    WORKSPACE_ON, cockpit_status)
from .terminal_safe import safe_text

# Keys every doctor report has; an object without all of them (`cairnmind init` prints one with data_dir alone)
# is not a report that found nothing.
REPORT_KEYS = frozenset({"environment_notes", "data_dir", "store_writer"})


def _line(text: object, limit: int = 300) -> str:
    return safe_text(" ".join(str(text).split()), limit=limit)


def _address(url: object) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _prerequisites(report: dict) -> list[str]:
    """Java, Docker or the engine's mode that stop an engine from starting; a line for each."""
    lines = []
    java = report.get("java") or {}
    if java and java.get("needed") is not False:
        if java.get("compatible") is False or not (java.get("executable") or java.get("on_path")):
            found = java.get("error") or java.get("version") or "not found"
            lines.append(_line(JAVA_NEEDED.format(found=found, hint=platform_hints.hint("java"))))
        elif java.get("error"):
            # A -version that timed out or failed: Java is there, it did not answer, as after a first install.
            lines.append(_line(f"Java did not answer: {java['error']}"))
    docker = report.get("docker") or {}
    if docker.get("available") is False:
        lines.append(_line(DOCKER_ATTENTION.format(error=docker.get("error") or "unknown")))
    elif (docker.get("container") or {}).get("state") in ("not ours", "unknown"):
        lines.append(_line(docker["container"].get("error") or "The engine container cannot be identified"))
    arcade = report.get("arcadedb") or {}
    if arcade.get("engine_problem"):
        lines.append(_line(arcade["engine_problem"]))
    return lines


def _on_demand(report: dict) -> bool:
    """A managed engine that is installed, can start and is simply not running: the next command that needs it
    starts it, as it does the service. Not a fault."""
    arcade = report.get("arcadedb") or {}
    return (arcade.get("mode") != "connect" and arcade.get("available") is False and arcade.get("installed") is not False
            and _cause(arcade.get("error")) == "ConnectError" and not _prerequisites(report))


def _cause(error: object) -> str:
    """The name in the parentheses of 'Local ArcadeDB is unavailable (ConnectError); ...'; the text when there is none."""
    text = str(error or "")
    return text.partition("(")[2].partition(")")[0] or text


def _copy_of_the_writer(writer: dict) -> bool:
    """A reader whose identity was made from the writer's own: its data directory was moved or copied (store_writer)."""
    return bool(writer.get("copied_from")) and writer.get("copied_from") == (writer.get("writer") or {}).get("installation")


def required_missing(report: dict) -> list[str]:
    """One plain line per required part of the report that is missing or unusable; cairnmind doctor exits 1 for these."""
    lines = _prerequisites(report)
    arcade = report.get("arcadedb") or {}
    if arcade.get("sign_in_refused"):  # another data directory's engine answers here: the text names the way out
        lines.append(_line(arcade.get("error") or "", limit=900))
    elif arcade.get("available") is False:
        if arcade.get("mode") == "connect":
            lines.append(_line(SERVER_DOWN_CONNECT.format(url=arcade.get("url") or "its address",
                                                          error=arcade.get("error") or arcade.get("reason") or "no answer")))
        elif arcade.get("installed") is False:
            lines.append(_line(ENGINE_NOT_INSTALLED))
        elif not lines and not _on_demand(report):  # Java, Docker or the mode above already names what stops it
            lines.append(_line(ENGINE_DOWN.format(url=arcade.get("url") or "its address",
                                                  error=_cause(arcade.get("error") or arcade.get("reason") or "no answer"))))
    elif arcade.get("error"):
        lines.append(_line(f"The database server cannot be used: {arcade['error']}"))
    elif arcade.get("version_matches") is False:
        lines.append(_line(ENGINE_RELEASE.format(url=arcade.get("url") or "its address",
                                                 found=arcade.get("version") or "unknown",
                                                 pinned=arcade.get("configured_version") or "release")))
    elif arcade.get("database_available") is False:
        lines.append(_line("The memory database cannot be read; see cairnmind doctor --json."))
    knowledge = report.get("knowledge_agent")
    if knowledge and knowledge != KNOWLEDGE_DISABLED:
        lines.append(_line(knowledge))  # a stored value that is neither enabled nor disabled
    writer = report.get("store_writer") or {}
    # "unknown" because the engine did not answer is the engine's state, already said above or present on demand.
    unknown_for_the_engine = writer.get("role") == "unknown" and arcade.get("available") is False
    # A reader is how a second installation is meant to be (#373); a lost role, an unknown one, a mixed store and a
    # copy of the writer's own data directory (it cannot write and nobody meant it) are not.
    if ((writer.get("role") in {"lost", "unknown"} or writer.get("problem") or _copy_of_the_writer(writer))
            and not unknown_for_the_engine):
        lines.append(_line(f"Store writer: {writer.get('role')}. {writer.get('problem') or writer.get('reason') or ''} "
                           f"{writer.get('next') or ''}"))
    # Commands here are refused; whole, so the command that ends a refusal is never cut off (#319).
    lines += [_line(note, limit=600) for note in report.get("environment_blockers") or []]
    return lines


def not_working(report: dict) -> list[str]:
    """Optional parts that are set up and do not work: configured models and their servers, an installed client whose
    contract fails. They never change the exit code (#313)."""
    lines = []
    knowledge = report.get("knowledge_agent")
    ollama = report.get("ollama") or {}
    ollama_down = ollama.get("available") is False and not ollama.get("refused")  # refused: nothing was asked
    if knowledge == KNOWLEDGE_DISABLED:
        pass  # no models are configured, which init said and how to get them: not a fault (#274)
    elif ollama_down:
        lines.append("Ollama does not answer; local models need `ollama serve`.")
    else:
        for role in ("chat", "embedding"):
            if ollama.get(f"{role}_model_installed") is False:
                lines.append(_line(f"The {role} model is not downloaded; run cairnmind models pull."))
    for role, model in sorted((report.get("memory_models") or {}).items()):
        if knowledge == KNOWLEDGE_DISABLED:
            break
        if not isinstance(model, dict) or model.get("reachable") is not False:
            continue
        if model.get("refused"):  # the address was refused, not asked: the refusal says why, once for both roles
            refusal = _line(model.get("error"), limit=600)
            if refusal not in lines:
                lines.append(refusal)
            continue
        if ollama_down and ollama.get("url") and _address(model.get("base_url")) == _address(ollama["url"]):
            continue  # the same server as the Ollama line above: one cause, one line
        lines.append(_line(f"The {role} model server at {model.get('base_url') or 'its address'} does not answer."))
    for name, client in sorted((report.get("clients") or {}).items()):
        if isinstance(client, dict) and client.get("installed") and not client.get("verified_resume"):
            lines.append(_line(f"{name} is installed but its contract checks fail; see cairnmind doctor --contract."))
    return lines


def attention(report: dict) -> list[str]:
    """Everything the report lists as missing or not working: required_missing, then not_working."""
    return required_missing(report) + not_working(report)


def _engine_present(report: dict) -> list[str]:
    arcade = report.get("arcadedb") or {}
    if _on_demand(report):
        return [ENGINE_ON_DEMAND.format(url=arcade.get("url") or "its address")]
    if not (arcade.get("available") and arcade.get("database_available")) or arcade.get("error") or arcade.get(
            "engine_problem") or arcade.get("version_matches") is False:
        return []
    # Where it runs is said once, by the topology lines at the top (#300).
    return [ENGINE_READY.format(url=arcade.get("url") or "its address")]


def _models_on(report: dict) -> bool:
    """Models count as on when configured and nothing about them is listed as missing."""
    ollama = report.get("ollama") or {}
    if report.get("knowledge_agent") or ollama.get("available") is False:
        return False
    if any(ollama.get(f"{role}_model_installed") is False for role in ("chat", "embedding")):
        return False
    return not any(isinstance(model, dict) and model.get("reachable") is False
                   for model in (report.get("memory_models") or {}).values())


def summary(report: dict) -> list[str]:
    """The lines of the first check: where memory is kept, the topology's three lines, required and present, required and missing, optional
    (off, then on), notes, the full report and the next step. Raises ValueError for a report of another shape."""
    notes = report.get("environment_notes") or []
    if not isinstance(notes, list):  # a string would be read one character at a time
        raise ValueError("environment_notes is not a list")
    missing, broken = required_missing(report), not_working(report)
    present = _engine_present(report)
    writer = report.get("store_writer") or {}
    if writer.get("role") == "reader" and not writer.get("problem") and not _copy_of_the_writer(writer):
        present.append(READER_ROLE)  # who writes is in the topology lines above
    elif report.get("service_running"):
        present.append(SERVICE_RUNNING.format(url=report.get("service_url") or "its address"))
    else:
        present.append(SERVICE_ON_DEMAND)
    off, on = [], []
    knowledge = report.get("knowledge_agent")
    if knowledge == KNOWLEDGE_DISABLED:
        off.append(MODELS_OFF)
    elif _models_on(report):
        on.append(MODELS_ON)
    tmux = report.get("tmux") or {}
    if tmux:
        status = cockpit_status(tmux)
        if status:
            off.append(WORKSPACE_OFF.format(status=status, hint=platform_hints.hint("tmux_launch").rstrip(".")))
        else:
            on.append(WORKSPACE_ON)
    for name, client in sorted((report.get("clients") or {}).items(), key=lambda item: (item[0] != "codex", item[0])):
        if not isinstance(client, dict):
            continue
        label = CLIENT_NAMES.get(name, name)
        if client.get("installed") and client.get("verified_resume"):
            on.append(CLIENT_ON.format(client=label, command=name))
        elif not client.get("installed"):
            off.append(CLIENT_OFF.format(client=label, note=platform_hints.hint("client_note", client=name)))
    lines = [_line(DATA_DIR.format(data_dir=report.get("data_dir") or "its data directory"))]
    # Storage, memory models and what runs here: topology's own three lines, the one wording of where things run.
    place = report.get("topology") if isinstance(report.get("topology"), dict) else {}
    lines += [_line(text, limit=1000) for text in place.get("lines") or []]
    gpu = report.get("gpu") if isinstance(report.get("gpu"), dict) else {}
    if gpu.get("chip") and gpu.get("memory_gib"):  # only a Mac with an Apple chip has them; Linux and WSL do not
        lines.append(_line(THIS_MAC.format(chip=gpu["chip"], gib=gpu["memory_gib"])))
    lines.append(REQUIRED_PRESENT)
    lines += [f"  - {_line(item)}" for item in present]
    if missing:
        lines.append(REQUIRED_MISSING)
        lines += [f"  - {item}" for item in missing]
    else:
        lines.append(NOTHING_MISSING)
    blockers = report.get("environment_blockers") or []  # listed above as missing
    # A project folder this machine cannot read (#371): a note, never a fault; the exit code does not change.
    folders = [e["message"] for e in report.get("project_folders") or []
               if isinstance(e, dict) and isinstance(e.get("message"), str)]
    for heading, items in ((OPTIONAL_BROKEN, broken), (OPTIONAL_OFF, off), (OPTIONAL_ON, on),
                           (NOTES, [n for n in notes if n not in blockers] + folders)):
        if items:
            lines.append(heading)
            lines += [f"  - {_line(item, limit=900 if item in folders else 300)}" for item in items]
    lines += [FULL_REPORT, NEXT_FIX if missing else NEXT_PROJECTS if report.get("projects") else NEXT_START]
    return lines


def main() -> int:
    try:
        report = json.loads(sys.stdin.read())
        if not isinstance(report, dict) or not REPORT_KEYS <= report.keys():
            # Any other object, even {}, is not a report that found nothing (#244).
            raise ValueError("not a doctor report")
        lines = summary(report)
    except (ValueError, AttributeError, TypeError):  # a report of another shape: a sentence, never a traceback
        print(f"{REPORT_UNREADABLE} {FULL_REPORT}")
        return 0
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
