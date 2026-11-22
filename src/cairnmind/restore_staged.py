"""A restore that holds the engine's memory in bounds, whatever the size of the store (#362).

What the trial found: every request of a restore was already small (a group of at most 250 rows or 1 MiB, #219),
but the whole restore was one transaction, and the engine keeps the pages a transaction changed in its heap until
the commit. Writing the rows of a store of this repository's size held about 1 GB, deleting the old ones about 0.9 GB:
on the engine's default heap of 1 GB the restore ran out of memory every time.

Here the old rows are deleted, and the backup's rows written, in transactions of at most ``ROWS_PER_TRANSACTION``
rows or ``BYTES_PER_TRANSACTION`` of text. One transaction no longer makes the restore all or nothing, so a
mark does: before the first row is deleted, a row ``restore_unfinished`` of ``devbrain_meta`` (the store's own
metadata, which no backup carries and no restore deletes, and which every installation of a shared store sees)
names the backup, the safety copy ``before-restore-*`` and the digest of that copy.

- A restore that fails puts the store back from the safety copy and removes the mark: the store holds the backup
  or what it held before, and the error says which.
- A restore that is cut off or killed, or whose undo failed too, leaves the mark. Every opening of the store is
  refused, for a writer and a reader, with the sentence that names both files and both commands, until
  `cairnmind restore` finishes it (``Store(for_restore=True)`` is the only opening that passes).

The English sentences are the source; ``MESSAGES`` holds their Polish.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
from pathlib import Path
import re
import sys
from typing import NoReturn
from uuid import uuid4

from .sql import utc_now
from .store_readonly import StoreNotReady
from .terminal_safe import safe_text

MARK = "restore_unfinished"  # the name of the row of devbrain_meta
ROWS_PER_TRANSACTION = 5000
BYTES_PER_TRANSACTION = 8 << 20
JOURNAL_TYPES = ("journal_state", "journal_events")

UNFINISHED = (
    "A restore of this store did not finish: it began at {started_at} on installation {installation}, from {backup}. "
    "Until a restore finishes, the store holds only part of that backup and no command uses it. On the installation "
    "that writes this store, run `cairnmind restore {backup}` to finish it, or `cairnmind restore {before}` to go back "
    "to the store as it was before (that file is in the backups of that installation).")
UNDO_UNFINISHED = (
    "A restore of this store failed, and putting the store back did not finish either: it began at {started_at} on "
    "installation {installation}, from {backup}. Until a restore finishes, no command uses the store. On the "
    "installation that writes this store, run `cairnmind restore {before}` to go back to the store as it was before, "
    "or `cairnmind restore {backup}` to restore that backup.")
UNREADABLE = (
    "This store carries a mark of an unfinished restore that cannot be read, so no command uses it. On the "
    "installation that writes this store, restore a whole backup with `cairnmind restore FILE`; the copies made before "
    "earlier restores are in backups/ of its data directory (before-restore-*).")
UNDONE = "{error} The restore stopped, and the store was put back as it was before, from {before}."
UNDONE_VECTORS = (
    "{error} The restore stopped, and the store was put back as it was before, from {before}. That copy holds no "
    "vectors: {jobs} embedding job(s) are pending again and the service rebuilds the vectors in the background; until "
    "then search uses the text only.")
NOT_UNDONE = (
    "{error} The restore stopped, and putting the store back from {before} failed too ({undo_error}). Until a restore "
    "finishes, no command uses the store: run `cairnmind restore {before}` to go back to the store as it was before, "
    "or `cairnmind restore {backup}` to restore that backup again.")
NO_UNDO = (
    "{error} The restore stopped. The copy from before the first, unfinished restore ({before}) is not on this machine, "
    "so the store was not put back. Until a restore finishes, no command uses the store: run `cairnmind restore "
    "{backup}` again, or restore another whole backup.")
COPY_CHANGED = ("the copy {before} changed after the restore wrote it, so it is not used to put the store back")
# What a person is told when the engine fails during a restore: what happened, then where its heap is set.
MEMORY_SEEN = "The database engine ran out of memory (Java heap space)."
MEMORY_MAYBE = ("The database engine failed or stopped answering; if its log names OutOfMemoryError or \"Available heap "
                "RAM is critically low\", it ran out of memory.")
_RESTORE_FITS = ("a restore commits every 5,000 rows or 8 MB of text whatever the size of the store, which fits in that "
                 "heap, so stop other work on the engine (`cairnmind stop`) and run the restore again.")
WHERE_NATIVE = ("Its log is {log}. The engine Cairnmind starts on this machine has a heap of 1 GB (-Xmx1024m), which "
                "this release does not change; " + _RESTORE_FITS)
WHERE_DOCKER = ("Its log: `docker logs {container}`. The container Cairnmind starts has a heap of 1 GB (-Xmx1024m), which "
                "this release does not change; " + _RESTORE_FITS)
WHERE_SERVER = ("Its log is the server's own (for deploy/kubernetes: `kubectl -n cairnmind-db logs "
                "statefulset/arcadedb`). To give the server more memory, add -Xmx4g to ARCADEDB_JAVA_OPTS and raise the "
                "container's memory limit with it, as docs/DEPLOYMENT.md says under What to change, Memory.")
_FITS_PL = ("przywracanie zatwierdza co 5000 wierszy albo 8 MB tekstu bez względu na rozmiar magazynu, co mieści się w "
            "tej stercie, więc zatrzymaj inną pracę silnika (`cairnmind stop`) i uruchom przywracanie ponownie.")
MESSAGES = {
    MEMORY_SEEN: "Silnikowi bazy danych zabrakło pamięci (Java heap space).",
    MEMORY_MAYBE: ("Silnik bazy danych zawiódł albo przestał odpowiadać; jeśli jego dziennik wymienia "
                   "OutOfMemoryError albo \"Available heap RAM is critically low\", zabrakło mu pamięci."),
    WHERE_NATIVE: ("Jego dziennik to {log}. Silnik, który Cairnmind uruchamia na tym komputerze, ma stertę 1 GB "
                   "(-Xmx1024m), której to wydanie nie zmienia; " + _FITS_PL),
    WHERE_DOCKER: ("Jego dziennik: `docker logs {container}`. Kontener, który Cairnmind uruchamia, ma stertę 1 GB "
                   "(-Xmx1024m), której to wydanie nie zmienia; " + _FITS_PL),
    WHERE_SERVER: ("Jego dziennik to dziennik samego serwera (dla deploy/kubernetes: `kubectl -n cairnmind-db logs "
                   "statefulset/arcadedb`). Aby dać serwerowi więcej pamięci, dopisz -Xmx4g do ARCADEDB_JAVA_OPTS i "
                   "razem z tym podnieś limit pamięci kontenera, tak jak mówi docs/DEPLOYMENT.md w części What to "
                   "change, Memory."),
    UNDONE: "{error} Przywracanie zostało przerwane, a magazyn przywrócono do stanu sprzed niego, z pliku {before}.",
    UNDONE_VECTORS: (
        "{error} Przywracanie zostało przerwane, a magazyn przywrócono do stanu sprzed niego, z pliku {before}. Ta "
        "kopia nie zawiera wektorów: zadań osadzania, które znów czekają, jest {jobs}, a usługa odbuduje wektory w "
        "tle; do tego czasu wyszukiwanie korzysta tylko z tekstu."),
    NOT_UNDONE: (
        "{error} Przywracanie zostało przerwane i przywrócenie magazynu z pliku {before} też się nie powiodło "
        "({undo_error}). Dopóki jakieś przywracanie się nie zakończy, żadne polecenie nie używa magazynu: uruchom "
        "`cairnmind restore {before}`, aby wrócić do stanu sprzed przywracania, albo `cairnmind restore {backup}`, aby "
        "ponownie przywrócić tamtą kopię."),
    NO_UNDO: (
        "{error} Przywracanie zostało przerwane. Kopii sprzed pierwszego, niedokończonego przywracania ({before}) nie "
        "ma na tym komputerze, więc magazynu nie przywrócono. Dopóki jakieś przywracanie się nie zakończy, żadne "
        "polecenie nie używa magazynu: uruchom ponownie `cairnmind restore {backup}` albo przywróć inną pełną kopię."),
    COPY_CHANGED: (
        "kopia {before} zmieniła się po tym, jak przywracanie ją zapisało, więc nie posłuży do przywrócenia magazynu"),
    UNFINISHED: (
        "Przywracanie tego magazynu nie zostało dokończone: zaczęło się {started_at} w instalacji {installation}, z "
        "pliku {backup}. Dopóki jakieś przywracanie się nie zakończy, magazyn zawiera tylko część tej kopii i żadne "
        "polecenie go nie używa. W instalacji, która zapisuje ten magazyn, uruchom `cairnmind restore {backup}`, aby je "
        "dokończyć, albo `cairnmind restore {before}`, aby wrócić do magazynu sprzed przywracania (ten plik jest w "
        "kopiach zapasowych tamtej instalacji)."),
    UNDO_UNFINISHED: (
        "Przywracanie tego magazynu nie powiodło się i przywrócenie poprzedniego stanu też nie zostało dokończone: "
        "zaczęło się {started_at} w instalacji {installation}, z pliku {backup}. Dopóki jakieś przywracanie się nie "
        "zakończy, żadne polecenie nie używa magazynu. W instalacji, która zapisuje ten magazyn, uruchom `cairnmind "
        "restore {before}`, aby wrócić do magazynu sprzed przywracania, albo `cairnmind restore {backup}`, aby "
        "przywrócić tamtą kopię."),
    UNREADABLE: (
        "Ten magazyn ma znacznik niedokończonego przywracania, którego nie da się odczytać, więc żadne polecenie go "
        "nie używa. W instalacji, która zapisuje ten magazyn, przywróć całą kopię poleceniem `cairnmind restore "
        "PLIK`; kopie zrobione przed wcześniejszymi przywracaniami są w backups/ jej katalogu danych "
        "(before-restore-*)."),
}

_TEXT_FIELDS = ("backup", "before", "installation_id", "started_at")


class RestoreFailed(ValueError):
    """A restore that stopped; the message says whether the store was put back."""


class RestoreUnfinished(StoreNotReady):
    """The store carries the mark of a restore that did not finish; ``mark`` is empty when it cannot be read."""

    def __init__(self, message: str, mark: dict):
        super().__init__(message)
        self.mark = mark


def unfinished(reader) -> dict | None:
    """The mark of an unfinished restore, None when there is none, and {} when a row is there that cannot be read.

    The row may have been written by another installation, so every field is checked here."""
    rows = reader.query("SELECT FROM devbrain_meta WHERE name=:name", {"name": MARK})
    if not rows:
        return None
    if len(rows) != 1:
        return {}
    row = rows[0]
    mark = {key: row.get(key) for key in (*_TEXT_FIELDS, "before_sha256", "undoing")}
    if (not all(isinstance(mark[key], str) and 1 <= len(mark[key]) <= 4096 and "\0" not in mark[key]
                for key in _TEXT_FIELDS)
            or not isinstance(mark["before_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", mark["before_sha256"])
            or not isinstance(mark["undoing"], bool)):
        return {}
    return mark


def refusal(mark: dict) -> str:
    if not mark:
        return UNREADABLE
    template = UNDO_UNFINISHED if mark["undoing"] else UNFINISHED
    return template.format(started_at=safe_text(mark["started_at"]), installation=safe_text(mark["installation_id"]),
                           backup=safe_text(mark["backup"]), before=safe_text(mark["before"]))


def refuse_if_unfinished(reader) -> None:
    mark = unfinished(reader)
    if mark is not None:
        raise RestoreUnfinished(refusal(mark), mark)


def write_mark(store, mark: dict) -> None:
    from .sql import insert_document, one_row, update_document
    with store.transaction() as tx:
        if one_row(tx, "SELECT FROM devbrain_meta WHERE name=:name", {"name": MARK}, False):
            update_document(tx, "devbrain_meta", mark, "name=:name", {"name": MARK})
        else:
            insert_document(tx, "devbrain_meta", {"name": MARK, **mark})


def clear_mark(store) -> None:
    with store.transaction() as tx:
        tx.command("DELETE FROM devbrain_meta WHERE name=:name", {"name": MARK})


class Batches:
    """The open transaction of the write phase: ``wrote`` counts what went into it and, at the bound, commits it.

    Each transaction resets the journal, as the one transaction of a restore did: that is what lets the graph be
    written in groups, and what leaves the journal free of single rows."""

    def __init__(self, store):
        self.store, self._stack, self.tx = store, None, None
        self._open()

    def _open(self) -> None:
        from .journal import Journal
        stack = ExitStack()
        try:
            self.tx = stack.enter_context(self.store.transaction())
            Journal.reset(self.tx, reason="restore")
        except BaseException:
            stack.close()
            raise
        self._stack, self.rows, self.size = stack, 0, 0

    def wrote(self, rows: int, size: int = 0) -> None:
        self.rows += rows
        self.size += size
        if self.rows >= ROWS_PER_TRANSACTION or self.size >= BYTES_PER_TRANSACTION:
            self.commit()
            self._open()

    def commit(self) -> None:
        """Commit the open transaction; none is open afterwards."""
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()

    def abandon(self) -> None:
        """Roll the open transaction back, inside the handler of the error that ends the restore. Never raises."""
        stack, self._stack = self._stack, None
        if stack is not None:
            try:
                stack.__exit__(*sys.exc_info())
            except Exception:  # noqa: BLE001 - the first error is the one the person needs
                pass


def clear(store, types: list[str]) -> None:
    """Delete every row of ``types``, in order, at most ``ROWS_PER_TRANSACTION`` rows per transaction."""
    from .journal import Journal
    from .sql import quote_name
    types = list(types)
    while types:
        with store.transaction() as tx:
            Journal.reset(tx, reason="restore")
            budget = ROWS_PER_TRANSACTION
            while types and budget:
                result = tx.command(f"DELETE FROM {quote_name(types[0])} LIMIT {budget}")
                count = result[0].get("count") if isinstance(result, list) and result and isinstance(result[0], dict) else None
                if type(count) is not int or not 0 <= count <= budget:
                    raise RuntimeError("The database did not say how many rows of a restore it deleted")
                budget -= count
                if budget:  # fewer than asked: this type is empty now
                    types.pop(0)


def _file_sha256(path: Path) -> str:
    """The digest ``backup_reader.StreamedBackup`` reads from the file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _engine_log(settings) -> Path:
    return settings.data_dir / "arcade" / "engine.log"


