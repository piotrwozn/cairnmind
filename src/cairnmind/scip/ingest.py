"""Incremental, source-preserving ingestion of project files and CLI histories.

The archive is independent of inference. Graph relationships here come from
syntax, never from filename resemblance or generated answers.
"""
from __future__ import annotations

import hashlib
import os
import subprocess  # noqa: F401 - kept on module for tests patching ingest.subprocess.run
import time
from pathlib import Path
from typing import Any

from cairnmind.memory import project_folders
from cairnmind.memory.memory_paths import (
    bound_metadata,
    imported_source_ids,
    is_memory_path,
    memory_bindings,
)
from cairnmind.memory.secret_mask import ingest_masker, masked_metadata  # noqa: F401
from cairnmind.pal.file_locks import lock_exclusive, unlock  # noqa: F401
from cairnmind.pal.path_spelling import has_alias, lies_in, path_identity, same_or_below
from cairnmind.pal.terminal_safe import safe_text
from cairnmind.scip import ingest_batch as _ingest_batch
from cairnmind.scip import ingest_graph as _ingest_graph
from cairnmind.scip import ingest_history as _ingest_history
from cairnmind.scip import ingest_walk as _ingest_walk
from cairnmind.scip.ingest_batch import (  # noqa: F401 - re-exported for callers and tests
    _KEPT_EDGE_KINDS,
    _KEPT_NODE_KINDS,
    _deactivate_source,
    _deactivate_together,
    _hanging_events,
    _source_before,
    _TooManyEvents,
    _vector_tables,
)
from cairnmind.scip.ingest_graph import (  # noqa: F401 - re-exported for callers and tests
    GRAPH_EDGE_KINDS,
    GRAPH_NODE_KINDS,
    _code_nodes,
    _graph_as_remembered,
    _graph_markers,
    _node_digest,
    _resolve_import,
    _stored_graph_is,
    _union,
)
from cairnmind.scip.ingest_history import (  # noqa: F401 - re-exported for callers and tests
    _CLAUDE_OPTIONAL_FIELDS,
    _CLAUDE_TECHNICAL_FIELDS,
    _POLLED,
    CLAUDE_TECHNICAL_RECORDS,
    CODEX_TECHNICAL_RECORDS,
    TRANSCRIPT_PARSER_REVISION,
    UNKNOWN_TRANSCRIPT_WARNING,
    _at_end,
    _claude_technical_metadata,
    _codex_technical_metadata,
    _event_cwd,
    _event_text,
    _fingerprint,
    _save_cursor,
    _source_project,
    _technical_metadata_client,
    _validate_reparse_options,
)
from cairnmind.scip.ingest_walk import (  # noqa: F401 - re-exported for callers and tests
    _BACKUP_MARKERS,
    _MANAGED_MCP_FILES,
    EXCLUDED_DIRS,
    MANAGED_FILES,
    MAX_FILE_BYTES,
    SECRET_NAMES,
    SECRET_SUFFIXES,
    _chunks,
    _file_identity,
    _files,
    _git,
    _identifier,
    _indexed_spans,
    _is_managed_mcp_config,
    _is_secret,
    _json,
    _metadata,
    _NestedMemory,
    _now,
    _roots,
    _walk_args,
    excluded_directory,
)
from cairnmind.scip.parsing import analyze_code, parser_identity
from cairnmind.storage.graph_transactions import GraphTransactions, await_vertices  # noqa: F401
from cairnmind.storage.journal import MAX_TRANSACTION_EVENTS
from cairnmind.storage.storage_query import col, db_find, db_update_where

FILES_PER_TRANSACTION = 8
EVENTS_PER_TRANSACTION = 200  # far below the number at which the journal stops naming single rows

