"""Small helpers for the SQL the store and its sibling modules run: JSON text, row cleaning, id-checked writes."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnmind.storage.domain_types import ProjectRecord

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ARCADE_RESERVED = frozenset({
    "key", "offset", "by", "order", "group", "select", "from", "where", "limit",
    "skip", "set", "remove", "delete", "insert", "update", "into", "values",
    "in", "not", "and", "or", "is", "null", "true", "false", "as", "distinct",
    "to", "index", "type", "class", "content", "upsert",
    "backup", "before", "after", "begin", "commit", "rollback", "return",
    "default", "lock", "timeout", "batch", "retry", "wait", "export", "import",
    "database", "user", "bucket", "vertex", "edge", "document", "property",
    "match", "while", "case", "when", "then", "desc", "asc", "between", "like",
    "contains", "traverse", "unwind", "truncate", "grant", "revoke", "sleep",
    "explain", "profile", "rebuild",
})


def _arcade_ident(name: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid database identifier: {name!r}")
    return f"`{name}`" if name.lower() in _ARCADE_RESERVED else name


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def parse_utc(value: Any) -> datetime | None:
    """Timezone-aware ISO 8601 text as UTC; naive or invalid values are unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


# Origins of the notes that record an act on a record (a review, a review published elsewhere, a command
# line confirmation, an evaluation's seed mark): they say that someone judged it, never why it is true.
ACT_NOTE_ORIGINS = ("local_user_review", "publication_review", "cli_person", "evaluation_seed", "quote_self_healing")


def evidence_time(
    tx: Any,
    source_ids: Iterable[str],
    *,
    source_rows: Mapping[str, Any] | None = None,
) -> dict[str, str | None]:
    """When the record's evidence existed, from its immutable sources.

    A transcript's own timestamp is when the statement was made (`stated`). Any other
    source existed no later than the moment Cairnmind stored its snapshot (`observed`).
    The audit note of a review or of a conflict decision records when someone judged the
    record, not when its evidence existed, so it is left out.
    """
    times, bases = [], set()
    for source_id in dict.fromkeys(source_ids):
        cached = source_rows.get(source_id) if source_rows is not None else None
        if cached is not None:
            meta = cached.get("metadata") if isinstance(cached.get("metadata"), dict) else {}
            row = {"created_at": cached.get("created_at"),
                   "stated": meta.get("source_timestamp"),
                   "origin": meta.get("origin")}
        else:
            if hasattr(tx, "find_one"):
                row = tx.find_one(
                    "sources",
                    {"id": source_id},
                    columns=(
                        "created_at",
                        ("metadata.source_timestamp", "stated"),
                        ("metadata.origin", "origin"),
                    ),
                ) or {}
            else:
                row = one_row(
                    tx,
                    "SELECT created_at, metadata.source_timestamp AS stated, metadata.origin AS origin "
                    "FROM sources WHERE id=:id",
                    {"id": source_id},
                    False,
                ) or {}
        if row.get("origin") in ACT_NOTE_ORIGINS:
            continue  # Also a review published elsewhere (#170): when someone judged, not when evidence existed.
        stated = parse_utc(row.get("stated"))
        when, basis = (stated, "stated") if stated else (parse_utc(row.get("created_at")), "observed")
        if when is not None:
            times.append(when)
            bases.add(basis)
    if not times:
        return {"earliest": None, "latest": None, "basis": "unknown"}
    return {"earliest": min(times).isoformat(), "latest": max(times).isoformat(),
            "basis": bases.pop() if len(bases) == 1 else "mixed"}


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def clean_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        raise ValueError("Database row is missing")
    return {key: value for key, value in row.items() if not key.startswith("@")}


def quote_name(name: Any) -> str:
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ValueError("Invalid database type/property name")
    return f"`{name}`"


# What a missing row is called in the refusal, by the collection the query reads (#244); any other is a record.
MISSING_KIND = {"plans": "Plan", "sessions": "Session", "sources": "Source", "projects": "Project", "jobs": "Job",
                "check_runs": "Check run", "resume_packets": "Resume packet"}
_FROM = re.compile(r"\bFROM\s+`?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def missing_row_text(text: str) -> bool:
    """Whether `text` is one_row's refusal for a missing row, as a service answer carries it (#244)."""
    return text in {f"{kind} does not exist" for kind in (*MISSING_KIND.values(), "Record")}


def unregistered_root(root: Path | str, project: ProjectRecord | Mapping[str, Any]) -> str:
    """The refusal for a folder that is not a registered root of `project`, with the command that registers it
    (#244). The project name and the path come from the user; they are quoted for the shell and made printable.
    The sentence stays bounded without losing the command: the shown folder and project keep their head and
    tail, and a command too long to paste names its two arguments instead of cutting them."""
    from cairnmind.pal.process_control import quote_arg
    from cairnmind.pal.terminal_safe import safe_text

    def middle(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]

    handle, folder = str(project.get("name") or project.get("id") or ""), str(root)
    command = safe_text(f"cairnmind project root {quote_arg(handle)} {quote_arg(folder)}")
    if len(command) > 1000:
        command = "cairnmind project root <project> <folder>"
    return (f"{middle(safe_text(folder), 300)} is not a registered project root of {middle(safe_text(handle), 200)}; "
            f"register it with `{command}`.")


def one_row(
    tx: Any,
    sql: str,
    params: dict[str, Any] | None = None,
    required: bool = True,
) -> dict[str, Any] | None:
    rows = tx.query(sql, params)
    if not rows:
        if required:
            table = _FROM.search(sql)
            raise ValueError(f"{MISSING_KIND.get(table.group(1) if table else '', 'Record')} does not exist")
        return None
    return clean_row(rows[0])


def insert_document(tx: Any, collection: str, data: Mapping[str, Any]) -> None:
    if hasattr(tx, "insert_document"):
        tx.insert_document(collection, data)
        return
    tx.command(f"INSERT INTO {quote_name(collection)} CONTENT {json_text(data)}")


def insert_documents(tx: Any, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert ``rows`` into one type with one request, the statement the journal stores its events with.

    The engine returns each row it stored; fewer than were sent fails the transaction instead of losing rows.
    """
    if hasattr(tx, "insert_documents"):
        stored = tx.insert_documents(collection, rows)
    else:
        stored = tx.command(f"INSERT INTO {_arcade_ident(collection)} CONTENT :rows", {"rows": rows})
    if not isinstance(stored, list) or len(stored) != len(rows):
        raise RuntimeError(f"The database did not store every row of a group in {collection}")
    return stored


def update_document(
    tx: Any,
    collection: str,
    data: Mapping[str, Any],
    where: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    if hasattr(tx, "update_document"):
        return tx.update_document(collection, data, where, params)
    assignments = ",".join(f"{quote_name(key)}=:value_{key}" for key in data)
    return tx.command(f"UPDATE {quote_name(collection)} SET {assignments} WHERE {where}",
                      {**params, **{f"value_{key}": value for key, value in data.items()}})