def _log_size(settings) -> int | None:
    """The size of the managed native engine's log now, to read what it says during the restore; None when there is
    no such log to read (a server, a container) or it cannot be asked."""
    from .database_mode import connect_only
    if connect_only(settings) or settings.engine_mode == "docker":
        return None
    try:
        return _engine_log(settings).stat().st_size
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def _log_names_memory(settings, offset: int) -> bool:
    try:
        with _engine_log(settings).open("rb") as handle:
            size = handle.seek(0, 2)
            start = offset if offset <= size else 0  # a smaller log was rotated: read it from its start
            handle.seek(max(start, size - (4 << 20)))
            return b"OutOfMemoryError" in handle.read()
    except OSError:
        return False


def memory_hint(exc: BaseException, settings, offset: int | None) -> str | None:
    """Say that the engine ran out of memory, or may have, and where its heap is set; None for any other error.

    Only an answer of 500 and a dropped or unanswered request are what an engine out of memory gives (in production
    mode it says only "Internal error"); `offset` is the engine's log size when the restore began."""
    from .arcade import ArcadeError
    from .database_mode import connect_only
    if not isinstance(exc, ArcadeError) or exc.status_code not in (500, None):
        return None
    server, docker = connect_only(settings), settings.engine_mode == "docker"
    seen = ("heap space" in str(exc) or "OutOfMemoryError" in exc.exception_name
            or (not server and not docker and offset is not None and _log_names_memory(settings, offset)))
    if server:
        where = WHERE_SERVER
    elif docker:
        from .engine_docker import container_name
        where = WHERE_DOCKER.format(container=container_name(settings))
    else:
        where = WHERE_NATIVE.format(log=_engine_log(settings))
    return f"{MEMORY_SEEN if seen else MEMORY_MAYBE} {where}"