# The tree signature of the last complete indexing done by this process, per store and project.
_INDEXED: dict[tuple[str, str], str] = {}
# Bumped whenever the index of a store is changed outside `index_project`.
_INVALIDATIONS: dict[str, int] = {}
# A signature is trusted only when every file was last touched at least this long before
# it was taken (git's "racy clean" rule): on a filesystem with coarse timestamps a second
# write of the same size inside one tick would otherwise look like no change.
RACY_WINDOW_NS = 2_000_000_000
# What the last indexing by this process learned, per store and project: the identity on disk of
# every file with what indexing derived from it, and the graph it wrote. The next indexing then
# opens only files whose identity changed and writes only graph rows that differ. It is a hint
# and never the truth: each reuse is first checked against the database, and anything that
# does not match is indexed in full.
_REMEMBERED: dict[tuple[str, str], dict] = {}


def _ingestion_lock(
    store,
    category: str,
    identity: str,
    *,
    deadline: float | None = None,
    waited: list | None = None,
):
    return _ingest_batch._ingestion_lock(
        store,
        category,
        identity,
        deadline=deadline,
        waited=waited,
        lock_exclusive_fn=lock_exclusive,
        time_mod=time,
    )


def _write_chunks(store, project_id: str, source_id: str, text: str, metadata: dict | None = None) -> int:
    return _ingest_batch._write_chunks(
        store,
        project_id,
        source_id,
        text,
        metadata,
        ingestion_lock_fn=_ingestion_lock,
        chunks_fn=_chunks,
    )


def _within_limit(tx, limit: int | None = None) -> None:
    _ingest_batch._within_limit(tx, EVENTS_PER_TRANSACTION if limit is None else limit)


def _groups(items: list, cost) -> list[list]:
    return _ingest_batch._groups(items, cost, events_per_tx=EVENTS_PER_TRANSACTION)


def _deactivate_sources(store, source_ids: list[str]) -> None:
    _ingest_batch._deactivate_sources(
        store,
        source_ids,
        files_per_tx=FILES_PER_TRANSACTION,
        events_per_tx=EVENTS_PER_TRANSACTION,
        hanging_events_fn=_hanging_events,
        deactivate_source_fn=_deactivate_source,
        within_limit_fn=_within_limit,
    )


def _write_files(store, project_id: str, batch: list[dict]) -> list:
    return _ingest_batch._write_files(
        store,
        project_id,
        batch,
        events_per_tx=EVENTS_PER_TRANSACTION,
        write_files_once_fn=_write_files_once,
        write_files_fn=_write_files,
    )


def _write_files_once(store, project_id: str, batch: list[dict], limit: int | None = None) -> list:
    return _ingest_batch._write_files_once(
        store,
        project_id,
        batch,
        EVENTS_PER_TRANSACTION if limit is None else limit,
        within_limit_fn=_within_limit,
    )


def index_pending_sources(store, limit: int = 100) -> dict:
    return _ingest_batch.index_pending_sources(store, limit, write_chunks_fn=_write_chunks)


def _tree_state(store, project_id: str, listing: dict | None = None) -> tuple[str, int, bool, list[dict]]:
    return _ingest_walk._tree_state(
        store,
        project_id,
        listing,
        files_fn=_files,
        parser_identity_fn=parser_identity,
    )


def _stored_analyses(store, source_ids: list[str]) -> dict[str, dict | None]:
    return _ingest_graph._stored_analyses(store, source_ids, parser_identity_fn=parser_identity)


def _graph_parts(project_id: str, indexed: dict[Path, dict], reuse: dict) -> tuple[dict, dict, dict]:
    return _ingest_graph._graph_parts(project_id, indexed, reuse, node_digest_fn=_node_digest)


def _build_graph(
    store,
    project_id: str,
    indexed: dict[Path, dict],
    remembered: dict | None = None,
    state: dict | None = None,
    retried: bool = False,
    links: tuple = (),
) -> dict:
    return _ingest_graph._build_graph(
        store,
        project_id,
        indexed,
        remembered,
        state,
        retried,
        links,
        parser_identity_fn=parser_identity,
        await_vertices_fn=await_vertices,
        graph_parts_fn=_graph_parts,
        stored_graph_is_fn=_stored_graph_is,
        build_graph_fn=_build_graph,
        time_mod=time,
    )


