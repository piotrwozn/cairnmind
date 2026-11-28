"""ArcadeDB source of truth, native graph, and verified portable backups."""
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import uuid4

from cairnmind.cli_cmds.first_start import PROJECT_NAME_TAKEN, PROJECT_ROOT_TAKEN, UNKNOWN_PROJECT_NEXT
from cairnmind.config import Settings

# The sensitivity class of a source and a record (#180): derived on every read, stored nowhere.
from cairnmind.memory.sensitivity import record_sensitivity, source_sensitivity  # noqa: F401
from cairnmind.pal.file_locks import lock_exclusive, unlock
from cairnmind.pal.path_spelling import lies_in
from cairnmind.pal.terminal_safe import safe_text
from cairnmind.storage import store_graph, store_jobs
from cairnmind.storage.arcade import REQUEST_TIMEOUT
from cairnmind.storage.domain_types import (
    GraphEdgeRecord,
    GraphNodeRecord,
    JobRecord,
    MemoryRecord,
    ProjectRecord,
    ResumePacketRecord,
    SourceRecord,
)
from cairnmind.storage.sql import clean_row, utc_now
from cairnmind.storage.storage_query import (
    any_of,
    asc,
    db_find_one,
    db_insert_document,
    db_update_where,
    eq,
    is_null,
)
from cairnmind.storage.store_backup_ops import _StoreBackupMixin
from cairnmind.storage.store_jobs import (  # noqa: F401
    _DIAGNOSTIC_CODE,
    _JOB_UNSET,
    JOB_STATES,
    JobNotFoundError,
    job_diagnostics,
)
from cairnmind.storage.store_records import _StoreRecordsMixin
from cairnmind.storage.store_validation import (  # noqa: F401
    _PROMPT_SHA,
    _UNCOMPARED_METADATA,
    ADVISORY_DEFINITION_FIELDS,
    CACHE_STAMP_IGNORED,
    CONFLICT_REVIEW_METADATA,
    DERIVED_RECORD_METADATA,
    DOCUMENT_TYPES,
    EXPORT_TYPES,
    EXTRACTION_MARK,
    EXTRACTOR_MODEL_CHARS,
    EXTRACTOR_STAMP,
    KINDS,
    LEGACY_DOCUMENT_TYPES,
    LEGACY_EXPORT_TYPES,
    MANAGED_DEFINITION_METADATA_FIELDS,
    ORIGIN_METADATA,
    RESTORE_TYPES,
    STATUSES,
    TASK_STATES,
    V03_DOCUMENT_TYPES,
    V03_EXPORT_TYPES,
    V04_DOCUMENT_TYPES,
    V04_EXPORT_TYPES,
    V05_DOCUMENT_TYPES,
    KeyTaken,
    RecordRefused,
    degraded_origin,
    normalise_criteria,
    outcome_changed,
    valid_argv,
    validate_criterion_links,
    validate_extractor_stamp,
    validate_scope,
    validate_task_state,
)

SCHEMA_VERSION = 4
# Indexes for lookups by a field other than id and project_id. They are not part of the schema version:
# an earlier version works with them unchanged, so adding one needs no migration and blocks no way back.
LOOKUP_INDEXES = (
    ("chunks", "source_id"), ("nodes", "source_id"), ("edges", "source_id"), ("edges", "from_id"),
    ("edges", "to_id"), ("embeddings", "source_id"), ("embeddings", "chunk_id"), ("sources", "uri"),
    ("record_revisions", "record_id"), ("check_runs", "task_id"), ("session_events", "session_id"),
    ("resume_packets", "session_id"),
)
LOOKUP_INDEX_SET = 1
# Graph rows written by one request of Store.write_graph (issue #167).
GRAPH_ROWS_PER_REQUEST = 250
_T = TypeVar("_T")


