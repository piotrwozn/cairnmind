"""Extract sources again after the extractor changed (#194).

Only a proposal the extraction worker wrote and nobody touched since can be replaced (sessions._automatic_proposal:
the store's extraction mark, still proposed): a confirmed, disputed or edited record is never selected, superseded
or edited. The extractor version is the stamp of #192 (metadata.extractor.version); a record without one counts as 0.
"""
from __future__ import annotations

from .sessions import _automatic_proposal
from .sql import clean_row, json_text

PAGE = 1000
CHUNK = 200


def stamp_version(record: dict) -> int:
    stamp = (record.get("metadata") or {}).get("extractor")
    version = stamp.get("version") if isinstance(stamp, dict) else None
    return version if type(version) is int else 0


def replaceable(store, project_id: str, source: dict, below: int) -> list[dict]:
    """The untouched proposals of one source whose extractor version is below ``below``, oldest ids first."""
    rows = [clean_row(row) for row in store.query(
        "SELECT FROM records WHERE project_id=:project AND status='proposed' AND metadata.extraction_source_id=:source "
        "ORDER BY id", {"project": project_id, "source": source["id"]})]
    return [row for row in rows if _automatic_proposal(row, {source["id"]: source}) and stamp_version(row) < below]


def plan(store, project_id: str, below: int, version: int) -> dict:
    """What a re-extraction with extractor ``version`` of the records below ``below`` would queue, per source."""
    found: dict[str, list[int]] = {}
    after = ""
    while True:
        rows = [clean_row(row) for row in store.query(
            "SELECT FROM records WHERE project_id=:project AND status='proposed' "
            "AND metadata.extraction_source_id IS NOT NULL AND id>:last ORDER BY id LIMIT :limit",
            {"project": project_id, "last": after, "limit": PAGE})]
        marks = sorted({r["metadata"]["extraction_source_id"] for r in rows
                        if isinstance(r["metadata"].get("extraction_source_id"), str)})
        sources = {}
        for start in range(0, len(marks), CHUNK):
            sources.update({s["id"]: s for s in map(clean_row, store.query(
                "SELECT FROM sources WHERE project_id=:project AND id IN :ids",
                {"project": project_id, "ids": marks[start:start + CHUNK]}))})
        for row in rows:
            if _automatic_proposal(row, sources) and stamp_version(row) < below:
                found.setdefault(row["metadata"]["extraction_source_id"], []).append(stamp_version(row))
        if len(rows) < PAGE:
            break
        after = rows[-1]["id"]
    queued = {source for source in found if store.query(
        "SELECT id FROM jobs WHERE job_key=:key", {"key": json_text(["extract", project_id, _key(source, version)])})}
    per_version: dict[int, dict] = {}
    for source, versions in found.items():
        for number in versions:
            row = per_version.setdefault(number, {"extractor_version": number, "records": 0, "sources": set()})
            row["records"] += 1
            row["sources"].add(source)
    return {"project_id": project_id, "below_version": below, "extractor_version": version,
            "versions": [{**row, "sources": len(row["sources"])} for _, row in sorted(per_version.items())],
            "sources": sorted(found), "already_queued": sorted(queued),
            "would_queue": sorted(set(found) - queued)}


def _key(source_id: str, version: int) -> str:
    return f"extract:{source_id}:v{version}"


def queue(store, project_id: str, below: int, version: int) -> dict:
    """Queue one extract job per source of ``plan``; a source that has the job for this version gets none again."""
    result = plan(store, project_id, below, version)
    for source in result["would_queue"]:
        store.enqueue("extract", project_id, {"source_id": source}, _key(source, version))
    return {**result, "queued": len(result["would_queue"])}