def reparse_history_sources(
    store,
    path: Path,
    project_id: str,
    client: str = "auto",
    limit: int = 1000,
    *,
    deadline: float | None = None,
) -> dict:
    return _ingest_history.reparse_history_sources(
        store,
        path,
        project_id,
        client,
        limit,
        deadline=deadline,
        ingestion_lock_fn=_ingestion_lock,
        reparse_locked_fn=_reparse_history_sources_locked,
    )


def _reparse_history_sources_locked(
    store,
    path: Path,
    project_id: str | None,
    client: str = "auto",
    limit: int = 1000,
    *,
    deadline: float | None = None,
) -> dict:
    return _ingest_history._reparse_history_sources_locked(
        store,
        path,
        project_id,
        client,
        limit,
        deadline=deadline,
        technical_metadata_client_fn=_technical_metadata_client,
        source_project_fn=_source_project,
        chunks_fn=_chunks,
        max_file_bytes=MAX_FILE_BYTES,
        time_mod=time,
    )


def import_history(store, path: Path, project_id: str | None, client: str = "auto") -> dict:
    return _ingest_history.import_history(
        store,
        path,
        project_id,
        client,
        ingestion_lock_fn=_ingestion_lock,
        import_locked_fn=_import_history_locked,
    )


def _import_history_locked(store, path: Path, project_id: str | None, client: str = "auto") -> dict:
    return _ingest_history._import_history_locked(
        store,
        path,
        project_id,
        client,
        import_history_fn=import_history,
        ingest_masker_fn=ingest_masker,
        reparse_locked_fn=_reparse_history_sources_locked,
        source_project_fn=_source_project,
        event_text_fn=_event_text,
        write_chunks_fn=_write_chunks,
        save_cursor_fn=_save_cursor,
    )


def poll_imports(store) -> dict:
    return _ingest_history.poll_imports(
        store,
        polled=_POLLED,
        ingest_masker_fn=ingest_masker,
        import_history_fn=import_history,
    )


def register_default_histories(store, project_id: str, root: Path) -> dict:
    return _ingest_history.register_default_histories(
        store,
        project_id,
        root,
        import_history_fn=import_history,
    )


def tree_signature(store, project_id: str) -> str:
    """What indexing would read, described without opening the files themselves.

    Every file the index would visit contributes its path, size, modification time and
    inode change time. Writing a file changes its change time even when a tool restores
    the modification time. `index_project` records a signature only when it can be trusted;
    see `RACY_WINDOW_NS`.
    """
    return _tree_state(store, project_id)[0]


def forget_index_signature(store, project_id: str | None = None) -> None:
    """Make the next `index_project_if_changed` index again, for one project or for all.

    Call it after changing code or document sources outside `index_project`. An indexing
    already in flight does not record its signature either.
    """
    _INVALIDATIONS[str(store.data_dir)] = _INVALIDATIONS.get(str(store.data_dir), 0) + 1
    for remembered in (_INDEXED, _REMEMBERED):
        # A snapshot of the keys: indexing in another thread adds and removes entries meanwhile.
        for key in [key for key in list(remembered) if key[0] == str(store.data_dir) and project_id in (None, key[1])]:
            remembered.pop(key, None)


def forget_missing_projects(store, project_ids) -> None:
    """Drop what is remembered about projects of this store that no longer exist.

    A project can be deleted by another process, which cannot reach this one's memory.
    """
    live = set(project_ids)
    for remembered in (_INDEXED, _REMEMBERED):
        for key in [key for key in list(remembered) if key[0] == str(store.data_dir) and key[1] not in live]:
            remembered.pop(key, None)


