"""How automatic extraction fares in review, per extractor version (#193).

Read-only and model-free: bounded, paged queries over record_revisions. A record counts as extracted when the
first version the store kept carries the extractor stamp (metadata.extractor, #192), which only the
extraction worker's own write sets; the version that stamp names is the group. The state of a record is its
newest revision's status. A record is counted as confirmed only when a person confirmed it on this machine
(metadata.confirmation.by is "person", #177): one a memory package brought confirmed, evaluation seed data or
a record confirmed before the mark existed is counted apart as ``confirmed_elsewhere`` and stays out of the
share, because nobody here reviewed it. Reviewed means confirmed or disputed; the share is confirmed of those.
"""
from __future__ import annotations

PAGE = 1000
CHUNK = 200
# Extracted records read at most; a project with more is reported as truncated, not read without bound.
MAX_RECORDS = 50000
STATES = ("proposed", "confirmed", "confirmed_elsewhere", "disputed", "superseded")
# The group of a stamp whose version is not a number (a store written by something else).
UNKNOWN = "unknown"


def _empty(version) -> dict:
    return {"extractor_version": version, **{state: 0 for state in STATES}, "other": 0, "total": 0}


def _state(status, by) -> str:
    if status == "confirmed":
        return "confirmed" if by == "person" else "confirmed_elsewhere"
    return status if status in STATES else "other"


def _first_versions(reader, project_id) -> tuple[dict, bool]:
    """Record id -> extractor version, for the records whose first version carries a stamp."""
    found, after = {}, ""
    while True:
        rows = reader.query(
            "SELECT record_id, snapshot.metadata.extractor.version AS extractor_version FROM record_revisions "
            "WHERE project_id=:project AND version=1 AND snapshot.metadata.extractor IS NOT NULL "
            "AND record_id>:last ORDER BY record_id LIMIT :limit",
            {"project": project_id, "last": after, "limit": PAGE})
        for row in rows:
            version = row.get("extractor_version")
            found[row["record_id"]] = version if type(version) is int else UNKNOWN
        if len(found) >= MAX_RECORDS:
            return found, len(rows) == PAGE
        if len(rows) < PAGE:
            return found, False
        after = rows[-1]["record_id"]


def _heads(reader, project_id, ids) -> dict:
    """Record id -> (status, confirmation.by) of its newest revision, for these records."""
    heads = {}
    for start in range(0, len(ids), CHUNK):
        chunk, after = ids[start:start + CHUNK], ""
        while True:
            rows = reader.query(
                "SELECT id, record_id, version, snapshot.status AS status, "
                "snapshot.metadata.confirmation.`by` AS confirmation_by FROM record_revisions "
                "WHERE project_id=:project AND record_id IN :ids AND id>:last ORDER BY id LIMIT :limit",
                {"project": project_id, "ids": chunk, "last": after, "limit": PAGE})
            for row in rows:
                best = heads.get(row["record_id"])
                if best is None or row["version"] > best[0]:
                    heads[row["record_id"]] = (row["version"], row.get("status"), row.get("confirmation_by"))
            if len(rows) < PAGE:
                break
            after = rows[-1]["id"]
    return {record: (status, by) for record, (_, status, by) in heads.items()}


def extraction_stats(reader, project_id: str) -> dict:
    """Per extractor version: how many extracted records are in each state, and the share confirmed of the
    reviewed ones (None while none is reviewed). Newest extractor version first."""
    first, truncated = _first_versions(reader, project_id)
    heads = _heads(reader, project_id, sorted(first))
    groups: dict = {}
    for record_id, version in first.items():
        group = groups.setdefault(version, _empty(version))
        status, by = heads.get(record_id, (None, None))
        group[_state(status, by)] += 1
        group["total"] += 1
    rows = []
    for version in sorted(groups, key=lambda item: (item == UNKNOWN, -item if type(item) is int else 0)):
        group = groups[version]
        reviewed = group["confirmed"] + group["disputed"]
        rows.append({**group, "reviewed": reviewed,
                     "confirmed_share": round(group["confirmed"] / reviewed, 4) if reviewed else None})
    return {"project_id": project_id, "extractors": rows, "records": len(first), "truncated": truncated}


def render(stats: dict) -> str:
    """The plain-text form of extraction_stats: only numbers and the project id, never record text."""
    lines = [f"Extraction review in {stats['project_id']}: {stats['records']} extracted records"
             + (f" (the first {MAX_RECORDS} only)" if stats["truncated"] else "")]
    if not stats["extractors"]:
        lines.append("No record was made by automatic extraction with an extractor stamp.")
    for row in stats["extractors"]:
        share = ("no review yet" if row["confirmed_share"] is None
                 else f"{row['confirmed_share']:.0%} of the reviewed ones confirmed")
        name = "unknown" if row["extractor_version"] == UNKNOWN else f"v{row['extractor_version']}"
        lines.append(f"Extractor {name}: {row['total']} records, {row['proposed']} proposed, {row['confirmed']} "
                     f"confirmed, {row['disputed']} disputed, {row['superseded']} superseded; {share}")
        if row["confirmed_elsewhere"]:
            lines.append(f"  {row['confirmed_elsewhere']} more confirmed elsewhere "
                         "(not counted: no person here reviewed them)")
        if row["other"]:
            lines.append(f"  {row['other']} with another status")
    return "\n".join(lines)
