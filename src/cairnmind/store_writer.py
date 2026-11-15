"""One declared writer per store (#70): which installation writes a database, and how that changes.

The file locks in the data directory (``database-writer.lock``, ``service.lock``, the ingestion and
configuration locks) serialise writers on one machine. They mean nothing to a second machine that
uses the same database. Instead of database leases, a database names the one installation that
writes it; every other installation reads it only, until a person moves the role explicitly.

The installation. ``installation.json`` in the data directory holds a random identifier and a
fingerprint of where that file belongs: the machine identity (``/etc/machine-id``, else the host
name), the absolute path of the data directory and its inode. A copy of the data directory, to
another machine or to another path, or a restore of it onto a new filesystem, therefore does not
match its fingerprint: it gets a new identifier at once and keeps the old one as ``copied_from``,
so the copy never writes as the original silently. A host name is only a label, shown to people:
two machines may share one.

The writer. The row ``devbrain_meta`` ``name='writer'`` names the writing installation, a label of
its host, since when, and the last changes of the role. The first installation that opens a
database without such a row becomes its writer, which is also how a store of an earlier version
gets one: for a store on one machine nothing changes. The same identifier is kept in the journal
head (``journal_state`` ``id='current'``, field ``writer``). Every committing transaction that
changes data already reads that row and updates it in the same commit (journal.JournalTransaction
.flush), so the check costs no extra round trip, and the database fences it: a transaction whose
read of the head is older than a takeover fails on commit with a conflict instead of writing.

The engine (ArcadeDB 26.11.1) decides a conflict per record and at commit, and only for a record the
transaction really changed: an UPDATE that sets the values a record already has in the transaction's
view is dropped, so it neither writes nor conflicts, and a WHERE clause is evaluated on that view
too. A takeover that read the head, then lost a race, then wrote the writer row and "stamped" the
head with the identifier its old view already showed would therefore commit the row alone, and leave
the row naming one installation and the head another. So every stamp also writes ``writer_change``,
a value that is new each time: a claim or a takeover always changes the head, and of two that overlap
the second commit fails whole (the person runs the command again and takes over from the first).

A takeover (``cairnmind store takeover --yes``) is a person's act. It rewrites both rows, records
who took over from whom, when, from which host and with which note, and puts the jobs the previous
writer was running back to pending, so that only the new writer's worker runs them. The previous
writer notices at its next write: the transaction is refused (StoreWriterLost), and every later
write of that store reads the writer row first and is refused while it names another installation.
When a person takes writing back on that installation, its store (and a service that kept running)
writes again at its next write, without a restart.

A takeover also works on a store this release cannot read as a reader (a schema of another release,
no journal head): the writer row is moved first, then the store is prepared as its writer would.

Not covered: work that is not a committed database write. A job claimed before a takeover keeps
running on the previous writer until that process tries to store its result (bounded by the job's
own time: its lease and the model timeout); files that process writes outside the database (local
backups, logs) are not fenced either. An installation of a release before #70 does not check the
writer at all.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from uuid import uuid4

from .sql import clean_row, insert_document, update_document, utc_now

INSTALLATION_FILE = "installation.json"
HISTORY_LIMIT = 20
NOTE_LIMIT = 200
TAKEOVER_COMMAND = "cairnmind store takeover --yes"
_ID = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._:@/+-]")


class StoreNotWriter(ValueError):
    """This installation reads the store only; the message names the writer and the command that changes it."""


class StoreWriterLost(StoreNotWriter):
    """Another installation took over writing after this store was opened."""


@dataclass(frozen=True)
class Installation:
    id: str
    copied_from: str | None = None


def machine_identity() -> str:
    """What tells this machine from another: the systemd machine id, else the host name."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if value:
            return "machine-id:" + value
    return "host:" + socket.gethostname()


def label(value) -> str:
    """A host name or identifier read from the database, safe to print: another installation wrote it."""
    if not isinstance(value, str) or not value:
        return "unknown"
    return _UNSAFE.sub("?", value[:64])


def short(installation_id) -> str:
    return label(installation_id)[:8]


def _fingerprint(data_dir: Path) -> str:
    text = "\0".join((machine_identity(), str(data_dir), str(data_dir.stat().st_ino)))
    return hashlib.sha256(text.encode()).hexdigest()