def index_project(store, project_id: str, walk: tuple | None = None) -> dict:
    """Snapshot changed files, update chunks, and bring the derived code graph up to date.

    Files that this process indexed before and whose identity on disk is unchanged are not
    opened again, and only graph rows that differ are written. The result is the index a
    run that reads every file would produce; the first run of a process is such a run.
    `walk` is the walk that `index_project_if_changed` made just before: the invalidations and the time
    taken before it, and what it found. It is used only when the lock was free: after waiting for another
    indexing, the sources it read are newer than the walk, and a file created meanwhile would be seen as deleted (#409).
    """
    project_id = store.resolve_project(project_id)["id"]
    key = (str(store.data_dir), project_id)
    waited: list[bool] = []
    with _ingestion_lock(store, "project", project_id, waited=waited):
        if waited:
            walk = None
        # No signature while indexing runs or after it raises: a concurrent or later caller
        # then waits for the lock and indexes, instead of trusting a half-written index.
        _INDEXED.pop(key, None)
        previous = _REMEMBERED.pop(key, None)
        if walk is None:
            invalidations = _INVALIDATIONS.get(key[0], 0)
            # Taken before any file is read: a file edited during indexing then differs next time.
            started, listing = time.time_ns(), {}
            tree = _tree_state(store, project_id, listing)
        else:
            invalidations, started, tree, listing = walk
        signature, newest, complete, _ = tree
        state: dict = {}
        result = _index_project_locked(store, project_id, previous, state, started, listing)
        if _INVALIDATIONS.get(key[0], 0) == invalidations:
            _REMEMBERED[key] = state
            # A folder that is not here is no reason to index again: its sentence is only repeated (#371).
            if (len(result.get("errors", [])) == len(result.get("unavailable", [])) + bool(state.get("graph_kept")) and complete
                    and newest < started - RACY_WINDOW_NS):
                _INDEXED[key] = signature
        return result


def index_project_if_changed(store, project_id: str) -> dict:
    """Index only when the tree differs from the one this process last indexed completely.

    An unchanged tree costs one directory walk with a `stat` per file. Apart from the
    `.gitignore` files that steer the walk, no file is opened, no source is looked up and
    the graph is not rebuilt. Files touched within `RACY_WINDOW_NS` of an indexing are
    indexed once more, because their timestamps cannot yet prove that nothing changed.
    """
    project_id = store.resolve_project(project_id)["id"]
    recorded = _INDEXED.get((str(store.data_dir), project_id))
    walk = None
    if recorded is not None:
        invalidations, started, listing = _INVALIDATIONS.get(str(store.data_dir), 0), time.time_ns(), {}
        tree = _tree_state(store, project_id, listing)
        signature, _, complete, away = tree
        walk = invalidations, started, tree, listing
        if complete and signature == recorded:
            skipped = {"files": 0, "changed": 0, "unchanged": 0, "deleted": 0, "chunks": 0, "errors": [], "skipped": True}
            if away:
                project = store.resolve_project(project_id)
                skipped["errors"] = [project_folders.message(project, folder) for folder in away]
                skipped["unavailable"] = [project_folders.entry(project, folder) for folder in away]
                kept = (_REMEMBERED.get((str(store.data_dir), project_id)) or {}).get("graph_kept")
                if kept:
                    skipped["errors"].append(kept)
            return skipped
    return index_project(store, project_id, walk)


