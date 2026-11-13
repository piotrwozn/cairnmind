"""Token usage of the built-in API agent, summed per day, profile and model from its session journals.

Nothing is written to the journals or the store and no model is asked. A counter nobody reported stays
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

MAX_ROWS = 60
MAX_DAYS = 366
MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_SINCE = re.compile(r"^([1-9][0-9]{0,5})([dh])$")
_KEYS = ("input_tokens", "output_tokens")
COST_NOTE = ("Cost is an estimate from the rates stored with each turn, not an invoice; unknown means it was "
             "not reported. Subscription usage and remaining quota are never converted into money.")


def parse_since(text) -> timedelta:
    match = _SINCE.fullmatch(str(text).strip())
    if not match:
        raise ValueError(f"--since must be a number of days or hours such as 7d or 36h, not {_line(text, 40)!r}")
    span = timedelta(days=int(match[1])) if match[2] == "d" else timedelta(hours=int(match[1]))
    if span > timedelta(days=MAX_DAYS):
        raise ValueError(f"--since cannot reach back more than {MAX_DAYS} days")
    return span


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
    Usage after an end event (its ``turn_started`` line was lost) forms a turn of its own, run unknown.
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
            current = current or new(None, at)
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


def _render(name, since, start, groups, totals, data, partial) -> str:
    lines = [f"Usage of the built-in API agent for {name}, last {since} "
             f"(from {start.isoformat(timespec='seconds')}); days are UTC", COST_NOTE]
    if partial:
        why = ([f"the run registry is {data['registry']}"] if data["registry"] != "ok" else []) + (
            [f"{data['sessions_skipped']} sessions were skipped because their project is unknown"]
            if data["sessions_skipped"] else [])
        lines.append("PARTIAL REPORT: " + "; ".join(why) + ", so turns of this project may be missing.")
    if len(groups) > MAX_ROWS:
        lines.append(f"{len(groups) - MAX_ROWS} older rows are only in the JSON output.")
    lines += [_row(f"{g['day']}  {g['profile']}  {g['model']}", g) for g in groups[-MAX_ROWS:]]
    lines.append(_row("Total", totals) if groups else "No turns of the built-in API agent were found in this period.")
    lines.append(f"Data: {data['turns_complete']} of {data['turns_seen']} turns had complete usage; "
                 f"{data['lines_skipped']} journal lines skipped as unreadable; "
                 f"{data['sessions_skipped']} sessions skipped because their project is unknown; "
                 f"{data['sessions_not_api']} sessions of other clients not counted.")
    return "\n".join(lines)


def build_usage(data_dir, project: dict, since="7d", *, now=None) -> dict:
    """Sum the usage of this project's API agent turns inside the period; reads files only."""
    span, data_dir = parse_since(since), Path(data_dir)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    start = now - span
    runs, registry = _registry(data_dir)
    by_run = {row["id"]: row for row in runs if row.get("id")}
    counts = {"lines_skipped": 0, "sessions_skipped": 0, "sessions_not_api": 0, "sessions_read": 0}
    groups, totals, seen = {}, _new(), 0
    root = data_dir / "runtime" / "agents"
    try:
        folders = sorted((e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
    except OSError:
        folders = []
    for folder in folders:
        if not _ID.fullmatch(folder.name):
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
        path = Path(folder.path) / "events.jsonl"
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < start:
                continue
        except (OSError, OverflowError, ValueError):
            continue
        counts["sessions_read"] += 1
        for turn in _turns(path, counts):
            if not start <= turn["at"] <= now:
                continue
            run = by_run.get(turn["run"]) or {}
            profile, model = (_line(run[key], 60) if isinstance(run.get(key), str) and _line(run[key], 60)
                              else "unknown" for key in ("profile_id", "model"))
            summary = _summary(turn)
            _fold(groups.setdefault((turn["at"].date().isoformat(), profile, model), _new()), summary)
            _fold(totals, summary)
            seen += 1
    partial = registry != "ok" or counts["sessions_skipped"] > 0
    rows = [{"day": day, "profile": profile, "model": model, **_final(acc)}
            for (day, profile, model), acc in sorted(groups.items())]
    final = _final(totals, partial)
    data = {"turns_seen": seen, "turns_complete": final["complete_turns"], **counts, "registry": registry}
    since = str(since).strip()
    name = _line(project["name"], 60)
    return {"project": {"id": project["id"], "name": name}, "since": since, "timezone": "UTC",
            "from": start.isoformat(timespec="seconds"), "to": now.isoformat(timespec="seconds"),
            "partial": partial, "groups": rows, "totals": final, "data": data, "cost_note": COST_NOTE,
            "text": _render(name, since, start, rows, final, data, partial)}
