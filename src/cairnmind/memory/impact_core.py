"""Shared symbol-definition parsing and impact-verdict primitives (acyclic base for impact & impact_verdicts)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from cairnmind.scip.parsing import analyze_code
from cairnmind.pal.private_files import open_no_follow
from cairnmind.storage.sql import json_text
from cairnmind.storage.storage_query import db_find, eq

MAX_FILE_BYTES = 2 * 1024 * 1024

VERDICTS = {"still_valid": "still valid", "needs_update": "needs update", "not_related": "not related"}
MAX_NOTE_CHARS = 4000
PREFIX = "review://impact/"
CLI_REFUSED = ("The command line no longer records an impact verdict. Nothing was saved. The person gives it in the "
               "inspector of the workspace (`cairnmind`): Impact, open the entry, then Verdict.")
STALE = "The code or the record changed since this list was shown; read the current list and decide again"


class StaleVerdict(ValueError):
    """The entry the person saw is no longer the entry the list shows."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _span(lines, symbol):
    """The text of ``symbol`` between its byte columns (the parser counts UTF-8 bytes) in its lines."""
    first, last = symbol["line_start"] - 1, symbol["line_end"] - 1
    chunk = [line.encode("utf-8") for line in lines[first:last + 1]]
    if not chunk:
        return ""
    if len(chunk) == 1:
        chunk[0] = chunk[0][symbol["column_start"]:symbol["column_end"]]
    else:
        chunk[0], chunk[-1] = chunk[0][symbol["column_start"]:], chunk[-1][:symbol["column_end"]]
    return b"\n".join(chunk).decode("utf-8", "replace")


def _definitions(name, text):
    """``{symbol name: sorted definition texts}``, or None when the file cannot be compared by symbol."""
    if text is None:
        return None
    analysis = analyze_code(Path(name), text)
    if analysis["language"] == "text" or any(str(error).startswith("Parser unavailable") for error in analysis["errors"]):
        return None
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    found = {}
    for symbol in analysis["symbols"]:
        start = symbol["line_start"] - 1
        if symbol.get("kind") == "parameter" and "column_start" in symbol:
            found.setdefault(symbol["name"], []).append(_span(lines, symbol))
            continue
        floor = max([other["line_end"] for other in analysis["symbols"] if other["line_end"] < symbol["line_start"]]
                    + [other["line_start"] for other in analysis["symbols"]
                       if other["line_start"] < symbol["line_start"] and other["line_end"] >= symbol["line_end"]] + [0])
        indent = len(lines[start]) - len(lines[start].lstrip())
        top = scan = start
        while scan > floor:
            line = lines[scan - 1]
            body = line.strip()
            depth = len(line) - len(line.lstrip())
            if depth == indent and body.startswith("@"):
                scan -= 1
                top = scan
            elif body and (depth > indent or (depth == indent and body[0] in ")]}"
                                              and body.rstrip(",").endswith((")", "]", "}")))):
                scan -= 1
            else:
                break
        found.setdefault(symbol["name"], []).append("\n".join(lines[top:symbol["line_end"]]))
    return {key: sorted(value) for key, value in found.items()}


def _new_text(root, name):
    path = root / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
        return None if b"\0" in raw else raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_verdict_source(row):
    """Whether a source row is an impact verdict: the one test for reading verdicts and for keeping them out
    of chunks. Reviews and conflict notes share the address scheme and origin but are not verdicts."""
    metadata = row.get("metadata")
    if isinstance(metadata, (str, bytes)):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    return (str(row.get("uri", "")).startswith(PREFIX) and row.get("kind") == "manual"
            and isinstance(metadata, dict) and metadata.get("origin") == "local_user_review"
            and metadata.get("impact_verdict") is True)


def _on_disk(root, name):
    """``(marker, text or None)`` for what is at ``name`` now, read without following a link or leaving root."""
    path = root / name
    try:
        if not path.parent.resolve().is_relative_to(root.resolve()):
            return "outside", None
        if path.is_symlink():
            return "link:" + _sha(os.readlink(path)), None
        if not os.path.lexists(path):
            return "removed", None
        if not path.is_file():
            return "not-a-file", None
        digest = hashlib.sha256()
        with os.fdopen(open_no_follow(path, os.O_RDONLY, private_on_create=False), "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return "unreadable", None
    return "bytes:" + digest.hexdigest(), _new_text(root, name)


def fingerprints(root, changed, records, rows):
    """``{record id: fingerprint}`` from the worktree as it is now; reads files, writes nothing."""
    seen, definitions = {}, {}

    def current(link):
        name = changed.get(link["path"], {}).get("renamed_to", link["path"])
        if name not in seen:
            seen[name] = _on_disk(root, name)
        marker, body = seen[name]
        if "symbol" not in link or body is None:
            return marker
        if name not in definitions:
            definitions[name] = _definitions(name, body)
        found = definitions[name]
        if found is None:
            return "file:" + marker
        return _sha(json_text(found[link["symbol"]])) if link["symbol"] in found else "removed"

    result = {}
    for row in rows:
        parts = [_sha(str(records[row["id"]].get("content", "")))]
        parts += sorted([link["via"], link["path"], link.get("symbol") or "", current(link)]
                        for link in row["links"])
        result[row["id"]] = _sha(json_text(parts))
    return result


def _uri(record_id, fingerprint):
    return f"{PREFIX}{record_id}/{fingerprint[:32]}"


def current_verdicts(store, project_id):
    """``{uri: (source, verdict document)}`` for the project's current verdicts, in one query."""
    found = {}
    for row in db_find(
        store,
        "sources",
        [
            eq("project_id", project_id),
            eq("is_current", True),
            eq("kind", "manual"),
            eq("metadata.impact_verdict", True),
        ],
        columns=("id", "uri", "kind", "content", "metadata", "created_at"),
    ):
        if not is_verdict_source(row):
            continue
        try:
            document = json.loads(row["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(document, dict) and document.get("verdict") in VERDICTS:
            found[row["uri"]] = (row, document)
    return found


def annotate(store, project_id, root, changed, records, rows):
    """Add ``fingerprint`` and ``verdict`` (None, or the verdict with its time and note) to each row."""
    prints = fingerprints(root, changed, records, rows)
    saved = current_verdicts(store, project_id) if rows else {}
    for row in rows:
        row["fingerprint"] = prints[row["id"]]
        source, document = saved.get(_uri(row["id"], row["fingerprint"]), (None, None))
        matches = (document is not None and document.get("record_id") == row["id"]
                   and document.get("fingerprint") == row["fingerprint"])
        row["verdict"] = None if not matches else {
            "verdict": document["verdict"], "at": document.get("at"), "note": document.get("note") or "",
            "source_id": source["id"]}


def verdict_line(row, line):
    """The short line under a reviewed entry, or None."""
    verdict = row.get("verdict")
    if not verdict:
        return None
    note = f": {line(verdict['note'], 120)}" if verdict.get("note") else ""
    return f"  checked: {VERDICTS[verdict['verdict']]} ({line(str(verdict.get('at') or '')[:10], 10)}){note}"