def _index_project_locked(store, project_id: str, previous: dict | None = None, state: dict | None = None,
                          started: int | None = None, listing: dict | None = None) -> dict:
    """Index under the project lock. `previous` is what an earlier run remembered; `state` receives this run's.
    `listing` is the walk that was made for the signature: the folders it found are not walked again (#409)."""
    project = store.resolve_project(project_id)
    project_id = project["id"]
    counts: dict[str, Any] = {"files": 0, "changed": 0, "unchanged": 0, "deleted": 0, "chunks": 0, "errors": [],
                              "read": 0}
    indexed: dict[Path, dict] = {}
    walk_errors: list[OSError] = list((listing or {}).get("errors", []))
    links: list[tuple[str, str]] = list((listing or {}).get("links", []))
    folders = project_folders.folder_states(store, project)
    registered_roots = [Path(folder["root"]) for folder in folders]
    away = {folder["root"] for folder in folders if folder["state"] != "available"}
    walked: list[str] = []  # rows of folders without a machine that this run reads, stamped when it was complete
    started = time.time_ns() if started is None else started
    known = previous["files"] if previous and previous.get("parser") == parser_identity() else {}
    files: dict[str, dict] = {}
    # Read before the walk: a remembered file is reused only while the database still holds
    # the very source that was remembered for it. Sources written below are all in `seen`.
    every = db_find(
        store,
        "sources",
        {"project_id": project_id, "is_current": True},
        columns=("id", "uri", "kind", "sha256", col("metadata.path", "path"), col("metadata.root", "root")),
    )
    rows = [row for row in every if row.get("kind") in ("code", "document")]
    current = {row["uri"]: row["id"] for row in rows}
    # A path with no current source of any kind needs no lookup of its own, and its source, chunks
    # and jobs can be written together with those of its neighbours. So does a path whose current
    # source has other content: it is replaced in a group, which checks that this source is still current.
    present = {row["uri"]: (row["id"], row.get("sha256")) for row in every}
    waiting: list[dict] = []

    def finish(path, root, name, identity, settled, source, analysis, chunks: int, extract: bool):
        counts["chunks"] += chunks
        resolved = path.resolve()
        # Only what the graph needs is kept; the content stays in the database.
        entry = {"source": {"id": source["id"], "uri": source["uri"], "kind": source["kind"]}, "analysis": analysis}
        indexed[resolved] = {**entry, "root": root}
        # The path has a current source from here on: reached again through another root, it is looked up.
        present[name] = (source["id"], source.get("sha256"))
        # Idempotent enqueue also repairs a crash between source write and
        # queue write. Code facts themselves are already represented by syntax.
        if extract:
            store.enqueue("extract", project_id, {"source_id": source["id"]}, f"extract:{source['id']}")
        # Remembered only after its chunks and jobs are written, so a reuse never skips a repair.
        if settled:
            files[name] = {"identity": identity, "root": root, "resolved": resolved, "entry": entry}

    def one_by_one(item):
        source = store.snapshot(project_id, item["uri"], item["content"], kind=item["kind"], metadata=item["metadata"])
        written = _write_chunks(store, project_id, source["id"], item["content"], item["chunk_metadata"])
        path, root, name, identity, settled = item["place"]
        finish(path, root, name, identity, settled, source, item["analysis"], written, item["extract"])

    vectors: list[int] = []  # how many vector tables there are, read once, when a replacement first needs it

    def write_waiting():
        """Everything is written, and remembered, in the order in which the files were walked."""
        batch = waiting[:]
        del waiting[:]
        replaced = [item["replaces"] for item in batch if item.get("replaces")]
        if replaced and not vectors:
            vectors.append(_vector_tables(store))
        # What the old sources leave behind is counted before the write, so a group fits on its first attempt.
        weights = _hanging_events(store, replaced, vectors[0]) if replaced else {}

        def cost(item):
            return item["cost"] + weights.get(item.get("replaces"), 1) - 1  # the old source is in both
        for group in _groups(batch, cost):
            if cost(group[0]) > MAX_TRANSACTION_EVENTS:
                one_by_one(group[0])  # too large even alone: as before, so its events stay named where they were
                continue
            while group:
                written = _write_files(store, project_id, group)
                for item, source in zip(group, written, strict=False):
                    if source is None:
                        one_by_one(item)  # another writer was first, or the file is too large to share: as before
                    else:
                        path, root, name, identity, settled = item["place"]
                        finish(path, root, name, identity, settled, source, item["analysis"], len(item["chunks"]), False)
                group = group[len(written):]  # what a group too large to commit left over

    for folder in folders:
        root = Path(folder["root"])
        if folder["state"] != "available":
            counts["errors"].append(project_folders.message(project, folder))
            continue
        if lies_in(root, store.data_dir):
            # Registered by 0.12.0, before #295 refused it: the walk leaves the data directory out, so it
            # would report 0 files. Named, not removed.
            from cairnmind.pal.process_control import quote_arg
            quoted = quote_arg(safe_text(project["name"]))
            head = (f"Project {project['name']!r} has a folder inside the data directory, which Cairnmind never "
                    f"indexes: {root}; this registration is left as it is. Move the files to a folder outside it, ")
            counts["errors"].append(head + (
                f"then `cairnmind project remove {quoted} --yes` unregisters this one and keeps its memory (this folder cannot be added again), and "
                f"`cairnmind project add {quoted} NEWFOLDER` registers the new folder as a new project"
                if root == registered_roots[0] else
                f"add that with `cairnmind project root {quoted} NEWFOLDER`, and remove this one with "
                f"`cairnmind project remove {quoted} --root {quote_arg(safe_text(str(root)))} --yes`"))
            continue
        commit = _git(root, "rev-parse", "HEAD") or None
        if folder["adopt"]:
            walked.append(folder["row_id"])
        walked_here = (listing or {}).get("files", {}).get(folder["root"])
        for path in walked_here if walked_here is not None else _files(root, *_walk_args(store, root, walk_errors, links)):
            name = str(path)
            try:
                # Before the file is opened: an edit made while it is read then shows as a new identity.
                status = path.stat()
            except OSError as exc:
                counts["errors"].append(f"{path}: {exc}")
                continue
            from cairnmind.pal.private_files import change_time_ns
            ctime_ns = change_time_ns(path, status)
            identity = _file_identity(status, ctime_ns)
            # A file touched this recently can change again without its times moving (see RACY_WINDOW_NS).
            settled = max(status.st_mtime_ns, ctime_ns) < started - RACY_WINDOW_NS
            remembered = known.get(name)
            if remembered and remembered["identity"] == identity and remembered["root"] == root:
                entry = remembered["entry"]
                if entry is None:  # not indexable when it was read: too large, binary or not UTF-8
                    files[name] = remembered
                    continue
                if current.get(name) == entry["source"]["id"]:
                    write_waiting()
                    files[name] = remembered
                    counts["files"] += 1
                    counts["unchanged"] += 1
                    indexed[remembered["resolved"]] = {**entry, "root": root}
                    continue
            try:
                if status.st_size > MAX_FILE_BYTES or not path.is_file():
                    raw = None
                else:
                    counts["read"] += 1
                    raw = path.read_bytes()
                content = None if raw is None or b"\x00" in raw else raw.decode("utf-8")
            except UnicodeDecodeError:
                content = None
            except OSError as exc:
                counts["errors"].append(f"{path}: {exc}")
                continue
            if content is None:
                if settled:
                    files[name] = {"identity": identity, "root": root, "entry": None}
                continue
            counts["files"] += 1
            digest = hashlib.sha256(content.encode()).hexdigest()
            before, replaces = None, None
            if name in present:
                known_id, known_digest = present[name]
                if known_digest is not None and known_digest != digest:
                    replaces = known_id
                else:
                    write_waiting()
                    before = _source_before(store, project_id, name)
                    if before and before["sha256"] != digest:
                        replaces = before["id"]
            if before and before["sha256"] == digest:
                source = before
                metadata = _metadata(source["metadata"])
                stored_version = metadata.get("analysis_version")
                analysis = metadata.get("analysis") if (
                    stored_version == parser_identity()
                    or (isinstance(stored_version, str) and stored_version.startswith("scip-v1:"))
                ) else None
                if not analysis:
                    analysis = analyze_code(path, content)
                    db_update_where(
                        store,
                        "sources",
                        set_fields={"metadata.analysis": analysis, "metadata.analysis_version": parser_identity()},
                        where={"id": source["id"]},
                    )
                counts["unchanged"] += 1
            else:
                analysis = analyze_code(path, content)
                metadata = {
                    "path": str(path), "root": str(root), "relative_path": path.relative_to(root).as_posix(),
                    "git_commit": commit, "content_hash": digest, "analysis": analysis,
                    "analysis_version": parser_identity(), "language": analysis["language"], "snapshot": "working_tree",
                }
                counts["changed"] += 1
                item = {"uri": str(path), "content": content, "metadata": metadata, "analysis": analysis,
                        "kind": "code" if analysis["language"] != "text" else "document",
                        "chunk_metadata": {"language": analysis["language"]},
                        "extract": path.suffix.lower() in {".md", ".mdx", ".rst", ".txt"},
                        "place": (path, root, name, identity, settled)}
                if name in present and replaces is None:
                    write_waiting()
                    one_by_one(item)  # its source vanished since the walk began: the ordinary path decides
                    continue
                item["replaces"] = replaces
                item["chunks"] = list(_chunks(content, name))
                # Journal events known from the file: source, chunks, their jobs, an extraction job and the old
                # source. What hangs on the old source is counted before the group is written (write_waiting).
                cost = 2 + 2 * len(item["chunks"]) + (1 if replaces else 0)
                if cost > EVENTS_PER_TRANSACTION:
                    # Too large to share a transaction: written as before, each part in its own.
                    write_waiting()
                    one_by_one(item)
                    continue
                if sum(entry["cost"] for entry in waiting) + cost > EVENTS_PER_TRANSACTION:
                    write_waiting()
                item["cost"] = cost
                waiting.append(item)
                if len(waiting) >= FILES_PER_TRANSACTION:
                    write_waiting()
                continue
            written = _write_chunks(store, project_id, source["id"], content, {"language": analysis["language"]})
            finish(path, root, name, identity, settled, source, analysis, written,
                   path.suffix.lower() in {".md", ".mdx", ".rst", ".txt"})
        write_waiting()

    counts["errors"].extend(f"Cannot list {error.filename}: {error.strerror or error}" for error in walk_errors)
    seen = {str(p) for p in indexed}
    # A directory that could not be listed says nothing about its files: do not deactivate anything.
    deleted = []
    for row in rows if not walk_errors else []:
        path = row.get("path")
        if path and path not in seen:
            # Do not call a temporarily unavailable root a project-wide deletion.
            # Roots are compared as folder_states resolves them: a path through a symlink spells differently.
            root = Path(row.get("root") or project["root"])
            if str(root.resolve()) not in away and root.is_dir():
                deleted.append(row["id"])
    _deactivate_sources(store, deleted)
    counts["deleted"] += len(deleted)
    # The graph follows every current file source of the registered folders, not only the files this run read:
    # a folder that is away or a subfolder that could not be listed keeps its files in the graph, from the
    # analysis stored with their source (#371). A file whose read failed is deactivated above, as before.
    registered = {str(root) for root in registered_roots}
    gone = set(deleted)
    kept = [row for row in rows if row.get("path") and row["path"] not in seen and row["id"] not in gone
            and str(Path(row.get("root") or project["root"]).resolve()) in registered]
    analyses = _stored_analyses(store, [row["id"] for row in kept])
    graph: dict = {}
    if any(analyses[row["id"]] is None for row in kept):
        # A file that was not read here and whose stored analysis is of another parser version cannot be
        # analysed again: rebuilding would drop it, so the graph stays as it is and the result says so.
        stuck = [row for row in kept if analyses[row["id"]] is None]
        counts["errors"].append(project_folders.GRAPH_KEPT.format(project=safe_text(project["name"]), count=len(stuck)))
        if state is not None and all(str(Path(row.get("root") or project["root"]).resolve()) in away for row in stuck):
            # Only folders that are away hold them: nothing changes until the signature does (a folder comes
            # back or this machine's parser changes), so the next idle round may skip and repeat the sentence.
            state["graph_kept"] = counts["errors"][-1]
        counts.update(_graph_markers(store, project_id))
    else:
        for row in kept:
            indexed[Path(row["path"])] = {
                "source": {"id": row["id"], "uri": row["uri"], "kind": row["kind"]}, "analysis": analyses[row["id"]],
                "root": Path(row.get("root") or project["root"])}
        counts.update(_build_graph(store, project_id, indexed, previous.get("graph") if previous else None, graph,
                                    False, tuple(links)))
    if away:
        counts["unavailable"] = [project_folders.entry(project, folder) for folder in folders
                                 if folder["root"] in away]
    if not walk_errors and not away:
        project_folders.adopt(store, walked)
    from cairnmind.memory.code_decisions import ingest_project_decisions
    available_roots = [Path(folder["root"]) for folder in folders if folder["state"] == "available"]
    decisions_result = ingest_project_decisions(store, project_id, indexed, roots=available_roots)
    if decisions_result.get("source") != "off":
        counts["code_decisions"] = decisions_result
    if state is not None:
        state.update(parser=parser_identity(), files=files, graph=graph)
    return counts


