"""ArcadeDB source of truth, native graph, and verified portable backups."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

from .config import BACKUP_FORMATS, BACKUP_SUFFIXES, Settings
from .sql import (IDENTIFIER, PROFILE_NAME, clean_row, evidence_time, insert_document, json_text, one_row, parse_utc,
                  quote_name, update_document, utc_now)

SCHEMA_VERSION = 4
# Indexes for lookups by a field other than id and project_id. They are not part of the schema version:
# an earlier version works with them unchanged, so adding one needs no migration and blocks no way back.
LOOKUP_INDEXES = (("chunks", "source_id"), ("nodes", "source_id"), ("edges", "source_id"), ("edges", "from_id"),
                  ("edges", "to_id"), ("embeddings", "source_id"), ("embeddings", "chunk_id"), ("sources", "uri"),
                  ("record_revisions", "record_id"), ("check_runs", "task_id"), ("session_events", "session_id"),
                  ("resume_packets", "session_id"))
LOOKUP_INDEX_SET = 1
KINDS = {"decision", "task", "fact", "constraint", "question", "preference", "checkpoint"}
STATUSES = {"proposed", "confirmed", "disputed", "superseded"}
TASK_STATES = {"todo", "in_progress", "needs_test", "needs_review", "done", "blocked"}
LEGACY_DOCUMENT_TYPES = ("projects", "project_roots", "sources", "chunks", "records", "record_revisions", "jobs",
                         "embeddings", "import_cursors", "ingestion_inbox", "vector_indexes")
V03_DOCUMENT_TYPES = ("plans", "plan_revisions", "plan_requests", "worktree_states", "sessions",
                      "resume_packets", "resume_acks", "session_events", "check_runs")
V04_DOCUMENT_TYPES = ("memory_settings", "memory_snapshots", "memory_imports", "memory_bindings")
V05_DOCUMENT_TYPES = ("journal_state", "journal_events")
DOCUMENT_TYPES = (*LEGACY_DOCUMENT_TYPES, *V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES)
LEGACY_EXPORT_TYPES = (*LEGACY_DOCUMENT_TYPES, "nodes", "edges")
V03_EXPORT_TYPES = (*LEGACY_DOCUMENT_TYPES, *V03_DOCUMENT_TYPES, "nodes", "edges")
V04_EXPORT_TYPES = (*LEGACY_DOCUMENT_TYPES, *V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES, "nodes", "edges")
EXPORT_TYPES = (*DOCUMENT_TYPES, "nodes", "edges")
MANAGED_DEFINITION_METADATA_FIELDS = frozenset({
    "plan_ref", "step_key", "required_profiles", "plan_context_hash", "depends_on", "acceptance_criteria",
    "requires_test", "requires_review", "step_index", "plan_archived", "criterion_checks", "check_proposals", "scope",
})
# Definition fields that say nothing about whether a task was proven: changing one keeps reported evidence.
ADVISORY_DEFINITION_FIELDS = frozenset({"check_proposals", "scope"})


def validate_scope(scope) -> None:
    """The files a task says it will touch: path patterns as in an ignore file, relative to the worktree."""
    if (not isinstance(scope, list) or len(scope) > 64
            or not all(isinstance(pattern, str) and pattern.strip() and len(pattern) <= 500 for pattern in scope)):
        raise ValueError("scope must be a list of at most 64 nonempty path patterns")
    for pattern in scope:
        parts = pattern.replace("\\", "/").split("/")
        if pattern.startswith(("/", "!", "#", "./")) or "\\" in pattern or "\0" in pattern or "\n" in pattern \
                or ".." in parts or ".git" in parts or re.match(r"^[A-Za-z]:", pattern):
            raise ValueError("scope patterns must stay inside the worktree, cannot negate and cannot start with # or ./")
    try:
        import pathspec
        pathspec.GitIgnoreSpec.from_lines(scope)
    except Exception as exc:  # noqa: BLE001 - the library raises more than one kind of error for a bad pattern
        raise ValueError(f"a scope pattern cannot be read: {str(exc)[:200]}") from None


def valid_argv(argv) -> bool:
    """A command that may be proposed for a check: a short list of non-empty strings."""
    return (isinstance(argv, list) and 0 < len(argv) <= 64
            and all(isinstance(part, str) and part and "\0" not in part and len(part) <= 2000 for part in argv))


def validate_criterion_links(metadata: dict, criteria: list) -> None:
    """The stored form of the links: every link belongs to a criterion, every proposal to a linked check."""
    links = metadata.get("criterion_checks", {})
    if (not isinstance(links, dict) or ("criterion_checks" in metadata and not links)
            or not all(text in criteria and isinstance(check, str) and PROFILE_NAME.fullmatch(check)
                       for text, check in links.items())):
        raise ValueError("criterion_checks must link acceptance criteria to check profile names")
    proposals = metadata.get("check_proposals", {})
    if (not isinstance(proposals, dict) or ("check_proposals" in metadata and not proposals)
            or not all(name in links.values() and valid_argv(argv) for name, argv in proposals.items())):
        raise ValueError("check_proposals must give an argument list for a linked check")


def normalise_criteria(metadata: dict) -> None:
    """Accept a criterion written as {text, check, argv} and store the link beside the text.

    `acceptance_criteria` stays a list of strings, which every version reads. The link from a
    criterion to the check profile that proves it lives in `criterion_checks` (text -> profile),
    and a command proposed for a profile that is not declared yet in `check_proposals`
    (profile -> argument list). A proposal is text for a person to review: nothing runs it.
    """
    criteria = metadata.get("acceptance_criteria")
    links, proposals = metadata.get("criterion_checks", {}), metadata.get("check_proposals", {})
    if not isinstance(criteria, list) or not isinstance(links, dict) or not isinstance(proposals, dict):
        return  # left as it is for the validation that follows to refuse
    links, proposals = dict(links), dict(proposals)
    if any(isinstance(item, dict) for item in criteria):
        texts = []
        for item in criteria:
            if isinstance(item, str):
                texts.append(item)
                continue
            if not isinstance(item, dict) or set(item) - {"text", "check", "argv"} or not isinstance(item.get("text"), str):
                raise ValueError("A criterion is text, or an object with text and optionally check and argv")
            text, check, argv = item["text"], item.get("check"), item.get("argv")
            texts.append(text)
            if check is None:
                if argv is not None:
                    raise ValueError("A criterion can propose a command only for a named check")
                continue
            if not isinstance(check, str) or not PROFILE_NAME.fullmatch(check):
                raise ValueError("criterion_checks must link acceptance criteria to check profile names")
            if links.get(text, check) != check:
                raise ValueError("One criterion cannot be linked to two checks")
            links[text] = check
            if argv is not None:
                if proposals.get(check, argv) != argv:
                    raise ValueError("Two different commands were proposed for one check")
                proposals[check] = argv
        metadata["acceptance_criteria"] = texts
    # A link or a proposal whose criterion or check is gone has nothing left to describe.
    links = {text: check for text, check in links.items() if text in metadata["acceptance_criteria"]}
    proposals = {name: argv for name, argv in proposals.items() if name in links.values()}
    for key, value in (("criterion_checks", links), ("check_proposals", proposals)):
        if value:
            metadata[key] = value
        else:
            metadata.pop(key, None)


_DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_JOB_UNSET = object()
JOB_STATES = {"pending", "running", "completed", "failed"}


class JobNotFoundError(ValueError):
    """No job is visible in the requested project."""


def validate_task_state(value):
    """Validate a supplied task state without coercing or inferring a value."""
    if not isinstance(value, str) or value not in TASK_STATES:
        raise ValueError(f"Task state must be one of: {', '.join(sorted(TASK_STATES))}")


def job_diagnostics(value):
    """Retain bounded operational codes/counts, never model/source text."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("stage", "code", "repair_outcome", "failure_reason", "persistence_stage"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _DIAGNOSTIC_CODE.fullmatch(candidate):
            result[key] = candidate
    if type(value.get("repair_attempted")) is bool:
        result["repair_attempted"] = value["repair_attempted"]
    codes = value.get("validation_codes")
    if isinstance(codes, list):
        result["validation_codes"] = [code for code in codes[:8]
                                      if isinstance(code, str) and _DIAGNOSTIC_CODE.fullmatch(code)]
    for group, keys in (("budget", ("timeout_seconds", "max_steps", "context_limit_chars", "elapsed_seconds")),
                        ("counts", ("candidate_records", "accepted_records", "skipped_records", "written_records"))):
        original = value.get(group)
        if isinstance(original, dict):
            result[group] = {key: original[key] for key in keys if type(original.get(key)) in (int, float)
                             and 0 <= original[key] <= 10**15 and math.isfinite(original[key])}
    rejections = value.get("rejection_counts")
    if isinstance(rejections, dict):
        result["rejection_counts"] = {key: rejections[key] for key in (
            "blank_content", "blank_evidence", "quote_absent_source", "reserved_metadata")
            if type(rejections.get(key)) is int and 0 <= rejections[key] <= 10**15}
    calls = value.get("model_calls")
    if isinstance(calls, list):
        result["model_calls"] = []
        for call in calls[-2:]:
            if not isinstance(call, dict):
                continue
            item = {key: call[key] for key in ("phase", "done_reason")
                    if isinstance(call.get(key), str) and _DIAGNOSTIC_CODE.fullmatch(call[key])}
            item.update({key: call[key] for key in ("prompt_eval_count", "eval_count", "total_duration")
                         if type(call.get(key)) is int and 0 <= call[key] <= 10**18})
            result["model_calls"].append(item)
    return result


# Where a write came from, set by the service from the session row and never by a client: today only
# for an external client's session, whose conversation is unarchived and whose evidence is degraded.
ORIGIN_METADATA = ("origin_client", "origin_session_id", "origin_history", "origin_evidence")


