"""Token usage of Codex, read from the rollout files that history import follows (``import_cursors``).

Import archives every line of a Codex rollout, ``token_usage_record`` events included, but as raw text in
the store; the usage is read from the original file, which a cursor names. Each record carries
``thread_token_usage``, the running total of its thread, so the usage of a period is the difference between
a thread's totals, not a sum of reports. The same thread can sit in two files (a rollout copied to
``archived_sessions``), so reports are keyed and counted once. Files are only read; no cost is computed,
because Codex is a subscription client.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from cairnmind.agent_journal import MAX_EVENT_BYTES

_MARKER = b"token_usage_record"
_COUNTERS = ("input_tokens", "output_tokens")


def _counters(usage):
    """(input, output) of a usage object, or None when either is not a plausible count."""
    if not isinstance(usage, dict):
        return None
    values = tuple(usage.get(key) for key in _COUNTERS)
    return values if all(type(v) is int and 0 <= v <= 10**12 for v in values) else None


def _reports(path: Path, parse_time) -> tuple[list, int]:
    """(reports in file order, damaged records) of one rollout file; lines of other kinds are not parsed."""
    found, damaged = [], 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        while True:
            raw = handle.readline(MAX_EVENT_BYTES + 1)
            if not raw:
                return found, damaged
            if len(raw) > MAX_EVENT_BYTES and not raw.endswith(b"\n"):
                # A usage record is a few hundred bytes; a line this long is some other event.
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(MAX_EVENT_BYTES)
                continue
            if _MARKER not in raw:
                continue
            try:
                event = json.loads(raw)
                if event.get("type") != _MARKER.decode():
                    continue
                payload, at = event["payload"], parse_time(event["timestamp"])
                mine, total = _counters(payload["usage"]), _counters(payload["thread_token_usage"])
                key = tuple(payload[name] for name in ("thread_id", "turn_id", "response_id"))
                if at is None or total is None or not all(isinstance(part, str) for part in key):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                damaged += 1
                continue
            found.append((at, key, total, mine == total))


def codex_units(transcripts, project_id, start, now, parse_time, counts: dict) -> list[tuple]:
    """(time, input, output, complete) of every Codex report inside the period, summed from thread totals.

    ``transcripts`` are the import cursors (path, project_id, client). A cursor of another project is left
    alone; one without a project is skipped and counted. A file that is gone or cannot be read is counted as
    missing. A report is complete only if the thread's earlier total is known: either an earlier report is
    in the files read, or the report's own usage equals the thread total, which only a first response does.
    Damaged records or a total that went down make what follows incomplete, never a guess.
    """
    seen, damaged = {}, 0
    for cursor in transcripts:
        path = cursor.get("path")
        owner = cursor.get("project_id")
        if cursor.get("client") != "codex" or not isinstance(path, str):
            continue
        if owner is not None and owner != project_id:
            continue
        try:
            modified = Path(path).stat().st_mtime
        except (OSError, OverflowError, ValueError):
            counts["transcripts_missing"] += 1
            continue
        # A rollout untouched since before the period cannot report in it (see build_usage for journals).
        if modified < start.timestamp():
            continue
        if owner is None:
            counts["transcripts_skipped"] += 1
            continue
        try:
            reports, bad = _reports(Path(path), parse_time)
        except OSError:
            counts["transcripts_missing"] += 1
            continue
        counts["transcripts_read"] += 1
        damaged += bad
        for at, key, total, first in reports:
            seen.setdefault(key, (at, total, first))
    counts["transcript_lines_skipped"] += damaged
    units, last = [], {}
    for key, (at, total, first) in sorted(seen.items(), key=lambda item: item[1][0]):
        before, complete = last.get(key[0]), not damaged
        if before is None and not first:
            before, complete = (total[0], total[1]), False
        elif before is None:
            before = (0, 0)
        elif total[0] < before[0] or total[1] < before[1]:
            before, complete = total, False
        last[key[0]] = total
        if start <= at <= now:
            units.append((at, total[0] - before[0], total[1] - before[1], complete))
    return units