def _read_identity(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}  # unreadable: treated as a file that belongs elsewhere
    if (not isinstance(value, dict) or not isinstance(value.get("installation_id"), str)
            or not _ID.fullmatch(value["installation_id"]) or not isinstance(value.get("bound_to"), str)
            or not _FINGERPRINT.fullmatch(value["bound_to"])):
        return {}
    return value


def _write_identity(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".installation-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def installation(data_dir: Path) -> Installation:
    """This data directory's installation, created on first use and renewed when the directory was copied."""
    data_dir = Path(data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = data_dir / INSTALLATION_FILE
    with (data_dir / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fingerprint = _fingerprint(data_dir)
        stored = _read_identity(path)
        if stored and stored["bound_to"] == fingerprint:
            copied = stored.get("copied_from")
            return Installation(stored["installation_id"], copied if isinstance(copied, str) else None)
        value = {"installation_id": uuid4().hex, "bound_to": fingerprint, "created_at": utc_now()}
        if stored:
            value["copied_from"] = stored["installation_id"]
        _write_identity(path, value)
        return Installation(value["installation_id"], value.get("copied_from"))


def read_writer(reader) -> dict | None:
    rows = [clean_row(row) for row in reader.query("SELECT FROM devbrain_meta WHERE name='writer'")]
    if len(rows) > 1:
        raise ValueError("The store records more than one writer; run `cairnmind doctor` and report it.")
    return rows[0] if rows else None


def describe(row: dict | None) -> dict | None:
    """The writer row as it may be shown: identifiers and labels only, each made safe to print."""
    if not row:
        return None
    history = [{key: label(entry.get(key)) for key in ("installation_id", "previous", "host", "at", "how")
                if entry.get(key) is not None}
               for entry in (row.get("history") or [])[-HISTORY_LIMIT:] if isinstance(entry, dict)]
    return {"installation": label(row.get("installation_id")), "host": label(row.get("host")),
            "since": label(row.get("since")), "history": history}


def reader_reason(me: Installation, row: dict) -> str:
    writer = row.get("installation_id")
    copied = (" This data directory is a copy of that installation's, or was moved, so it got an identity of its "
              "own and does not write as the original." if me.copied_from and me.copied_from == writer else "")
    return (f"This installation only reads this store: installation {short(writer)} on {label(row.get('host'))} "
            f"writes it since {label(row.get('since'))}.{copied} To make this installation the writer, run "
            f"`{TAKEOVER_COMMAND}`; the other one then stops writing at its next write.")


def lost_reason(row: dict | None) -> str:
    row = row or {}
    return (f"Installation {short(row.get('installation_id'))} on {label(row.get('host'))} took over writing this "
            f"store at {label(row.get('since'))}; this installation no longer writes it. Run `{TAKEOVER_COMMAND}` "
            "here to take it back.")


def stamp(tx, installation_id: str) -> None:
    """Name the writer in the journal head, always as a change of that record (see the module text)."""
    tx.command("UPDATE journal_state SET writer=:writer,writer_change=:change WHERE id='current'",
               {"writer": installation_id, "change": uuid4().hex})


def head_writer(reader) -> str | None:
    """The installation the journal head names; None for a store without a head, or a head that names none."""
    from .arcade import ArcadeError
    try:
        rows = reader.query("SELECT writer FROM journal_state WHERE id='current'")
    except ArcadeError:  # a store of another release without the type
        return None
    return rows[0].get("writer") if rows else None


def mixed_reason(row: dict, head) -> str:
    """For a store whose writer row and journal head name different installations (left by a takeover race
    of a build before the fix): what holds until a person repairs it."""
    return (f"The writer row names installation {short(row.get('installation_id'))} on {label(row.get('host'))}, "
            f"but the journal head names installation {short(head)}: two takeovers ran at the same time. Until it "
            f"is repaired, a process of {short(head)} that was already running can still commit, and a running "
            f"process of {short(row.get('installation_id'))} is refused. Run `{TAKEOVER_COMMAND}` on the "
            "installation that should write this store.")


def settle(client, me: Installation) -> dict | None:
    """Make this installation the writer of a store that has none. The other writer's row, or None.

    Called by Store.init with the local writer lock held. Writes nothing when the role is already
    recorded for this installation. Two installations that claim one new store at the same time both
    change the journal head (stamp), so one of them fails on commit; Store.init then reads again.
    """
    row = read_writer(client)
    if row and row.get("installation_id") != me.id:
        return row
    head = client.query("SELECT writer FROM journal_state WHERE id='current'")
    if row and head and head[0].get("writer") == me.id:
        return None
    with client.transaction() as tx:
        # The head first: the engine refuses an update of a record only when it changed after this transaction
        # read it, so a claim committed after this read makes this commit fail, and one committed before it is
        # seen in the writer row read next (a page read for the first time shows the latest commit).
        heads = tx.query("SELECT writer FROM journal_state WHERE id='current'")
        row = read_writer(tx)
        if row and row.get("installation_id") != me.id:
            return row
        if row and heads and heads[0].get("writer") not in (None, me.id):
            # The row names this installation and the head another one (mixed_reason). Opening a store never
            # moves the fence away from an installation: writes are refused until a person runs the takeover.
            return None
        if row is None:
            now = utc_now()
            insert_document(tx, "devbrain_meta", {
                "name": "writer", "installation_id": me.id, "host": label(socket.gethostname()), "since": now,
                "history": [{"installation_id": me.id, "host": label(socket.gethostname()), "at": now, "how": "first"}]})
        stamp(tx, me.id)
    return None


def check_head(store, head: dict | None, native) -> None:
    """Refuse the commit of a store whose installation is no longer the writer (journal flush hook)."""
    me = store.installation.id
    recorded = head.get("writer") if head else None
    if recorded == me:
        return
    row = read_writer(native)
    if recorded is None and (row is None or row.get("installation_id") == me):
        # A head written without the field (a journal recreated by an earlier release): put it back.
        if head:
            stamp(native, me)
        return
    # The row may name this installation while the head names another (mixed_reason). Not repaired here: this
    # transaction may have read the row before a takeover and the head after it.
    store._writer_lost = mixed_reason(row, recorded) if row and row.get("installation_id") == me and recorded \
        else lost_reason(row)
    raise StoreWriterLost(store._writer_lost)


def recheck(store) -> None:
    """A store that lost the role reads the writer row again: a person may have taken writing back here."""
    if not store._writer_lost:
        return
    row = read_writer(getattr(store, "_writable", None) or store.client)
    store._writer_lost = None if row and row.get("installation_id") == store.installation.id else lost_reason(row)


def require_writer(store) -> None:
    """For a write that does not pass through the journal (schema statements, restore): one read."""
    recheck(store)
    if store._writer_lost:
        raise StoreWriterLost(store._writer_lost)
    if store.not_writer:
        raise StoreNotWriter(store.not_writer)
    row = read_writer(store.client)
    if row and row.get("installation_id") != store.installation.id:
        store._writer_lost = lost_reason(row)
        raise StoreWriterLost(store._writer_lost)


def valid_note(note) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str) or len(note) > NOTE_LIMIT or any(ord(char) < 32 or ord(char) == 127 for char in note):
        raise ValueError(f"The note must be one line of at most {NOTE_LIMIT} characters")
    return note.strip() or None


def take_over(store, note: str | None = None) -> dict:
    """Make this store's installation the writer, recording the change. A person's act, never automatic."""
    from .journal import JournalTransaction
    note = valid_note(note)
    me = store.installation.id
    client = getattr(store, "_writable", None) or store.client
    # A store this release could not read (Store(path, for_takeover=True)): only the writer row and, when there is
    # one, the head are changed here; take_over_at then prepares the store as the writer and returns the jobs.
    ready = not getattr(store, "not_ready", None)
    with store._lock(), client.transaction() as native:
        has_head = ready or "journal_state" in {row.get("name") for row in native.query("SELECT name FROM schema:types")}
        # The head first, as in settle: the stamp below changes it, so a claim or a takeover committed after
        # this read makes this commit fail, and one committed before it is seen in the writer row read next.
        heads = native.query("SELECT writer FROM journal_state WHERE id='current'") if has_head else []
        has_head, named = bool(heads), heads[0].get("writer") if heads else None
        row = read_writer(native)
        previous = row.get("installation_id") if row else None
        # The row names this installation and the head another one (mixed_reason): the head is put right.
        repair = previous == me and named not in (None, me)
        if previous == me and not repair:
            store._writer_lost = None
            return {"taken_over": False, "reason": "This installation is already the writer.", "writer": describe(row)}
        if repair:
            previous = named
        now, host = utc_now(), label(socket.gethostname())
        entry = {"installation_id": me, "previous": previous, "host": host, "at": now,
                 "how": "repair" if repair else "takeover"}
        if note:
            entry["note"] = note
        history = [*(row.get("history") or []), entry][-HISTORY_LIMIT:] if row else [entry]
        values = {"installation_id": me, "host": host, "since": now, "history": history}
        if row:
            update_document(native, "devbrain_meta", values, "name='writer'", {})
        else:
            insert_document(native, "devbrain_meta", {"name": "writer", **values})
        if has_head:
            stamp(native, me)
        released = 0
        if ready:
            tx = JournalTransaction(native, writer=me, check=lambda head, reader: None)
            released = release_running_jobs(tx, now)
            tx.flush()
    store._writer_lost = None
    result = {"taken_over": True, "previous": label(previous) if previous else None, "released_jobs": released,
              "writer": describe({**values, "history": history})}
    if repair:
        result["repaired"] = "The writer row already named this installation; the journal head now does too."
    return result


def release_running_jobs(tx, now: str) -> int:
    """Put the jobs the previous writer was running back to pending, so that only this writer's worker runs them."""
    from .store import Store
    released = 0
    for job in tx.query("SELECT FROM jobs WHERE state='running'"):
        job = clean_row(job)
        job["state"] = "pending"
        update_document(tx, "jobs", {"state": "pending", "worker": None, "lease_until": None, "updated_at": now,
                                     "attempt_history": Store._job_event(job, "writer_takeover", state="pending")},
                        "id=:id AND state='running'", {"id": job["id"]})
        released += 1
    return released


def take_over_at(data_dir: Path, note: str | None = None) -> dict:
    """`cairnmind store takeover --yes`: also for a store this release cannot read as a reader."""
    from .arcade import ArcadeError
    from .store import Store
    note = valid_note(note)
    store = Store(data_dir, for_takeover=True)
    try:
        result = take_over(store, note)
    except ArcadeError as exc:
        if "ConcurrentModification" not in exc.exception_name:
            raise
        raise StoreNotWriter("Another installation claimed or took over this store at the same moment, so nothing "
                             f"was changed here. Run `{TAKEOVER_COMMAND}` again to take over from it.") from None
    if not store.not_ready or not result["taken_over"]:
        return result
    # Now that this installation is the writer, open the store as the writer: that upgrades the schema it can
    # upgrade and recreates a missing journal head at the first write.
    try:
        writer = Store(data_dir)
        with writer.transaction() as tx:
            result["released_jobs"] = release_running_jobs(tx, utc_now())
    except (ValueError, ArcadeError) as exc:
        result["next"] = f"This installation now writes the store, but this release cannot prepare it: {exc}"[:600]
    return result


def state(store) -> dict:
    """What `status` and `doctor` say about the writer role of this store's installation."""
    me = store.installation
    row = read_writer(store.client)
    if store._writer_lost and row and row.get("installation_id") == me.id:
        store._writer_lost = None  # taken back by a person since the role was lost
    if store._writer_lost:
        store._writer_lost = lost_reason(row)
        role, reason = "lost", store._writer_lost
    elif row is None:
        role, reason = "none", "No installation is recorded as the writer; the next one that opens the store for writing becomes it."
    elif row.get("installation_id") == me.id:
        role, reason = "writer", None
    else:
        role, reason = "reader", reader_reason(me, row)
    result = {"role": role, "installation": me.id, "writer": describe(row)}
    if me.copied_from:
        result["copied_from"] = label(me.copied_from)
    if reason:
        result["reason"] = reason
    named = head_writer(store.client) if row else None
    mixed = bool(named) and named != row.get("installation_id")
    if mixed:
        result["head_writer"], result["problem"] = label(named), mixed_reason(row, named)
    if mixed or role in {"reader", "lost"}:
        result["next"] = TAKEOVER_COMMAND
    return result


def inspect(settings) -> dict:
    """The writer block of `doctor`: read without starting the engine or preparing the store."""
    from .arcade import ArcadeError
    from .store import Store
    from .store_readonly import StoreNotReady
    try:
        store = Store.open_readonly(settings)
        store.installation = installation(settings.data_dir)
        store._writer_lost, store.not_writer = None, None
        return state(store)
    except (StoreNotReady, ArcadeError, ValueError, OSError) as exc:
        return {"role": "unknown", "reason": str(exc)[:300]}