# Record metadata derived by the store; values supplied by a client are never kept.
DERIVED_RECORD_METADATA = ("evidence_earliest", "evidence_latest", "evidence_basis",
                           "supersede_warning", "supersede_replaced_evidence_latest", *ORIGIN_METADATA)


# Not part of what a plan step says: plan-level or transient fields and everything the store derives.
_UNCOMPARED_METADATA = frozenset({"plan_context_hash", "plan_archived", "worktree", *DERIVED_RECORD_METADATA})


def outcome_changed(old, content, key, metadata) -> bool:
    """Whether a write stores a record that says something else than the stored version.

    Compares the result, not the request: a field the write drops as a consequence counts as well.
    """
    if not old:
        return True
    before = {name: value for name, value in (old.get("metadata") or {}).items() if name not in _UNCOMPARED_METADATA}
    after = {name: value for name, value in metadata.items() if name not in _UNCOMPARED_METADATA}
    return content != old["content"] or key != old["key"] or before != after


def degraded_origin(metadata) -> bool:
    """Whether a record, source or plan was written by an external session (never verified)."""
    return isinstance(metadata, dict) and metadata.get("origin_evidence") == "degraded"


# Bookkeeping of conflict decisions. Only the conflict inbox, acting for a person, may
# write these; from any other writer they are dropped, like derived metadata.
CONFLICT_REVIEW_METADATA = ("conflicts_resolved", "conflict_decisions", "conflict_scope_note",
                            "superseded_ids", "superseded_by")


# Journal entity types that do not move the answer cache stamp. Jobs, sessions, imports and
# snapshots cannot alter an answer. The derived graph is rebuilt, and reported as reset, on
# every indexing even when it comes out identical; a real change of it is seen through the
# project row, whose graph fingerprint changes, and through the sources and records behind it.
CACHE_STAMP_IGNORED = frozenset({"job", "session", "import", "inbox", "memory", "plan_request",
                                 "graph", "node", "edge"})


