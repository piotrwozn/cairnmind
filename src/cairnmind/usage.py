"""Token usage of the built-in API agent (and of Codex), summed per day or week, client, profile and model.

The API agent is read from its session journals, Codex from the transcript lines that history import stored
(see ``codex_usage``; the command passes them in, this module never queries). Nothing is written to the journals or the store and no model is asked. A counter nobody reported stays
``unknown``. A row or total that holds a turn without full usage is marked incomplete and its sums are
lower bounds ("at least N"). A cost appears only for a group in which every turn was complete and carried
an estimate. The command resolves the project name through the store (see ``cli.usage``); this module
itself only reads files.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat

from cairnmind.agent_journal import MAX_EVENT_BYTES, _ID
from cairnmind.brief import _line
from cairnmind.codex_usage import codex_units

MAX_ROWS = 60
MAX_DAYS = 366
MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_SINCE = re.compile(r"^([1-9][0-9]{0,5})([dh])$")
_KEYS = ("input_tokens", "output_tokens")
COST_NOTE = ("Cost is an estimate from the rates stored with each turn, not an invoice; unknown means it was "
             "not reported. Subscription usage and remaining quota are never converted into money.")
CODEX_NOTE = ("Codex rows are read from the transcript lines that history import stored for this project, one "
              "\"turn\" being one model response; rollouts that were not imported yet are not seen. Codex is a "
              "subscription client, so its cost is always unknown. Claude Code is not covered.")


def parse_since(text) -> timedelta:
    match = _SINCE.fullmatch(str(text).strip())
    if not match:
        raise ValueError(f"--since must be a number of days or hours such as 7d or 36h, not {_line(text, 40)!r}")
    span = timedelta(days=int(match[1])) if match[2] == "d" else timedelta(hours=int(match[1]))
    if span > timedelta(days=MAX_DAYS):
        raise ValueError(f"--since cannot reach back more than {MAX_DAYS} days")
    return span


def _period(at: datetime, by: str) -> str:
    """The day (``2026-11-13``) or ISO week (``2026-W46``, Monday to Sunday) of a UTC time."""
    if by == "week":
        year, week, _ = at.isocalendar()
        return f"{year}-W{week:02d}"
    return at.date().isoformat()


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _usable(row) -> bool:
    return isinstance(row, dict) and all(row.get(k) is None or isinstance(row.get(k), str)
                                         for k in ("id", "session_id", "project_id"))


def _registry(data_dir: Path):
    """The launcher's run records (project, profile and model of each run) and whether they could be read."""
    try:
        with open(data_dir / "runtime" / "runs.json", "rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_REGISTRY_BYTES:
                return [], "unreadable"
            data = json.load(handle)
    except FileNotFoundError:
        return [], "missing"
    except (OSError, ValueError, RecursionError):
        return [], "unreadable"
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return [], "unreadable"
    return [row for row in runs if _usable(row)], "ok"


def _events(path: Path, counts: dict):
    """Yield (type, time, data) line by line; a damaged, oversized or truncated line is counted and skipped."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return
        while True:
            raw = handle.readline(MAX_EVENT_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_EVENT_BYTES and not raw.endswith(b"\n"):
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(MAX_EVENT_BYTES)
                counts["lines_skipped"] += 1
                continue
            try:
                event = json.loads(raw)
                at = _time(event["created_at"])
                if not isinstance(event["type"], str) or not isinstance(event["data"], dict) or at is None:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, RecursionError):
                counts["lines_skipped"] += 1
                continue
            yield event["type"], at, event["data"]


def _turns(path: Path, counts: dict) -> list[dict]:
    """A turn runs from ``turn_started`` to its end event; only its last ``usage`` event counts.

    The engine adds each model call to one running total of the turn and records that total after every
    call, so summing the events would count early calls again. ``provider_started`` is written before every
    model call (API and Copilot paths) and ``usage`` only after a call returned, so a turn with fewer usage
    events than started calls lost a report. A skipped line inside a turn also makes it not complete.
    Usage after an end event (its ``turn_started`` line was lost) forms a turn of its own, run unknown; it can
    never be complete or priced, because its started calls cannot be counted.
    """
    turns, current, skipped = [], None, counts["lines_skipped"]

    def new(run, at):
        turn = {"run": run if isinstance(run, str) else None, "at": at, "usage": None,
                "calls": 0, "reports": 0, "damaged": False}
        turns.append(turn)
        return turn

    for kind, at, data in _events(path, counts):
        if counts["lines_skipped"] != skipped:
            skipped = counts["lines_skipped"]
            if current is not None:
                current["damaged"] = True
        if kind == "turn_started":
            current = new(data.get("run_id"), at)
        elif kind in ("turn_finished", "turn_interrupted", "turn_error"):
            current = None
        elif kind == "provider_started" and current is not None:
            current["calls"] += 1
        elif kind == "usage":
            if current is None:
                current = new(None, at)
                current["damaged"] = True
            current["usage"], current["at"] = data, at
            current["reports"] += 1
    if current is not None and counts["lines_skipped"] != skipped:
        current["damaged"] = True
    return turns


def _summary(turn):
    """(complete, input, output, cost) of one turn; complete only if every call reported both counts."""
    usage = turn["usage"]
    if not usage:
        return False, None, None, None
    tokens = [usage.get(key) if type(usage.get(key)) is int and 0 <= usage[key] <= 10**12 else None for key in _KEYS]
    complete = (usage.get("usage_complete") is True and None not in tokens and not turn["damaged"]
                and turn["reports"] >= turn["calls"])
    cost = usage.get("estimated_cost")
    valid = (complete and type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost <= 10**9
             and usage.get("cost_currency") == "USD")
    return complete, tokens[0], tokens[1], float(cost) if valid else None


def _fold(acc: dict, summary) -> None:
    complete, incoming, outgoing, cost = summary
    acc["turns"] += 1
    acc["complete_turns"] += complete
    for key, value in zip(_KEYS, (incoming, outgoing)):
        if value is not None:
            acc[key] = (acc[key] or 0) + value
    if cost is None:
        acc["cost_known"] = False
    else:
        acc["cost"] += cost


def _new() -> dict:
    return {"turns": 0, "complete_turns": 0, "input_tokens": None, "output_tokens": None,
            "cost": 0.0, "cost_known": True}


def _final(acc: dict, partial=False) -> dict:
    """``partial``: turns may be missing from the sums, so nothing here may read as complete."""
    complete = not partial and acc["turns"] > 0 and acc["complete_turns"] == acc["turns"]
    known = complete and acc["cost_known"] and math.isfinite(acc["cost"])
    return {"turns": acc["turns"], "complete_turns": acc["complete_turns"], "complete": complete,
            "tokens_are_lower_bound": not complete,
            "input_tokens": acc["input_tokens"], "output_tokens": acc["output_tokens"],
            "estimated_cost": round(acc["cost"], 8) if known else None, "cost_currency": "USD" if known else None}


def _cell(value, row):
    if value is None:
        return "unknown"
    return f"at least {value}" if row["tokens_are_lower_bound"] else str(value)


def _row(label, row):
    cost = row["estimated_cost"]
    cost = ("unknown" if cost is None else "<$0.0001 (estimate)" if 0 < cost < 0.00005
            else f"${cost:.4f} (estimate)")
    state = "complete" if row["complete"] else "incomplete"
    return (f"{label}  turns {row['complete_turns']}/{row['turns']} {state}  input {_cell(row['input_tokens'], row)}"
            f"  output {_cell(row['output_tokens'], row)}  cost {cost}")


def _warning(totals: dict, threshold, since: str):
    """A notice, never a limit, when the observed tokens of the period (input plus output) pass ``threshold``."""
    observed = (totals["input_tokens"] or 0) + (totals["output_tokens"] or 0)
    if threshold is None or observed <= threshold:
        return None
    bound = totals["tokens_are_lower_bound"]
    return {"threshold_tokens": threshold, "observed_tokens": observed, "tokens_are_lower_bound": bound,
            "message": f"{'at least ' if bound else ''}{observed} tokens (input plus output, all clients) in the "
                       f"last {since}, over the warning threshold of {threshold}; nothing is blocked."}


def _render(report: dict) -> str:
    groups, data, by = report["groups"], report["data"], report["by"]
    unit = "days are UTC" if by == "day" else "weeks are ISO weeks (Monday to Sunday) in UTC"
    lines = [f"WARNING: {report['warning']['message']}"] if "warning" in report else []
    if "warning_unavailable" in report:
        lines.append(f"Warning threshold not read: {report['warning_unavailable']}")
    lines += [f"Usage of the built-in API agent for {report['project']['name']}, last {report['since']} "
              f"(from {report['from']}); {unit}", COST_NOTE]
    if data["codex_covered"]:
        lines.append(CODEX_NOTE)
    if report["partial"]:
        why = ([f"the run registry is {data['registry']}"] if data["registry"] != "ok" else []) + (
            [f"{data['sessions_skipped']} sessions were skipped because their project is unknown"]
            if data["sessions_skipped"] else []) + (
            [f"{data['codex_waiting']} Codex usage records are waiting in the ingestion inbox, not yet "
             f"attributed to a project"] if data["codex_waiting"] else [])
        lines.append("PARTIAL REPORT: " + "; ".join(why) + ", so usage of this project may be missing.")
    if len(groups) > MAX_ROWS:
        lines.append(f"{len(groups) - MAX_ROWS} older rows are only in the JSON output.")
    lines += [_row(f"{g['period']}  {g['client']}  {g['profile']}  {g['model']}", g) for g in groups[-MAX_ROWS:]]
    if len(report["totals_by_client"]) > 1:
        lines += [_row(f"Total {client}", row) for client, row in report["totals_by_client"].items()]
    what = "the built-in API agent or Codex" if data["codex_covered"] else "the built-in API agent"
    lines.append(_row("Total", report["totals"]) if groups else f"No turns of {what} were found in this period.")
    codex = (f"; {data['codex_records']} Codex usage records, {data['codex_unreadable']} unreadable, "
             f"{data['codex_waiting']} waiting in the ingestion inbox" if data["codex_covered"] else "")
    lines.append(f"Data: {data['turns_complete']} of {data['turns_seen']} turns had complete usage; "
                 f"{data['lines_skipped']} journal lines skipped as unreadable; "
                 f"{data['sessions_skipped']} sessions skipped because their project is unknown; "
                 f"{data['sessions_not_api']} sessions of other clients not counted{codex}.")
    return "\n".join(lines)


def build_usage(data_dir, project: dict, since="7d", *, by="day", codex_records=None, codex_waiting=0,
                warn_over=None, warn_unavailable=None, now=None) -> dict:
    """Sum the usage of this project's API agent turns inside the period; reads files only.

    ``codex_records``: a function from the start of the period to the raw ``token_usage_record`` lines that
    import stored for this project (see ``codex_usage``); this module cannot query the store, so without it
    Codex is not looked at. ``codex_waiting``: such records still in the ingestion inbox, which makes the
    report partial. ``warn_over``: a number of tokens; when the observed total (input plus output) is above it
    the result has a ``warning`` and the text starts with ``WARNING:``; it never blocks anything.
    ``warn_unavailable``: why a default threshold could not be read, shown as a line and a JSON key.
    """
    if by not in ("day", "week"):
        raise ValueError("by must be day or week")
    if warn_over is not None and (type(warn_over) is not int or warn_over < 1):
        raise ValueError("warn_over must be a whole number of tokens, at least 1")
    span, data_dir = parse_since(since), Path(data_dir)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    start = now - span
    runs, registry = _registry(data_dir)
    by_run = {row["id"]: row for row in runs if row.get("id")}
    counts = {"lines_skipped": 0, "sessions_skipped": 0, "sessions_not_api": 0, "sessions_read": 0}
    groups, clients, totals = {}, {}, _new()

    def add(at, client, profile, model, summary):
        _fold(groups.setdefault((_period(at, by), client, profile, model), _new()), summary)
        _fold(clients.setdefault(client, _new()), summary)
        _fold(totals, summary)

    root = data_dir / "runtime" / "agents"
    try:
        folders = sorted((e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
    except OSError:
        folders = []
    for folder in folders:
        if not _ID.fullmatch(folder.name):
            continue
        # A journal untouched since before the period cannot hold a turn of it, so it is neither read nor
        # counted as skipped or as another client's: an old stray folder must not make every report partial.
        path = Path(folder.path) / "events.jsonl"
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < start:
                continue
        except (OSError, OverflowError, ValueError):
            continue
        rows = [row for row in runs if row.get("session_id") == folder.name]
        projects = {row.get("project_id") for row in rows}
        if len(projects) != 1 or None in projects:
            counts["sessions_skipped"] += 1
            continue
        if projects != {project["id"]}:
            continue
        if not any(row.get("client") == "api" for row in rows):
            counts["sessions_not_api"] += 1
            continue
        counts["sessions_read"] += 1
        for turn in _turns(path, counts):
            if not start <= turn["at"] <= now:
                continue
            run = by_run.get(turn["run"]) or {}
            profile, model = (_line(run[key], 60) if isinstance(run.get(key), str) and _line(run[key], 60)
                              else "unknown" for key in ("profile_id", "model"))
            add(turn["at"], "api", profile, model, _summary(turn))
    codex = {"codex_records": 0, "codex_unreadable": 0, "codex_waiting": codex_waiting if codex_records else 0}
    for at, incoming, outgoing, complete in codex_units(
            codex_records(start) if codex_records else [], start, now, _time, codex):
        # The cost stays None: Codex is a subscription client and its transcripts hold no price.
        add(at, "codex", "unknown", "unknown", (complete, incoming, outgoing, None))
    partial_by = {"api": registry != "ok" or counts["sessions_skipped"] > 0, "codex": codex["codex_waiting"] > 0}
    partial = any(partial_by.values())
    # ``day`` stays in day rows for readers of earlier versions; a week is not a day, so week rows have none.
    rows = [{"period": period, **({"day": period} if by == "day" else {}), "client": client, "profile": profile,
             "model": model, **_final(acc)} for (period, client, profile, model), acc in sorted(groups.items())]
    final = _final(totals, partial)
    data = {"turns_seen": totals["turns"], "turns_complete": final["complete_turns"], **counts,
            "registry": registry, "codex_covered": codex_records is not None, **codex}
    report = {"project": {"id": project["id"], "name": _line(project["name"], 60)}, "since": str(since).strip(),
              "timezone": "UTC", "from": start.isoformat(timespec="seconds"),
              "to": now.isoformat(timespec="seconds"), "partial": partial, "groups": rows, "totals": final,
              "totals_by_client": {client: _final(acc, partial_by[client]) for client, acc in sorted(clients.items())},
              "data": data, "cost_note": COST_NOTE, "by": by}
    warning = _warning(final, warn_over, report["since"])
    report = {**report, **({"warning": warning} if warning else {}),
              **({"warning_unavailable": _line(warn_unavailable, 200)} if warn_unavailable else {})}
    return {**report, "text": _render(report)}
