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
same commit, so an unchanged head means no such change landed in between. Otherwise the read is
repeated, a few times at most, and then refused with one sentence instead of a mixed result.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import threading

from .arcade import ArcadeClient, ArcadeError
from .config import Settings

_READ = re.compile(r"^\s*\(*\s*(SELECT|MATCH|TRAVERSE)\b", re.I)


class StoreNotReady(ValueError):
    """The store cannot be read as it is now; the message names the command that makes it ready."""


class StoreBusy(ValueError):
    """The store changed during every attempt to read it."""


class ReadOnlyStoreError(RuntimeError):
    """A write was attempted through a store opened for reading. A programming error, never a user's."""


class ReadOnlyClient:
    """The database client of a store opened for reading: queries, and transactions that are rolled back."""

    def __init__(self, client: ArcadeClient):
        self._client = client
        self.settings = client.settings
        self.database = client.database

    def query(self, sql, params=None):
        if not isinstance(sql, str) or not _READ.match(sql):
            raise ReadOnlyStoreError("A store opened for reading runs only SELECT, MATCH and TRAVERSE queries")
        return self._client.query(sql, params)

    def command(self, sql, params=None):
        raise ReadOnlyStoreError("A store opened for reading cannot run a database command")

    def server_command(self, command):
        raise ReadOnlyStoreError("A store opened for reading cannot run a server command")

    def ensure_database(self):
        raise ReadOnlyStoreError("A store opened for reading cannot create the database")

    @contextmanager
    def transaction(self):
        with self._client.transaction(commit=False) as native:
            yield ReadOnlyClient(native)

    def close(self) -> None:
        self._client.close()


def check_ready(client: ArcadeClient, settings: Settings) -> None:
    """Refuse, without changing anything, a store that only ``Store(path)`` could make readable."""
    from .store import SCHEMA_VERSION
    try:
        info = client.server_info()
    except ArcadeError as exc:
        if exc.status_code is not None:
            raise  # the engine answered: its own error says more than "not running"
        raise StoreNotReady("The database engine is not running; start it with `cairnmind engine start` "
                            "(this command does not start it).") from None
    if str(info.get("version", "")).split(" ", 1)[0] != settings.arcade_version:
        raise ArcadeError(f"ArcadeDB release does not match the pinned {settings.arcade_version}")
    if not client.database_exists():
        raise StoreNotReady("The Cairnmind database does not exist yet; create it with `cairnmind init`.")
    types = {row.get("name") for row in client.query("SELECT name FROM schema:types")}
    rows = client.query("SELECT value FROM devbrain_meta WHERE name='schema_version'") \
        if "devbrain_meta" in types else []
    version = rows[0].get("value") if rows else None
    if version is None:
        raise StoreNotReady("The Cairnmind database has no schema yet; create it with `cairnmind init`.")
    if version in {1, 2, 3}:
        # Only Store.init upgrades, under the writer lock and after a backup; the version is written last.
        raise StoreNotReady(f"The Cairnmind database has schema version {version} and needs the upgrade to "
                            f"{SCHEMA_VERSION}; run `cairnmind init`, which writes a backup first.")
    if version != SCHEMA_VERSION:
        raise StoreNotReady(f"Database schema version is incompatible: the database has version {version!r} "
                            f"and this release reads version {SCHEMA_VERSION}.")
    if "journal_state" not in types or not client.query("SELECT id FROM journal_state WHERE id='current'"):
        raise StoreNotReady("The Cairnmind database has no journal state; any ordinary write, for example "
                            "`cairnmind remember`, recreates it, and `cairnmind restore FILE` is for when the "
                            "data itself is damaged.")


READ_ATTEMPTS = 3


def read_consistently(store, read, attempts: int = READ_ATTEMPTS):
    """``read()`` repeated until no committed change landed while it ran, else StoreBusy."""
    from .journal import Journal
    journal = Journal(store)
    for _ in range(attempts):
        # Through the client, outside any transaction: the head is always the latest commit.
        before = journal.cursor(store.client)
        result = read()
        if journal.cursor(store.client) == before:
            return result
    raise StoreBusy(f"The store changed during each of {attempts} attempts to read it; "
                    "run the command again when the current work is done.")


def open_readonly(store_class, settings: Settings):
    """A ``store_class`` instance that reads the existing store, or ``StoreNotReady`` saying what to run."""
    data_dir = Path(settings.data_dir).expanduser().resolve()
    if not (data_dir / "config.json").is_file():
        raise StoreNotReady(f"There is no Cairnmind configuration in {data_dir}; run `cairnmind init` first.")
    client = ArcadeClient(settings)
    check_ready(client, settings)
    store = store_class.__new__(store_class)
    store.data_dir, store.settings, store.client, store.read_only = data_dir, settings, ReadOnlyClient(client), True
    store._local = threading.local()
    store._stamp_lock = threading.Lock()
    store._stamp_cursor, store._stamp_base, store._stamp_global, store._stamp_seq = None, 0, 0, {}
    return store