def _described(exc: BaseException, settings, offset: int | None) -> str:
    text = str(exc).strip() or type(exc).__name__
    text = text if text.endswith((".", "!", "?", ")")) else text + "."
    hint = memory_hint(exc, settings, offset)
    return f"{text} {hint}" if hint else text


def staged_restore(store, source, path: Path, claims) -> None:
    """Replace the store with the validated backup `source` (read from `path`), as one outcome: the backup, or the
    store as it was. Called by ``Store.restore`` once the writer is known."""
    from .config import BACKUP_SUFFIXES
    offset = _log_size(store.settings)
    mark = unfinished(store.client)
    if mark:
        # Finishing an unfinished restore: the copy to go back to is the one from before it, not one of its half store.
        before, before_sha256 = Path(mark["before"]), mark["before_sha256"]
    else:
        # No mark, or one that cannot be read and so names no copy: a copy of the store as it is now.
        before = store.data_dir / "backups" / f"before-restore-{uuid4().hex}{BACKUP_SUFFIXES[0]}"
        store.backup(before)
        before_sha256 = _file_sha256(before)
    mark = {"backup": str(path), "before": str(before), "before_sha256": before_sha256,
            "installation_id": store.installation.id, "started_at": utc_now(), "undoing": False}
    write_mark(store, mark)
    try:
        store._restore_from(source, claims)
    except Exception as exc:  # noqa: BLE001 - every failure is undone or leaves the mark; none passes as it was
        _undo(store, mark, _described(exc, store.settings, offset), exc)
    clear_mark(store)