def current_source(store, source_id: str) -> dict:
    """Refresh code/document evidence against disk; never read outside registered roots."""
    source = store.get_source(source_id)
    metadata = _metadata(source.get("metadata"))
    if source["kind"] not in {"code", "document"} or not metadata.get("path"):
        return source
    project = store.resolve_project(source["project_id"])
    folders = project_folders.folder_states(store, project)  # the registered folders, as _roots lists them
    roots = [Path(folder["root"]) for folder in folders]
    path = Path(os.path.normpath(metadata["path"]))
    bindings = memory_bindings(store, project["id"])
    local = bound_metadata(store, project["id"], metadata, bindings=bindings)
    mapped_path = Path(local["path"])
    imported = source_id in imported_source_ids(store, project["id"])
    if imported:
        # A saved foreign URI is provenance, never a request to read that host
        # path. Only the actual local index can establish a current file source.
        latest = _source_before(store, project["id"], str(mapped_path)) if mapped_path != path or any(
            path.is_relative_to(origin) for origin, _ in bindings) else None
        if latest and latest["id"] != source_id:
            return current_source(store, latest["id"])
        return {**source, "is_current": False,
                "freshness_warning": "Imported file snapshot is historical until the local checkout is indexed"}
    # A cited file under a folder this machine cannot read, or that belongs to another machine, is neither
    # deactivated nor indexed from a same-named path here (#371); the same sentence as a missing root.
    if (any(f["state"] != "available" and path.is_relative_to(f["root"]) for f in folders)
            and not any(f["state"] == "available" and path.is_relative_to(f["root"]) for f in folders)):
        return {**source, "freshness_warning": "Project root is unavailable; the current file cannot be checked"}
    allowed_roots = [root for root in roots if path.is_absolute() and path.is_relative_to(root)
                     and path.resolve().is_relative_to(root)]
    in_excluded = any(excluded_directory(part) for root in allowed_roots for part in path.relative_to(root).parts[:-1])
    # A short 8.3 alias (`SSH~1`) and a data directory spelled with other letter case are not seen by the
    # name checks; they are refused by spelling and by identity (#230).
    if (path.is_symlink() or not allowed_roots or _is_secret(path) or in_excluded
            or any(has_alias(path, root) for root in allowed_roots)
            or any(is_memory_path(path, root) for root in roots)
            or any(_NestedMemory(store, root).covers(path) for root in allowed_roots)
            or path.resolve().is_relative_to(store.data_dir)
            or same_or_below(path.resolve(), path_identity(store.data_dir))):
        raise ValueError("Source path is outside registered roots or excluded")
    if not path.is_file():
        if not any(root.is_dir() for root in allowed_roots):
            # As in index_project: an unavailable root is not a deletion of its files.
            return {**source, "freshness_warning": "Project root is unavailable; the current file cannot be checked"}
        active = db_find(
            store,
            "sources",
            {"project_id": project["id"], "uri": source["uri"], "is_current": True},
            columns=("id",),
        )
        for row in active:
            _deactivate_source(store, row["id"])
        # The index changed although no file's size or times did: if the file comes back
        # untouched (a parent directory renamed away and back), it must be indexed again.
        forget_index_signature(store, project["id"])
        return {**source, "is_current": False, "deleted": True}
    if path.stat().st_size > MAX_FILE_BYTES:
        return {**source, "freshness_warning": "Current file exceeds indexing size limit"}
    try:
        # Preserve BOM and CRLF exactly as archived by index_project.
        content = path.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return {**source, "freshness_warning": str(exc)}
    digest = hashlib.sha256(content.encode()).hexdigest()
    latest = _source_before(store, project["id"], source["uri"])
    if latest and latest["sha256"] == digest:
        return latest
    index_project(store, project["id"])
    return _source_before(store, project["id"], source["uri"]) or {**source, "is_current": False, "freshness_warning": "Source no longer indexed"}