class Store(_StoreRecordsMixin, _StoreBackupMixin):
    # True for a store from open_readonly, which starts, creates and writes nothing, and for a store whose
    # installation is not the writer of its database (#70, store_writer).
    read_only = False
    # Why this installation only reads the store (another installation is its writer), or None.
    not_writer: str | None = None
    # Set when another installation took over writing after this store was opened: every write is refused.
    _writer_lost: str | None = None
    installation: Any = None
    # False in the service: its working directory is not the caller's, so resolve_project refuses a relative path.
    relative_paths = True
    _writable: Any = None
    # Store(path, for_takeover=True) of a reader whose store this release cannot read: why (see take_over_at).
    not_ready: str | None = None
    _for_takeover = False
    # Store(path, for_restore=True): the only opening that passes the mark of an unfinished restore (restore_staged).
    _for_restore = False
    _unfinished_restore = False
    # What the one-time step that marks records confirmed before 0.11.0 did when it ran on this open or the
    # last restore ({"marked", "before_rule", "at"}, confirmation_upgrade, #215); None when it did not run.
    confirmations_before_rule: dict[str, Any] | None = None
    _backend: Any = None

    def _uuid4_fn(self) -> Any:
        return uuid4

    def _insert_doc(self, tx: Any, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        return db_insert_document(tx, collection, data)

    def _validate_scope(self, scope: Any) -> None:
        validate_scope(scope)

    @property
    def backend(self) -> Any:
        if self._backend is None:
            from cairnmind.storage.arcade_backend import ArcadeBackend
            self._backend = ArcadeBackend(self)
        return self._backend

    @backend.setter
    def backend(self, value: Any) -> None:
        self._backend = value

    # After a restore: how many completed embed jobs it set back to pending because the backup brought no vectors
    # (#187); None when the backup brought them or nothing was restored.
    embed_jobs_requeued: int | None = None
    restore_changed = False  # restore_staged: a delete of the restore may have been committed

    @classmethod
    def open_readonly(cls, settings: Settings) -> Store:
        """Read the existing store. Raises StoreNotReady, naming the command to run, instead of preparing it."""
        from cairnmind.storage.store_readonly import open_readonly
        return open_readonly(cls, settings)

    def read_consistently(self, read: Callable[[], tuple[str | None, _T]], kinds: Iterable[str]) -> _T:
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
        root = (
            Path(data_dir).expanduser().resolve()
            if data_dir is not None
            else Path(tempfile.gettempdir()) / "cairnmind-memory"
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings = Settings(data_dir=root, storage_backend="sqlite", db_path=":memory:")
        return cls(root, settings=settings, backend="sqlite", db_path=":memory:")

    def close(self) -> None:
        if hasattr(self, "backend") and self.backend is not None:
            self.backend.close()

    def __init__(
        self,
        data_dir: Path,
        *,
        settings: Settings | None = None,
        for_takeover: bool = False,
        for_restore: bool = False,
        backend: str | None = None,
        db_path: Path | str | None = None,
    ) -> None:
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
            self.client: Any = self.backend.as_client_proxy()
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
        self._stamp_cursor: dict[str, Any] | None = None
        self._stamp_base = 0
        self._stamp_global = 0
        self._stamp_seq: dict[Any, int] = {}
        self.init()

    @contextmanager
    def held(self) -> Iterator[None]:
        """Keep the writer lock across the transactions this thread runs inside, as a restore does (#362): their
        own ``_lock`` is then this one, and every other process and thread waits."""
        with self._lock():
            self._local.lock_held = True
            try:
                yield
            finally:
                self._local.lock_held = False

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if getattr(self._local, "lock_held", False):
            yield  # this thread holds it already; a second flock on a new handle would wait for itself
            return
        with (self.data_dir / "database-writer.lock").open("a", encoding="utf-8") as handle:
            lock_exclusive(handle)
            try:
                yield
            finally:
                unlock(handle)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return (getattr(self._local, "transaction", None) or self.client).query(sql, params)

    def find(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        limit: int | None = None,
        distinct: bool = False,
    ) -> list[dict[str, Any]]:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "find"):
            return tx.find(collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct)
        return self.backend.find(
            collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct, reader=tx
        )

    def find_one(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        required: bool = False,
    ) -> dict[str, Any] | None:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "find_one"):
            return tx.find_one(collection, where, columns=columns, order_by=order_by, required=required)
        return self.backend.find_one(
            collection, where, columns=columns, order_by=order_by, required=required, reader=tx
        )

    def count(self, collection: str, where: Any = ()) -> int:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "count"):
            return int(tx.count(collection, where))
        return int(self.backend.count(collection, where, reader=tx))

    def group_counts(
        self,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
    ) -> list[dict[str, Any]]:
        tx = getattr(self._local, "transaction", None)
        if tx is not None and hasattr(tx, "group_counts"):
            return tx.group_counts(collection, group_by, where, count_alias=count_alias)
        return self.backend.group_counts(collection, group_by, where, count_alias=count_alias, reader=tx)

    def update_where(
        self,
        collection: str,
        *,
        set_fields: dict[str, Any] | None = None,
        remove_fields: Any = (),
        where: Any = (),
    ) -> list[dict[str, Any]]:
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

    def delete_where(
        self,
        collection: str,
        where: Any = (),
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
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

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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
    def transaction(self, commit_timeout: float = REQUEST_TIMEOUT) -> Iterator[Any]:
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
            tx = JournalTransaction(
                native,
                writer=inst.id,
                check=lambda head, reader: check_head(self, head, reader),
            )
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
    def write_origin(self, mark: Mapping[str, Any] | None) -> Iterator[None]:
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

    def current_origin(self) -> dict[str, Any] | None:
        return getattr(self._local, "write_origin", None)

    @contextmanager
    def session_write(self, session_id: str | None) -> Iterator[None]:
        """Mark what this thread writes inside the block as a session's: never a person's act (#177)."""
        previous = getattr(self._local, "session_write", None)
        self._local.session_write = session_id or previous
        try:
            yield
        finally:
            self._local.session_write = previous

    @contextmanager
    def person_act(self, route: str, source_id: str) -> Iterator[None]:
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

    def _proven_act(self) -> dict[str, str] | None:
        """The person's act open on this thread, counted only when the request that opened it carried the
        workspace's key (#214). Evaluation seed data is not a person and needs none."""
        act = getattr(self._local, "person_act", None)
        if not act:
            return None
        from cairnmind.memory.confirmation import SEED_ROUTE
        from cairnmind.terminal.person_proof import present
        return act if act["route"] == SEED_ROUTE or present() else None

    def _persons_act(self) -> bool:
        """Whether this thread writes as a person: an act of a person's route is open (evaluation seed data
        is not one), and no session or marked origin writes."""
        from cairnmind.memory.confirmation import PERSON_ROUTES
        act = self._proven_act()
        return bool(act) and act["route"] in PERSON_ROUTES and not (
            self.current_origin() or getattr(self._local, "session_write", None)
        )

    def open_person_act(self) -> dict[str, str] | None:
        """The person's act open on this thread ({route, source_id}), or None when this write is not one."""
        return dict(self._local.person_act) if self._persons_act() else None

    def _kept(self, record: Any) -> bool:
        """Whether this write is not a person's act and `record` is confirmed by a person here (#177)."""
        from cairnmind.memory.confirmation import kept_verdict
        return not self._persons_act() and kept_verdict(record)

    def _keep_verdict(
        self,
        record: Any,
        change: str,
        status: str | None = None,
        differs: Sequence[str] = (),
    ) -> None:
        """A write that is not a person's act never takes a person's confirmation away (#177): refused."""
        from cairnmind.memory.confirmation import VerdictKept
        if self._kept(record):
            raise VerdictKept(record, change, status, differs)

    def init(self) -> None:
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

    def _open_as_reader(self, writer: dict[str, Any]) -> None:
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

    def writer_state(self) -> dict[str, Any]:
        """Which installation writes this store, and whether this one is the writer, a reader or lost it (#70)."""
        from cairnmind.storage.store_writer import state
        return state(self)

    def _prepare_schema(self) -> None:
        """Create or upgrade the schema; called by init, with the writer lock held, when no other installation writes."""
        self.backend.init_schema(self)

    def _lookup_indexes(self, recorded: Any) -> None:
        """Create the lookup indexes that are missing (delegated to backend)."""
        if hasattr(self.backend, "_lookup_indexes"):
            self.backend._lookup_indexes(self, recorded)

    @staticmethod
    def _require_project(tx: Any, project_id: str | None) -> None:
        known = getattr(tx, "known", None)
        if known is not None and project_id in known["projects"]:
            return
        if project_id is not None and not db_find_one(tx, "projects", {"id": project_id}, columns=("id",)):
            raise ValueError(f"Unknown project: {project_id}")
        if known is not None and project_id is not None:
            known["projects"].add(project_id)

    @staticmethod
    def _remembered(tx: Any, table: str, identifier: str, load: Callable[[], _T]) -> _T:
        """A row as this transaction already read or wrote it, else read now. Without prefetch_graph: always read."""
        known = getattr(tx, "known", None)
        if known is None:
            return load()
        if identifier not in known[table]:
            known[table][identifier] = load()
        return known[table][identifier]

    @staticmethod
    @contextmanager
    def _keeping(tx: Any) -> Iterator[dict[str, Any] | None]:
        """Graph writes that keep what the transaction remembers current, instead of forgetting it."""
        with store_graph.keeping_known(tx) as known:
            yield known

    def prefetch_graph(
        self,
        tx: Any,
        nodes: Iterable[GraphNodeRecord | Mapping[str, Any]],
        edges: Iterable[GraphEdgeRecord | Mapping[str, Any]],
    ) -> None:
        """Read, in a few requests, what upsert_node and upsert_edge would look up row by row."""
        return store_graph.prefetch_graph(self, tx, nodes, edges)

    def require_safe_project_root(self, root: Path | str) -> str:
        path = str(Path(root).expanduser().resolve())
        if lies_in(path, self.data_dir):
            # Indexing always leaves the data directory out, so such a folder would report 0 files (#295).
            raise ValueError(
                "Project folder cannot be inside the data directory: Cairnmind never indexes "
                "it; choose a folder outside it"
            )
        if (self.data_dir / "credentials" / "api-keys.json").exists() and lies_in(self.data_dir, path):
            raise ValueError("Project root cannot contain Cairnmind's private credential store")
        return path

    def add_project(self, name: str, root: Path | str, restore: bool = True) -> dict[str, Any]:
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
            project: ProjectRecord = {"id": str(uuid4()), "name": name, "root": path, "created_at": utc_now()}
            db_insert_document(tx, "projects", cast(dict[str, Any], project))
            from cairnmind.memory.project_folders import stamp
            db_insert_document(tx, "project_roots", {"id": str(uuid4()), "project_id": project["id"], "path": path, **stamp()})
            return cast(dict[str, Any], project)

    def add_project_root(self, project_id: str, path: Path | str) -> dict[str, Any]:
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

    def list_projects(self) -> list[ProjectRecord]:
        return [
            cast(ProjectRecord, clean_row(row))
            for row in self.find("projects", is_null("removed_at"), order_by=(asc("name"),))
        ]

    def resolve_project(self, identifier: Path | str, *, recorded: bool = False) -> dict[str, Any]:
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
        candidates = [
            r for r in self.find("project_roots")
            if path == Path(r["path"]) or Path(r["path"]) in path.parents
        ]
        if candidates:
            selected = max(candidates, key=lambda row: len(row["path"]))
            found = db_find_one(self, "projects", {"id": selected["project_id"]}, required=True)
            if found is None:
                raise ValueError(f"Unknown project: {selected['project_id']}")
            return found
        removed = find_removed(self, value, path)
        if removed:
            raise unknown_removed(value, removed, self.data_dir)
        raise ValueError(f"Unknown project: {value}. {UNKNOWN_PROJECT_NEXT}")

    def _delete_old_vectors(self, tx: Any, source_ids: list[str]) -> None:
        self.backend.delete_vectors_for_sources(tx, source_ids)

    def enqueue_new(
        self,
        tx: Any,
        kind: str,
        project_id: str | None,
        payload: Any,
        dedupe_key: str,
    ) -> str:
        """A job whose key cannot exist yet, because it names a row created in this same transaction."""
        return store_jobs.enqueue_new(self, tx, kind, project_id, payload, dedupe_key)

    def upsert_node(self, node: GraphNodeRecord | Mapping[str, Any], tx: Any = None) -> GraphNodeRecord:
        return store_graph.upsert_node(self, node, tx=tx)

    def _checked_node(
        self,
        tx: Any,
        node: GraphNodeRecord | Mapping[str, Any],
    ) -> tuple[GraphNodeRecord, dict[str, Any] | None]:
        """The vertex upsert_node writes, after its checks, and the stored row with its ID if there is one."""
        return store_graph.checked_node(self, tx, node)

    def upsert_edge(self, edge: GraphEdgeRecord | Mapping[str, Any], tx: Any = None) -> GraphEdgeRecord:
        return store_graph.upsert_edge(self, edge, tx=tx)

    def _checked_edge(
        self,
        tx: Any,
        edge: GraphEdgeRecord | Mapping[str, Any],
    ) -> tuple[GraphEdgeRecord, dict[str, Any] | None]:
        """The edge upsert_edge writes, after its checks, and the stored row with its ID if there is one."""
        return store_graph.checked_edge(self, tx, edge)

    def write_graph(
        self,
        tx: Any,
        project_id: str,
        nodes: Iterable[GraphNodeRecord | Mapping[str, Any]],
        edges: Iterable[GraphEdgeRecord | Mapping[str, Any]],
    ) -> None:
        """Write graph rows as upsert_node, then upsert_edge, would for each, with one request per group of rows."""
        return store_graph.write_graph(self, tx, project_id, nodes, edges)

    def remove_graph_nodes(self, tx: Any, project_id: str, identifiers: Iterable[Any]) -> list[str]:
        """Delete the project's vertices among `identifiers` that no edge names, one group per request."""
        return store_graph.remove_graph_nodes(self, tx, project_id, identifiers)

    @staticmethod
    def _load_checked_sources(tx: Any, project_id: str, sources: Any) -> dict[str, SourceRecord]:
        return store_graph.load_checked_sources(tx, project_id, sources)

    def _record_graph(
        self,
        tx: Any,
        record: MemoryRecord | Mapping[str, Any],
        *,
        source_rows: Mapping[str, SourceRecord | Mapping[str, Any]] | None = None,
    ) -> None:
        return store_graph.record_graph(self, tx, record, source_rows=source_rows)

    def _revision(
        self,
        tx: Any,
        record: MemoryRecord | Mapping[str, Any],
        *,
        source_rows: Mapping[str, SourceRecord | Mapping[str, Any]] | None = None,
    ) -> None:
        db_insert_document(
            tx,
            "record_revisions",
            {
                "id": f"{record['id']}:{record['version']}",
                "project_id": record["project_id"],
                "record_id": record["id"],
                "version": record["version"],
                "snapshot": record,
                "recorded_at": utc_now(),
            },
        )
        self._record_graph(tx, record, source_rows=source_rows)

    def _after_commit(self, call: Callable[[], Any]) -> None:
        pending = getattr(self._local, "after_commit", None)
        if pending is not None:
            pending.append(call)

    def enqueue(self, kind: str, project_id: str | None, payload: Any, dedupe_key: str) -> str:
        return store_jobs.enqueue(self, kind, project_id, payload, dedupe_key)

    def claim_job(
        self,
        worker: str,
        lease_seconds: float = 60,
        kinds: Iterable[str] | None = None,
        *,
        project_ids: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        return store_jobs.claim_job(self, worker, lease_seconds=lease_seconds, kinds=kinds, project_ids=project_ids)

    @staticmethod
    def _job_event(
        job: JobRecord | Mapping[str, Any],
        event: str,
        *,
        state: str | None = None,
        error: Any = _JOB_UNSET,
    ) -> list[dict[str, Any]]:
        return store_jobs.job_event(job, event, state=state, error=error)

    def set_job_diagnostics(
        self,
        job_id: str,
        diagnostics: Any,
        *,
        expected_attempts: int | None = None,
        worker: str | None = None,
    ) -> bool:
        """Publish sanitized attempt diagnostics without completing/reclaiming a job."""
        return store_jobs.set_job_diagnostics(
            self, job_id, diagnostics, expected_attempts=expected_attempts, worker=worker
        )

    def finish_job(
        self,
        job_id: str,
        error: str | None = None,
        *,
        worker: str | None = None,
        expected_attempts: int | None = None,
    ) -> None:
        return store_jobs.finish_job(
            self, job_id, error=error, worker=worker, expected_attempts=expected_attempts
        )

    def job_summary(self, project_id: str | None = None) -> dict[str, dict[str, int]]:
        return store_jobs.job_summary(self, project_id=project_id)

    def list_jobs(
        self,
        project_id: str,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return store_jobs.list_jobs(self, project_id, state=state, kind=kind, limit=limit)

    def job_status(self, project_id: str, job_id: str) -> dict[str, Any]:
        return store_jobs.job_status(self, project_id, job_id)

    def _job_proofs(self, project_id: str) -> list[dict[str, Any]]:
        return store_jobs.job_proofs(self, project_id)

    def _import_job_coverage(
        self,
        job: JobRecord | Mapping[str, Any],
        packets: Iterable[ResumePacketRecord | Mapping[str, Any]],
    ) -> dict[str, Any]:
        return store_jobs.import_job_coverage(self, job, packets)

    def _public_job(
        self,
        job: JobRecord | Mapping[str, Any],
        *,
        detail: bool = False,
        packets: Iterable[ResumePacketRecord | Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        return store_jobs.public_job(self, job, detail=detail, packets=packets)

    @staticmethod
    def _job_error(error: Any, diagnostics: Mapping[str, Any]) -> dict[str, str | None]:
        """Expose actionable fixed categories; keep original exceptions in storage/export."""
        return store_jobs.public_job_error(error, diagnostics)

    def retry_job(
        self,
        project_id: str,
        job_id: str,
        expected_attempts: int,
        *,
        include_omissions: bool = False,
    ) -> dict[str, Any]:
        return store_jobs.retry_job(
            self, project_id, job_id, expected_attempts, include_omissions=include_omissions
        )

    @staticmethod
    def _all_proposals_rejected(job: JobRecord | Mapping[str, Any]) -> bool:
        return store_jobs.all_proposals_rejected(job)

    def _retry_job_locked(self, tx: Any, job: JobRecord | Mapping[str, Any]) -> None:
        return store_jobs.retry_job_locked(self, tx, job)

    def _count(self, collection: str, project_id: str | None = None, extra: Any = ()) -> int:
        return store_jobs.count_rows(self, collection, project_id=project_id, extra=extra)

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        return store_jobs.status(self, project_id=project_id)

    def recent_job_errors(self, project_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        return store_jobs.recent_job_errors(self, project_id=project_id, limit=limit)

    def retry_jobs(self) -> dict[str, Any]:
        """Explicit legacy global compatibility; every requeue retains its audit."""
        return store_jobs.retry_jobs(self)

    def missing_embeddings(self, namespace: str, project_id: str | None = None) -> list[dict[str, Any]]:
        return store_jobs.missing_embeddings(self, namespace, project_id=project_id)

    def delete_embeddings(self, project_id: str) -> None:
        with self.transaction() as tx:
            self.backend.delete_vectors_for_project(tx, project_id)
