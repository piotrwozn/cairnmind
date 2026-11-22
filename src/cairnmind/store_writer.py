"""One declared writer per store (#70): which installation writes a database, and how that changes.

The file locks in the data directory (``database-writer.lock``, ``service.lock``, the ingestion and
configuration locks) serialise writers on one machine. They mean nothing to a second machine that
uses the same database. Instead of database leases, a database names the one installation that
writes it; every other installation reads it only, until a person moves the role explicitly.

The installation. ``installation.json`` in the data directory holds a random identifier and a
fingerprint of where that file belongs: the machine identity (``/etc/machine-id`` on Linux,
IOPlatformUUID on macOS, else the host name), the absolute path of the data directory and its inode. A copy of the data directory, to
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

The times. ``since`` and each history entry's ``at`` are the database server's time (server_now), read in the
transaction that writes them, so they do not go backwards in the order of commits whatever the clients' clocks say (#372);
an entry of a release before 0.13.0 carries its installation's clock.

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
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from uuid import UUID, uuid4

from .sql import clean_row, insert_document, update_document, utc_now

INSTALLATION_FILE = "installation.json"
HISTORY_LIMIT = 20
NOTE_LIMIT = 200
TAKEOVER_COMMAND = "cairnmind store takeover --yes"
# The engine refused the takeover's commit: the journal head changed after the takeover read it.
REFUSED_TAKEOVER = ("The store changed while this takeover ran: its writer wrote to it, or another installation "
                    f"claimed it or took it over. Nothing was changed here. Run `{TAKEOVER_COMMAND}` again; while "
                    "the writer keeps writing, this can happen more than once.")
_ID = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._:@/+-]")
# macOS: the absolute path, so that no command of that name earlier on PATH answers instead (ioreg(8)).
IOREG = ("/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice")
IOREG_TIMEOUT = 5.0
_IOREG_UUID = re.compile(r'^[ \t|]*"IOPlatformUUID" = "([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
                         r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"[ \t]*$', re.MULTILINE)
_known_platform_uuid: str | None = None  # read once per process: a later failed run does not change it


class StoreNotWriter(ValueError):
    """This installation reads the store only; the message names the writer and the command that changes it."""


class StoreWriterLost(StoreNotWriter):
    """Another installation took over writing after this store was opened."""


@dataclass(frozen=True)
class Installation:
    id: str
    copied_from: str | None = None


def platform_uuid(output: str) -> str | None:
    """The IOPlatformUUID in the output of ``ioreg -rd1 -c IOPlatformExpertDevice``, in lower case; None unless
    the output names exactly one UUID on a line of its own (the nil UUID names no machine)."""
    found = {match.lower() for match in _IOREG_UUID.findall(output)}
    if len(found) != 1:
        return None
    value = str(UUID(found.pop()))
    return None if value == str(UUID(int=0)) else value


def _darwin_identity() -> str | None:
    """IOPlatformUUID of this Mac: unlike its host name, it does not change with the network (#234)."""
    global _known_platform_uuid
    if _known_platform_uuid is None:
        try:
            done = subprocess.run(IOREG, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, timeout=IOREG_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        _known_platform_uuid = platform_uuid(done.stdout) if done.returncode == 0 else None
    return _known_platform_uuid


def machine_identity() -> str:
    """What tells this machine from another: the platform UUID on macOS, the systemd machine id on Linux, else
    the host name."""
    if sys.platform == "darwin":
        value = _darwin_identity()
        return "platform-uuid:" + value if value else "host:" + socket.gethostname()
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


def _note(value) -> dict:
    """The note of a takeover (#242): free text another installation wrote, shown as inert text, one line."""
    if not isinstance(value, str) or not value.strip():
        return {}
    from .terminal_safe import safe_text
    return {"note": safe_text(" ".join(value.split()), limit=NOTE_LIMIT)}


def describe(row: dict | None) -> dict | None:
    """The writer row as it may be shown: identifiers and labels only, each made safe to print."""
    if not row:
        return None
    history = [{**{key: label(entry.get(key)) for key in ("installation_id", "previous", "host", "at", "how")
                   if entry.get(key) is not None}, **_note(entry.get("note"))}
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


def server_now(tx) -> str:
    """The database server's time, in the format ``utc_now`` gives, read in the transaction that writes the writer
    row (#372): clocks of installations differ, the server's is one, so the history's times do not go backwards in
    the order of commits (two takeovers that overlap cannot both commit: the head fences them)."""
    milliseconds = tx.query("SELECT sysdate().asLong() AS milliseconds")[0]["milliseconds"]
    if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
        raise ValueError("The database server gave no time; the writer row was not changed.")
    return (datetime.fromtimestamp(0, timezone.utc) + timedelta(milliseconds=milliseconds)).isoformat(timespec="microseconds")


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


def read_fence(reader) -> tuple[dict | None, str | None]:
    """The writer row and the installation the journal head names, as one commit left them.

    ``reader`` is a client outside a transaction. A transaction does not give one view of two records here
    (measured: a record read for the first time shows the latest commit, one read before keeps its old
    value), so the row is read again after the head: every claim and takeover changes the row, and a row
    that did not change means no takeover was committed between the reads.
    """
    row = read_writer(reader)
    for _ in range(3):
        named = head_writer(reader) if row else None
        again = read_writer(reader)
        if again == row:
            break
        row = again
    return row, named


def fence_reason(row: dict | None, named, me: str) -> str | None:
    """Why this installation does not write a store with this row and head; None when it does."""
    if not row or row.get("installation_id") != me:
        return lost_reason(row)
    return mixed_reason(row, named) if named and named != me else None


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
            now = server_now(tx)
            insert_document(tx, "devbrain_meta", {
                "name": "writer", "installation_id": me.id, "host": label(socket.gethostname()), "since": now,
                "history": [{"installation_id": me.id, "host": label(socket.gethostname()), "at": now, "how": "first"}]})
        stamp(tx, me.id)
    return None


CLAIM_WAIT = 10.0


def claimed_meanwhile(client, me: Installation, error, *, wait: float = CLAIM_WAIT) -> dict | None:
    """The writer row of another installation, when ``error`` is the engine's refusal of a commit that met
    that installation's: two of them opened one store without a writer at the same moment. None otherwise.

    The one whose commit came first goes on to name itself as the writer, which takes it a few more
    requests; this one waits for that row, for ``wait`` seconds at most. Without it (the other one stopped
    in between) the caller raises ``error`` as it was, and the same command run again finishes the store.
    """
    import time
    # The exception name is in the body in every server mode; the detail text is only in development mode.
    if getattr(error, "status_code", None) != 503 or "ConcurrentModification" not in getattr(error, "exception_name", ""):
        return None
    deadline = time.monotonic() + wait
    while True:
        try:
            row = read_writer(client)
        except Exception:  # noqa: BLE001 - the type may not exist yet; the caller raises the first error
            row = None
        if row and row.get("installation_id") != me.id:
            return row
        if row or time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


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
    client = getattr(store, "_writable", None) or store.client
    row, named = read_writer(client), None
    if row and row.get("installation_id") == store.installation.id:
        row, named = read_fence(client)  # the head too: it may name another installation (mixed_reason)
    store._writer_lost = fence_reason(row, named, store.installation.id)


def require_writer(store) -> None:
    """For a write that does not pass through the journal (schema statements, restore): the row and the head
    are read first, and either one naming another installation refuses it."""
    if store.not_writer:
        raise StoreNotWriter(store.not_writer)
    row, named = read_fence(store.client)
    # A store without a writer row (one being created or claimed) is refused only when it had lost the role.
    store._writer_lost = fence_reason(row, named, store.installation.id) if row or store._writer_lost else None
    if store._writer_lost:
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
        now, host = utc_now(), label(socket.gethostname())  # now: the jobs' updated_at, as everywhere else
        at = server_now(native)  # the writer row and its history: one clock for every installation (#372)
        entry = {"installation_id": me, "previous": previous, "host": host, "at": at,
                 "how": "repair" if repair else "takeover"}
        if note:
            entry["note"] = note
        history = [*(row.get("history") or []), entry][-HISTORY_LIMIT:] if row else [entry]
        values = {"installation_id": me, "host": host, "since": at, "history": history}
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
        raise StoreNotWriter(REFUSED_TAKEOVER) from None
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
    row, named = read_fence(store.client)
    mine = bool(row) and row.get("installation_id") == me.id
    mixed = bool(row) and bool(named) and named != row.get("installation_id")
    if store._writer_lost and mine and not mixed:
        store._writer_lost = None  # taken back by a person since the role was lost
    if store._writer_lost and not mine:
        store._writer_lost = lost_reason(row)
        role, reason = "lost", store._writer_lost
    elif row is None:
        role, reason = "none", "No installation is recorded as the writer; the next one that opens the store for writing becomes it."
    elif mine:
        role, reason = "writer", None
    else:
        role, reason = "reader", reader_reason(me, row)
    result = {"role": role, "installation": me.id, "writer": describe(row)}
    if me.copied_from:
        result["copied_from"] = label(me.copied_from)
    if reason:
        result["reason"] = reason
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
