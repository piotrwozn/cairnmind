"""Open the store for reading only: no engine start, no new database, no DDL and no write.

``Store(path)`` prepares everything it needs: it creates the data directory and configuration,
starts the engine, creates the database and creates or upgrades the schema. A command that only
reads must not do any of that. ``Store.open_readonly(settings)`` checks instead that each of these
is already in place and says, in one sentence, which command puts it in place when it is not.

What a store opened this way refuses:

- ``Store.command`` and every write through it, before anything is sent;
- ``client.command``, ``client.server_command`` and ``client.ensure_database``;
- a query that does not start with SELECT, MATCH or TRAVERSE. The engine refuses a statement
  that changes data on its query endpoint as well;
- a commit: ``Store.transaction`` opens a server transaction that is always rolled back, takes no
  writer lock, and whose ``command`` is refused like the store's.

Without the writer lock a read is not a snapshot: ArcadeDB keeps REPEATABLE_READ per page, so a
page read for the first time late in a transaction shows the latest commit. A read made of several
queries therefore goes through ``Store.read_consistently``: it notes the head of the change journal,
reads, and notes the head again. Every commit that changes a journaled table moves the head in the
same commit, so an unchanged head means no such change landed in between. When it moved, the events
in between say which project and which kind of data changed: the read is repeated only when one of
them concerns its project (or no project) and a kind it reads, a few times at most, and is then
refused with one sentence instead of a mixed result. Job, session, index and other projects' commits
therefore do not repeat it.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import threading

from cairnmind.storage.arcade import ArcadeClient, ArcadeError
from cairnmind.config import Settings
from cairnmind.storage.database_mode import connect_only, missing_database, version_problem

_READ = re.compile(r"^\s*\(*\s*(SELECT|MATCH|TRAVERSE)\b", re.I)


class StoreNotReady(ValueError):
    """The store cannot be read as it is now; the message names the command that makes it ready."""


class StoreBusy(ValueError):
    """The store changed during every attempt to read it."""


class ReadOnlyStoreError(RuntimeError):
    """A write was attempted through a store opened for reading. A programming error, never a user's."""


class ReadOnlyClient:
    """The database client of a store opened for reading: queries, and transactions that are rolled back."""

    def __init__(self, client: ArcadeClient, refusal: str | None = None):
        self._client = client
        self.settings = client.settings
        self.database = client.database
        # Set for a store whose installation is not the writer (#70): a write is then the person's request,
        # refused with this reason (StoreNotWriter), not a programming error.
        self.refusal = refusal

    def _refuse(self, message):
        if self.refusal:
            from cairnmind.storage.store_writer import StoreNotWriter
            raise StoreNotWriter(self.refusal)
        raise ReadOnlyStoreError(message)

    def query(self, sql, params=None):
        if not isinstance(sql, str) or not _READ.match(sql):
            raise ReadOnlyStoreError("A store opened for reading runs only SELECT, MATCH and TRAVERSE queries")
        return self._client.query(sql, params)

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
        from cairnmind.storage.sql import clean_row
        from cairnmind.storage.storage_query import db_find
        return [
            clean_row(row)
            for row in db_find(
                self._client,
                collection,
                where,
                columns=columns,
                order_by=order_by,
                limit=limit,
                distinct=distinct,
            )
        ]

    def find_one(
        self,
        collection: str,
        where=(),
        *,
        columns=None,
        order_by=(),
        required: bool = False,
    ) -> dict | None:
        from cairnmind.storage.storage_query import missing_row_message
        rows = self.find(collection, where, columns=columns, order_by=order_by, limit=1)
        if not rows:
            if required:
                raise ValueError(missing_row_message(collection))
            return None
        return rows[0]

    def count(self, collection: str, where=()) -> int:
        from cairnmind.storage.storage_query import db_count
        return db_count(self._client, collection, where)

    def group_counts(self, collection: str, group_by, where=(), *, count_alias: str = "total") -> list[dict]:
        from cairnmind.storage.sql import clean_row
        from cairnmind.storage.storage_query import db_group_counts
        return [
            clean_row(row)
            for row in db_group_counts(self._client, collection, group_by, where, count_alias=count_alias)
        ]

    def list_collections(self) -> set[str]:
        from cairnmind.storage.storage_query import db_list_collections
        return db_list_collections(self._client)

    def server_time_ms(self) -> int:
        from cairnmind.storage.storage_query import db_server_time_ms
        return db_server_time_ms(self._client)

    def iter_backup_pages(self, collection: str, *, page_rows: int = 1000, page_bytes: int = 2 * 1024 * 1024):
        return self._client.iter_backup_pages(collection, page_rows=page_rows, page_bytes=page_bytes)

    def insert_document(self, collection: str, data: dict):
        self._refuse("A store opened for reading cannot run a database command")

    def insert_documents(self, collection: str, rows: list[dict]):
        self._refuse("A store opened for reading cannot run a database command")

    def upsert_document(self, collection: str, data: dict, key_field: str = "id"):
        self._refuse("A store opened for reading cannot run a database command")

    def update_where(self, collection: str, *, set_fields=None, remove_fields=(), where=()):
        self._refuse("A store opened for reading cannot run a database command")

    def delete_where(self, collection: str, where=(), *, limit: int | None = None):
        self._refuse("A store opened for reading cannot run a database command")

    def command(self, sql, params=None):
        self._refuse("A store opened for reading cannot run a database command")

    def server_command(self, command):
        self._refuse("A store opened for reading cannot run a server command")

    def ensure_database(self):
        self._refuse("A store opened for reading cannot create the database")

    @contextmanager
    def transaction(self):
        with self._client.transaction(commit=False) as native:
            yield ReadOnlyClient(native, self.refusal)

    def close(self) -> None:
        self._client.close()


