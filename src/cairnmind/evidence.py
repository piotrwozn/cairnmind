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
  and the local files have not been indexed yet, so nothing can be said; or it rests only on
  sources imported from a published memory directory (``unverified_by: published_elsewhere``),
  which were seen on another machine and never here. Once `memory import-commit` compared such a
  file source with the code of a commit (#172), it stands or is gone like a local one: anchored when
  the quoted passage (or, without one, the same content) is there, orphaned when it is not.
- ``degraded``: an external client's session (whose conversation Cairnmind cannot read)
  wrote the record, or the record rests only on sources such a session wrote; or the built-in
  agent wrote it, or the text it rests on, after a third-party MCP tool result in its session
  (``degraded_by: third_party_tool``). It is never ``anchored``, whatever its sources: nothing it
  says has been verified.

The state is only as fresh as the last indexing of the project's files.
"""
from __future__ import annotations

from cairnmind.memory_paths import bound_metadata, imported_source_ids, memory_bindings
from cairnmind.store import degraded_origin

STATES = ("anchored", "evidence_replaced", "orphaned", "unverified", "degraded")
_FILE_KINDS = ("code", "document", "file")


def _metadata(row):
    from cairnmind.retrieval import json_value
    value = json_value(row.get("metadata"), {})
    return value if isinstance(value, dict) else {}


def _text(source):
    from cairnmind.agent import source_text
    return source_text(source)


def _is_audit(source):
    # A review adds its own audit note as a source. The note says that someone reviewed
    # the record, not why the record is true, so it cannot keep a record anchored. A review
    # published elsewhere (#170) neither anchors a record nor counts against it either.
    return ((str(source.get("uri", "")).startswith("review://")
             and _metadata(source).get("origin") == "local_user_review")
            # The note of a command line confirmation, or of an evaluation's seed (#177): the same kind of note.
            or _metadata(source).get("origin") in ("cli_person", "evaluation_seed")
            or (source.get("kind") == "published" and _metadata(source).get("origin") == "publication_review"))


def _file_verdicts(store, project_id, stale):
    """Classify non-current file sources with a fixed number of queries, whatever their count."""
    from cairnmind.sql import json_text
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
    keys = sorted({json_text([project_id, uri]) for uris in addresses.values() for uri in uris})
    # current_key is the unique index the store keeps for "the current version at this address".
    current = {row["current_key"]: row for row in store.query(
        "SELECT id,sha256,current_key FROM sources WHERE current_key IN :keys AND is_current=true", {"keys": keys})}
    verdicts, unmatched = {}, {}
    for source_id, source in stale.items():
        found = next((current[key] for key in (json_text([project_id, uri]) for uri in addresses[source_id])
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


def _symbol_and_comment_states(store, project_id, records, cited_sources, verdicts):
    """Derive symbol_state / symbol_change (#411) and comment_state (#412) in one bulk query."""
    from pathlib import Path
    from cairnmind.parsing import analyze_code
    from cairnmind.symbol_anchor import find_symbol_in_analysis, posix_path, validate_symbol_anchor

    anchors: dict[str, dict] = {}
    comments: dict[str, dict] = {}
    wanted_paths: set[str] = set()
    replacement_ids: set[str] = set()

    for record in records:
        own = _metadata(record)
        anchor = validate_symbol_anchor(own.get("symbol_anchor"))
        if anchor is not None:
            anchors[record["id"]] = anchor
            wanted_paths.add(anchor["path"])
        comment = own.get("comment_anchor")
        if (isinstance(comment, dict) and isinstance(comment.get("tag"), str)
                and isinstance(comment.get("path"), str) and isinstance(comment.get("text_sha256"), str)):
            clean_comment = {**comment, "path": posix_path(comment["path"])}
            comments[record["id"]] = clean_comment
            if clean_comment["path"]:
                wanted_paths.add(clean_comment["path"])
        if record["id"] in anchors or record["id"] in comments:
            for source_id in record.get("source_ids") or []:
                if isinstance(source_id, str) and source_id in verdicts:
                    _, repl = verdicts[source_id]
                    if isinstance(repl, str):
                        replacement_ids.add(repl)

    if not anchors and not comments:
        return {}

    current_rows = store.query(
        "SELECT id,uri,kind,sha256,created_at,metadata FROM sources "
        "WHERE project_id=:project AND is_current=true AND kind IN :kinds AND metadata.relative_path IN :paths",
        {"project": project_id, "kinds": list(_FILE_KINDS), "paths": sorted(wanted_paths)},
    ) if wanted_paths else []

    by_rel_root: dict[tuple[str, str], dict] = {}
    by_rel: dict[str, dict] = {}
    by_id: dict[str, dict] = {}

    def register_current(row):
        by_id[row["id"]] = row
        meta = _metadata(row)
        rel = meta.get("relative_path")
        if isinstance(rel, str) and rel.strip():
            norm = posix_path(rel)
            by_rel.setdefault(norm, row)
            root = meta.get("root")
            if isinstance(root, str) and root:
                by_rel_root[(norm, root)] = row

    for row in current_rows:
        register_current(row)
    missing_repl = sorted(replacement_ids - set(by_id))
    if missing_repl:
        for row in store.query(
                "SELECT id,uri,kind,sha256,created_at,metadata FROM sources WHERE id IN :ids AND is_current=true",
                {"ids": missing_repl}):
            if row.get("project_id", project_id) == project_id:
                register_current(row)

    parsed_cache: dict[str, dict] = {}

    def analysis_for(source_row, fallback_rel: str) -> dict | None:
        sid = source_row["id"]
        if sid in parsed_cache:
            return parsed_cache[sid]
        meta = _metadata(source_row)
        analysis = meta.get("analysis") if isinstance(meta.get("analysis"), dict) else None
        has_hashes = bool(
            analysis
            and (
                any(isinstance(sym.get("body_hash"), str) for sym in (analysis.get("symbols") or []))
                or "tagged_comments" in analysis
            )
        )
        if not has_hashes:
            full = store.get_source(sid)
            rel = meta.get("relative_path") or fallback_rel or source_row.get("uri") or ""
            analysis = analyze_code(Path(str(rel)), str(full.get("content") or ""))
        parsed_cache[sid] = analysis
        return analysis

    def current_for(record, target_path: str):
        own = _metadata(record)
        preferred_roots: list[str] = []
        if isinstance(own.get("root"), str) and own["root"]:
            preferred_roots.append(own["root"])
        for source_id in record.get("source_ids") or []:
            src = cited_sources.get(source_id)
            if src is not None:
                r = _metadata(src).get("root")
                if isinstance(r, str) and r and r not in preferred_roots:
                    preferred_roots.append(r)
        for root in preferred_roots:
            hit = by_rel_root.get((target_path, root))
            if hit is not None:
                return hit
        # Fall back to the replacement source ID if the cited file was updated in place.
        for source_id in record.get("source_ids") or []:
            if source_id in verdicts:
                _, repl = verdicts[source_id]
                if isinstance(repl, str) and repl in by_id:
                    return by_id[repl]
            src = cited_sources.get(source_id)
            if src is not None and src.get("is_current") and src.get("kind") in _FILE_KINDS:
                meta = _metadata(src)
                if posix_path(meta.get("relative_path") or "") == target_path:
                    return src
        return by_rel.get(target_path)

    def cited_commit_for(record) -> str | None:
        for source_id in record.get("source_ids") or []:
            src = cited_sources.get(source_id)
            if src is not None and src.get("kind") in _FILE_KINDS:
                commit = _metadata(src).get("git_commit")
                if isinstance(commit, str) and commit:
                    return commit
        return None

    derived: dict[str, dict] = {}
    for record in records:
        rid = record["id"]
        anchor = anchors.get(rid)
        comment = comments.get(rid)
        if anchor is None and comment is None:
            continue
        info: dict = {}
        if anchor is not None:
            cur = current_for(record, anchor["path"])
            info["symbol_anchor"] = anchor
            if cur is None:
                info["symbol_state"] = "symbol_gone"
                info["symbol_change"] = {
                    "symbol": anchor["symbol"],
                    "kind": anchor["kind"],
                    "path": anchor["path"],
                    "commit": None,
                    "changed_at": None,
                }
            else:
                analysis = analysis_for(cur, anchor["path"])
                found = find_symbol_in_analysis(analysis, anchor["symbol"], anchor["kind"])
                cur_meta = _metadata(cur)
                cur_commit = cur_meta.get("git_commit") if isinstance(cur_meta.get("git_commit"), str) else None
                cited_commit = cited_commit_for(record)
                diff_commit = cur_commit if cur_commit and cur_commit != cited_commit else None
                if found is None:
                    info["symbol_state"] = "symbol_gone"
                    info["symbol_change"] = {
                        "symbol": anchor["symbol"],
                        "kind": anchor["kind"],
                        "path": anchor["path"],
                        "commit": diff_commit,
                        "changed_at": cur.get("created_at"),
                    }
                elif found.get("body_hash") == anchor["body_hash"]:
                    info["symbol_state"] = "unchanged"
                else:
                    info["symbol_state"] = "symbol_changed"
                    info["symbol_change"] = {
                        "symbol": anchor["symbol"],
                        "kind": anchor["kind"],
                        "path": anchor["path"],
                        "commit": diff_commit,
                        "changed_at": cur.get("created_at"),
                    }
        if comment is not None:
            cur = current_for(record, comment["path"])
            info["comment_anchor"] = comment
            if cur is None:
                info["comment_state"] = "tag_removed"
            else:
                analysis = analysis_for(cur, comment["path"]) or {}
                tagged = analysis.get("tagged_comments") or []
                matched = any(
                    isinstance(item, dict)
                    and item.get("tag") == comment["tag"]
                    and item.get("text_sha256") == comment["text_sha256"]
                    for item in tagged
                )
                info["comment_state"] = "present" if matched else "tag_removed"
        derived[rid] = info
    return derived


def evidence_states(store, project_id, records, *, quotes=True):
    """Return ``{record_id: state}``; reads sources in bulk and writes nothing.

    ``quotes=False`` skips loading file bodies and leaves ``quote_survives`` as ``None``;
    callers that only need the state use it.
    """
    records = list(records)
    source_ids = sorted({source_id for record in records for source_id in record.get("source_ids") or []
                         if isinstance(source_id, str)})
    rows = store.query("SELECT id,project_id,uri,kind,sha256,is_current,created_at,metadata FROM sources WHERE id IN :ids",
                       {"ids": source_ids}) if source_ids else []
    sources = {row["id"]: row for row in rows if row.get("project_id") == project_id}
    stale = {source_id: source for source_id, source in sources.items()
             if not source.get("is_current") and source.get("kind") in _FILE_KINDS}
    verdicts = _file_verdicts(store, project_id, stale) if stale else {}
    anchored = _symbol_and_comment_states(store, project_id, records, sources, verdicts)
    bodies = {}

    def body(source_id):
        if source_id not in bodies:
            bodies[source_id] = _text(store.get_source(source_id))
        return bodies[source_id]

    result = {}
    for record in records:
        cited = [source_id for source_id in dict.fromkeys(record.get("source_ids") or []) if isinstance(source_id, str)]
        counts = {"current": 0, "replaced": 0, "gone": 0, "pending": 0}
        replaced, external, third_party, published, compared, statements = [], 0, 0, 0, 0, 0
        for source_id in cited:
            source = sources.get(source_id)
            if source is not None and _is_audit(source):
                continue
            if source is not None and degraded_origin(_metadata(source)):
                # An external session's own statement cannot keep anything anchored, its own records
                # or a record another session based on it.
                external += 1
                third_party += third_party_origin(_metadata(source))
                continue
            if source is not None and source.get("kind") == "published":
                # A source imported from a published directory (#170) was seen elsewhere, not here: only a
                # re-check against the code of an imported commit (#172) can anchor it.
                verdict, replacement = published_verdict(_metadata(source), _metadata(record).get("evidence")), None
                published += verdict == "pending"
                compared += verdict != "pending"
                statements += verdict == "pending" and not _published_file(_metadata(source))
            else:
                verdict, replacement = (("gone", None) if source is None else verdicts.get(source_id, ("current", None)))
            counts[verdict] += 1
            if verdict == "replaced":
                replaced.append((source_id, replacement))
        # A statement seen elsewhere (a conversation, a note) can never be compared; once an imported file
        # source was compared with a commit's code (#172), such statements no longer hold the record back.
        pending = counts["pending"] - (statements if compared else 0)
        state = ("degraded" if degraded_origin(_metadata(record))
                 else "anchored" if counts["current"] else "evidence_replaced" if counts["replaced"]
                 else "unverified" if pending else "degraded" if external else "orphaned")
        extra = anchored.get(record["id"])
        if extra and state not in ("degraded", "unverified"):
            sym_state = extra.get("symbol_state")
            com_state = extra.get("comment_state")
            if sym_state == "unchanged" and com_state != "tag_removed":
                state = "anchored"
            elif sym_state == "symbol_changed":
                state = "evidence_replaced"
            elif sym_state == "symbol_gone":
                state = "orphaned"
            elif com_state == "tag_removed" and state != "orphaned":
                state = "evidence_replaced"
        quote = _metadata(record).get("evidence")
        survives = None
        if quotes and state == "evidence_replaced" and isinstance(quote, str) and quote.strip():
            quote = quote.replace("\r\n", "\n").replace("\r", "\n")
            # A client can store any text as "evidence". Only a quote that really occurs in
            # the cited version can be said to have survived or to be gone.
            verified = [replacement for original, replacement in replaced if quote in body(original)]
            if verified:
                survives = any(quote in body(replacement) for replacement in verified)
        entry = {"evidence_state": state, "quote_survives": survives,
                 "sources_current": counts["current"], "sources_replaced": counts["replaced"],
                 "sources_gone": counts["gone"], "sources_unverified": counts["pending"]}
        if extra:
            entry.update(extra)
        result[record["id"]] = entry
        if state == "unverified" and published:
            result[record["id"]]["unverified_by"] = "published_elsewhere"
        own = _metadata(record)
        if state == "degraded" and (third_party_origin(own) if degraded_origin(own) else external == third_party):
            # Only for this cause, so every other result stays exactly what it was.
            result[record["id"]]["degraded_by"] = "third_party_tool"
    return result


def _published_file(metadata):
    reference = metadata.get("published") if isinstance(metadata, dict) else None
    return isinstance(reference, dict) and reference.get("kind") in _FILE_KINDS


def published_verdict(metadata, quote):
    """The verdict on an imported source stub (#172): pending until `memory import-commit` compared it with the
    code of a commit. Then the record's quoted passage decides when it was compared (still there: current;
    gone: gone); without one, the file decides (same content: current; changed: replaced; gone: gone)."""
    checked = metadata.get("rechecked") if isinstance(metadata, dict) else None
    if not isinstance(checked, dict):
        return "pending"
    if checked.get("file") == "gone":
        return "gone"
    quotes = checked.get("quotes") if isinstance(checked.get("quotes"), dict) else {}
    if isinstance(quote, str) and quote.strip():
        from cairnmind.memory_published_format import sha256_text
        found = quotes.get(sha256_text(quote.replace("\r\n", "\n")))
        if isinstance(found, bool):
            return "current" if found else "gone"
    return {"same": "current", "changed": "replaced"}.get(checked.get("file"), "pending")


THIRD_PARTY_WRITTEN = ("Written after a third-party tool result in a built-in agent session, or resting only "
                       "on such text")


def third_party_origin(metadata) -> bool:
    """Whether a degraded mark comes from a built-in agent session after a third-party MCP tool result
    (origin_client ``mcp:<server>``) rather than from an external client's session."""
    return isinstance(metadata, dict) and str(metadata.get("origin_client", "")).startswith("mcp:")


def unanchored(store, project_id, records, *, quotes=True):
    """Records whose evidence does not stand, each with its state; orphaned first."""
    records = [record for record in records if record.get("status") != "superseded"]
    states = evidence_states(store, project_id, records, quotes=quotes)
    order = {"orphaned": 0, "evidence_replaced": 1, "unverified": 2, "degraded": 3}
    found = [{"record": record, **states[record["id"]]} for record in records
             if states[record["id"]]["evidence_state"] != "anchored"]
    found.sort(key=lambda row: (order[row["evidence_state"]], row["record"]["kind"], row["record"]["id"]))
    return found


_QUOTE = {True: "the quoted passage is still there", False: "the quoted passage is gone",
          None: "no verified quoted passage to compare"}


def orphans_report(store, project, root, records):
    """The records whose evidence does not stand, as rows and as ready text for the command line."""
    from cairnmind.terminal_safe import safe_text

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

    def symbol_note(row):
        notes = []
        sym_state = row.get("symbol_state")
        change = row.get("symbol_change") if isinstance(row.get("symbol_change"), dict) else {}
        if sym_state == "symbol_changed":
            sym = clean(change.get("symbol") or "", 80)
            path = clean(change.get("path") or "", 120)
            commit = clean(str(change.get("commit") or "")[:12], 12)
            day = clean(str(change.get("changed_at") or "")[:19], 19)
            when = f"commit {commit}" if commit else (day if day else "code edited")
            notes.append(f"symbol {sym} in {path} changed ({when})")
        elif sym_state == "symbol_gone":
            sym = clean(change.get("symbol") or "", 80)
            path = clean(change.get("path") or "", 120)
            notes.append(f"symbol {sym} is gone from {path}")
        if row.get("comment_state") == "tag_removed":
            ca = row.get("comment_anchor") if isinstance(row.get("comment_anchor"), dict) else {}
            tag = clean(ca.get("tag") or "comment", 20)
            path = clean(ca.get("path") or "", 120)
            notes.append(f"{tag}: comment removed from {path}")
        return ("; " + "; ".join(notes)) if notes else ""

    if groups["orphaned"]:
        lines += ["", f"Orphaned: no supporting source has a current version ({len(groups['orphaned'])})"]
        lines += [entry(row) + symbol_note(row) for row in groups["orphaned"]]
    if groups["evidence_replaced"]:
        lines += ["", f"No supporting version is current and a supporting file changed ({len(groups['evidence_replaced'])})"]
        lines += [entry(row) + f"; {_QUOTE[row['quote_survives']]}" + symbol_note(row)
                  + (f"; {row['sources_gone']} other supporting source{'' if row['sources_gone'] == 1 else 's'} gone"
                     if row["sources_gone"] else "") for row in groups["evidence_replaced"]]
    if groups["unverified"]:
        lines += ["", f"Imported file evidence not yet compared with local files ({len(groups['unverified'])}); "
                      "index the project first"]
        lines += [entry(row) for row in groups["unverified"]]
    external = [row for row in groups["degraded"] if row.get("degraded_by") != "third_party_tool"]
    third_party = [row for row in groups["degraded"] if row.get("degraded_by") == "third_party_tool"]
    if external:
        lines += ["", f"Written by, or resting only on, an external client's session; never verified "
                      f"({len(external)})"]
        lines += [entry(row) for row in external]
    if third_party:
        lines += ["", f"{THIRD_PARTY_WRITTEN}; never verified ({len(third_party)})"]
        lines += [entry(row) for row in third_party]
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root),
            **{state: len(groups[state]) for state in STATES[1:]}, "records": rows, "text": "\n".join(lines)}