def _undo(store, mark: dict, error: str, cause: BaseException) -> NoReturn:
    """Put the store back from the copy made before the restore, then raise ``RestoreFailed`` saying so."""
    from .backup_reader import opened_backup
    from .store import RESTORE_TYPES
    before, backup = Path(mark["before"]), safe_text(mark["backup"])
    if not before.is_file():
        raise RestoreFailed(NO_UNDO.format(error=error, before=safe_text(before), backup=backup)) from cause
    try:
        write_mark(store, {**mark, "undoing": True})
        with opened_backup(before, store._validated_backup, RESTORE_TYPES) as copy:
            # The copy is this store's own state, written by this restore and checked by its digest, not a foreign
            # backup: nothing in it becomes a claim (#214), and the one-time upgrade step does not run.
            if copy.digest != mark["before_sha256"]:
                raise ValueError(COPY_CHANGED.format(before=safe_text(before)))
            store._restore_from(copy, None)
        clear_mark(store)
    except Exception as second:  # noqa: BLE001 - the mark stays, and the person is told how to finish
        raise RestoreFailed(NOT_UNDONE.format(error=error, undo_error=_described(second, store.settings, None).rstrip("."),
                                              before=safe_text(before), backup=backup)) from cause
    jobs = store.embed_jobs_requeued
    raise RestoreFailed((UNDONE_VECTORS if jobs else UNDONE).format(error=error, before=safe_text(before), jobs=jobs)) from cause


def replace(store, source, claims) -> int | None:
    """Delete every row of the store and write the backup's, in transactions of bounded size.

    Returns how many embed jobs were set back to pending (Store._requeue_embed_jobs). A failure leaves the store
    part way: ``staged_restore`` is what puts it back."""
    from .restore_groups import write_restored
    from .store import DOCUMENT_TYPES
    vectors = [row["type_name"] for row in store.query("SELECT type_name FROM vector_indexes")
               if str(row.get("type_name", "")).startswith("vector_")]
    # The journal's own rows are rewritten by every commit of a reset (journal.JournalTransaction.flush).
    clear(store, [*vectors, "edges", "nodes", *(name for name in reversed(DOCUMENT_TYPES) if name not in JOURNAL_TYPES)])
    batches = Batches(store)
    try:
        write_restored(store, batches, source, claims)
        requeued = store._requeue_embed_jobs(batches)
        batches.commit()
    except BaseException:
        batches.abandon()
        raise
    return requeued