def check_ready(client: ArcadeClient, settings: Settings) -> None:
    """Refuse, without changing anything, a store that only ``Store(path)`` could make readable."""
    from cairnmind.store import SCHEMA_VERSION
    if hasattr(client, "server_info"):
        try:
            info = client.server_info()
        except ArcadeError as exc:
            if exc.status_code is not None:
                raise  # the engine answered: its own error says more than "not running"
            if connect_only(settings):
                # Connect-only (#68): there is no engine to start here; the client's message says where it looked.
                raise StoreNotReady(str(exc)) from None
            raise StoreNotReady("The database engine is not running; start it with `cairnmind engine start` "
                                "(this command does not start it).") from None
        refused = version_problem(settings, info)
        if refused:
            raise ArcadeError(refused)
    if not client.database_exists():
        if connect_only(settings):
            raise StoreNotReady(missing_database(settings))
        raise StoreNotReady("The Cairnmind database does not exist yet; create it with `cairnmind init`.")
    from cairnmind.storage.storage_query import db_find_one, db_list_collections
    types = db_list_collections(client)
    meta_row = db_find_one(client, "devbrain_meta", {"name": "schema_version"}, columns=("value",)) \
        if "devbrain_meta" in types else None
    version = meta_row.get("value") if meta_row else None
    if version is None:
        raise StoreNotReady("The Cairnmind database has no schema yet; create it with `cairnmind init`.")
    if version in {1, 2, 3}:
        # Only Store.init upgrades, under the writer lock and after a backup; the version is written last.
        raise StoreNotReady(f"The Cairnmind database has schema version {version} and needs the upgrade to "
                            f"{SCHEMA_VERSION}; run `cairnmind init`, which writes a backup first.")
    if version != SCHEMA_VERSION:
        raise StoreNotReady(f"Database schema version is incompatible: the database has version {version!r} "
                            f"and this release reads version {SCHEMA_VERSION}.")
    from cairnmind.storage.restore_staged import refuse_if_unfinished
    refuse_if_unfinished(client)  # a restore that did not finish leaves part of a backup (#362)
    if "journal_state" not in types or not db_find_one(client, "journal_state", {"id": "current"}, columns=("id",)):
        raise StoreNotReady("The Cairnmind database has no journal state; any ordinary write, for example "
                            "`cairnmind remember`, recreates it, and `cairnmind restore FILE` is for when the "
                            "data itself is damaged.")


READ_ATTEMPTS = 3
# Events looked at when the head moved during a read; more than this counts as a change of the read.
EVENT_LIMIT = 500


