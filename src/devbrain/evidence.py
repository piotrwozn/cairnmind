"""Whether a record's evidence still exists in current memory.

The state is derived when it is read and never stored: a source can change or
disappear without any write to the records that cite it, and a stored value would
go stale, change the portable state hash and travel wrongly to another machine.

- ``anchored``: at least one supporting source version is current, or the current
  version of the same address has identical content.
- ``evidence_replaced``: every supporting version was replaced by different content
  at the same address. ``quote_survives`` says whether the passage quoted when the
  record was extracted still occurs there; it is ``None`` when no quote was stored.
- ``orphaned``: no supporting source has a current version at all.

The state is only as fresh as the last indexing of the project's files.
"""
from __future__ import annotations

from devbrain.memory_paths import bound_metadata, memory_bindings

STATES = ("anchored", "evidence_replaced", "orphaned")
# A review adds its own audit note as a source. The note says that someone reviewed
# the record, not why the record is true, so it cannot keep a record anchored.
_AUDIT_PREFIX = "review://"
_FILE_KINDS = {"code", "document", "file"}


def _metadata(row):
    from devbrain.retrieval import json_value
    value = json_value(row.get("metadata"), {})
    return value if isinstance(value, dict) else {}


def _text(source):
    from devbrain.agent import source_text
    return source_text(source)


def evidence_states(store, project_id, records):
    """Return ``{record_id: state}`` for the given records, reading sources in bulk and writing nothing."""
    records = list(records)
    source_ids = sorted({source_id for record in records for source_id in record.get("source_ids") or []
                         if isinstance(source_id, str)})
    rows = store.query("SELECT id,project_id,uri,kind,sha256,is_current,metadata FROM sources WHERE id IN :ids",
                       {"ids": source_ids}) if source_ids else []
    sources = {row["id"]: row for row in rows if row.get("project_id") == project_id}
    bindings, current, bodies = [], {}, {}

    def current_version(uri):
        if uri not in current:
            found = store.query("SELECT id,sha256 FROM sources WHERE project_id=:project AND uri=:uri "
                                "AND is_current=true", {"project": project_id, "uri": uri})
            current[uri] = found[0] if found else None
        return current[uri]

    def addresses(source):
        # A file snapshot imported from another machine keeps its foreign path; the
        # local file lives at the bound path.
        result = [source["uri"]]
        path = _metadata(source).get("path")
        if source.get("kind") in _FILE_KINDS and isinstance(path, str):
            if not bindings:
                bindings.append(memory_bindings(store, project_id))
            local = bound_metadata(store, project_id, {"path": path}, bindings=bindings[0]).get("path")
            if isinstance(local, str) and local not in result:
                result.append(local)
        return result

    def classify(source_id):
        source = sources.get(source_id)
        if source is None:
            return "gone", None
        if source.get("is_current") or source.get("kind") not in _FILE_KINDS:
            # A conversation, a note or a check output is a statement made once: it is
            # never "replaced". Only a file has a current version to compare with.
            return "current", None
        for uri in addresses(source):
            replacement = current_version(uri)
            if replacement is not None:
                same = replacement.get("sha256") == source.get("sha256")
                return ("current" if same else "replaced"), replacement["id"]
        return "gone", None

    def quote_found(quote, replacement_ids):
        quote = quote.replace("\r\n", "\n").replace("\r", "\n")
        for replacement_id in replacement_ids:
            if replacement_id not in bodies:
                bodies[replacement_id] = _text(store.get_source(replacement_id))
            if quote in bodies[replacement_id]:
                return True
        return False

    verdicts = {source_id: classify(source_id) for source_id in source_ids}
    result = {}
    for record in records:
        cited = [source_id for source_id in dict.fromkeys(record.get("source_ids") or []) if isinstance(source_id, str)]
        supporting = [source_id for source_id in cited
                      if not str(sources.get(source_id, {}).get("uri", "")).startswith(_AUDIT_PREFIX)] or cited
        counts = {"current": 0, "replaced": 0, "gone": 0}
        replacements = []
        for source_id in supporting:
            verdict, replacement = verdicts[source_id]
            counts[verdict] += 1
            if verdict == "replaced":
                replacements.append(replacement)
        state = "anchored" if counts["current"] else "evidence_replaced" if counts["replaced"] else "orphaned"
        quote = _metadata(record).get("evidence")
        survives = None
        if state == "evidence_replaced" and isinstance(quote, str) and quote.strip():
            survives = quote_found(quote, list(dict.fromkeys(replacements)))
        result[record["id"]] = {"evidence_state": state, "quote_survives": survives,
                                "sources_current": counts["current"], "sources_replaced": counts["replaced"],
                                "sources_gone": counts["gone"]}
    return result


def unanchored(store, project_id, records):
    """Records whose evidence is not current, each with its state; orphaned first."""
    records = [record for record in records if record.get("status") != "superseded"]
    states = evidence_states(store, project_id, records)
    found = [{"record": record, **states[record["id"]]} for record in records
             if states[record["id"]]["evidence_state"] != "anchored"]
    found.sort(key=lambda row: (row["evidence_state"] != "orphaned", row["record"]["kind"], row["record"]["id"]))
    return found


_QUOTE = {True: "the quoted passage is still there", False: "the quoted passage is gone",
          None: "no quoted passage was recorded"}


def orphans_report(store, project, root, records):
    """The records without current evidence, as rows and as ready text for the command line."""
    from devbrain.terminal_safe import safe_text

    def title(record):
        text = " ".join(safe_text(str(record.get("content", "")).strip().split("\n", 1)[0]).split())
        return text if len(text) <= 100 else text[:99] + "\u2026"

    found = unanchored(store, project["id"], records)
    rows = [{"id": row["record"]["id"], "kind": row["record"]["kind"], "status": row["record"]["status"],
             "version": row["record"]["version"], "title": title(row["record"]),
             **{key: value for key, value in row.items() if key != "record"}} for row in found]
    orphaned = [row for row in rows if row["evidence_state"] == "orphaned"]
    replaced = [row for row in rows if row["evidence_state"] == "evidence_replaced"]
    lines = [f"Evidence of {' '.join(safe_text(project['name']).split())} in {' '.join(safe_text(root).split())}, "
             "as of the last indexing of the project's files"]
    if not rows:
        lines.append("Every record rests on a current source.")
    if orphaned:
        lines += ["", f"Orphaned: no supporting source has a current version ({len(orphaned)})"]
        lines += [f"  [{row['kind']}] {row['status']}, version {row['version']}: {row['title']} ({row['id']})"
                  for row in orphaned]
    if replaced:
        lines += ["", f"Source changed since the record was written ({len(replaced)})"]
        lines += [f"  [{row['kind']}] {row['status']}, version {row['version']}: {row['title']} ({row['id']}); "
                  f"{_QUOTE[row['quote_survives']]}" for row in replaced]
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root),
            "orphaned": len(orphaned), "evidence_replaced": len(replaced), "records": rows, "text": "\n".join(lines)}
