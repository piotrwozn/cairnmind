"""Whether a record's evidence still exists in current memory.

The state is derived when it is read and never stored: a source can change or
disappear without any write to the records that cite it, and a stored value would
go stale, change the portable state hash and travel wrongly to another machine.

- ``anchored``: at least one supporting source still stands. A file version stands
  when it is current, or when identical content is current at the same address or,
  after a move or rename, at another address of the project. Notes, conversations,
  plan snapshots and check outputs are statements made once and always stand.
- ``evidence_replaced``: no supporting version stands and at least one supporting
  file has different content now; other supporting files may be gone
  (``sources_gone``). ``quote_survives`` says whether the passage quoted when the
  record was written still occurs in the changed file. It is ``None`` when no quote
  was stored or when the stored quote does not occur in the cited version.
- ``orphaned``: no supporting source has a current version at all.
- ``unverified``: the record rests on file snapshots imported from another machine,
  and the local files have not been indexed yet, so nothing can be said.

The state is only as fresh as the last indexing of the project's files.
"""
from __future__ import annotations

from devbrain.memory_paths import bound_metadata, imported_source_ids, memory_bindings

STATES = ("anchored", "evidence_replaced", "orphaned", "unverified")
_FILE_KINDS = ("code", "document", "file")


def _metadata(row):
    from devbrain.retrieval import json_value
    value = json_value(row.get("metadata"), {})
    return value if isinstance(value, dict) else {}


def _text(source):
    from devbrain.agent import source_text
    return source_text(source)


def _is_audit(source):
    # A review adds its own audit note as a source. The note says that someone reviewed
    # the record, not why the record is true, so it cannot keep a record anchored.
    return (str(source.get("uri", "")).startswith("review://")
            and _metadata(source).get("origin") == "local_user_review")


def _file_verdicts(store, project_id, stale):
    """Classify non-current file sources with a fixed number of queries, whatever their count."""
    from devbrain.store import _json
    imported = imported_source_ids(store, project_id) & set(stale)
    bindings = memory_bindings(store, project_id) if imported else []
    addresses = {}
    for source_id, source in stale.items():
        addresses[source_id] = [source["uri"]]
        path = _metadata(source).get("path")
        # Only a snapshot imported from another machine lives at a bound local path; mapping
        # a local snapshot would judge a file deleted in one worktree by another worktree's file.
        if source_id in imported and isinstance(path, str):
            local = bound_metadata(store, project_id, {"path": path}, bindings=bindings).get("path")
            if isinstance(local, str) and local not in addresses[source_id]:
                addresses[source_id].append(local)
    keys = sorted({_json([project_id, uri]) for uris in addresses.values() for uri in uris})
    # current_key is the unique index the store keeps for "the current version at this address".
    current = {row["current_key"]: row for row in store.query(
        "SELECT id,sha256,current_key FROM sources WHERE current_key IN :keys AND is_current=true", {"keys": keys})}
    verdicts, unmatched = {}, {}
    for source_id, source in stale.items():
        found = next((current[key] for key in (_json([project_id, uri]) for uri in addresses[source_id])
                      if key in current), None)
        if found is None:
            unmatched[source_id] = source
        elif found.get("sha256") == source.get("sha256"):
            verdicts[source_id] = ("current", None)
        else:
            verdicts[source_id] = ("replaced", found["id"])
    if unmatched:
        # A moved or renamed file keeps its content: identical current content elsewhere in the
        # same root still stands. Another worktree's copy of the file is not this evidence.
        def home(source_id, source):
            metadata = _metadata(source)
            if source_id in imported:
                metadata = bound_metadata(store, project_id, {"root": metadata.get("root")}, bindings=bindings)
            return metadata.get("root")

        hashes = sorted({source["sha256"] for source in unmatched.values() if isinstance(source.get("sha256"), str)})
        moved = {(row["sha256"], row.get("root")) for row in store.query(
            "SELECT sha256,metadata.root AS root FROM sources WHERE project_id=:project AND is_current=true "
            "AND kind IN :kinds AND sha256 IN :hashes",
            {"project": project_id, "kinds": list(_FILE_KINDS), "hashes": hashes})} if hashes else set()
        indexed = None
        for source_id, source in unmatched.items():
            if home(source_id, source) is not None and (source.get("sha256"), home(source_id, source)) in moved:
                verdicts[source_id] = ("current", None)
                continue
            if source_id in imported:
                if indexed is None:
                    indexed = bool(store.query(
                        "SELECT id FROM sources WHERE project_id=:project AND is_current=true AND kind IN :kinds LIMIT 1",
                        {"project": project_id, "kinds": list(_FILE_KINDS)}))
                if not indexed:
                    verdicts[source_id] = ("pending", None)
                    continue
            verdicts[source_id] = ("gone", None)
    return verdicts


