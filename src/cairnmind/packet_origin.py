"""Where a resume packet record comes from, and the preview order that follows from it (#178).

Derived only from what the store wrote itself: the origin mark it stamps on an external session's writes
(store.ORIGIN_METADATA) and the first version of the record it kept in record_revisions, which no later
write changes. A client's own metadata can only make its own record look less trusted, never more.
"""
from __future__ import annotations

from collections import Counter

from .storage_query import db_find, eq, is_in
from .store import EXTRACTION_MARK

AUTOMATIC_EXTRACTION = "automatic_extraction"
EXTERNAL = "external"
# Written through the service by a person, a coding session or a memory import: the store does not record which.
UNATTRIBUTED = "unattributed"
ORIGINS = (UNATTRIBUTED, EXTERNAL, AUTOMATIC_EXTRACTION)


def _extracted(metadata) -> bool:
    return isinstance(metadata, dict) and (EXTRACTION_MARK in metadata
                                           or metadata.get("origin") == AUTOMATIC_EXTRACTION)


def record_origins(store, records: list[dict]) -> dict[str, str]:
    """The origin of each record, by id."""
    ids = [row["id"] for row in records]
    first = {row["record_id"]: row.get("metadata") for row in db_find(
        store, "record_revisions", [is_in("record_id", ids), eq("version", 1)],
        columns=("record_id", ("snapshot.metadata", "metadata")))} if ids else {}
    origins = {}
    for row in records:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        # The extraction worker's mark, or its label on the first version: an edit or a confirmation
        # later does not change who drafted the content.
        if _extracted(first.get(row["id"])) or (row["id"] not in first and _extracted(metadata)):
            origins[row["id"]] = AUTOMATIC_EXTRACTION
        elif metadata.get("origin_client"):
            # An external client's session, or a built-in agent session after a third-party tool result.
            origins[row["id"]] = EXTERNAL
        else:
            origins[row["id"]] = UNATTRIBUTED
    return origins


def preview_key(row: dict, origin: str) -> tuple:
    """The checkpoint first, then confirmed records, then the rest; within each, by origin, extractions last.

    A confirmation says that a person accepted the record (#177, shown as confirmed_by), not who drafted it,
    so a confirmed record still sorts by its origin and a confirmed extraction never goes ahead of a confirmed
    record nobody marked as less trusted."""
    return (0 if row["kind"] == "checkpoint" else 1, 0 if row["status"] == "confirmed" else 1,
            ORIGINS.index(origin), row["kind"], row["id"])


def omitted_by_origin(records: list[dict], shown: list[dict], origins: dict[str, str]) -> dict[str, int]:
    """How many records were left out of the preview, per origin."""
    kept = {row["id"] for row in shown}
    return dict(sorted(Counter(origins[row["id"]] for row in records if row["id"] not in kept).items()))
