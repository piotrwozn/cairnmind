"""Token usage of the built-in API agent, summed per day, profile and model from its session journals.

Read-only: no model, no service, nothing written. A figure that was not reported stays ``unknown``;
a cost appears only for a group in which every turn reported complete usage and carried an estimate.
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
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _registry(data_dir: Path) -> list[dict]:
    """The launcher's run records, which hold each run's project, profile and model; unreadable means none."""
    try:
        with open(data_dir / "runtime" / "runs.json", "rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_REGISTRY_BYTES:
                return []
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    return [row for row in runs if isinstance(row, dict)] if isinstance(runs, list) else []


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
    """A turn runs from its ``turn_started`` event to the next one; only its last ``usage`` event counts.

    The engine adds each model call to one running total of the turn and records that total every time,
    so the usage events of a turn are cumulative and summing them would count early calls again.
    """
    turns, current = [], None
    for kind, at, data in _events(path, counts):
        if kind == "turn_started":
            current = {"run": data.get("run_id"), "at": at, "usage": None}
            turns.append(current)
        elif kind == "usage":
            if current is None:
                current = {"run": None, "at": at, "usage": None}
                turns.append(current)
            current["usage"], current["at"] = data, at
    return turns


def _summary(usage):
    """(complete, input, output, cost) of one turn; complete only if both counts were really observed."""
    if not usage:
        return False, None, None, None
    tokens = [usage.get(key) if type(usage.get(key)) is int and 0 <= usage[key] <= 10**12 else None for key in _KEYS]
    complete = usage.get("usage_complete") is True and None not in tokens
    cost = usage.get("estimated_cost")
    valid = (complete and type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
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


def _final(acc: dict) -> dict:
    empty = acc["turns"] == 0
    return {"turns": acc["turns"], "complete_turns": acc["complete_turns"],
            "complete": acc["complete_turns"] == acc["turns"],
            "input_tokens": 0 if empty else acc["input_tokens"], "output_tokens": 0 if empty else acc["output_tokens"],
            "estimated_cost": round(acc["cost"], 6) if acc["cost_known"] else None,
            "cost_currency": "USD" if acc["cost_known"] else None}


def _cell(value):
    return "unknown" if value is None else str(value)


def _row(label, row):
    cost = "unknown" if row["estimated_cost"] is None else f"${row['estimated_cost']:.4f} (estimate)"
    state = "complete" if row["complete"] else "incomplete"
    return (f"{label}  turns {row['complete_turns']}/{row['turns']} {state}  "
            f"input {_cell(row['input_tokens'])}  output {_cell(row['output_tokens'])}  cost {cost}")


def _render(project, since, start, groups, totals, data) -> str:
    lines = [f"Usage of the built-in API agent for {_line(project['name'], 60)}, last {since} "
             f"(from {start.isoformat(timespec='seconds')}); days are UTC", COST_NOTE]
    lines += [_row(f"{_line(g['day'], 12)}  {_line(g['profile'], 40)}  {_line(g['model'], 60)}", g)
              for g in groups[:MAX_ROWS]]
    if len(groups) > MAX_ROWS:
        lines.append(f"{len(groups) - MAX_ROWS} more rows are in the JSON output.")
    if not groups:
        lines.append("No turns of the built-in API agent were found in this period.")
    lines.append(_row("Total", totals))
    lines.append(f"Data: {data['turns_complete']} of {data['turns_seen']} turns had complete usage; "
                 f"{data['lines_skipped']} journal lines skipped as unreadable; "
                 f"{data['sessions_skipped']} sessions skipped because their project is unknown; "
                 f"{data['sessions_not_api']} sessions of other clients not counted.")
    return "\n".join(lines)


def build_usage(data_dir, project: dict, since="7d", *, now=None) -> dict:
    """Sum the usage of this project's API agent turns inside the period; reads files only."""
    span, data_dir = parse_since(since), Path(data_dir)
    now = now or datetime.now(timezone.utc)
    start = now - span
    runs = _registry(data_dir)
    by_run = {row.get("id"): row for row in runs}
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
        if len(projects) != 1:
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
        except OSError:
            continue
        counts["sessions_read"] += 1
        for turn in _turns(path, counts):
            if not start <= turn["at"] <= now:
                continue
            run = by_run.get(turn["run"]) or {}
            profile, model = (run.get(key) if isinstance(run.get(key), str) and run.get(key) else "unknown"
                              for key in ("profile_id", "model"))
            summary = _summary(turn["usage"])
            key = (turn["at"].date().isoformat(), profile, model)
            _fold(groups.setdefault(key, _new()), summary)
            _fold(totals, summary)
            seen += 1
    rows = [{"day": day, "profile": profile, "model": model, **_final(acc)}
            for (day, profile, model), acc in sorted(groups.items())]
    final = _final(totals)
    data = {"turns_seen": seen, "turns_complete": final["complete_turns"], **counts}
    since = str(since).strip()
    return {"project": {"id": project["id"], "name": project["name"]}, "since": since, "timezone": "UTC",
            "from": start.isoformat(timespec="seconds"), "to": now.isoformat(timespec="seconds"),
            "groups": rows, "totals": final, "data": data, "cost_note": COST_NOTE,
            "text": _render(project, since, start, rows, final, data)}