def evidence_states(store, project_id, records, *, quotes=True):
    """Return ``{record_id: state}``; reads sources in bulk and writes nothing.

    ``quotes=False`` skips loading file bodies and leaves ``quote_survives`` as ``None``;
    callers that only need the state use it.
    """
    records = list(records)
    source_ids = sorted({source_id for record in records for source_id in record.get("source_ids") or []
                         if isinstance(source_id, str)})
    rows = store.query("SELECT id,project_id,uri,kind,sha256,is_current,metadata FROM sources WHERE id IN :ids",
                       {"ids": source_ids}) if source_ids else []
    sources = {row["id"]: row for row in rows if row.get("project_id") == project_id}
    stale = {source_id: source for source_id, source in sources.items()
             if not source.get("is_current") and source.get("kind") in _FILE_KINDS}
    verdicts = _file_verdicts(store, project_id, stale) if stale else {}
    bodies = {}

    def body(source_id):
        if source_id not in bodies:
            bodies[source_id] = _text(store.get_source(source_id))
        return bodies[source_id]

    result = {}
    for record in records:
        cited = [source_id for source_id in dict.fromkeys(record.get("source_ids") or []) if isinstance(source_id, str)]
        counts = {"current": 0, "replaced": 0, "gone": 0, "pending": 0}
        replaced = []
        for source_id in cited:
            source = sources.get(source_id)
            if source is not None and _is_audit(source):
                continue
            verdict, replacement = (("gone", None) if source is None else verdicts.get(source_id, ("current", None)))
            counts[verdict] += 1
            if verdict == "replaced":
                replaced.append((source_id, replacement))
        state = ("anchored" if counts["current"] else "evidence_replaced" if counts["replaced"]
                 else "unverified" if counts["pending"] else "orphaned")
        quote = _metadata(record).get("evidence")
        survives = None
        if quotes and state == "evidence_replaced" and isinstance(quote, str) and quote.strip():
            quote = quote.replace("\r\n", "\n").replace("\r", "\n")
            # A client can store any text as "evidence". Only a quote that really occurs in
            # the cited version can be said to have survived or to be gone.
            verified = [replacement for original, replacement in replaced if quote in body(original)]
            if verified:
                survives = any(quote in body(replacement) for replacement in verified)
        result[record["id"]] = {"evidence_state": state, "quote_survives": survives,
                                "sources_current": counts["current"], "sources_replaced": counts["replaced"],
                                "sources_gone": counts["gone"], "sources_unverified": counts["pending"]}
    return result


def unanchored(store, project_id, records, *, quotes=True):
    """Records whose evidence does not stand, each with its state; orphaned first."""
    records = [record for record in records if record.get("status") != "superseded"]
    states = evidence_states(store, project_id, records, quotes=quotes)
    order = {"orphaned": 0, "evidence_replaced": 1, "unverified": 2}
    found = [{"record": record, **states[record["id"]]} for record in records
             if states[record["id"]]["evidence_state"] != "anchored"]
    found.sort(key=lambda row: (order[row["evidence_state"]], row["record"]["kind"], row["record"]["id"]))
    return found


_QUOTE = {True: "the quoted passage is still there", False: "the quoted passage is gone",
          None: "no verified quoted passage to compare"}


def orphans_report(store, project, root, records):
    """The records whose evidence does not stand, as rows and as ready text for the command line."""
    from devbrain.terminal_safe import safe_text

    def clean(value, limit=100):
        text = " ".join(safe_text(value).split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    found = unanchored(store, project["id"], records)
    rows = [{"id": row["record"]["id"], "kind": row["record"]["kind"], "status": row["record"]["status"],
             "version": row["record"]["version"],
             "title": clean(str(row["record"].get("content", "")).strip().split("\n", 1)[0]),
             **{key: value for key, value in row.items() if key != "record"}} for row in found]
    groups = {state: [row for row in rows if row["evidence_state"] == state] for state in STATES[1:]}
    lines = [f"Evidence of {clean(project['name'], 60)} in {clean(root, 160)}, as of the last indexing of the project's files"]
    if not rows:
        lines.append("Every record has a supporting source that still stands.")

    def entry(row):
        return f"  [{row['kind']}] {row['status']}, version {row['version']}: {row['title']} ({clean(row['id'])})"

    if groups["orphaned"]:
        lines += ["", f"Orphaned: no supporting source has a current version ({len(groups['orphaned'])})"]
        lines += [entry(row) for row in groups["orphaned"]]
    if groups["evidence_replaced"]:
        lines += ["", f"No supporting version is current and a supporting file changed ({len(groups['evidence_replaced'])})"]
        lines += [entry(row) + f"; {_QUOTE[row['quote_survives']]}"
                  + (f"; {row['sources_gone']} other supporting source{'' if row['sources_gone'] == 1 else 's'} gone"
                     if row["sources_gone"] else "") for row in groups["evidence_replaced"]]
    if groups["unverified"]:
        lines += ["", f"Imported file evidence not yet compared with local files ({len(groups['unverified'])}); "
                      "index the project first"]
        lines += [entry(row) for row in groups["unverified"]]
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root),
            **{state: len(groups[state]) for state in STATES[1:]}, "records": rows, "text": "\n".join(lines)}
