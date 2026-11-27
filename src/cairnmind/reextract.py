"""Extract sources again after the extractor changed (#194).

Only a proposal the extraction worker wrote and nobody touched since can be replaced (sessions._automatic_proposal:
the store's extraction mark, still proposed): a confirmed, disputed or edited record is never selected, superseded
or edited. The extractor version is the stamp of #192 (metadata.extractor.version); a record without one counts as 0.
"""
from __future__ import annotations

from .extraction_input import extraction_request
from .retrieval import json_value
from .sessions import _automatic_proposal
from .sql import clean_row, json_text
from .storage_query import asc, db_find, eq, gt, is_in, is_not_null

PAGE = 1000
CHUNK = 200
UNEXTRACTED_REASON = "The source is no longer extracted: the current extractor skips it."


def stamp_version(record: dict) -> int:
    stamp = (record.get("metadata") or {}).get("extractor")
    version = stamp.get("version") if isinstance(stamp, dict) else None
    return version if type(version) is int else 0


def replaceable(store, project_id: str, source: dict, below: int) -> list[dict]:
    """The untouched proposals of one source whose extractor version is below ``below``, oldest ids first."""
    rows = [
        clean_row(row)
        for row in db_find(
            store,
            "records",
            {"project_id": project_id, "status": "proposed", "metadata.extraction_source_id": source["id"]},
            order_by=(asc("id"),),
        )
    ]
    return [row for row in rows if _automatic_proposal(row, {source["id"]: source}) and stamp_version(row) < below]


def supersede_unextracted(store, project_id: str, source: dict, below: int, check=lambda: None) -> int:
    """Supersede the untouched proposals of a source the current extractor skips (a job of ``queue`` only); the
    caller's transaction makes it all or nothing. The reason is stored on each superseded version."""
    gone = replaceable(store, project_id, source, below)
    for old in gone:
        check()
        store.remember(project_id, {"id": old["id"], "expected_version": old["version"], "status": "superseded",
                                    "metadata": {"superseded_reason": UNEXTRACTED_REASON}}, old["source_ids"])
    return len(gone)


def _skipped(source: dict) -> bool:
    # shortcut: a masked source needs the secrets to decide, so the dry run counts it as read; the job decides.
    metadata = json_value(source.get("metadata"), {})
    return not (isinstance(metadata, dict) and metadata.get("masked")) and extraction_request(source) is None


def plan(store, project_id: str, below: int, version: int) -> dict:
    """What a re-extraction with extractor ``version`` of the records below ``below`` would queue, per source."""
    found: dict[str, list[int]] = {}
    skipped: set[str] = set()
    after = ""
    while True:
        rows = [
            clean_row(row)
            for row in db_find(
                store,
                "records",
                [
                    eq("project_id", project_id),
                    eq("status", "proposed"),
                    is_not_null("metadata.extraction_source_id"),
                    gt("id", after),
                ],
                order_by=(asc("id"),),
                limit=PAGE,
            )
        ]
        marks = sorted({r["metadata"]["extraction_source_id"] for r in rows
                        if isinstance(r["metadata"].get("extraction_source_id"), str)})
        sources = {}
        for start in range(0, len(marks), CHUNK):
            sources.update({
                s["id"]: s
                for s in map(
                    clean_row,
                    db_find(store, "sources", [eq("project_id", project_id), is_in("id", marks[start:start + CHUNK])]),
                )
            })
        for row in rows:
            if _automatic_proposal(row, sources) and stamp_version(row) < below:
                found.setdefault(row["metadata"]["extraction_source_id"], []).append(stamp_version(row))
        skipped |= {s for s in found if s in sources and _skipped(sources[s])}
        if len(rows) < PAGE:
            break
        after = rows[-1]["id"]
    queued = {
        source
        for source in found
        if db_find(
            store,
            "jobs",
            {"job_key": json_text(["extract", project_id, _key(source, version)])},
            columns=("id",),
        )
    }
    per_version: dict[int, dict] = {}
    for source, versions in found.items():
        for number in versions:
            row = per_version.setdefault(number, {"extractor_version": number, "records": 0, "sources": set()})
            row["records"] += 1
            row["sources"].add(source)
    return {"project_id": project_id, "below_version": below, "extractor_version": version,
            "versions": [{**row, "sources": len(row["sources"])} for _, row in sorted(per_version.items())],
            "sources": sorted(found), "skipped_sources": sorted(skipped),
            "skipped_records": sum(len(found[s]) for s in skipped), "already_queued": sorted(queued),
            "would_queue": sorted(set(found) - queued)}


def _key(source_id: str, version: int) -> str:
    return f"extract:{source_id}:v{version}"


def queue(store, project_id: str, below: int, version: int) -> dict:
    """Queue one extract job per source of ``plan``; a source that has the job for this version gets none again."""
    result = plan(store, project_id, below, version)
    for source in result["would_queue"]:
        store.enqueue("extract", project_id, {"source_id": source, "reextract": True}, _key(source, version))
    return {**result, "queued": len(result["would_queue"])}
