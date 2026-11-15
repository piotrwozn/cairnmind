"""How private a source or a record is: one class, derived from what the store already knows (#180).

The class is computed on every read from the source kind and the origin mark (store.ORIGIN_METADATA),
which the service writes and no client can set. Nothing is stored, so a class can neither be forged
nor go stale, and a row written before this existed gets one without being rewritten.

- project: material of the project itself: code, documents, files, check output and profiles,
  verification and test results, plans.
- conversation: what people and agents said: transcripts, manual notes (/remember), checkpoints,
  findings, and any kind this table does not name.
- third_party: text from outside: written by an external client's session (history unarchived), or
  degraded by a third-party tool result.

A record's class is the highest class among the sources behind it, now or in any earlier version, and
conversation when it has none or one cannot be read. A record an external session wrote is third_party
whatever it cites.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping

CLASSES = ("project", "conversation", "third_party")
_RANK = {name: rank for rank, name in enumerate(CLASSES)}
PROJECT_KINDS = frozenset({"code", "document", "file", "check_output", "check_profile", "verification",
                           "test_result", "plan"})
# Named for the reader; any kind that is not a project kind is conversation, a future one included.
CONVERSATION_KINDS = frozenset({"transcript", "transcript_unknown", "agent_transcript", "manual", "checkpoint",
                                "findings"})
# Only these fields of a source row are read; a caller selecting columns by hand selects these.
SOURCE_FIELDS = "id,kind,metadata.origin_evidence AS origin_evidence,metadata.origin_history AS origin_history"


def _metadata(value: Any) -> dict:
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def outside_origin(metadata: Any) -> bool:
    """Whether the origin mark says the text came from outside: an external session or a third-party result."""
    metadata = _metadata(metadata)
    return metadata.get("origin_evidence") == "degraded" or metadata.get("origin_history") == "unarchived"


def source_sensitivity(source: Mapping) -> str:
    """project, conversation or third_party, from the kind and the origin mark of one source row."""
    marks = _metadata(source.get("metadata"))
    for key in ("origin_evidence", "origin_history"):  # a row selected with SOURCE_FIELDS
        if key in source and key not in marks:
            marks[key] = source[key]
    if outside_origin(marks):
        return "third_party"
    return "project" if source.get("kind") in PROJECT_KINDS else "conversation"


def highest(classes: Iterable[str]) -> str:
    """The most private of the classes; conversation for none."""
    found = [name for name in classes if name in _RANK]
    return max(found, key=_RANK.__getitem__) if found else "conversation"


def _source_ids(row: Mapping) -> list:
    value = row.get("source_ids")
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def rollup(record: Mapping, class_of: Callable[[str], str], earlier: Iterable[str] = ()) -> str:
    """A record's class from the class of each source id behind it, as the module docstring says.

    ``earlier``: the source ids its earlier versions cited (``earlier_source_ids``). An update replaces the
    sources and may keep the text, so a class taken from the current sources alone could be lowered by
    citing a project file for text that came from a conversation. A class never goes down.
    """
    # A record carries its mark in its metadata, a plan at its top level.
    if outside_origin(record.get("metadata")) or outside_origin(record):
        return "third_party"
    ids = list(dict.fromkeys([*_source_ids(record), *earlier]))
    return highest(class_of(i) for i in ids) if ids else "conversation"


def record_sensitivity(record: Mapping, sources: Mapping[str, Mapping | None], earlier: Iterable[str] = ()) -> str:
    """The highest class among the record's sources, given as rows by id; a missing one is conversation."""
    return rollup(record, lambda i: source_sensitivity(sources[i]) if sources.get(i) else "conversation", earlier)


def earlier_source_ids(reader, record_ids: Iterable[str]) -> dict[str, set[str]]:
    """The source ids each record's stored versions (record_revisions) cited, by record id, in bounded queries."""
    wanted = list(dict.fromkeys(i for i in record_ids if isinstance(i, str)))
    found: dict[str, set[str]] = {}
    for start in range(0, len(wanted), 500):
        for row in reader.query("SELECT record_id,snapshot.source_ids AS source_ids FROM record_revisions "
                                "WHERE record_id IN :ids", {"ids": wanted[start:start + 500]}):
            found.setdefault(row["record_id"], set()).update(_source_ids(row))
    return found


def source_rows(reader, ids: Iterable[str]) -> dict[str, dict]:
    """The source rows ``source_sensitivity`` needs, by id, in bounded queries."""
    wanted = list(dict.fromkeys(i for i in ids if isinstance(i, str)))
    rows: dict[str, dict] = {}
    for start in range(0, len(wanted), 500):
        for row in reader.query(f"SELECT {SOURCE_FIELDS} FROM sources WHERE id IN :ids",
                                {"ids": wanted[start:start + 500]}):
            rows[row["id"]] = row
    return rows


def record_classes(reader, records: Iterable[Mapping]) -> dict[str, str]:
    """The class of each record, by record id, reading every source behind them, now or before, once."""
    records = list(records)
    earlier = earlier_source_ids(reader, (record["id"] for record in records))
    rows = source_rows(reader, [*(i for record in records for i in _source_ids(record)),
                                *(i for ids in earlier.values() for i in ids)])
    return {record["id"]: record_sensitivity(record, rows, earlier.get(record["id"], ())) for record in records}