def _affected(store, before, after, project_id, kinds) -> bool:
    """Whether a change committed in (before, after] can alter a read of ``kinds`` in ``project_id``."""
    from cairnmind.storage.storage_query import asc, db_find, eq, gt, lte
    if before["epoch"] != after["epoch"] or after["seq"] - before["seq"] > EVENT_LIMIT:
        return True  # a reset journal, or too many changes to look at: count them as affecting
    rows = db_find(
        store.client,
        "journal_events",
        [eq("epoch", before["epoch"]), gt("seq", before["seq"]), lte("seq", after["seq"])],
        columns=("seq", "project_id", "entity_type"),
        order_by=(asc("seq"),),
        limit=EVENT_LIMIT,
    )
    if len(rows) != after["seq"] - before["seq"]:
        return True  # events pruned meanwhile: nothing is known about them
    # An event without a project (a journal reset, rows shared by every project) concerns every read.
    return any(row.get("project_id") in (None, project_id)
               and (row.get("entity_type") in kinds or row.get("entity_type") == "reset") for row in rows)


def read_consistently(store, read, kinds, attempts: int = READ_ATTEMPTS):
    """The result of ``read()``, repeated while a change it depends on landed during it, else StoreBusy.

    ``read()`` returns ``(project_id, result)``; ``kinds`` are the journal entity types it reads.
    Commits of other kinds or of other projects move the journal head but leave the read as it was.
    """
    from cairnmind.storage.journal import Journal
    journal = Journal(store)
    for _ in range(attempts):
        # Through the client, outside any transaction: the head is always the latest commit.
        before = journal.cursor(store.client)
        project_id, result = read()
        after = journal.cursor(store.client)
        if after == before or not _affected(store, before, after, project_id, kinds):
            return result
    raise StoreBusy(f"The project's own data changed during each of {attempts} attempts to read it; "
                    "run the command again when the current work on it is done.")


def open_readonly(store_class, settings: Settings):
    """A ``store_class`` instance that reads the existing store, or ``StoreNotReady`` saying what to run."""
    from cairnmind.config import resolved_backend, sqlite_db_path
    data_dir = Path(settings.data_dir).expanduser().resolve()
    if resolved_backend(settings) == "sqlite":
        db_target = sqlite_db_path(settings)
        if str(db_target) != ":memory:" and not Path(db_target).is_file():
            raise StoreNotReady(f"The Cairnmind SQLite database does not exist yet at {db_target}; create it with `cairnmind init --backend sqlite`.")
        from cairnmind.storage.sqlite_backend import SQLiteBackend
        from cairnmind.store import SCHEMA_VERSION
        store = store_class.__new__(store_class)
        backend = SQLiteBackend(db_target, read_only=True, store=store)
        try:
            collections = backend.list_collections()
            if "devbrain_meta" not in collections:
                raise StoreNotReady("The Cairnmind database has no schema yet; create it with `cairnmind init --backend sqlite`.")
            meta_row = backend.find_one("devbrain_meta", {"name": "schema_version"}, columns=("value",))
            version = meta_row.get("value") if meta_row else None
            if version != SCHEMA_VERSION:
                raise StoreNotReady(
                    f"Database schema version is incompatible: the database has version {version!r} "
                    f"and this release reads version {SCHEMA_VERSION}."
                )
            if "journal_state" not in collections or not backend.find_one("journal_state", {"id": "current"}, columns=("id",)):
                raise StoreNotReady("The Cairnmind database has no journal state.")
        except BaseException:
            backend.close()
            raise
        store.data_dir, store.settings = data_dir, settings
        store.backend = backend
        store.client = backend.as_client_proxy()
        store.read_only = True
        store._local = threading.local()
        store._stamp_lock = threading.Lock()
        store._stamp_cursor, store._stamp_base, store._stamp_global, store._stamp_seq = None, 0, 0, {}
        return store
    if not (data_dir / "config.json").is_file():
        raise StoreNotReady(f"There is no Cairnmind configuration in {data_dir}; run `cairnmind init` first.")
    client = ArcadeClient(settings)
    check_ready(client, settings)
    store = store_class.__new__(store_class)
    store.data_dir, store.settings, store.client, store.read_only = data_dir, settings, ReadOnlyClient(client), True
    from cairnmind.storage.arcade_backend import ArcadeBackend
    store.backend = ArcadeBackend(store)
    store._local = threading.local()
    store._stamp_lock = threading.Lock()
    store._stamp_cursor, store._stamp_base, store._stamp_global, store._stamp_seq = None, 0, 0, {}
    return store