class Store:
    # True only for a store from open_readonly, which starts, creates and writes nothing.
    read_only = False

    @classmethod
    def open_readonly(cls, settings: Settings) -> Store:
        """Read the existing store. Raises StoreNotReady, naming the command to run, instead of preparing it."""
        from .store_readonly import open_readonly
        return open_readonly(cls, settings)

    def read_consistently(self, read, kinds):
        """The result of ``read()``, which returns ``(project_id, result)`` and reads the journal entity ``kinds``.

        A read-only store repeats it while a change of those kinds in that project landed during it
        (else StoreBusy). A store opened with Store(path) reads behind the writer lock where it needs to,
        so ``read`` runs once.
        """
        if not self.read_only:
            return read()[1]
        from .store_readonly import read_consistently
        return read_consistently(self, read, kinds)

    def __init__(self, data_dir: Path, *, settings: Settings | None = None):
        from .arcade import ArcadeClient
        from .engine import ensure_engine
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if settings is not None and settings.data_dir.expanduser().resolve() != self.data_dir:
            raise ValueError("Provided settings must belong to this data directory")
        self.settings = settings if settings is not None else Settings.load(self.data_dir, create=True)
        ensure_engine(self.settings)
        self.client = ArcadeClient(self.settings)
        self.client.ensure_database()
        self._local = threading.local()
        self._stamp_lock = threading.Lock()
        self._stamp_cursor, self._stamp_base, self._stamp_global, self._stamp_seq = None, 0, 0, {}
        self.init()

    @contextmanager
    def _lock(self):
        with (self.data_dir / "database-writer.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def query(self, sql, params=None):
        return (getattr(self._local, "transaction", None) or self.client).query(sql, params)

    def command(self, sql, params=None):
        from .journal import mutation
        if self.read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        tx = getattr(self._local, "transaction", None)
        if tx:
            return tx.command(sql, params)
        if mutation(sql):
            with self.transaction() as tx:
                return tx.command(sql, params)
        with self._lock():
            return self.client.command(sql, params)

    @contextmanager
    def transaction(self):
        active = getattr(self._local, "transaction", None)
        if active:
            yield active
            return
        if self.read_only:
            # No writer lock and no journal, and the server transaction is rolled back. Not a snapshot:
            # a read of several queries is made consistent by read_consistently, not by this transaction.
            with self.client.transaction() as reader:
                self._local.transaction = reader
                try:
                    yield reader
                finally:
                    del self._local.transaction
            return
        from .journal import JournalTransaction
        with self._lock(), self.client.transaction() as native:
            tx = JournalTransaction(native)
            self._local.transaction = tx
            self._local.after_commit = pending = []
            try:
                yield tx
                tx.flush()
            finally:
                del self._local.transaction
                del self._local.after_commit
        # Reached only when the transaction committed; the writer lock is no longer held.
        for call in pending:
            call()

    @contextmanager
    def write_origin(self, mark):
        """Stamp every record and source this thread writes inside the block with a session mark.

        The mark comes from the service's session row, never from the caller's payload. A record keeps
        it on every later write by anyone: its content may still be what the external session wrote.
        """
        previous = getattr(self._local, "write_origin", None)
        self._local.write_origin = {key: mark[key] for key in ORIGIN_METADATA} if mark else None
        try:
            yield
        finally:
            self._local.write_origin = previous

    def current_origin(self):
        return getattr(self._local, "write_origin", None)

    def init(self):
        with self._lock():
            self.client.command("CREATE DOCUMENT TYPE devbrain_meta IF NOT EXISTS")
            meta = {row.get("name"): row.get("value") for row in self.client.query(
                "SELECT FROM devbrain_meta WHERE name IN ['schema_version','lookup_indexes']")}
            version, indexed = meta.get("schema_version"), meta.get("lookup_indexes")
            if version is not None:
                if version not in {1, 2, 3, SCHEMA_VERSION}:
                    raise ValueError("Database schema version is incompatible")
                if version == SCHEMA_VERSION:
                    if indexed != LOOKUP_INDEX_SET:
                        self._lookup_indexes(indexed)
                    return
                # Snapshot only existing types. Hold the writer lock through
                # backup, additive DDL, and the version-last commit.
                with self.client.transaction() as tx:
                    payload = self._export_snapshot(tx, None,
                        LEGACY_EXPORT_TYPES if version == 1 else V03_EXPORT_TYPES if version == 2 else V04_EXPORT_TYPES,
                        version + 1)
                destination = self.data_dir / "backups" / f"before-schema-{version}-to-{SCHEMA_VERSION}-{uuid4().hex}{BACKUP_SUFFIXES[0]}"
                self._write_backup(destination, payload)
            collections = ((*V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES) if version == 1
                           else (*V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES) if version == 2
                           else V05_DOCUMENT_TYPES if version == 3 else DOCUMENT_TYPES)
            exports = collections if version is not None else EXPORT_TYPES
            for collection in collections:
                self.client.command(f"CREATE DOCUMENT TYPE {collection} IF NOT EXISTS")
            if version is None:
                self.client.command("CREATE VERTEX TYPE nodes IF NOT EXISTS")
                self.client.command("CREATE EDGE TYPE edges IF NOT EXISTS")
            for collection in exports:
                self.client.command(f"CREATE PROPERTY {collection}.id IF NOT EXISTS STRING")
                self.client.command(f"CREATE INDEX IF NOT EXISTS ON {collection}(id) UNIQUE NULL_STRATEGY SKIP")
                if collection != "projects":
                    self.client.command(f"CREATE PROPERTY {collection}.project_id IF NOT EXISTS STRING")
                    self.client.command(f"CREATE INDEX IF NOT EXISTS ON {collection}(project_id) NOTUNIQUE NULL_STRATEGY SKIP")
            if version is None:
                for collection, field in (("projects", "name"), ("projects", "root"), ("project_roots", "path"),
                                          ("sources", "current_key"), ("records", "active_key"), ("jobs", "job_key"),
                                          ("import_cursors", "path")):
                    self.client.command(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                    self.client.command(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) UNIQUE NULL_STRATEGY SKIP")
                for collection in ("sources", "chunks", "nodes", "edges"):
                    self.client.command(f"CREATE PROPERTY {collection}.is_current IF NOT EXISTS BOOLEAN")
                for collection, field in (("chunks", "text"), ("records", "content")):
                    self.client.command(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                    self.client.command(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) FULL_TEXT METADATA {{similarity:'BM25'}}")
            self.client.command("CREATE PROPERTY journal_events.epoch IF NOT EXISTS STRING")
            self.client.command("CREATE PROPERTY journal_events.seq IF NOT EXISTS LONG")
            self.client.command("CREATE INDEX IF NOT EXISTS ON journal_events(epoch,seq) UNIQUE")
            with self.client.transaction() as tx:
                if not tx.query("SELECT id FROM journal_state WHERE id='current'"):
                    insert_document(tx, "journal_state", {"id": "current", "epoch": str(uuid4()), "seq": 0, "first_seq": 1})
                if version is None:
                    insert_document(tx, "devbrain_meta", {"name": "schema_version", "value": SCHEMA_VERSION})
                else:
                    update_document(tx, "devbrain_meta", {"value": SCHEMA_VERSION}, "name='schema_version'", {})
            if indexed != LOOKUP_INDEX_SET:
                self._lookup_indexes(indexed)

    def _lookup_indexes(self, recorded):
        """Create the lookup indexes that are missing. Called with the writer lock held; safe to repeat.

        An index only makes a lookup faster. When one cannot be built now, for example because a large
        table takes longer than a request may, the database is opened all the same and the next start
        tries again: nothing is recorded until every index exists.
        """
        from .arcade import ArcadeError
        try:
            for collection, field in LOOKUP_INDEXES:
                self.client.command(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                self.client.command(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) NOTUNIQUE NULL_STRATEGY SKIP")
        except ArcadeError:
            return
        with self.client.transaction() as tx:
            if recorded is None:
                insert_document(tx, "devbrain_meta", {"name": "lookup_indexes", "value": LOOKUP_INDEX_SET})
            else:
                update_document(tx, "devbrain_meta", {"value": LOOKUP_INDEX_SET}, "name='lookup_indexes'", {})

    @staticmethod
    def _require_project(tx, project_id):
        known = getattr(tx, "known", None)
        if known is not None and project_id in known["projects"]:
            return
        if project_id is not None and not tx.query("SELECT id FROM projects WHERE id=:id", {"id": project_id}):
            raise ValueError(f"Unknown project: {project_id}")
        if known is not None and project_id is not None:
            known["projects"].add(project_id)

    @staticmethod
    def _remembered(tx, table, identifier, load):
        """A row as this transaction already read or wrote it, else read now. Without prefetch_graph: always read."""
        known = getattr(tx, "known", None)
        if known is None:
            return load()
        if identifier not in known[table]:
            known[table][identifier] = load()
        return known[table][identifier]

    @staticmethod
    @contextmanager
    def _keeping(tx):
        """Graph writes that keep what the transaction remembers current, instead of forgetting it."""
        if getattr(tx, "known", None) is None:
            yield None
            return
        tx.keeps_known = True
        try:
            yield tx.known
        finally:
            tx.keeps_known = False

    def prefetch_graph(self, tx, nodes, edges):
        """Read, in a few requests, what upsert_node and upsert_edge would look up row by row.

        Only inside the transaction that then writes these rows. The checks stay the same checks:
        a row that was not read here is read when it is needed, and one known to be absent fails as before.
        """
        if not hasattr(tx, "known"):
            return
        nodes, edges = list(nodes), list(edges)
        wanted = {
            "sources": {item["source_id"] for item in (*nodes, *edges) if isinstance(item.get("source_id"), str) and item["source_id"]},
            "nodes": {item["id"] for item in nodes if isinstance(item.get("id"), str)}
            | {item[key] for item in edges for key in ("from_id", "to_id") if isinstance(item.get(key), str)},
            "edges": {item["id"] for item in edges if isinstance(item.get("id"), str)},
        }
        fields = {"sources": "id,project_id,is_current", "nodes": "id,project_id", "edges": "id,project_id,from_id,to_id"}
        known = {"projects": set(), "sources": {}, "nodes": {}, "edges": {}}
        for table, identifiers in wanted.items():
            identifiers = sorted(identifiers)
            for offset in range(0, len(identifiers), 500):
                part = identifiers[offset:offset + 500]
                found = {row["id"]: row for row in tx.query(f"SELECT {fields[table]} FROM {table} WHERE id IN :ids", {"ids": part})}
                known[table].update({identifier: found.get(identifier) for identifier in part})
        tx.known = known

    def require_safe_project_root(self, root):
        path = str(Path(root).expanduser().resolve())
        if ((self.data_dir / "credentials" / "api-keys.json").exists()
                and self.data_dir.is_relative_to(Path(path))):
            raise ValueError("Project root cannot contain Cairnmind's private credential store")
        return path

    def add_project(self, name, root):
        name, path = name.strip(), self.require_safe_project_root(root)
        if not name:
            raise ValueError("Project name must not be empty")
        with self.transaction() as tx:
            existing = one_row(tx, "SELECT FROM projects WHERE name=:name OR root=:root", {"name": name, "root": path}, False)
            if existing:
                if existing["name"] != name or existing["root"] != path:
                    raise ValueError("Project name or root is already registered")
                return existing
            project = {"id": str(uuid4()), "name": name, "root": path, "created_at": utc_now()}
            insert_document(tx, "projects", project)
            insert_document(tx, "project_roots", {"id": str(uuid4()), "project_id": project["id"], "path": path})
            return project

    def add_project_root(self, project_id, path):
        root = self.require_safe_project_root(path)
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            existing = one_row(tx, "SELECT FROM project_roots WHERE path=:path", {"path": root}, False)
            if existing and existing["project_id"] != project_id:
                raise ValueError("Directory already belongs to another project")
            if not existing:
                insert_document(tx, "project_roots", {"id": str(uuid4()), "project_id": project_id, "path": root})

    def list_projects(self):
        return [clean_row(row) for row in self.query("SELECT FROM projects ORDER BY name")]

    def resolve_project(self, identifier):
        value = str(identifier)
        project = one_row(self, "SELECT FROM projects WHERE id=:value OR name=:value", {"value": value}, False)
        if project:
            return project
        path = Path(value).expanduser().resolve()
        candidates = [r for r in self.query("SELECT FROM project_roots")
                      if path == Path(r["path"]) or Path(r["path"]) in path.parents]
        if candidates:
            selected = max(candidates, key=lambda row: len(row["path"]))
            return one_row(self, "SELECT FROM projects WHERE id=:id", {"id": selected["project_id"]})
        raise ValueError(f"Unknown project: {value}")

    def _delete_old_vectors(self, tx, source_ids):
        if not source_ids:
            return
        tx.command("DELETE FROM embeddings WHERE source_id IN :ids", {"ids": source_ids})
        for row in tx.query("SELECT FROM vector_indexes"):
            name = row.get("type_name")
            if name and IDENTIFIER.fullmatch(name) and name.startswith("vector_"):
                tx.command(f"DELETE FROM {quote_name(name)} WHERE source_id IN :ids", {"ids": source_ids})

    def snapshot(self, project_id, uri, content, kind="document", metadata=None):
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        digest, key = hashlib.sha256(content.encode()).hexdigest(), json_text([project_id, uri])
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            existing = one_row(tx, "SELECT FROM sources WHERE current_key=:key AND is_current=true", {"key": key}, False)
            if existing and existing["sha256"] == digest:
                return existing
            if existing:
                params = {"id": existing["id"]}
                update_document(tx, "sources", {"is_current": False, "current_key": None}, "id=:id", params)
                update_document(tx, "chunks", {"is_current": False}, "source_id=:id", params)
                update_document(tx, "nodes", {"is_current": False}, "source_id=:id AND kind NOT IN ['source','decision','task','fact','constraint','question','preference','checkpoint']", params)
                update_document(tx, "edges", {"is_current": False}, "source_id=:id AND kind NOT IN ['supported_by','depends_on','supersedes']", params)
                self._delete_old_vectors(tx, [existing["id"]])
            source = {"id": str(uuid4()), "project_id": project_id, "uri": uri, "kind": kind, "content": content,
                      "sha256": digest, "metadata": {**(metadata or {}), **(self.current_origin() or {})},
                      "is_current": True, "current_key": key, "created_at": utc_now()}
            insert_document(tx, "sources", source)
            return source

    def snapshot_new(self, tx, project_id, uri, content, kind="document", metadata=None):
        """The source of a URI that has no current one, written inside the caller's transaction.

        Returns None, having written nothing, when the URI has a current source: the caller then
        takes ``snapshot``, which knows how to keep or replace it. The row is the one ``snapshot`` writes.
        """
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        if one_row(tx, "SELECT id FROM sources WHERE current_key=:key AND is_current=true",
                   {"key": json_text([project_id, uri])}, False):
            return None
        return self.new_source(tx, project_id, uri, content, kind, metadata)

    def new_source(self, tx, project_id, uri, content, kind="document", metadata=None):
        """Insert the current source of a URI whose earlier one, if any, this transaction already deactivated.

        The row is the one ``snapshot`` writes; the unique index on the current key refuses a second current one.
        """
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        source = {"id": str(uuid4()), "project_id": project_id, "uri": uri, "kind": kind, "content": content,
                  "sha256": hashlib.sha256(content.encode()).hexdigest(),
                  "metadata": {**(metadata or {}), **(self.current_origin() or {})},
                  "is_current": True, "current_key": json_text([project_id, uri]), "created_at": utc_now()}
        insert_document(tx, "sources", source)
        return source

    def enqueue_new(self, tx, kind, project_id, payload, dedupe_key):
        """A job whose key cannot exist yet, because it names a row created in this same transaction.

        The row is the one ``enqueue`` writes. The unique index on the key still refuses a second one.
        """
        if not kind or not isinstance(dedupe_key, str) or not dedupe_key:
            raise ValueError("A job requires a kind and deduplication key")
        job_id, now = str(uuid4()), utc_now()
        insert_document(tx, "jobs", {"id": job_id, "project_id": project_id, "kind": kind, "payload": payload, "dedupe_key": dedupe_key,
                             "job_key": json_text([kind, project_id, dedupe_key]), "state": "pending", "lease_until": None,
                             "worker": None, "error": None, "attempts": 0, "created_at": now, "updated_at": now})
        return job_id

    def get_source(self, source_id):
        return one_row(self, "SELECT FROM sources WHERE id=:id", {"id": source_id})

    @staticmethod
    def _validate_sources(tx, project_id, sources):
        if not isinstance(sources, list) or not sources or not all(isinstance(item, str) for item in sources):
            raise ValueError("A memory record requires at least one source ID: cite a source you read, "
                             "or the source_ids a checkpoint returns")
        for source_id in sources:
            source = one_row(tx, "SELECT project_id FROM sources WHERE id=:id", {"id": source_id}, False)
            if not source or source.get("project_id") != project_id:
                raise ValueError("Sources must exist and belong to the same project")

    def _validate_task(self, tx, project_id, record_id, metadata, sources, *, assert_completion=True, validate_dependencies=True):
        for key, value in {"state": "todo", "depends_on": [], "acceptance_criteria": [], "requires_test": False, "requires_review": False}.items():
            metadata.setdefault(key, value)
        validate_task_state(metadata["state"])
        if not isinstance(metadata["depends_on"], list) or not all(isinstance(x, str) for x in metadata["depends_on"]):
            raise ValueError("Task depends_on must be a list of task IDs")
        criteria = metadata["acceptance_criteria"]
        if not isinstance(criteria, list) or not all(isinstance(x, str) and x.strip() for x in criteria):
            raise ValueError("acceptance_criteria must be a list of nonempty strings")
        for key in ("required_profiles", "check_run_ids"):
            values = metadata.get(key, [])
            if not isinstance(values, list) or not all(isinstance(x, str) and x.strip() for x in values):
                raise ValueError(f"{key} must be a list of nonempty strings")
        validate_criterion_links(metadata, criteria)
        for run_id in metadata.get("check_run_ids", []):
            run = one_row(tx, "SELECT task_id,project_id FROM check_runs WHERE id=:id", {"id": run_id}, False)
            if not run or run.get("project_id") != project_id or run.get("task_id") != record_id:
                raise ValueError("check_run_ids must reference machine runs for this task and project")
        for flag in ("requires_test", "requires_review", "test_passed", "review_passed", "acceptance_passed"):
            if flag in metadata and not isinstance(metadata[flag], bool):
                raise ValueError(f"{flag} must be a boolean")
        if "step_index" in metadata and (type(metadata["step_index"]) is not int or metadata["step_index"] < 0):
            raise ValueError("step_index must be a nonnegative integer")
        unfinished, seen = [], set()

        def visit(task_id):
            if task_id == record_id:
                raise ValueError("Task dependencies must not contain a cycle")
            if task_id in seen:
                return
            seen.add(task_id)
            task = one_row(tx, "SELECT FROM records WHERE id=:id AND project_id=:project AND kind='task' AND status!='superseded'",
                        {"id": task_id, "project": project_id}, False)
            if not task:
                raise ValueError("Task dependencies must be active tasks from the same project")
            if task["metadata"].get("state", "todo") != "done":
                unfinished.append(task_id)
            for dependency in task["metadata"].get("depends_on", []):
                visit(dependency)

        if validate_dependencies:
            for dependency in metadata["depends_on"]:
                visit(dependency)
        if metadata["state"] != "done" or not assert_completion:
            return
        if unfinished:
            raise ValueError("Task cannot be done while dependencies are unfinished")
        for needed, flag, field in ((bool(criteria), "acceptance_passed", "acceptance_source_ids"),
                                    (metadata["requires_review"], "review_passed", "review_source_ids")):
            if needed:
                evidence = metadata.get(field)
                if metadata.get(flag) is not True or not isinstance(evidence, list) or not evidence:
                    raise ValueError(f"Task completion requires {flag}=true and {field}")
                if not all(isinstance(x, str) and x in sources for x in evidence):
                    raise ValueError(f"{field} must reference this record's source IDs")

    def upsert_node(self, node, tx=None):
        if tx is None:
            with self.transaction() as native:
                return self.upsert_node(node, native)
        node = {"metadata": {}, **node}
        for key in ("id", "project_id", "kind", "label"):
            if not isinstance(node.get(key), str):
                raise ValueError(f"Graph vertex requires {key}")
        self._require_project(tx, node["project_id"])
        source = None
        if node.get("source_id"):
            source = self._remembered(tx, "sources", node["source_id"], lambda: one_row(
                tx, "SELECT project_id,is_current FROM sources WHERE id=:id", {"id": node["source_id"]}, False))
            if not source or source.get("project_id") != node["project_id"]:
                raise ValueError("Graph vertex source must belong to the same project")
        # Evidence/decision vertices remain traversable as provenance even if a code file changed.
        node.setdefault("is_current", bool(source["is_current"]) if source and node["kind"] not in KINDS | {"source"} else True)
        existing = self._remembered(tx, "nodes", node["id"], lambda: one_row(
            tx, "SELECT FROM nodes WHERE id=:id", {"id": node["id"]}, False))
        if existing and existing.get("project_id") != node["project_id"]:
            raise ValueError("Graph vertex belongs to another project")
        with self._keeping(tx) as known:
            if existing:
                # The project, checked above, lets a transaction that resets the project's graph skip reading
                # the row before and after: the journal names no single node of a graph it reports as reset.
                update_document(tx, "nodes", node, "id=:id", {"id": node["id"], "project_id": node["project_id"]})
            else:
                tx.command(f"CREATE VERTEX nodes CONTENT {json_text(node)}")
            if known is not None:
                known["nodes"][node["id"]] = {"id": node["id"], "project_id": node["project_id"]}
        return node

    def upsert_edge(self, edge, tx=None):
        if tx is None:
            with self.transaction() as native:
                return self.upsert_edge(edge, native)
        edge = {"status": "observed", **edge}
        for key in ("id", "project_id", "from_id", "to_id", "kind"):
            if not isinstance(edge.get(key), str):
                raise ValueError(f"Graph edge requires {key}")
        for endpoint in (edge["from_id"], edge["to_id"]):
            vertex = self._remembered(tx, "nodes", endpoint, lambda endpoint=endpoint: one_row(
                tx, "SELECT project_id FROM nodes WHERE id=:id", {"id": endpoint}, False))
            if not vertex or vertex.get("project_id") != edge["project_id"]:
                raise ValueError("Graph edge endpoints must exist in the same project")
        source = None
        if edge.get("source_id"):
            source = self._remembered(tx, "sources", edge["source_id"], lambda: one_row(
                tx, "SELECT project_id,is_current FROM sources WHERE id=:id", {"id": edge["source_id"]}, False))
            if not source or source.get("project_id") != edge["project_id"]:
                raise ValueError("Graph edge source must belong to the same project")
        edge.setdefault("is_current", bool(source["is_current"]) if source and edge["kind"] not in {"supported_by", "depends_on", "supersedes"} else True)
        existing = self._remembered(tx, "edges", edge["id"], lambda: one_row(
            tx, "SELECT FROM edges WHERE id=:id", {"id": edge["id"]}, False))
        if existing and existing.get("project_id") != edge["project_id"]:
            raise ValueError("Graph edge belongs to another project")
        with self._keeping(tx) as known:
            if known is not None:
                known["edges"][edge["id"]] = {key: edge[key] for key in ("id", "project_id", "from_id", "to_id")}
            if existing:
                # The project is the checked one, as in upsert_node.
                mine = {"id": edge["id"], "project_id": edge["project_id"]}
                if any(existing.get(k) != edge[k] for k in ("project_id", "from_id", "to_id")):
                    tx.command("DELETE FROM edges WHERE id=:id", mine)
                else:
                    update_document(tx, "edges", edge, "id=:id", mine)
                    return edge
            tx.command("CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:from_id) "
                       f"TO (SELECT FROM nodes WHERE id=:to_id) CONTENT {json_text(edge)}",
                       {"from_id": edge["from_id"], "to_id": edge["to_id"]})
        return edge

    def _record_graph(self, tx, record):
        node_id = f"record:{record['id']}"
        self.upsert_node({"id": node_id, "project_id": record["project_id"], "kind": record["kind"],
                          "label": record["content"][:1000], "source_id": record["source_ids"][0],
                          "is_current": record["status"] != "superseded",
                          "metadata": {**record["metadata"], "record_id": record["id"], "status": record["status"]}}, tx)
        tx.command("DELETE FROM edges WHERE from_id=:id AND kind IN ['supported_by','depends_on','supersedes']", {"id": node_id})

        def edge(target, kind, source_id):
            eid = hashlib.sha256(f"{node_id}\0{kind}\0{target}".encode()).hexdigest()
            self.upsert_edge({"id": eid, "project_id": record["project_id"], "from_id": node_id, "to_id": target,
                              "kind": kind, "source_id": source_id, "is_current": record["status"] != "superseded"}, tx)

        for source_id in record["source_ids"]:
            source = one_row(tx, "SELECT FROM sources WHERE id=:id", {"id": source_id})
            target = f"source:{source_id}"
            self.upsert_node({"id": target, "project_id": record["project_id"], "kind": "source", "label": source["uri"],
                              "source_id": source_id, "metadata": {"source_id": source_id, "source_kind": source["kind"]}}, tx)
            edge(target, "supported_by", source_id)
        for dependency in record["metadata"].get("depends_on", []):
            target = f"record:{dependency}"
            if tx.query("SELECT id FROM nodes WHERE id=:id", {"id": target}):
                edge(target, "depends_on", record["source_ids"][0])
        if record.get("supersedes"):
            edge(f"record:{record['supersedes']}", "supersedes", record["source_ids"][0])

    def _revision(self, tx, record):
        insert_document(tx, "record_revisions", {"id": f"{record['id']}:{record['version']}", "project_id": record["project_id"],
                                        "record_id": record["id"], "version": record["version"], "snapshot": record, "recorded_at": utc_now()})
        self._record_graph(tx, record)

    def _remember(self, tx, project_id, record, sources, *, managed_batch=False, conflict_review=False,
                  replace_origin=False, replace_metadata=False, mark_only_changes=False):
        """Write one record version.

        replace_origin: the origin mark follows this writer instead of the old version (checkpoints).
        replace_metadata: the metadata is built from this write alone, not merged onto the old version's.
        Used when a non-external checkpoint replaces an external one, so no external text stays behind
        unmarked; state that earlier native checkpoints set and neither checkpoint repeats is dropped too.
        mark_only_changes: stamp the current origin only if the stored result differs (plan steps).
        """
        self._require_project(tx, project_id)
        self._validate_sources(tx, project_id, sources)
        if not isinstance(record, dict):
            raise ValueError("record must be an object")
        old = None
        if record.get("id"):
            old = one_row(tx, "SELECT FROM records WHERE id=:id AND project_id=:project", {"id": record["id"], "project": project_id}, False)
            if not old and not managed_batch:
                raise ValueError("Cannot update an unknown record")
            if old and (type(record.get("expected_version")) is not int or record["expected_version"] != old["version"]):
                raise ValueError("Record version conflict; read the current version and retry")
            if not old and record.get("expected_version") is not None:
                raise ValueError("New managed records must not provide expected_version")
            if old and old["status"] == "superseded" and not managed_batch:
                raise ValueError("A superseded record is immutable; create a new record")
        kind, status = record.get("kind", old["kind"] if old else None), record.get("status", old["status"] if old else "proposed")
        content = record.get("content", old["content"] if old else None)
        if not isinstance(kind, str) or not isinstance(status, str) or kind not in KINDS or status not in STATUSES:
            raise ValueError("Unknown record kind or status")
        if old and kind != old["kind"]:
            raise ValueError("Record kind cannot change")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Record content must be nonempty text")
        record_id, key = (old["id"] if old else record.get("id") or str(uuid4())), record.get("key", old["key"] if old else None)
        key = record_id if key is None else key
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Record key must be nonempty text")
        metadata = dict(old.get("metadata", {})) if old and not replace_metadata else {}
        updates = record.get("metadata", {})
        if not isinstance(updates, dict):
            raise ValueError("Record metadata must be an object")
        if not conflict_review:
            # An agent may mark a possible conflict when it creates a finding, but it can
            # neither unmark one later nor record a decision about it.
            updates = {key: value for key, value in updates.items() if key not in CONFLICT_REVIEW_METADATA
                       and not (old and key == "possible_conflicts")}
        metadata.update({key: value for key, value in updates.items() if key not in DERIVED_RECORD_METADATA})
        if kind == "task":
            if "acceptance_criteria" in updates:
                # A criteria list that is declared again says everything about its links: what the
                # record held before is dropped unless the caller sends the stored form as well.
                for stored in ("criterion_checks", "check_proposals"):
                    if stored not in updates:
                        metadata.pop(stored, None)
            normalise_criteria(metadata)
            # Checked when it is set, not whenever the task is written: a task that already carries
            # something else under this name stays writable, and is simply never compared.
            if "scope" in updates and updates["scope"] != (old or {}).get("metadata", {}).get("scope"):
                validate_scope(updates["scope"])
        evidence = evidence_time(tx, sources)
        metadata.update(evidence_earliest=evidence["earliest"], evidence_latest=evidence["latest"],
                        evidence_basis=evidence["basis"])
        # Set after the client's metadata, which can neither forge nor clear it; kept from the old version,
        # except when a checkpoint replaces the checkpoint record: it replaces its content and its only source,
        # so there the mark follows the writer of the current checkpoint.
        if replace_origin:
            for name in ORIGIN_METADATA:
                metadata.pop(name, None)
        explicit_completion = kind == "task" and metadata.get("state") == "done" and (not old or updates.get("state") == "done")
        if managed_batch and old and old.get("metadata", {}).get("state") == "done" and metadata.get("state") == "done":
            explicit_completion = False  # Internal plan batches preserve progress; they do not assert a new check.
        managed = kind == "task" and bool((old or {}).get("metadata", {}).get("plan_ref") or metadata.get("plan_ref"))
        if kind == "task":
            if any(field in updates for field in ("verification_status", "machine_verified", "verified")):
                raise ValueError("Machine verification status is derived from immutable check runs")
            if metadata.get("plan_ref") is not None and not isinstance(metadata["plan_ref"], str):
                raise ValueError("plan_ref must be a managed plan ID")
            if managed and "worktree" in updates:
                raise ValueError("Managed task worktree is transient; supply record.worktree")
            definition_changed = bool(old) and (content != old["content"] or key != old["key"] or any(
                metadata.get(k) != old.get("metadata", {}).get(k) for k in MANAGED_DEFINITION_METADATA_FIELDS
            ))
            if managed and not managed_batch:
                if not old or definition_changed or status == "superseded":
                    raise ValueError("Managed task definitions and membership must be changed through PlanManager")
                if type(record.get("expected_plan_version")) is not int or record["expected_plan_version"] < 1:
                    raise ValueError("Managed task updates require expected_plan_version")
            # A proposed command is not part of what a task is: changing one keeps reported evidence.
            proof_changed = bool(old) and (content != old["content"] or key != old["key"] or any(
                metadata.get(k) != old.get("metadata", {}).get(k)
                for k in MANAGED_DEFINITION_METADATA_FIELDS - ADVISORY_DEFINITION_FIELDS))
            if proof_changed:
                for field in ("acceptance_passed", "acceptance_source_ids", "test_passed", "test_source_ids", "review_passed", "review_source_ids"):
                    if field not in updates:
                        metadata.pop(field, None)
            self._validate_task(tx, project_id, record_id, metadata, sources,
                                assert_completion=explicit_completion, validate_dependencies=not managed_batch)
        # Decided on the final metadata, after every field this write drops (criteria links, reported proof).
        origin = self.current_origin()
        if origin and mark_only_changes and not outcome_changed(old, content, key, metadata):
            # A plan save rewrites every step; one it leaves as it was keeps its own mark, or none.
            origin = None
        metadata.update(origin or {})
        supersedes = record.get("supersedes", old.get("supersedes") if old else None)
        if supersedes and (not old or supersedes != old.get("supersedes")):
            previous = one_row(tx, "SELECT FROM records WHERE id=:id AND project_id=:project", {"id": supersedes, "project": project_id}, False)
            if not previous or previous["id"] == record_id or previous["status"] == "superseded":
                raise ValueError("supersedes must identify an active record in the same project")
            if previous["kind"] != kind:
                raise ValueError("Replacement and replaced record must have the same kind")
            if previous["kind"] == "task" and previous.get("metadata", {}).get("plan_ref") and not managed_batch:
                raise ValueError("Managed task membership must be changed through PlanManager")
            if not old and "expected_version" in record and record["expected_version"] != previous["version"]:
                raise ValueError("Record version conflict; replaced record has changed")
            previous.update(status="superseded", active_key=None, version=previous["version"] + 1, updated_at=utc_now())
            update_document(tx, "records", previous, "id=:id", {"id": supersedes})
            self._revision(tx, previous)
        # Recomputed on every write of a replacement, so a later source change never
        # leaves a stale warning behind. Warn, never block.
        metadata.pop("supersede_warning", None)
        metadata.pop("supersede_replaced_evidence_latest", None)
        if supersedes:
            replaced = one_row(tx, "SELECT source_ids FROM records WHERE id=:id AND project_id=:project",
                            {"id": supersedes, "project": project_id}, False) or {}
            replaced_sources = replaced.get("source_ids") or []
            if isinstance(replaced_sources, str):
                replaced_sources = json.loads(replaced_sources)
            replaced_time = evidence_time(tx, replaced_sources)
            # Only a stated time is a real evidence time; an observed time is only an
            # upper bound, so comparing against it would flag snapshot order, not age.
            replaced_latest = parse_utc(replaced_time["latest"]) if replaced_time["basis"] == "stated" else None
            latest = parse_utc(metadata["evidence_latest"])
            if replaced_latest and latest and latest < replaced_latest:
                metadata["supersede_warning"] = "anachronistic_supersede"
                metadata["supersede_replaced_evidence_latest"] = replaced_time["latest"]
        active_key = json_text([project_id, kind, key]) if status != "superseded" else None
        collision = one_row(tx, "SELECT id FROM records WHERE active_key=:key", {"key": active_key}, False) if active_key else None
        if collision and collision["id"] != record_id:
            raise ValueError("Active record key already exists; update using id and expected_version")
        now = utc_now()
        result = {"id": record_id, "project_id": project_id, "kind": kind, "key": key, "content": content, "status": status,
                  "source_ids": list(dict.fromkeys(sources)), "version": old["version"] + 1 if old else 1, "supersedes": supersedes,
                  "metadata": metadata, "created_at": old["created_at"] if old else now, "updated_at": now, "active_key": active_key}
        if explicit_completion and metadata.get("requires_test"):
            from .verification import validate_completion
            validate_completion(self, tx, project_id, {**result, "_completion_worktree": record.get("worktree")})
        if old:
            update_document(tx, "records", result, "id=:id AND version=:version", {"id": record_id, "version": old["version"]})
        else:
            insert_document(tx, "records", result)
        self._revision(tx, result)
        if managed and not managed_batch:
            from .plans import note_task_mutation
            note_task_mutation(self, tx, result, record["expected_plan_version"])
        if kind == "task" and metadata.get("scope"):
            before, after = (old.get("metadata", {}).get("state", "todo") if old else None), metadata.get("state")
            working = after in ("in_progress", "needs_test", "needs_review", "blocked")
            declared_now = working and not (old and old.get("metadata", {}).get("scope"))
            # A start is entering in_progress, taking a finished task up again, or declaring a scope
            # on a task that is already being worked on. A note that is still open is never replaced.
            change = ("start" if (after == "in_progress" and before != "in_progress") or (before == "done" and working)
                      or declared_now else "finish" if after == "done" and before not in (None, "done") else None)
            if change:
                # A scoped task starts or ends: the tree is noted for the scope-drift warning. Only after
                # the whole write is committed and the writer lock is released, and never as part of it.
                from .scope import note_progress
                worktree = record.get("worktree")
                self._after_commit(lambda: note_progress(self, result, worktree, change))
        return result

    def _after_commit(self, call):
        pending = getattr(self._local, "after_commit", None)
        if pending is not None:
            pending.append(call)

    def remember(self, project_id, record, sources, *, conflict_review=False):
        with self.transaction() as tx:
            return self._remember(tx, project_id, record, sources, conflict_review=conflict_review)

    def checkpoint(self, project_id, state):
        if not isinstance(state, dict):
            raise ValueError("Checkpoint state must be an object")
        content = json_text(state)
        source = self.snapshot(project_id, f"checkpoint://{uuid4()}", content, "checkpoint", {"origin": "client"})
        with self.transaction() as tx:
            old = one_row(tx, "SELECT FROM records WHERE project_id=:project AND kind='checkpoint' AND `key`='latest' AND status!='superseded'", {"project": project_id}, False)
            record = {"kind": "checkpoint", "key": "latest", "content": content, "metadata": state}
            if old:
                record.update(id=old["id"], expected_version=old["version"])
            # The record's metadata accumulates the fields of earlier checkpoints. When a writer that is not
            # an external session replaces an external checkpoint, none of the external fields may stay behind
            # unmarked: the metadata is then built from this state alone. Any other succession merges as before.
            fresh = bool(old) and degraded_origin(old.get("metadata")) and not self.current_origin()
            return self._remember(tx, project_id, record, [source["id"]], replace_origin=True, replace_metadata=fresh)

    def list_records(self, project_id, include_history=False):
        self._require_project(self, project_id)
        return [clean_row(row) for row in self.query("SELECT FROM records WHERE project_id=:project" +
            ("" if include_history else " AND status!='superseded'") + " ORDER BY updated_at DESC,id", {"project": project_id})]

    def record_history(self, record_id):
        return [row["snapshot"] for row in self.query("SELECT snapshot FROM record_revisions WHERE record_id=:id ORDER BY version", {"id": record_id})]

    def enqueue(self, kind, project_id, payload, dedupe_key):
        if not kind or not isinstance(dedupe_key, str) or not dedupe_key:
            raise ValueError("A job requires a kind and deduplication key")
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            job_key = json_text([kind, project_id, dedupe_key])
            old = one_row(tx, "SELECT id FROM jobs WHERE job_key=:key", {"key": job_key}, False)
            if old:
                return old["id"]
            job_id, now = str(uuid4()), utc_now()
            insert_document(tx, "jobs", {"id": job_id, "project_id": project_id, "kind": kind, "payload": payload, "dedupe_key": dedupe_key,
                                 "job_key": job_key, "state": "pending", "lease_until": None, "worker": None, "error": None,
                                 "attempts": 0, "created_at": now, "updated_at": now})
            return job_id

    def claim_job(self, worker, lease_seconds=60, kinds=None, *, project_ids=None):
        if lease_seconds <= 0 or not worker:
            raise ValueError("Worker and positive lease duration are required")
        if kinds is not None and not kinds:
            return None
        if project_ids is not None:
            if (not isinstance(project_ids, (list, tuple))
                    or any(not isinstance(value, str) or not value for value in project_ids)):
                raise ValueError("Job project filter must contain explicit project IDs")
            if not project_ids:
                return None  # An empty authorization set must never claim global work.
        with self.transaction() as tx:
            job = one_row(tx, "SELECT FROM jobs WHERE (state='pending' OR (state='running' AND lease_until<:now))" +
                       (" AND kind IN :kinds" if kinds is not None else "") +
                       (" AND project_id IN :projects" if project_ids is not None else "") +
                       " ORDER BY created_at,id LIMIT 1",
                       {"now": time.time(), "kinds": kinds, "projects": project_ids}, False)
            if not job:
                return None
            if job["state"] == "running":
                job["attempt_history"] = self._job_event(job, "lease_expired")
            job.update(state="running", worker=worker, lease_until=time.time() + lease_seconds, attempts=job.get("attempts", 0) + 1, updated_at=utc_now())
            job["diagnostics"] = {}
            update_document(tx, "jobs", job, "id=:id", {"id": job["id"]})
            return job

    @staticmethod
    def _job_event(job, event, *, state=None, error=_JOB_UNSET):
        history = list(job.get("attempt_history") or [])
        history.append({"event": event, "attempts": job.get("attempts", 0),
                        "state": state or job["state"], "at": utc_now(),
                        "worker": job.get("worker"), "error": job.get("error") if error is _JOB_UNSET else error,
                        "diagnostics": job_diagnostics(job.get("diagnostics"))})
        return history

    def set_job_diagnostics(self, job_id, diagnostics, *, expected_attempts=None, worker=None):
        """Publish sanitized attempt diagnostics without completing/reclaiming a job."""
        cleaned = job_diagnostics(diagnostics)
        with self.transaction() as tx:
            job = one_row(tx, "SELECT FROM jobs WHERE id=:id", {"id": job_id}, False)
            if not job:
                return False  # Project deletion never recreates operational rows.
            if (expected_attempts is not None and (job["state"] != "running"
                    or job.get("attempts", 0) != expected_attempts)):
                return False
            if worker is not None and (job["state"] != "running" or job.get("worker") != worker):
                return False
            update_document(tx, "jobs", {"diagnostics": cleaned, "updated_at": utc_now()}, "id=:id", {"id": job_id})
        return True

    def finish_job(self, job_id, error=None, *, worker=None, expected_attempts=None):
        with self.transaction() as tx:
            job = one_row(tx, "SELECT FROM jobs WHERE id=:id", {"id": job_id}, False)
            if (not job or (worker is not None and (job.get("worker") != worker or job["state"] != "running"))
                    or (expected_attempts is not None and (job["state"] != "running"
                        or job.get("attempts", 0) != expected_attempts))):
                raise ValueError("Unknown job or stale job lease")
            state = "failed" if error else "completed"
            updates = {"state": state, "error": error, "lease_until": None, "updated_at": utc_now(),
                       "attempt_history": self._job_event(job, "finished", state=state, error=error)}
            if error:
                updates.update(last_error=error, last_error_at=updates["updated_at"])
            update_document(tx, "jobs", updates, "id=:id", {"id": job_id})

    def job_summary(self, project_id=None):
        self._require_project(self, project_id)
        summary = {name: {state: 0 for state in sorted(JOB_STATES)}
                   for name in ("archive", "indexing", "extraction", "other")}
        for row in self.query("SELECT kind,state,count(*) AS total FROM jobs" +
                              (" WHERE project_id=:project" if project_id else "") + " GROUP BY kind,state",
                              {"project": project_id}):
            group = {"import": "archive", "embed": "indexing", "extract": "extraction"}.get(row["kind"], "other")
            if row["state"] in JOB_STATES:
                summary[group][row["state"]] += int(row["total"])
        return summary

    def list_jobs(self, project_id, state=None, kind=None, limit=50):
        self._require_project(self, project_id)
        if state is not None and state not in JOB_STATES:
            raise ValueError("Unknown job state")
        if kind is not None and (not isinstance(kind, str) or not 1 <= len(kind) <= 80):
            raise ValueError("Invalid job kind")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Job list limit must be between 1 and 100")
        rows = self.query("SELECT FROM jobs WHERE project_id=:project"
                          + (" AND state=:state" if state else "") + (" AND kind=:kind" if kind else "")
                          + " ORDER BY updated_at DESC,id LIMIT :limit",
                          {"project": project_id, "state": state, "kind": kind, "limit": limit})
        packets = self._job_proofs(project_id) if any(row["kind"] == "import" for row in rows) else []
        return [self._public_job(clean_row(row), packets=packets) for row in rows]

    def job_status(self, project_id, job_id):
        self._require_project(self, project_id)
        job = one_row(self, "SELECT FROM jobs WHERE id=:id AND project_id=:project",
                   {"id": job_id, "project": project_id}, False)
        if not job:
            raise JobNotFoundError("Unknown job in requested project")
        return self._public_job(job, detail=True,
                                packets=self._job_proofs(project_id) if job["kind"] == "import" else [])

    def _job_proofs(self, project_id):
        # Read already published bounded evidence; never prepare, repair or
        # recapture a transcript merely to display operational diagnostics.
        return self.query("SELECT id,created_at,payload FROM resume_packets WHERE project_id=:project "
                          "ORDER BY created_at DESC,id LIMIT 100", {"project": project_id})

    def _import_job_coverage(self, job, packets):
        unknown = {"status": "scope_unknown", "scope": "requested_capture"}
        payload = job.get("payload") or {}
        if not isinstance(payload, dict):
            return {**unknown, "reason": "legacy_missing_capture"}
        capture, event_id = payload.get("requested_watermark"), payload.get("origin_event_id")
        path, client = payload.get("path"), payload.get("client")
        if (not isinstance(capture, dict) or not isinstance(event_id, str)
                or not isinstance(path, str) or client not in {"codex", "claude"}
                or type(capture.get("size")) is not int or capture["size"] < 0
                or any(type(capture.get(key)) is not int for key in ("device", "inode", "head_bytes"))
                or not isinstance(capture.get("head_sha256"), str)):
            return {**unknown, "reason": "legacy_missing_capture"}
        event = one_row(self, "SELECT FROM session_events WHERE id=:id AND project_id=:project",
                     {"id": event_id, "project": job["project_id"]}, False)
        if (not event or event.get("transcript_path") != path or event.get("client") != client
                or event.get("transcript_watermark") != capture):
            return {**unknown, "reason": "capture_receipt_unavailable"}
        for packet in packets:
            for row in (packet.get("payload") or {}).get("archive_watermarks", []):
                if (row.get("path") != path or row.get("client") != client
                        or any(row.get(key) != capture[key] for key in ("device", "inode", "head_bytes", "head_sha256"))):
                    continue
                if type(row.get("size")) is not int or row["size"] < capture["size"]:
                    continue
                pending = capture.get("pending_tail_bytes", 0)
                if pending and (row.get("pending_tail_sha256") != capture.get("pending_tail_sha256")
                                or row.get("captured_tail_start", row["size"]) != capture["size"]):
                    continue
                proof = {"packet_id": packet["id"], "at": packet["created_at"], "captured_size": row["size"]}
                if (row.get("status") == "complete" and row.get("archived_offset", -1) >= row["size"]
                        and type(row.get("source_count")) is int and isinstance(row.get("source_ids_hash"), str)):
                    sources = self.query("SELECT id FROM sources WHERE project_id=:project "
                                         "AND metadata.import_path=:path AND metadata.byte_offset<:size",
                                         {"project": job["project_id"], "path": path, "size": row["size"]})
                    ids = sorted(source["id"] for source in sources)
                    digest = hashlib.sha256(json_text(ids).encode()).hexdigest()
                    if len(ids) == row["source_count"] and digest == row["source_ids_hash"]:
                        return {"status": "captured_complete", "scope": "requested_capture", "proof_ref": proof}
                    return {**unknown, "reason": "published_source_set_unavailable"}
                if row.get("status") == "partial" and row["size"] == capture["size"]:
                    return {"status": "captured_blocked", "scope": "requested_capture", "proof_ref": proof}
        return {**unknown, "reason": "no_matching_published_proof"}

    def _public_job(self, job, *, detail=False, packets=()):
        result = {key: job.get(key) for key in ("id", "project_id", "kind", "state", "attempts", "created_at",
                                               "updated_at", "lease_until", "worker", "retry_count", "last_retry_at")}
        result["attempts"] = job.get("attempts", 0)
        result["diagnostics"] = job_diagnostics(job.get("diagnostics"))
        result["has_error"] = bool(job.get("error"))
        result.update(self._job_error(job.get("error"), result["diagnostics"]))
        history = job.get("attempt_history") or []
        result["attempt_history_count"] = len(history)
        if detail:
            result["attempt_history"] = [{key: event.get(key) for key in ("event", "attempts", "state", "at")}
                                          | {"has_error": bool(event.get("error")),
                                             "diagnostics": job_diagnostics(event.get("diagnostics"))}
                                          | self._job_error(event.get("error"), job_diagnostics(event.get("diagnostics")))
                                          for event in history[-20:] if isinstance(event, dict)]
            result["attempt_history_omitted"] = max(0, len(history) - 20)
        payload = job.get("payload") or {}
        result["target"] = ({key: payload[key][:1024] for key in ("path", "client", "source_id", "chunk_id", "origin_event_id")
                             if isinstance(payload.get(key), str)} if isinstance(payload, dict) else {})
        if job["kind"] == "import":
            result["coverage"] = self._import_job_coverage(job, packets)
        return result

    @staticmethod
    def _job_error(error, diagnostics):
        """Expose actionable fixed categories; keep original exceptions in storage/export."""
        if not error:
            return {"error_code": None, "error": None}
        text = str(error).lower()
        categories = (
            (("history does not exist", "no such file or directory", "filenotfounderror"),
             "transcript_missing", "The captured transcript file was unavailable."),
            (("unknown transcript schema", "unrecognized transcript", "unsupported transcript"),
             "unknown_transcript_schema", "A transcript record did not match a supported schema."),
            (("jsondecodeerror", "invalid complete jsonl", "expecting value", "expecting property name", "invalid json"),
             "invalid_jsonl", "A complete transcript record could not be parsed as JSON."),
            (("unparsed or conflicting transcript", "unparsed transcript", "unknown_schema"),
             "unparsed_transcript_record", "A transcript record requires compatible parsing or conflict review."),
            (("another project", "outside project", "project scope", "different project"),
             "project_scope_conflict", "Transcript project attribution did not match this project."),
            (("cursor has not archived", "captured eof", "archived prefix"),
             "archive_incomplete", "The captured transcript horizon was not completely archived."),
            (("transcript eof is unknown",),
             "capture_not_initialized", "The original event did not capture a finite transcript horizon."),
            (("arcadedb",), "storage_error", "The storage request failed."),
            (("timeout", "timed out"), "job_timeout", "The job exceeded its execution budget."),
        )
        for needles, code, message in categories:
            if any(needle in text for needle in needles):
                return {"error_code": diagnostics.get("code", code), "error": message}
        return {"error_code": diagnostics.get("code", "legacy_job_error"),
                "error": "The job failed; its original error is retained in the database and export."}

    def retry_job(self, project_id, job_id, expected_attempts, *, include_omissions=False):
        if type(expected_attempts) is not int or expected_attempts < 0:
            raise ValueError("Expected attempts must be a nonnegative integer")
        if type(include_omissions) is not bool:
            raise ValueError("Include omissions must be a boolean")
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            job = one_row(tx, "SELECT FROM jobs WHERE id=:id AND project_id=:project",
                       {"id": job_id, "project": project_id}, False)
            if not job:
                raise JobNotFoundError("Unknown job in requested project")
            eligible = job["state"] == "failed" or (include_omissions and self._all_proposals_rejected(job))
            if not eligible or job.get("attempts", 0) != expected_attempts:
                raise ValueError("Job retry conflict: read the current eligible attempt before retrying")
            self._retry_job_locked(tx, job)
        return self.job_status(project_id, job_id)

    @staticmethod
    def _all_proposals_rejected(job):
        diagnostics = job.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return False
        counts = diagnostics.get("counts")
        codes = diagnostics.get("validation_codes")
        return (job.get("kind") == "extract" and job["state"] == "completed"
                and diagnostics.get("code") == "completed_with_omissions"
                and isinstance(counts, dict) and isinstance(codes, list) and "proposal_rejected" in codes
                and all(type(counts.get(key)) is int for key in (
                    "candidate_records", "accepted_records", "written_records", "skipped_records"))
                and counts["candidate_records"] > 0 and counts["accepted_records"] == 0
                and counts["written_records"] == 0 and counts["skipped_records"] == counts["candidate_records"])

    def _retry_job_locked(self, tx, job):
        now = utc_now()
        updates = {"state": "pending", "error": None, "lease_until": None, "worker": None,
                   "updated_at": now, "last_retry_at": now, "retry_count": job.get("retry_count", 0) + 1,
                   "attempt_history": self._job_event(job, "retry_requested")}
        if job.get("error"):
            updates.update(last_error=job["error"], last_error_at=job.get("updated_at"))
        update_document(tx, "jobs", updates, "id=:id AND state=:state", {"id": job["id"], "state": job["state"]})

    def _count(self, collection, project_id=None, extra=""):
        condition = " WHERE project_id=:project" if project_id else ""
        if extra:
            condition += (" AND " if condition else " WHERE ") + extra
        rows = self.query(f"SELECT count(*) AS total FROM {quote_name(collection)}{condition}", {"project": project_id})
        return int(rows[0]["total"]) if rows else 0

    def status(self, project_id=None):
        self._require_project(self, project_id)
        counts = {key: self._count(key, project_id) for key in ("sources", "chunks", "records", "nodes", "edges", "embeddings")}
        counts["projects"] = 1 if project_id else self._count("projects")
        counts["jobs"] = {state: self._count("jobs", project_id, f"state='{state}'") for state in ("pending", "running", "completed", "failed")}
        counts["archived"] = counts["sources"]
        counts["indexed"] = len(self.query("SELECT DISTINCT source_id FROM chunks" + (" WHERE project_id=:project" if project_id else ""), {"project": project_id}))
        counts["processed"] = self._count("jobs", project_id, "kind='extract' AND state='completed'")
        counts["unassigned_sources"], counts["inbox"] = self._count("sources", extra="project_id IS NULL"), self._count("ingestion_inbox")
        for collection in ("plans", "sessions", "check_runs"):
            counts[collection] = self._count(collection, project_id)
        return counts

    def cache_stamp(self, project_id):
        """A value that changes whenever a committed write could change an answer for the project.

        It is a counter read from the change journal, which every mutation of the store
        advances in its own transaction, so no collection is scanned: an unchanged store
        costs one point read, and otherwise only the events added since the last call are
        read. Writes that cannot affect an answer (jobs, sessions, imports, snapshots) do
        not move it. The value is comparable only within one Store object.
        """
        # Plain reads, never the writer lock: a question must not wait behind a long write
        # transaction of another project. Events are committed together with the journal
        # state, so every event up to the head read first is visible to the queries below.
        with self._stamp_lock:
            state = self.query("SELECT epoch,seq,first_seq FROM journal_state WHERE id='current'")
            if not state:
                raise ValueError("Journal state is unavailable")
            head = {"epoch": state[0]["epoch"], "seq": state[0]["seq"]}
            known = self._stamp_cursor
            if head != known:
                position = known["seq"] if known and known["epoch"] == head["epoch"] else None
                if position is not None and not state[0].get("first_seq", 1) - 1 <= position <= head["seq"]:
                    position = None
                seq, shared, complete = dict(self._stamp_seq), self._stamp_global, position is not None
                while complete and position < head["seq"]:
                    rows = self.query(
                        "SELECT project_id,entity_type,seq FROM journal_events WHERE epoch=:epoch AND seq>:position "
                        "AND seq<=:upto ORDER BY seq LIMIT 1000",
                        {"epoch": head["epoch"], "position": position, "upto": head["seq"]})
                    # Sequence numbers are contiguous. A gap means the events were pruned or
                    # the journal was reset while we read.
                    if not rows or [row["seq"] for row in rows] != list(range(position + 1, position + 1 + len(rows))):
                        complete = False
                        break
                    for event in rows:
                        if event["entity_type"] in CACHE_STAMP_IGNORED:
                            continue
                        if event.get("project_id") is None:
                            shared = event["seq"]
                        else:
                            seq[event["project_id"]] = event["seq"]
                    position = rows[-1]["seq"]
                if complete:
                    self._stamp_seq, self._stamp_global = seq, shared
                else:
                    # First call, a new journal epoch, or events no longer available: nothing
                    # is known about the gap, so every project counts as changed.
                    self._stamp_base, self._stamp_global, self._stamp_seq = self._stamp_base + 1, 0, {}
                self._stamp_cursor = head
            parts = [head["epoch"], self._stamp_base, self._stamp_global, self._stamp_seq.get(project_id, 0)]
        return hashlib.sha256(json_text(parts).encode()).hexdigest()

    def recent_job_errors(self, project_id=None, limit=10):
        rows = self.query("SELECT FROM jobs WHERE state='failed'" +
            (" AND project_id=:project" if project_id else "") + " ORDER BY updated_at DESC LIMIT :limit",
            {"project": project_id, "limit": max(1, min(100, limit))})
        proofs = {}
        for row in rows:
            if row["kind"] == "import" and row["project_id"] not in proofs:
                proofs[row["project_id"]] = self._job_proofs(row["project_id"])
        return [self._public_job(clean_row(row), packets=proofs.get(row["project_id"], ())) for row in rows]

    def retry_jobs(self):
        """Explicit legacy global compatibility; every requeue retains its audit."""
        with self.transaction() as tx:
            rows = tx.query("SELECT FROM jobs WHERE state='failed'")
            for row in rows:
                self._retry_job_locked(tx, clean_row(row))
        return {"retried": len(rows), "job_ids": [row["id"] for row in rows]}

    def missing_embeddings(self, namespace, project_id=None):
        chunks = self.query("SELECT id,project_id FROM chunks WHERE is_current=true" + (" AND project_id=:project" if project_id else ""), {"project": project_id})
        existing = {r["chunk_id"] for r in self.query("SELECT chunk_id FROM embeddings WHERE namespace=:namespace", {"namespace": namespace})}
        return [{"id": row["id"], "project_id": row.get("project_id")} for row in chunks if row["id"] not in existing]

    def reset_missing_embedding_job(self, job_id, chunk_id, namespace):
        with self.transaction() as tx:
            if not tx.query("SELECT id FROM embeddings WHERE chunk_id=:chunk AND namespace=:namespace", {"chunk": chunk_id, "namespace": namespace}):
                update_document(tx, "jobs", {"state": "pending", "error": None, "lease_until": None, "updated_at": utc_now()}, "id=:id AND state='completed'", {"id": job_id})

    def delete_embeddings(self, project_id):
        with self.transaction() as tx:
            self._delete_old_vectors(tx, [r["id"] for r in tx.query("SELECT id FROM sources WHERE project_id=:project", {"project": project_id})])
            tx.command("DELETE FROM embeddings WHERE project_id=:project", {"project": project_id})

    def export(self, project_id=None):
        with self.transaction() as tx:
            return self._export_snapshot(tx, project_id, EXPORT_TYPES, 5)

    def _export_snapshot(self, tx, project_id, collections, format_version):
        result = {"format_version": format_version, "backend": "arcadedb", "exported_at": utc_now()}
        self._require_project(tx, project_id)
        for collection in collections:
            field = "id" if collection == "projects" else "project_id"
            result[collection] = [] if collection == "ingestion_inbox" and project_id else [
                clean_row(row) for row in tx.query(f"SELECT FROM {quote_name(collection)}" + (f" WHERE {field}=:project" if project_id else ""), {"project": project_id})]
        result["vector_data"] = {}
        for registry in result["vector_indexes"]:
            name = registry.get("type_name")
            if isinstance(name, str) and name.startswith("vector_"):
                result["vector_data"][name] = [clean_row(row) for row in tx.query(f"SELECT FROM {quote_name(name)}")]
        return result

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if destination.is_dir():
            destination /= f"cairnmind-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}{BACKUP_SUFFIXES[0]}"
        return self._write_backup(destination, self.export())

    @staticmethod
    def _write_backup(destination, payload):
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        envelope = {"format": BACKUP_FORMATS[0], "version": 1, "sha256": hashlib.sha256(json_text(payload).encode()).hexdigest(), "payload": payload}
        fd, name = tempfile.mkstemp(prefix=".cairnmind-backup-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                with gzip.GzipFile(fileobj=handle, mode="wb") as compressed:
                    compressed.write(json_text(envelope).encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validated_backup(path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
            if not isinstance(envelope, dict) or envelope.get("format") not in BACKUP_FORMATS or envelope.get("version") != 1:
                raise ValueError("This file is not a supported Cairnmind ArcadeDB backup")
            payload = envelope["payload"]
            if hashlib.sha256(json_text(payload).encode()).hexdigest() != envelope.get("sha256"):
                raise ValueError("Backup checksum mismatch")
            if not isinstance(payload, dict):
                raise ValueError("Backup payload must be an object")
            payload_version = payload.get("format_version")
            if payload_version not in {2, 3, 4, 5} or payload.get("backend") != "arcadedb":
                raise ValueError("Backup schema version is incompatible")
            if payload_version == 2:
                # Validate the original checksum above before any legacy transformation.
                # Legacy plan_id labels and historical evidence stay untouched.
                payload = dict(payload)
                for collection in V03_DOCUMENT_TYPES:
                    payload[collection] = []
            if payload_version in {2, 3}:
                payload = dict(payload)
                for collection in V04_DOCUMENT_TYPES:
                    payload[collection] = []
            if payload_version in {2, 3, 4}:
                payload = dict(payload)
                for collection in V05_DOCUMENT_TYPES:
                    payload[collection] = []
            payload["format_version"] = 5
            for collection in EXPORT_TYPES:
                if not isinstance(payload.get(collection), list) or not all(isinstance(r, dict) for r in payload[collection]):
                    raise ValueError(f"Backup is missing collection {collection}")
            projects = {r["id"] for r in payload["projects"]}
            sources, nodes = {r["id"]: r for r in payload["sources"]}, {r["id"]: r for r in payload["nodes"]}
            records = {r["id"]: r for r in payload["records"]}
            for collection in EXPORT_TYPES:
                for row in payload[collection]:
                    if collection in V05_DOCUMENT_TYPES:
                        continue  # Tombstones may legitimately refer to removed projects.
                    if row.get("project_id") is not None and row["project_id"] not in projects:
                        raise ValueError("Backup contains an invalid project reference")
            for source in sources.values():
                if hashlib.sha256(source["content"].encode()).hexdigest() != source["sha256"]:
                    raise ValueError("Backup contains a corrupt source snapshot")
            for record in payload["records"]:
                if any(sid not in sources or sources[sid].get("project_id") != record["project_id"] for sid in record["source_ids"]):
                    raise ValueError("Backup contains an invalid evidence reference")
            for edge in payload["edges"]:
                if any(nid not in nodes or nodes[nid]["project_id"] != edge["project_id"] for nid in (edge["from_id"], edge["to_id"])):
                    raise ValueError("Backup contains an invalid graph endpoint")
            for plan in payload["plans"]:
                if not isinstance(plan.get("step_ids"), list) or any(
                    task_id not in records or records[task_id].get("kind") != "task"
                    or records[task_id].get("project_id") != plan["project_id"] for task_id in plan["step_ids"]
                ):
                    raise ValueError("Backup contains invalid managed plan steps")
            for run in payload["check_runs"]:
                record = records.get(run.get("task_id"))
                if not record or record.get("kind") != "task" or record.get("project_id") != run.get("project_id"):
                    raise ValueError("Backup contains an invalid machine check task reference")
                evidence = run.get("source_ids", {})
                if not isinstance(evidence, dict):
                    raise ValueError("Backup contains invalid machine check evidence")
                evidence_ids = [*evidence.values(), run.get("profile_source_id")]
                if any(not isinstance(sid, str) or sid not in sources
                       or sources[sid].get("project_id") != run.get("project_id") for sid in evidence_ids):
                    raise ValueError("Backup contains an invalid machine check source reference")
            if not isinstance(payload.get("vector_data", {}), dict):
                raise ValueError("Backup vector data is invalid")
            return payload
        except (OSError, EOFError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid or corrupt Cairnmind backup") from exc

    def restore(self, backup):
        """Restore validated data atomically. Stop Cairnmind workers before calling."""
        payload = self._validated_backup(Path(backup).expanduser().resolve())
        self.backup(self.data_dir / "backups" / f"before-restore-{uuid4().hex}{BACKUP_SUFFIXES[0]}")
        # DDL is deliberately outside the data transaction; no live data has been removed.
        for registry in payload["vector_indexes"]:
            name, dimension = registry.get("type_name"), registry.get("dimension")
            if not isinstance(name, str) or not name.startswith("vector_") or type(dimension) is not int or dimension <= 0:
                raise ValueError("Invalid vector namespace in backup")
            self.command(f"CREATE DOCUMENT TYPE {quote_name(name)} IF NOT EXISTS")
            self.command(f"CREATE PROPERTY {quote_name(name)}.id IF NOT EXISTS STRING")
            self.command(f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(id) UNIQUE")
            self.command(f"CREATE PROPERTY {quote_name(name)}.vector IF NOT EXISTS ARRAY_OF_FLOATS")
            self.command(f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(vector) LSM_VECTOR METADATA {{dimensions:{dimension},similarity:'COSINE',idPropertyName:'id'}}")
        with self.transaction() as tx:
            from .journal import Journal
            Journal.reset(tx, reason="restore")
            old_vectors = tx.query("SELECT FROM vector_indexes")
            for row in old_vectors:
                if row.get("type_name", "").startswith("vector_"):
                    tx.command(f"DELETE FROM {quote_name(row['type_name'])}")
            tx.command("DELETE FROM edges")
            tx.command("DELETE FROM nodes")
            for collection in reversed(DOCUMENT_TYPES):
                tx.command(f"DELETE FROM {quote_name(collection)}")
            for collection in DOCUMENT_TYPES:
                for original in payload[collection]:
                    row = dict(original)
                    if collection == "jobs" and row.get("state") == "running":
                        row.update(state="pending", worker=None, lease_until=None)
                    insert_document(tx, collection, row)
            for node in payload["nodes"]:
                self.upsert_node(node, tx)
            for edge in payload["edges"]:
                self.upsert_edge(edge, tx)
            for name, rows in payload.get("vector_data", {}).items():
                if not isinstance(name, str) or not name.startswith("vector_"):
                    raise ValueError("Invalid vector type in backup")
                for row in rows:
                    insert_document(tx, name, row)

    def forget_project(self, project_id):
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            from .journal import Journal
            Journal.reset(tx, reason="forget")
            self._delete_old_vectors(tx, [r["id"] for r in tx.query("SELECT id FROM sources WHERE project_id=:project", {"project": project_id})])
            tx.command("DELETE FROM edges WHERE project_id=:project", {"project": project_id})
            tx.command("DELETE FROM nodes WHERE project_id=:project", {"project": project_id})
            for collection in reversed(DOCUMENT_TYPES):
                if collection != "projects":
                    tx.command(f"DELETE FROM {quote_name(collection)} WHERE project_id=:project", {"project": project_id})
            tx.command("DELETE FROM projects WHERE id=:project", {"project": project_id})
