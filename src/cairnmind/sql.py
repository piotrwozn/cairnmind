"""Small helpers for the SQL the store and its sibling modules run: JSON text, row cleaning, id-checked writes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_utc(value):
    """Timezone-aware ISO 8601 text as UTC; naive or invalid values are unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def evidence_time(tx, source_ids):
    """When the record's evidence existed, from its immutable sources.

    A transcript's own timestamp is when the statement was made (`stated`). Any other
    source existed no later than the moment Cairnmind stored its snapshot (`observed`).
    The audit note of a review or of a conflict decision records when someone judged the
    record, not when its evidence existed, so it is left out.
    """
    times, bases = [], set()
    for source_id in dict.fromkeys(source_ids):
        row = one_row(tx, "SELECT created_at, metadata.source_timestamp AS stated, metadata.origin AS origin "
                         "FROM sources WHERE id=:id", {"id": source_id}, False) or {}
        if row.get("origin") == "local_user_review":
            continue
        stated = parse_utc(row.get("stated"))
        when, basis = (stated, "stated") if stated else (parse_utc(row.get("created_at")), "observed")
        if when is not None:
            times.append(when)
            bases.add(basis)
    if not times:
        return {"earliest": None, "latest": None, "basis": "unknown"}
    return {"earliest": min(times).isoformat(), "latest": max(times).isoformat(),
            "basis": bases.pop() if len(bases) == 1 else "mixed"}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def clean_row(row):
    return {key: value for key, value in row.items() if not key.startswith("@")}


def quote_name(name):
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ValueError("Invalid database type/property name")
    return f"`{name}`"


def one_row(tx, sql, params=None, required=True):
    rows = tx.query(sql, params)
    if not rows:
        if required:
            raise ValueError("Record does not exist")
        return None
    return clean_row(rows[0])


def insert_document(tx, collection, data):
    tx.command(f"INSERT INTO {quote_name(collection)} CONTENT {json_text(data)}")


def update_document(tx, collection, data, where, params):
    assignments = ",".join(f"{quote_name(key)}=:value_{key}" for key in data)
    return tx.command(f"UPDATE {quote_name(collection)} SET {assignments} WHERE {where}",
                      {**params, **{f"value_{key}": value for key, value in data.items()}})
