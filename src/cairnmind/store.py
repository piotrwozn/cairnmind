"""ArcadeDB source of truth, native graph, and verified portable backups."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cairnmind.cli_cmds.first_start import PROJECT_NAME_TAKEN, PROJECT_ROOT_TAKEN, UNKNOWN_PROJECT_NEXT
from cairnmind.config import BACKUP_FORMATS, BACKUP_SUFFIXES, Settings

# The sensitivity class of a source and a record (#180): derived on every read, stored nowhere.
from cairnmind.memory.sensitivity import record_sensitivity, source_sensitivity  # noqa: F401
from cairnmind.pal.file_locks import lock_exclusive, unlock
from cairnmind.pal.path_spelling import lies_in
from cairnmind.pal.terminal_safe import safe_text
from cairnmind.storage import store_graph, store_jobs
from cairnmind.storage.arcade import REQUEST_TIMEOUT
from cairnmind.storage.backup_reader import opened_backup
from cairnmind.storage.sql import PROFILE_NAME, clean_row, evidence_time, json_text, parse_utc, utc_now
from cairnmind.storage.storage_query import (
    any_of,
    asc,
    db_delete_where,
    db_find,
    db_find_one,
    db_insert_document,
    db_update_where,
    desc,
    eq,
    gt,
    is_in,
    is_null,
    lte,
    ne,
    not_in,
)
from cairnmind.storage.store_backup import VECTOR_COLLECTIONS, payload_digest, streamed_backup, write_backup
from cairnmind.storage.store_jobs import (  # noqa: F401
    _DIAGNOSTIC_CODE,
    _JOB_UNSET,
    JOB_STATES,
    JobNotFoundError,
    job_diagnostics,
)

SCHEMA_VERSION = 4
# Indexes for lookups by a field other than id and project_id. They are not part of the schema version:
# an earlier version works with them unchanged, so adding one needs no migration and blocks no way back.
LOOKUP_INDEXES = (("chunks", "source_id"), ("nodes", "source_id"), ("edges", "source_id"), ("edges", "from_id"),
                  ("edges", "to_id"), ("embeddings", "source_id"), ("embeddings", "chunk_id"), ("sources", "uri"),
                  ("record_revisions", "record_id"), ("check_runs", "task_id"), ("session_events", "session_id"),
                  ("resume_packets", "session_id"))
LOOKUP_INDEX_SET = 1
# Graph rows written by one request of Store.write_graph (issue #167).
GRAPH_ROWS_PER_REQUEST = 250
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
# What restore checks a backup against; an earlier format's payload has the later types empty.
RESTORE_TYPES = {"export": EXPORT_TYPES, "v03": V03_DOCUMENT_TYPES, "v04": V04_DOCUMENT_TYPES, "v05": V05_DOCUMENT_TYPES}
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
                or ".." in parts or ".git" in {part.casefold() for part in parts} or re.match(r"^[A-Za-z]:", pattern):
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


class KeyTaken(ValueError):
    """Another current record of the same kind holds the key of this write; it names that record (#238)."""

    def __init__(self, record_id):
        self.record_id = record_id
        super().__init__(f"Active record key already exists: record {record_id} holds it. Update that record "
                         "using its id and expected_version, or store this one under another key")


class RecordRefused(ValueError):
    """A record of a shape the store does not save (kind, status, content, key, metadata). The service marks
    it, so that the built-in agent hands the model the reason instead of a bare tool failure (#240)."""


def validate_task_state(value):
    """Validate a supplied task state without coercing or inferring a value."""
    if not isinstance(value, str) or value not in TASK_STATES:
        raise ValueError(f"Task state must be one of: {', '.join(sorted(TASK_STATES))}")


# Where a write came from, set by the service from the session row and never by a client: today only
# for an external client's session, whose conversation is unarchived and whose evidence is degraded.
ORIGIN_METADATA = ("origin_client", "origin_session_id", "origin_history", "origin_evidence")


# Set only by the extraction worker's own write, on the version it wrote, and dropped by any later write:
# the archived source this version was extracted from (#168). A client can neither set nor keep it.
EXTRACTION_MARK = "extraction_source_id"

# How the extraction worker made a record (#192): the extractor version, the prompt hash and the model as
# name@digest (extraction_input.extractor_stamp). Set only by the worker's own write; unlike EXTRACTION_MARK
# it describes the record, not this machine, so later writes keep it and portable memory carries it.
EXTRACTOR_STAMP = "extractor"
EXTRACTOR_MODEL_CHARS = 400
_PROMPT_SHA = re.compile(r"[0-9a-f]{16}")

# Record metadata derived by the store; values supplied by a client are never kept.
# "publication" is set only by the importer of published memory (#170): a client can neither forge it
# nor clear it, and it stays on later local versions the way an origin mark does.
# "confirmation" says how the record came to be confirmed (#177, confirmation.py): the store sets it from a
# person's act it checked itself, an importer sets it for what a package brings, and no client can.
DERIVED_RECORD_METADATA = ("evidence_earliest", "evidence_latest", "evidence_basis",
                           "supersede_warning", "supersede_replaced_evidence_latest", EXTRACTION_MARK,
                           EXTRACTOR_STAMP, *ORIGIN_METADATA, "publication", "confirmation", "quote_ref",
                           "symbol_anchor", "comment_anchor", "adr_anchor", "trailer_anchor")


def validate_extractor_stamp(value) -> dict:
    """A copy of a well-formed extractor stamp; ValueError for anything else."""
    if (not isinstance(value, dict) or set(value) != {"version", "prompt_sha256", "model"}
            or type(value["version"]) is not int or not 1 <= value["version"] <= 10**6
            or not isinstance(value["prompt_sha256"], str) or not _PROMPT_SHA.fullmatch(value["prompt_sha256"])
            or not isinstance(value["model"], str) or not 3 <= len(value["model"]) <= EXTRACTOR_MODEL_CHARS
            or "@" not in value["model"][1:-1]):
        raise ValueError("Invalid extractor stamp")
    return dict(value)


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
    # True for a store from open_readonly, which starts, creates and writes nothing, and for a store whose
    # installation is not the writer of its database (#70, store_writer).
    read_only = False
    # Why this installation only reads the store (another installation is its writer), or None.
    not_writer = None
    # Set when another installation took over writing after this store was opened: every write is refused.
    _writer_lost = None
    installation = None
    # False in the service: its working directory is not the caller's, so resolve_project refuses a relative path.
    relative_paths = True
    _writable = None
    # Store(path, for_takeover=True) of a reader whose store this release cannot read: why (see take_over_at).
    not_ready = None
    _for_takeover = False
    # Store(path, for_restore=True): the only opening that passes the mark of an unfinished restore (restore_staged).
    _for_restore = False
    _unfinished_restore = False
    # What the one-time step that marks records confirmed before 0.11.0 did when it ran on this open or the
    # last restore ({"marked", "before_rule", "at"}, confirmation_upgrade, #215); None when it did not run.
    confirmations_before_rule = None
    _backend = None

    @property
    def backend(self):
        if self._backend is None:
            from cairnmind.storage.arcade_backend import ArcadeBackend
            self._backend = ArcadeBackend(self)
        return self._backend

    @backend.setter
    def backend(self, value) -> None:
        self._backend = value
    # After a restore: how many completed embed jobs it set back to pending because the backup brought no vectors
    # (#187); None when the backup brought them or nothing was restored.
    embed_jobs_requeued = None
    restore_changed = False  # restore_staged: a delete of the restore may have been committed

    @classmethod
    def open_readonly(cls, settings: Settings) -> Store:
        """Read the existing store. Raises StoreNotReady, naming the command to run, instead of preparing it."""
        from cairnmind.storage.store_readonly import open_readonly
        return open_readonly(cls, settings)

    def read_consistently(self, read, kinds):
        """The result of ``read()``, which returns ``(project_id, result)`` and reads the journal entity ``kinds``.

        A read-only store repeats it while a change of those kinds in that project landed during it
        (else StoreBusy). A store opened with Store(path) reads behind the writer lock where it needs to,
        so ``read`` runs once.
        """
        if not self.read_only:
            return read()[1]
        from cairnmind.storage.store_readonly import read_consistently
        return read_consistently(self, read, kinds)

    @classmethod
    def open_memory(cls, data_dir: Path | None = None) -> Store:
        """Open a fast ephemeral in-memory SQLite store (zero Java/Docker; used by ``cairnmind try``)."""
        import tempfile
        root = Path(data_dir).expanduser().resolve() if data_dir is not None else Path(tempfile.gettempdir()) / "cairnmind-memory"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings = Settings(data_dir=root, storage_backend="sqlite", db_path=":memory:")
        return cls(root, settings=settings, backend="sqlite", db_path=":memory:")

    def close(self) -> None:
        if hasattr(self, "backend") and self.backend is not None:
            self.backend.close()

    def __init__(self, data_dir: Path, *, settings: Settings | None = None, for_takeover: bool = False,
                 for_restore: bool = False, backend: str | None = None, db_path: Path | str | None = None):
        from cairnmind.config import resolved_backend, sqlite_db_path
        self._for_takeover = bool(for_takeover)
        self._for_restore = bool(for_restore)
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if settings is not None and settings.data_dir.expanduser().resolve() != self.data_dir:
            raise ValueError("Provided settings must belong to this data directory")
        self.settings = settings if settings is not None else Settings.load(self.data_dir, create=True)
        from cairnmind.storage.store_writer import installation
        self.installation = installation(self.data_dir)
        chosen = (backend or "").strip().lower() or resolved_backend(self.settings)
        if chosen == "sqlite":
            from cairnmind.storage.sqlite_backend import SQLiteBackend
            target_db = db_path if db_path is not None else sqlite_db_path(self.settings)
            self.backend = SQLiteBackend(target_db, store=self)
            self.client = self.backend.as_client_proxy()
            self.backend.ensure_database()
        elif chosen == "arcade":
            from cairnmind.storage.arcade import ArcadeClient
            from cairnmind.storage.arcade_backend import ArcadeBackend
            from cairnmind.storage.engine import ensure_engine
            ensure_engine(self.settings)
            self.client = ArcadeClient(self.settings)
            self.backend = ArcadeBackend(self)
            self.client.ensure_database()
        else:
            raise ValueError(f"Unsupported storage backend: {chosen!r}")
        self._local = threading.local()
        self._stamp_lock = threading.Lock()
        self._stamp_cursor, self._stamp_base, self._stamp_global, self._stamp_seq = None, 0, 0, {}
        self.init()

    @contextmanager
    def held(self):
        """Keep the writer lock across the transactions this thread runs inside, as a restore does (#362): their
        own ``_lock`` is then this one, and every other process and thread waits."""
        with self._lock():
            self._local.lock_held = True
            try:
                yield
            finally:
                self._local.lock_held = False

    @contextmanager
    def _lock(self):
        if getattr(self._local, "lock_held", False):
            yield  # this thread holds it already; a second flock on a new handle would wait for itself
            return
        with (self.data_dir / "database-writer.lock").open("a", encoding="utf-8") as handle:
            lock_exclusive(handle)
            try:
                yield
            finally:
                unlock(handle)

    def query(self, sql, params=None):
        return (getattr(self._local, "transaction", None) or self.client).query(sql, params)

    def find(
        self,
        collection: str,
        where=(),
        *,
        columns=None,
        order_by=(),
        limit: int | None = None,
        distinct: bool = False,
    ) -> list[dict]:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "find"):
            return tx.find(collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct)
        return self.backend.find(
            collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct, reader=tx
        )

    def find_one(
        self,
        collection: str,
        where=(),
        *,
        columns=None,
        order_by=(),
        required: bool = False,
    ) -> dict | None:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "find_one"):
            return tx.find_one(collection, where, columns=columns, order_by=order_by, required=required)
        return self.backend.find_one(
            collection, where, columns=columns, order_by=order_by, required=required, reader=tx
        )

    def count(self, collection: str, where=()) -> int:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "count"):
            return int(tx.count(collection, where))
        return int(self.backend.count(collection, where, reader=tx))

    def group_counts(self, collection: str, group_by, where=(), *, count_alias: str = "total") -> list[dict]:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "group_counts"):
            return tx.group_counts(collection, group_by, where, count_alias=count_alias)
        return self.backend.group_counts(collection, group_by, where, count_alias=count_alias, reader=tx)

    def update_where(self, collection: str, *, set_fields=None, remove_fields=(), where=()) -> list[dict]:
        if self.read_only:
            if self.not_writer:
                from cairnmind.storage.store_writer import StoreNotWriter
                raise StoreNotWriter(self.not_writer)
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        tx = getattr(self._local, "transaction", None)
        if tx is not None:
            return self.backend.update_where(
                tx, collection, set_fields=set_fields, remove_fields=remove_fields, where=where
            )
        with self.transaction() as active_tx:
            return self.backend.update_where(
                active_tx, collection, set_fields=set_fields, remove_fields=remove_fields, where=where
            )

    def delete_where(self, collection: str, where=(), *, limit: int | None = None) -> list[dict]:
        if self.read_only:
            if self.not_writer:
                from cairnmind.storage.store_writer import StoreNotWriter
                raise StoreNotWriter(self.not_writer)
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        tx = getattr(self._local, "transaction", None)
        if tx is not None:
            return self.backend.delete_where(tx, collection, where, limit=limit)
        with self.transaction() as active_tx:
            return self.backend.delete_where(active_tx, collection, where, limit=limit)

    def command(self, sql, params=None):
        from cairnmind.storage.journal import mutation
        if self.read_only:
            if self.not_writer:
                from cairnmind.storage.store_writer import StoreNotWriter
                raise StoreNotWriter(self.not_writer)
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        tx = getattr(self._local, "transaction", None)
        if tx:
            return tx.command(sql, params)
        if mutation(sql):
            with self.transaction() as tx:
                return tx.command(sql, params)
        from cairnmind.storage.store_writer import require_writer
        with self._lock():
            # Not journaled, so not fenced by the journal head: the writer is read before the statement.
            require_writer(self)
            return self.client.command(sql, params)

    @contextmanager
    def transaction(self, commit_timeout=REQUEST_TIMEOUT):
        """``commit_timeout``: seconds the client waits for the commit (a restore's transactions ask for longer)."""
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
        from cairnmind.storage.journal import JournalTransaction
        from cairnmind.storage.store_writer import StoreWriterLost, check_head, recheck
        recheck(self)  # one read, only while the role is lost: a person may have taken it back
        if self._writer_lost:
            raise StoreWriterLost(self._writer_lost)
        inst = self.installation
        if inst is None:
            raise RuntimeError("Store installation is not initialized")
        with self._lock(), self.client.transaction(commit_timeout=commit_timeout) as native:
            tx = JournalTransaction(native, writer=inst.id,
                                    check=lambda head, reader: check_head(self, head, reader))
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

    @contextmanager
    def session_write(self, session_id):
        """Mark what this thread writes inside the block as a session's: never a person's act (#177)."""
        previous = getattr(self._local, "session_write", None)
        self._local.session_write = session_id or previous
        try:
            yield
        finally:
            self._local.session_write = previous

    @contextmanager
    def person_act(self, route, source_id):
        """Open a person's act for the record writes of this thread inside the block (#177).

        Only the code of a person's route opens it, where it knows a person is acting, and it names the
        audit or note source that route has just written. Nothing in a request can open it. The write
        itself checks the source again (_confirmation), so a wrong caller is refused, not trusted.

        Opening it is not enough (#214): the act counts only when the request that opened it passed
        person_proof.require, which proves the open workspace (_proven_act). An act that does not count is
        what a session's write is: it confirms nothing and takes no confirmation away.
        """
        from cairnmind.memory.confirmation import PERSON_ROUTES, SEED_ROUTE
        if route not in PERSON_ROUTES and route != SEED_ROUTE:
            raise ValueError("Unknown route of a person's act")
        previous = getattr(self._local, "person_act", None)
        self._local.person_act = {"route": route, "source_id": source_id}
        try:
            yield
        finally:
            self._local.person_act = previous

    def _proven_act(self):
        """The person's act open on this thread, counted only when the request that opened it carried the
        workspace's key (#214). Evaluation seed data is not a person and needs none."""
        act = getattr(self._local, "person_act", None)
        if not act:
            return None
        from cairnmind.memory.confirmation import SEED_ROUTE
        from cairnmind.terminal.person_proof import present
        return act if act["route"] == SEED_ROUTE or present() else None

    def _confirmation(self, tx, project_id, old, asked, status, statement, sources, version):
        """The one rule for the status confirmed (#177): (status, mark) as this write stores them.

        A write stores `confirmed` only (a) as a person's act the store checks here, or (b) by keeping a
        confirmation whose statement it does not change (progress on a task, a note of a conflict decision).
        Anything else that asks for `confirmed` is refused. A write that is not a person's act never
        reaches this point on a record a person confirmed (_keep_verdict refuses it first). On a
        confirmation that is not a person's verdict given here (imported, seed data, the checkpoint record)
        a write that does not ask for a status and changes what was confirmed is stored as proposed, with a
        mark that says the confirmation lapsed; the same mark is left when such a write sets another status.
        A person's act leaves no such mark.
        """
        from cairnmind.memory.confirmation import (
            MARK,
            NOT_PROVEN,
            PERSON_ROUTES,
            SEED_ROUTE,
            SEED_SOURCE,
            ConfirmationRefused,
            lowered,
            person_mark,
            said,
        )
        act = self._proven_act()
        if status != "confirmed":
            # A person's act (a dispute, a conflict decision) leaves no such mark and clears an earlier one.
            was = ((old or {}).get("metadata") or {}).get(MARK)
            return status, None if act else lowered(old, status, version) or (
                was if isinstance(was, dict) and was.get("by") == "lapsed" else None)
        if asked == "confirmed" and not act and getattr(self._local, "person_act", None):
            raise ConfirmationRefused(NOT_PROVEN)  # an act is open, but not proven by the workspace's key
        if asked == "confirmed" and act:
            if self.current_origin() or getattr(self._local, "session_write", None):
                raise ConfirmationRefused()
            prefix, origin = SEED_SOURCE if act["route"] == SEED_ROUTE else PERSON_ROUTES[act["route"]]
            source = db_find_one(
                tx,
                "sources",
                {"id": act["source_id"]},
                columns=("id", "project_id", "uri", "metadata", "content"),
            ) if act["source_id"] in sources else None
            if (not source or source.get("project_id") != project_id
                    or not str(source.get("uri", "")).startswith(prefix)
                    or (source.get("metadata") or {}).get("origin") != origin):
                raise ConfirmationRefused()
            return status, person_mark(act["route"], source, version, utc_now())
        if old and old["status"] == "confirmed" and statement == said(
                old["kind"], old["content"], old["key"], old.get("supersedes"), old.get("metadata")):
            kept = (old.get("metadata") or {}).get(MARK)
            return status, kept if isinstance(kept, dict) and kept.get("by") != "lapsed" else None
        if asked == "confirmed" or not old or old["status"] != "confirmed":
            raise ConfirmationRefused()
        return "proposed", None if act else lowered(old, "proposed", version)

    def _persons_act(self) -> bool:
        """Whether this thread writes as a person: an act of a person's route is open (evaluation seed data
        is not one), and no session or marked origin writes."""
        from cairnmind.memory.confirmation import PERSON_ROUTES
        act = self._proven_act()
        return bool(act) and act["route"] in PERSON_ROUTES and not (
            self.current_origin() or getattr(self._local, "session_write", None))

    def open_person_act(self):
        """The person's act open on this thread ({route, source_id}), or None when this write is not one."""
        return dict(self._local.person_act) if self._persons_act() else None

    def _kept(self, record) -> bool:
        """Whether this write is not a person's act and `record` is confirmed by a person here (#177)."""
        from cairnmind.memory.confirmation import kept_verdict
        return not self._persons_act() and kept_verdict(record)

    def _keep_verdict(self, record, change, status=None, differs=()):
        """A write that is not a person's act never takes a person's confirmation away (#177): refused."""
        from cairnmind.memory.confirmation import VerdictKept
        if self._kept(record):
            raise VerdictKept(record, change, status, differs)

    def init(self):
        from cairnmind.storage.arcade import ArcadeError
        from cairnmind.storage.store_writer import (
            claimed_meanwhile,
            mixed_reason,
            read_fence,
            read_writer,
            settle,
        )
        inst = self.installation
        if inst is None:
            raise RuntimeError("Store installation is not initialized")
        with self._lock():
            types = self.backend.list_collections(self.client)
            writer, named = read_writer(self.client) if "devbrain_meta" in types else None, None
            if writer and "journal_state" in types:
                writer, named = read_fence(self.client)
            if writer and writer.get("installation_id") != inst.id:
                # Another installation writes this database: no schema statement, no upgrade and no write here.
                self._open_as_reader(writer)
                return
            if "devbrain_meta" in types:
                # A restore that did not finish (#362): no command uses a half-restored store, and nothing runs over it.
                from cairnmind.storage.restore_staged import RestoreUnfinished, refusal, unfinished
                mark = unfinished(self.client)
                if mark is not None and not (self._for_restore or self._for_takeover):
                    raise RestoreUnfinished(refusal(mark), mark)
                self._unfinished_restore = mark is not None
            if writer and named not in (None, inst.id):
                # The row names this installation and the journal head another one: no schema statement and no
                # write here either, until a person runs the takeover (store_writer.mixed_reason).
                self._writer_lost = mixed_reason(writer, named)
                return
            try:
                self._prepare_schema()
            except ArcadeError as exc:
                # Another installation, on any machine, is creating this store at the same moment and its
                # commit came first (#68): this one opens it as a reader, as it would a moment later.
                other = claimed_meanwhile(self.client, inst, exc)
                if other is None:
                    raise
                self._open_as_reader(other)
                return
            try:
                other = settle(self.client, inst)
            except ArcadeError:
                # Two installations claimed a store without a writer at the same moment: one commit failed.
                other = settle(self.client, inst)
            if other:
                self._open_as_reader(other)
                return
        if self._unfinished_restore:
            return  # the one-time step would write into a store that holds only part of a backup
        # As the writer, once the schema is current: the writer lock is taken again by the transaction.
        from cairnmind.memory.confirmation_upgrade import mark_confirmed_before_rule
        self.confirmations_before_rule = mark_confirmed_before_rule(self)

    def _open_as_reader(self, writer):
        """Go on as a store that reads only: every write is refused with the reason (store_writer)."""
        from cairnmind.storage.store_readonly import ReadOnlyClient, StoreNotReady, check_ready
        from cairnmind.storage.store_writer import reader_reason
        inst = self.installation
        if inst is None:
            raise RuntimeError("Store installation is not initialized")
        reason = reader_reason(inst, writer)
        try:
            check_ready(self.client, self.settings)
        except StoreNotReady as exc:
            if not self._for_takeover:
                # `cairnmind init` does not help a reader: the writer, or a takeover, makes the store ready.
                raise StoreNotReady(f"{exc} {reason}") from None
            self.not_ready = str(exc)
        self.not_writer = reason
        self._writable = self.client
        self.client = ReadOnlyClient(self.client, self.not_writer)
        self.read_only = True

    def writer_state(self):
        """Which installation writes this store, and whether this one is the writer, a reader or lost it (#70)."""
        from cairnmind.storage.store_writer import state
        return state(self)

    def _prepare_schema(self):
        """Create or upgrade the schema; called by init, with the writer lock held, when no other installation writes."""
        self.backend.init_schema(self)

    def _lookup_indexes(self, recorded):
        """Create the lookup indexes that are missing (delegated to backend)."""
        if hasattr(self.backend, "_lookup_indexes"):
            self.backend._lookup_indexes(self, recorded)

    @staticmethod
    def _require_project(tx, project_id):
        known = getattr(tx, "known", None)
        if known is not None and project_id in known["projects"]:
            return
        if project_id is not None and not db_find_one(tx, "projects", {"id": project_id}, columns=("id",)):
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
        with store_graph.keeping_known(tx) as known:
            yield known

    def prefetch_graph(self, tx, nodes, edges):
        """Read, in a few requests, what upsert_node and upsert_edge would look up row by row."""
        return store_graph.prefetch_graph(self, tx, nodes, edges)

    def require_safe_project_root(self, root):
        path = str(Path(root).expanduser().resolve())
        if lies_in(path, self.data_dir):
            # Indexing always leaves the data directory out, so such a folder would report 0 files (#295).
            raise ValueError("Project folder cannot be inside the data directory: Cairnmind never indexes "
                             "it; choose a folder outside it")
        if ((self.data_dir / "credentials" / "api-keys.json").exists()
                and lies_in(self.data_dir, path)):
            raise ValueError("Project root cannot contain Cairnmind's private credential store")
        return path

    def add_project(self, name, root, restore=True):
        name, path = name.strip(), self.require_safe_project_root(root)
        if not name:
            raise ValueError("Project name must not be empty")
        if len(name) > 256:
            # The limit of the service's route, for every route (#238).
            raise ValueError("Project name must be at most 256 characters")
        with self.transaction() as tx:
            existing = db_find_one(tx, "projects", any_of(eq("name", name), eq("root", path)))
            if existing:
                if existing["name"] != name or existing["root"] != path:
                    if existing["name"] == name:
                        raise ValueError(PROJECT_NAME_TAKEN.format(name=safe_text(name), root=safe_text(existing["root"])))
                    raise ValueError(PROJECT_ROOT_TAKEN.format(name=safe_text(existing["name"]), root=safe_text(path)))
                return existing
            from cairnmind.memory.project_removal import restore_in
            restored = restore_in(tx, name, path, restore)  # the same name and folder as a removed project (#327)
            if restored:
                return restored
            project = {"id": str(uuid4()), "name": name, "root": path, "created_at": utc_now()}
            db_insert_document(tx, "projects", project)
            from cairnmind.memory.project_folders import stamp
            db_insert_document(tx, "project_roots", {"id": str(uuid4()), "project_id": project["id"], "path": path, **stamp()})
            return project

    def add_project_root(self, project_id, path) -> dict:
        """Register a folder of the project, recording this machine as the one that has it (#371). A folder that
        another machine registered is moved to this one, and `moved_from` says from which host."""
        from cairnmind.memory.project_folders import stamp, this_machine
        from cairnmind.storage.store_writer import label
        root = self.require_safe_project_root(path)
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            existing = db_find_one(tx, "project_roots", {"path": root})
            if existing and existing["project_id"] != project_id:
                raise ValueError("Directory already belongs to another project")
            if not existing:
                db_insert_document(tx, "project_roots", {"id": str(uuid4()), "project_id": project_id, "path": root, **stamp()})
            elif (here := this_machine()) and existing.get("machine") != here:
                db_update_where(tx, "project_roots", set_fields=stamp(), where={"id": existing["id"]})
                if existing.get("machine"):
                    return {"moved_from": label(existing.get("host"))}
        return {}

    def list_projects(self):
        return [clean_row(row) for row in self.find("projects", is_null("removed_at"), order_by=(asc("name"),))]

    def resolve_project(self, identifier, *, recorded: bool = False):
        """The project of an id, a name, or a path inside one of its registered folders (#238).

        A value that matches no id or name is read as a path only when it is written as one (absolute,
        holding a separator, or starting with "." or "~") and the path exists; anything else is an
        unknown project, so a mistyped name never falls back to the working directory's project. A
        store whose working directory is not the caller's (the service, relative_paths False) refuses
        a relative path. recorded=True is for an absolute working directory a client recorded (a
        transcript or a hook event): it names a place, typed by nobody, that may since have been removed.
        """
        value = str(identifier)
        from cairnmind.memory.project_removal import find_removed, unknown_removed
        project = db_find_one(
            self, "projects", [is_null("removed_at"), any_of(eq("id", value), eq("name", value))]
        )
        if project:
            return project
        if not value.startswith((".", "~")) and "/" not in value and os.sep not in value:
            # A removed project is named, with the way back, never reported as unknown (#327).
            removed = find_removed(self, value)
            if removed:
                raise unknown_removed(value, removed, self.data_dir)
            raise ValueError(f"Unknown project: {value}. {UNKNOWN_PROJECT_NEXT}")
        written = Path(value).expanduser()
        if not written.is_absolute() and (recorded or not self.relative_paths):
            raise ValueError(f"Unknown project: {value} (a relative path is not accepted here; give an absolute one)")
        path = written.resolve()
        if not recorded and not path.exists():
            raise ValueError(f"Unknown project: {value} (no such file or folder)")
        candidates = [r for r in self.find("project_roots")
                      if path == Path(r["path"]) or Path(r["path"]) in path.parents]
        if candidates:
            selected = max(candidates, key=lambda row: len(row["path"]))
            return db_find_one(self, "projects", {"id": selected["project_id"]}, required=True)
        removed = find_removed(self, value, path)
        if removed:
            raise unknown_removed(value, removed, self.data_dir)
        raise ValueError(f"Unknown project: {value}. {UNKNOWN_PROJECT_NEXT}")

    def _delete_old_vectors(self, tx, source_ids):
        self.backend.delete_vectors_for_sources(tx, source_ids)

    @staticmethod
    def drop_chunks(tx, source_ids):
        """Delete the chunks of sources that stop being current, in the transaction that replaces them (#186).

        Nothing reads a chunk of a source that is not current: search, vectors and the reparse of a transcript take
        current chunks only, and an import rebuilds chunks from the source text. The source itself, its text and
        its hash stay, so evidence that cites the old version can still be read and checked."""
        if source_ids:
            db_delete_where(tx, "chunks", is_in("source_id", list(source_ids)))

    def snapshot(self, project_id, uri, content, kind="document", metadata=None):
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        digest, key = hashlib.sha256(content.encode()).hexdigest(), json_text([project_id, uri])
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            existing = db_find_one(tx, "sources", {"current_key": key, "is_current": True})
            if existing and existing["sha256"] == digest:
                return existing
            if existing:
                db_update_where(
                    tx, "sources", set_fields={"is_current": False, "current_key": None}, where={"id": existing["id"]}
                )
                self.drop_chunks(tx, [existing["id"]])
                db_update_where(
                    tx,
                    "nodes",
                    set_fields={"is_current": False},
                    where=[
                        eq("source_id", existing["id"]),
                        not_in(
                            "kind",
                            ("source", "decision", "task", "fact", "constraint", "question", "preference", "checkpoint"),
                        ),
                    ],
                )
                db_update_where(
                    tx,
                    "edges",
                    set_fields={"is_current": False},
                    where=[
                        eq("source_id", existing["id"]),
                        not_in("kind", ("supported_by", "depends_on", "supersedes")),
                    ],
                )
                self._delete_old_vectors(tx, [existing["id"]])
            source = {"id": str(uuid4()), "project_id": project_id, "uri": uri, "kind": kind, "content": content,
                      "sha256": digest, "metadata": {**(metadata or {}), **(self.current_origin() or {})},
                      "is_current": True, "current_key": key, "created_at": utc_now()}
            db_insert_document(tx, "sources", source)
            return source

    def snapshot_new(self, tx, project_id, uri, content, kind="document", metadata=None):
        """The source of a URI that has no current one, written inside the caller's transaction.

        Returns None, having written nothing, when the URI has a current source: the caller then
        takes ``snapshot``, which knows how to keep or replace it. The row is the one ``snapshot`` writes.
        """
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        if db_find_one(
            tx, "sources", {"current_key": json_text([project_id, uri]), "is_current": True}, columns=("id",)
        ):
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
        db_insert_document(tx, "sources", source)
        return source

    def enqueue_new(self, tx, kind, project_id, payload, dedupe_key):
        """A job whose key cannot exist yet, because it names a row created in this same transaction."""
        return store_jobs.enqueue_new(self, tx, kind, project_id, payload, dedupe_key)

    def get_source(self, source_id):
        return db_find_one(self, "sources", {"id": source_id}, required=True)

    @staticmethod
    def _validate_sources(tx, project_id, sources):
        if not isinstance(sources, list) or not sources or not all(isinstance(item, str) for item in sources):
            raise ValueError("A memory record requires at least one source ID: cite a source you read, "
                             "or the source_ids a checkpoint returns")
        for source_id in sources:
            source = db_find_one(tx, "sources", {"id": source_id}, columns=("project_id",))
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
            run = db_find_one(tx, "check_runs", {"id": run_id}, columns=("task_id", "project_id"))
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
            task = db_find_one(
                tx,
                "records",
                [eq("id", task_id), eq("project_id", project_id), eq("kind", "task"), ne("status", "superseded")],
            )
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
        return store_graph.upsert_node(self, node, tx=tx)

    def _checked_node(self, tx, node):
        """The vertex upsert_node writes, after its checks, and the stored row with its ID if there is one."""
        return store_graph.checked_node(self, tx, node)

    def upsert_edge(self, edge, tx=None):
        return store_graph.upsert_edge(self, edge, tx=tx)

    def _checked_edge(self, tx, edge):
        """The edge upsert_edge writes, after its checks, and the stored row with its ID if there is one."""
        return store_graph.checked_edge(self, tx, edge)

    def write_graph(self, tx, project_id, nodes, edges):
        """Write graph rows as upsert_node, then upsert_edge, would for each, with one request per group of rows."""
        return store_graph.write_graph(self, tx, project_id, nodes, edges)

    def remove_graph_nodes(self, tx, project_id, identifiers):
        """Delete the project's vertices among `identifiers` that no edge names, one group per request."""
        return store_graph.remove_graph_nodes(self, tx, project_id, identifiers)

    @staticmethod
    def _load_checked_sources(tx, project_id, sources):
        return store_graph.load_checked_sources(tx, project_id, sources)

    def _record_graph(self, tx, record, *, source_rows=None):
        return store_graph.record_graph(self, tx, record, source_rows=source_rows)

    def _revision(self, tx, record, *, source_rows=None):
        db_insert_document(tx, "record_revisions", {"id": f"{record['id']}:{record['version']}", "project_id": record["project_id"],
                                        "record_id": record["id"], "version": record["version"], "snapshot": record, "recorded_at": utc_now()})
        self._record_graph(tx, record, source_rows=source_rows)

    def _derive_symbol_anchor(self, tx, project_id, sources, metadata, *, source_rows=None):
        """Derive a cross-OS symbol anchor from cited code sources when metadata names a symbol or quote."""
        from cairnmind.memory.symbol_anchor import posix_path, resolve_symbol_anchor
        from cairnmind.scip.parsing import analyze_code
        symbol_name = metadata.get("symbol") if isinstance(metadata.get("symbol"), str) else None
        quote = metadata.get("evidence") if isinstance(metadata.get("evidence"), str) else None
        if not (symbol_name and symbol_name.strip()) and not (quote and quote.strip()):
            return None
        if source_rows is not None:
            code_sources = [source_rows[sid] for sid in dict.fromkeys(sources)
                            if sid in source_rows and source_rows[sid].get("kind") == "code"]
        else:
            code_sources = db_find(
                tx,
                "sources",
                [eq("project_id", project_id), is_in("id", list(dict.fromkeys(sources))), eq("kind", "code")],
                columns=("id", "uri", "kind", "content", "metadata"),
            )
        if not code_sources:
            return None
        by_id = {row["id"]: row for row in code_sources}
        for source_id in sources:
            src = by_id.get(source_id)
            if not src:
                continue
            raw_meta = src.get("metadata")
            src_meta = raw_meta if isinstance(raw_meta, dict) else {}
            rel = src_meta.get("relative_path")
            if not isinstance(rel, str) or not rel.strip():
                raw_path = src_meta.get("path") or src.get("uri") or ""
                raw_root = src_meta.get("root")
                if isinstance(raw_path, str) and isinstance(raw_root, str) and raw_path and raw_root:
                    try:
                        rel = Path(raw_path).relative_to(Path(raw_root)).as_posix()
                    except ValueError:
                        rel = Path(raw_path).name
                else:
                    rel = Path(str(raw_path)).name
            rel = posix_path(rel)
            if not rel:
                continue
            raw_analysis = src_meta.get("analysis")
            analysis = raw_analysis if isinstance(raw_analysis, dict) else None
            has_hashes = bool(
                analysis
                and any(isinstance(sym.get("body_hash"), str) for sym in (analysis.get("symbols") or []))
            )
            if not has_hashes:
                analysis = analyze_code(Path(rel), str(src.get("content") or ""))
            resolved = resolve_symbol_anchor(
                rel,
                str(src.get("content") or ""),
                analysis,
                quote=quote,
                symbol_name=symbol_name,
            )
            if resolved is not None:
                return resolved
        return None

    def _remember(self, tx, project_id, record, sources, *, managed_batch=False, conflict_review=False,
                  replace_origin=False, replace_metadata=False, mark_only_changes=False, extracted_from=None,
                  extractor=None, slot=False, new_id=None, symbol_anchor=None, comment_anchor=None,
                  adr_anchor=None, trailer_anchor=None, quote_ref=None):
        """Write one record version.

        new_id: the ID of a new record, chosen by a person's act whose audit names it before the write (#222).

        slot: the write replaces the content of the one checkpoint record, which is a slot for the latest
        state and not a statement: a confirmation of an earlier state lapses instead of refusing the write.

        replace_origin: the origin mark follows this writer instead of the old version (checkpoints).
        replace_metadata: the metadata is built from this write alone, not merged onto the old version's.
        Used when a non-external checkpoint replaces an external one, so no external text stays behind
        unmarked; state that earlier native checkpoints set and neither checkpoint repeats is dropped too.
        mark_only_changes: stamp the current origin only if the stored result differs (plan steps).
        extracted_from: the extraction worker's new record from this one source (EXTRACTION_MARK).
        extractor: that record's extractor stamp (EXTRACTOR_STAMP), only together with extracted_from.
        """
        self._require_project(tx, project_id)
        source_rows = self._load_checked_sources(tx, project_id, sources)
        if not isinstance(record, dict):
            raise RecordRefused("record must be an object")
        old = None
        if record.get("id"):
            old = db_find_one(tx, "records", {"id": record["id"], "project_id": project_id})
            if not old and not managed_batch:
                raise ValueError("Cannot update an unknown record")
            if old and (type(record.get("expected_version")) is not int or record["expected_version"] != old["version"]):
                raise ValueError("Record version conflict; read the current version and retry")
            if not old and record.get("expected_version") is not None:
                raise ValueError("New managed records must not provide expected_version")
            if old and old["status"] == "superseded" and not managed_batch:
                raise ValueError("A superseded record is immutable; create a new record")
        if new_id is not None and (old is not None or record.get("id")):
            raise ValueError("new_id names a new record; it cannot update one")
        # A write that is not a person's act and reaches a record a person confirmed (it can only leave the
        # statement as it is: progress on a task, or nothing) does not make that confirmation worth less: the
        # record keeps every source it had, the note of the person's act among them, and the write only adds.
        kept = old is not None and not slot and self._kept(old)
        if kept and old is not None:
            held = old.get("source_ids") or []
            sources = list(dict.fromkeys([*(json.loads(held) if isinstance(held, str) else held), *sources]))
            extra_sources = [sid for sid in sources if sid not in source_rows]
            for start in range(0, len(extra_sources), 500):
                for row in db_find(tx, "sources", is_in("id", extra_sources[start:start + 500])):
                    cleaned = clean_row(row)
                    source_rows[cleaned["id"]] = cleaned
        kind, status = record.get("kind", old["kind"] if old else None), record.get("status", old["status"] if old else "proposed")
        content = record.get("content", old["content"] if old else None)
        if not isinstance(kind, str) or not isinstance(status, str) or kind not in KINDS or status not in STATUSES:
            raise RecordRefused("Unknown record kind or status")
        if old and kind != old["kind"]:
            raise RecordRefused("Record kind cannot change")
        if not isinstance(content, str) or not content.strip():
            raise RecordRefused("Record content must be nonempty text")
        record_id, key = (old["id"] if old else new_id or record.get("id") or str(uuid4())), record.get("key", old["key"] if old else None)
        key = record_id if key is None else key
        if not isinstance(key, str) or not key.strip():
            raise RecordRefused("Record key must be nonempty text")
        metadata = dict(old.get("metadata", {})) if old and not replace_metadata else {}
        updates = record.get("metadata", {})
        if not isinstance(updates, dict):
            raise RecordRefused("Record metadata must be an object")
        if not conflict_review:
            # An agent may mark a possible conflict when it creates a finding, but it can
            # neither unmark one later nor record a decision about it.
            updates = {key: value for key, value in updates.items() if key not in CONFLICT_REVIEW_METADATA
                       and not (old and key == "possible_conflicts")}
        metadata.update({key: value for key, value in updates.items() if key not in DERIVED_RECORD_METADATA})
        if quote_ref is not None:
            metadata["quote_ref"] = quote_ref
        elif old and "evidence" in updates and updates["evidence"] != (old.get("metadata") or {}).get("evidence"):
            metadata.pop("quote_ref", None)
        if "depends_on" in metadata and metadata["depends_on"] is None:
            # A model's metadata may say depends_on: null; that is no dependencies, not a failed write (#238).
            del metadata["depends_on"]
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
        evidence = evidence_time(tx, sources, source_rows=source_rows)
        metadata.update(evidence_earliest=evidence["earliest"], evidence_latest=evidence["latest"],
                        evidence_basis=evidence["basis"])
        if symbol_anchor is not None:
            from cairnmind.memory.symbol_anchor import validate_symbol_anchor
            validated_anchor = validate_symbol_anchor(symbol_anchor)
            if validated_anchor is None:
                raise ValueError("Invalid symbol_anchor")
            metadata["symbol_anchor"] = validated_anchor
        elif "symbol_anchor" not in metadata or any(field in updates for field in ("symbol", "evidence")):
            derived_anchor = self._derive_symbol_anchor(tx, project_id, sources, metadata, source_rows=source_rows)
            if derived_anchor is not None:
                metadata["symbol_anchor"] = derived_anchor
        if comment_anchor is not None:
            if not isinstance(comment_anchor, dict) or not comment_anchor.get("tag") or not comment_anchor.get("text_sha256"):
                raise ValueError("Invalid comment_anchor")
            metadata["comment_anchor"] = dict(comment_anchor)
        if adr_anchor is not None:
            if not isinstance(adr_anchor, dict) or not adr_anchor.get("path") or not adr_anchor.get("text_sha256"):
                raise ValueError("Invalid adr_anchor")
            metadata["adr_anchor"] = dict(adr_anchor)
        if trailer_anchor is not None:
            if not isinstance(trailer_anchor, dict) or not trailer_anchor.get("commit") or not trailer_anchor.get("text_sha256"):
                raise ValueError("Invalid trailer_anchor")
            metadata["trailer_anchor"] = dict(trailer_anchor)
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
                                assert_completion=explicit_completion,
                                validate_dependencies=not managed_batch and status != "superseded")  # retiring needs none
        # Decided on the final metadata, after every field this write drops (criteria links, reported proof).
        origin = self.current_origin()
        if origin and (mark_only_changes or kept) and not outcome_changed(old, content, key, metadata):
            # A plan save rewrites every step; one it leaves as it was keeps its own mark, or none. The same
            # for a record a person confirmed: a write that stores nothing new does not stamp its origin on it.
            origin = None
        metadata.update(origin or {})
        metadata.pop(EXTRACTION_MARK, None)
        if extracted_from is not None:
            if old or list(sources) != [extracted_from]:
                raise ValueError("An extraction mark belongs only to a new record from its one source")
            metadata[EXTRACTION_MARK] = extracted_from
        if extractor is not None:
            # After the client's metadata, which can neither set nor clear it; a later write keeps it.
            if extracted_from is None:
                raise ValueError("An extractor stamp belongs only to the extraction worker's new record")
            metadata[EXTRACTOR_STAMP] = validate_extractor_stamp(extractor)
        supersedes = record.get("supersedes", old.get("supersedes") if old else None)
        if supersedes and (not old or supersedes != old.get("supersedes")):
            previous = db_find_one(tx, "records", {"id": supersedes, "project_id": project_id})
            if not previous or previous["id"] == record_id or previous["status"] == "superseded":
                raise ValueError("supersedes must identify an active record in the same project")
            if previous["kind"] != kind:
                raise ValueError("Replacement and replaced record must have the same kind")
            if previous["kind"] == "task" and previous.get("metadata", {}).get("plan_ref") and not managed_batch:
                raise ValueError("Managed task membership must be changed through PlanManager")
            if not old and "expected_version" in record and record["expected_version"] != previous["version"]:
                raise ValueError("Record version conflict; replaced record has changed")
            from cairnmind.memory.confirmation import MARK as mark_name
            from cairnmind.memory.confirmation import lowered
            self._keep_verdict(previous, "replace")
            act = self._proven_act()
            if not act and previous["status"] == "confirmed":
                # A replacement that is not a person's act takes a confirmed record out of use: it says so.
                previous["metadata"] = {**(previous.get("metadata") or {}),
                                        mark_name: lowered(previous, "superseded", previous["version"] + 1)}
            if conflict_review and act:
                # A conflict decision judged both findings (#239): the replaced one's last version cites its audit,
                # so that version travels as the person's act and not as a mechanical supersession.
                cited = previous.get("source_ids") or []
                cited = json.loads(cited) if isinstance(cited, str) else cited
                previous["source_ids"] = list(dict.fromkeys([*cited, act["source_id"]]))
            previous.update(status="superseded", active_key=None, version=previous["version"] + 1, updated_at=utc_now())
            db_update_where(tx, "records", set_fields=previous, where={"id": supersedes})
            self._revision(tx, previous)
        # Recomputed on every write of a replacement, so a later source change never
        # leaves a stale warning behind. Warn, never block.
        metadata.pop("supersede_warning", None)
        metadata.pop("supersede_replaced_evidence_latest", None)
        if supersedes:
            replaced = db_find_one(
                tx, "records", {"id": supersedes, "project_id": project_id}, columns=("source_ids",)
            ) or {}
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
        # The rule for `confirmed`, on the final statement of this write and for every route (#177).
        from cairnmind.memory.confirmation import MARK, said, said_difference
        statement = said(kind, content, key, supersedes, metadata)
        if old and status != "confirmed":
            self._keep_verdict(old, "status", status)
        elif old and not slot:
            before = said(old["kind"], old["content"], old["key"], old.get("supersedes"), old.get("metadata"))
            if statement != before:
                self._keep_verdict(old, "change", differs=said_difference(before, statement))
        status, confirmation = self._confirmation(
            tx, project_id, old, record.get("status"), status, statement, sources, old["version"] + 1 if old else 1)
        metadata.pop(MARK, None)
        if confirmation:
            metadata[MARK] = confirmation
        active_key = json_text([project_id, kind, key]) if status != "superseded" else None
        collision = db_find_one(tx, "records", {"active_key": active_key}, columns=("id",)) if active_key else None
        if collision and collision["id"] != record_id:
            # A record a person confirmed is refused as such: updating it by id would only be refused again.
            self._keep_verdict(db_find_one(tx, "records", {"id": collision["id"]}, required=True), "key")
            raise KeyTaken(collision["id"])
        now = utc_now()
        result = {"id": record_id, "project_id": project_id, "kind": kind, "key": key, "content": content, "status": status,
                  "source_ids": list(dict.fromkeys(sources)), "version": old["version"] + 1 if old else 1, "supersedes": supersedes,
                  "metadata": metadata, "created_at": old["created_at"] if old else now, "updated_at": now, "active_key": active_key}
        if explicit_completion and metadata.get("requires_test"):
            from cairnmind.memory.verification import validate_completion
            validate_completion(self, tx, project_id, {**result, "_completion_worktree": record.get("worktree")})
        if old:
            updated = db_update_where(
                tx, "records", set_fields=result, where={"id": record_id, "version": old["version"]}
            )
            if isinstance(updated, list) and (
                not updated
                or (isinstance(updated[0], dict) and type(updated[0].get("count")) is int and updated[0]["count"] != 1)
            ):
                raise ValueError("Record version conflict; read the current version and retry")
        else:
            db_insert_document(tx, "records", result)
        self._revision(tx, result, source_rows=source_rows)
        if managed and not managed_batch:
            from cairnmind.memory.plans import note_task_mutation
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
                from cairnmind.memory.scope import note_progress
                worktree = record.get("worktree")
                change_kind: str = change
                self._after_commit(lambda: note_progress(self, result, worktree, change_kind))
        return result

    def _after_commit(self, call):
        pending = getattr(self._local, "after_commit", None)
        if pending is not None:
            pending.append(call)

    def remember(self, project_id, record, sources, *, conflict_review=False, extracted_from=None, extractor=None,
                 symbol_anchor=None, comment_anchor=None, adr_anchor=None, trailer_anchor=None):
        with self.transaction() as tx:
            return self._remember(tx, project_id, record, sources, conflict_review=conflict_review,
                                  extracted_from=extracted_from, extractor=extractor,
                                  symbol_anchor=symbol_anchor, comment_anchor=comment_anchor,
                                  adr_anchor=adr_anchor, trailer_anchor=trailer_anchor)

    def checkpoint(self, project_id, state):
        if not isinstance(state, dict):
            raise ValueError("Checkpoint state must be an object")
        content = json_text(state)
        from cairnmind.memory.secret_mask import (
            note_metadata,  # indexed with its known secrets replaced (#182)
        )
        source = self.snapshot(project_id, f"checkpoint://{uuid4()}", content, "checkpoint",
                               note_metadata(self.settings, content, {"origin": "client"}))
        with self.transaction() as tx:
            old = db_find_one(
                tx,
                "records",
                [eq("project_id", project_id), eq("kind", "checkpoint"), eq("key", "latest"), ne("status", "superseded")],
            )
            record = {"kind": "checkpoint", "key": "latest", "content": content, "metadata": state}
            if old:
                record.update(id=old["id"], expected_version=old["version"])
            # The record's metadata accumulates the fields of earlier checkpoints. When a writer that is not
            # an external session replaces an external checkpoint, none of the external fields may stay behind
            # unmarked: the metadata is then built from this state alone. Any other succession merges as before.
            fresh = bool(old) and degraded_origin(old.get("metadata")) and not self.current_origin()
            return self._remember(tx, project_id, record, [source["id"]], replace_origin=True, replace_metadata=fresh,
                                  slot=True)

    def list_records(self, project_id, include_history=False):
        self._require_project(self, project_id)
        conds = [eq("project_id", project_id)]
        if not include_history:
            conds.append(ne("status", "superseded"))
        return [clean_row(row) for row in self.find("records", conds, order_by=(desc("updated_at"), asc("id")))]

    def record_history(self, record_id):
        return [
            row["snapshot"]
            for row in self.find("record_revisions", {"record_id": record_id}, columns=("snapshot",), order_by=(asc("version"),))
        ]

    def enqueue(self, kind, project_id, payload, dedupe_key):
        return store_jobs.enqueue(self, kind, project_id, payload, dedupe_key)

    def claim_job(self, worker, lease_seconds=60, kinds=None, *, project_ids=None):
        return store_jobs.claim_job(self, worker, lease_seconds=lease_seconds, kinds=kinds, project_ids=project_ids)

    @staticmethod
    def _job_event(job, event, *, state=None, error=_JOB_UNSET):
        return store_jobs.job_event(job, event, state=state, error=error)

    def set_job_diagnostics(self, job_id, diagnostics, *, expected_attempts=None, worker=None):
        """Publish sanitized attempt diagnostics without completing/reclaiming a job."""
        return store_jobs.set_job_diagnostics(
            self, job_id, diagnostics, expected_attempts=expected_attempts, worker=worker
        )

    def finish_job(self, job_id, error=None, *, worker=None, expected_attempts=None):
        return store_jobs.finish_job(
            self, job_id, error=error, worker=worker, expected_attempts=expected_attempts
        )

    def job_summary(self, project_id=None):
        return store_jobs.job_summary(self, project_id=project_id)

    def list_jobs(self, project_id, state=None, kind=None, limit=50):
        return store_jobs.list_jobs(self, project_id, state=state, kind=kind, limit=limit)

    def job_status(self, project_id, job_id):
        return store_jobs.job_status(self, project_id, job_id)

    def _job_proofs(self, project_id):
        return store_jobs.job_proofs(self, project_id)

    def _import_job_coverage(self, job, packets):
        return store_jobs.import_job_coverage(self, job, packets)

    def _public_job(self, job, *, detail=False, packets=()):
        return store_jobs.public_job(self, job, detail=detail, packets=packets)

    @staticmethod
    def _job_error(error, diagnostics):
        """Expose actionable fixed categories; keep original exceptions in storage/export."""
        return store_jobs.public_job_error(error, diagnostics)

    def retry_job(self, project_id, job_id, expected_attempts, *, include_omissions=False):
        return store_jobs.retry_job(
            self, project_id, job_id, expected_attempts, include_omissions=include_omissions
        )

    @staticmethod
    def _all_proposals_rejected(job):
        return store_jobs.all_proposals_rejected(job)

    def _retry_job_locked(self, tx, job):
        return store_jobs.retry_job_locked(self, tx, job)

    def _count(self, collection, project_id=None, extra=()):
        return store_jobs.count_rows(self, collection, project_id=project_id, extra=extra)

    def status(self, project_id=None):
        return store_jobs.status(self, project_id=project_id)

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
            state = self.find_one("journal_state", {"id": "current"}, columns=("epoch", "seq", "first_seq"))
            if not state:
                raise ValueError("Journal state is unavailable")
            head = {"epoch": state["epoch"], "seq": state["seq"]}
            known = self._stamp_cursor
            if head != known:
                position = known["seq"] if known and known["epoch"] == head["epoch"] else None
                if position is not None and not state.get("first_seq", 1) - 1 <= position <= head["seq"]:
                    position = None
                seq, shared, complete = dict(self._stamp_seq), self._stamp_global, position is not None
                while complete and position is not None and position < head["seq"]:
                    rows = self.find(
                        "journal_events",
                        [eq("epoch", head["epoch"]), gt("seq", position), lte("seq", head["seq"])],
                        columns=("project_id", "entity_type", "seq"),
                        order_by=(asc("seq"),),
                        limit=1000,
                    )
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
        return store_jobs.recent_job_errors(self, project_id=project_id, limit=limit)

    def retry_jobs(self):
        """Explicit legacy global compatibility; every requeue retains its audit."""
        return store_jobs.retry_jobs(self)

    def missing_embeddings(self, namespace, project_id=None):
        return store_jobs.missing_embeddings(self, namespace, project_id=project_id)

    def delete_embeddings(self, project_id):
        with self.transaction() as tx:
            self.backend.delete_vectors_for_project(tx, project_id)

    def export(self, project_id=None):
        from cairnmind.storage.restore_staged import refuse_if_unfinished
        refuse_if_unfinished(self.client)  # part of a backup is not an export of the store (#362)
        with self.transaction() as tx:
            return self._export_snapshot(tx, project_id, EXPORT_TYPES, 5)

    def _export_snapshot(self, tx, project_id, collections, format_version):
        result = {"format_version": format_version, "backend": "arcadedb", "exported_at": utc_now()}
        self._require_project(tx, project_id)
        for collection in collections:
            field = "id" if collection == "projects" else "project_id"
            result[collection] = [] if collection in VECTOR_COLLECTIONS or (collection == "ingestion_inbox" and project_id) else [
                clean_row(row) for row in db_find(tx, collection, {field: project_id} if project_id else ())]
        result["vector_data"] = {}  # no vectors in an export, as in a backup (#187)
        return result

    def backup(self, destination):
        from cairnmind.storage.restore_staged import refuse_if_unfinished
        refuse_if_unfinished(self.client)  # a backup of part of a restore would pass for a whole one (#362)
        destination = Path(destination).expanduser().resolve()
        if destination.is_dir():
            destination /= f"cairnmind-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}{BACKUP_SUFFIXES[0]}"
        # Rows are read, hashed and compressed a page at a time inside one transaction; the file is moved
        # into place only after that transaction commits (#196).
        return streamed_backup(self.transaction, destination, EXPORT_TYPES, 5)

    @staticmethod
    def _write_backup(destination, payload):
        return write_backup(destination, payload)

    @staticmethod
    def _validated_backup(path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
            if not isinstance(envelope, dict) or envelope.get("format") not in BACKUP_FORMATS or envelope.get("version") != 1:
                raise ValueError("This file is not a supported Cairnmind ArcadeDB backup")
            payload = envelope["payload"]
            if payload_digest(payload) != envelope.get("sha256"):
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

    def restore(self, backup, *, claims=None):
        """Restore validated data as one outcome: the whole backup, or the store as it was. Stop Cairnmind workers
        before calling. The rows are written in transactions of bounded size, so a failure is undone from the safety
        copy, and a restore that was cut off leaves a mark that refuses the store until a restore finishes (#362).

        With `claims` (restore_claims.BackupClaims) the restore keeps no person's act of the backup: each
        confirmation becomes the backup's claim and accepted record kinds are withdrawn (#214). Without it the
        rows are written as they are; `cairnmind restore` decides by the open workspace's key."""
        # The file is validated in two passes over it before anything changes, and read a third time for
        # the inserts; it is never held in memory whole (#196, backup_reader).
        from cairnmind.storage.restore_staged import staged_restore
        from cairnmind.storage.store_writer import require_writer
        path = Path(backup).expanduser().resolve()
        with opened_backup(path, self._validated_backup, RESTORE_TYPES) as source:
            require_writer(self)  # only the writer restores, and the restored data keeps naming it (#70)
            # The safety copy, the mark, the bounded transactions and, when one fails, the way back (#362).
            staged_restore(self, source, path, claims)
        if claims is not None:
            self.confirmations_before_rule = None  # nothing restored is a person's, so nothing is marked as one
            return
        # A backup of a store before 0.11.0 brings records confirmed before the rule back (#215).
        from cairnmind.memory.confirmation_upgrade import mark_confirmed_before_rule
        self.confirmations_before_rule = mark_confirmed_before_rule(self, force=True)

    def _restore_from(self, source, claims=None):
        # DDL is deliberately outside the data transaction; no live data has been removed.
        self.backend.ensure_restored_vector_types(source.vector_indexes)
        # Rows are deleted and written in transactions of bounded size, the engine's heap holding a transaction (#362).
        from cairnmind.storage.restore_staged import replace
        self.embed_jobs_requeued = replace(self, source, claims)

    @staticmethod
    def _requeue_embed_jobs(batches):
        """A backup holds no vectors (#187): set each completed embed job of a current chunk back to pending, in the
        restore's transactions, and return how many. None when the backup brought vectors, which stay as they are."""
        tx = batches.tx
        if db_find_one(tx, "embeddings", columns=("id",)):
            return None
        from cairnmind.agents.retrieval import json_value  # retrieval imports this module
        current = {row["id"] for row in db_find(tx, "chunks", {"is_current": True}, columns=("id",))}
        first = {}  # one job per chunk: a retry can leave two completed jobs of the same chunk
        for row in db_find(tx, "jobs", {"kind": "embed", "state": "completed"}, columns=("id", "payload")):
            chunk = (json_value(row.get("payload"), {}) or {}).get("chunk_id")
            if chunk in current:
                first.setdefault(chunk, row["id"])
        ids = list(first.values())
        for start in range(0, len(ids), 500):
            db_update_where(
                batches.tx,
                "jobs",
                set_fields={"state": "pending", "error": None, "worker": None, "lease_until": None, "updated_at": utc_now()},
                where=is_in("id", ids[start:start + 500]),
            )
            batches.wrote(len(ids[start:start + 500]))
        return len(ids)

    def forget_project(self, project_id):
        from cairnmind.memory.confirmation import ConfirmationRefused
        from cairnmind.terminal.person_proof import FORGET_REFUSED, present
        if not present():
            raise ConfirmationRefused(FORGET_REFUSED)  # only the workspace's route deletes a project (#347)
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            from cairnmind.storage.journal import Journal
            Journal.reset(tx, reason="forget")
            self._delete_old_vectors(
                tx, [r["id"] for r in db_find(tx, "sources", {"project_id": project_id}, columns=("id",))]
            )
            db_delete_where(tx, "edges", {"project_id": project_id})
            db_delete_where(tx, "nodes", {"project_id": project_id})
            for collection in reversed(DOCUMENT_TYPES):
                if collection != "projects":
                    db_delete_where(tx, collection, {"project_id": project_id})
            db_delete_where(tx, "projects", {"id": project_id})
