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

import re

from .store_readonly import StoreNotReady
from .terminal_safe import safe_text

MARK = "restore_unfinished"  # the name of the row of devbrain_meta
ROWS_PER_TRANSACTION = 5000
BYTES_PER_TRANSACTION = 8 << 20

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
MESSAGES = {
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
