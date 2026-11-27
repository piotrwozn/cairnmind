"""The whole-database backup, written as a stream (#196).

The file is the checksummed envelope 1 that every version writes and restores: the gzip of
``{"format":...,"payload":...,"sha256":...,"version":1}`` with ``sha256`` the digest of the canonical
JSON text of the payload (``json_text``). The payload is no longer built in memory. Rows are read a page
at a time inside one transaction, serialized, hashed and compressed as they come, and the envelope's
keys are written in the sorted order of ``json_text``, so the bytes are the ones the earlier writer
produced and the earlier restore reads the file unchanged. The digest is known only at the end, which
is where the sorted order puts it anyway.

The file is written under a temporary name in the destination folder while the transaction is open, and
synced and moved into place only after the transaction has committed; a backup that is interrupted or
whose transaction fails leaves no file under the destination name. A partial temporary file left by a
killed process ends without its gzip trailer, so restore refuses it; the service removes such files
once they are a day old (``stale_temporaries``).
"""
from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path
import tempfile
import time
from typing import Callable, Iterable, Iterator

from cairnmind.config import BACKUP_FORMATS
from cairnmind.storage.sql import clean_row, json_text, utc_now

# Rows are read in pages whose size adapts to the size of the rows: a page whose text is larger than
# PAGE_BYTES halves the next page, a page much smaller doubles it, between 1 and PAGE_ROWS rows.
PAGE_ROWS = 1000
PAGE_BYTES = 2 * 1024 * 1024
# Serialized text is handed to the compressor in pieces of about this size.
WRITE_BYTES = 1024 * 1024
# The name a backup has until it is complete; a killed backup leaves a file with this prefix behind.
TEMPORARY_PREFIX = ".cairnmind-backup-"
# The collections that hold vectors or name their index types: never written to a backup (#187).
VECTOR_COLLECTIONS = ("embeddings", "vector_indexes")


class Rows:
    """Rows of one collection, produced lazily as their canonical JSON text, one string per row."""

    def __init__(self, produce: Callable[[], Iterator[str]]):
        self._produce = produce

    def __iter__(self) -> Iterator[str]:
        return self._produce()


def payload_chunks(value, depth: int = 3) -> Iterator[str]:
    """The canonical JSON text of a payload in pieces; joined they equal ``json_text(value)``.

    The two outer levels (the payload and its ``vector_data``) are opened here, with keys in sorted order
    and the same separators as ``json_text``; everything inside a row is ``json_text`` itself. A ``Rows``
    value is already text.
    """
    if isinstance(value, Rows):
        yield "["
        for index, text in enumerate(value):
            yield ("," if index else "") + text
        yield "]"
    elif depth > 1 and isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            # json_text would turn other keys into strings and sort them as strings; refuse instead.
            raise TypeError("Backup payload keys must be strings")
        yield "{"
        for index, key in enumerate(sorted(value)):
            yield ("," if index else "") + json_text(key) + ":"
            yield from payload_chunks(value[key], depth - 1)
        yield "}"
    elif depth > 0 and isinstance(value, (list, tuple)):
        yield "["
        for index, item in enumerate(value):
            yield ("," if index else "") + json_text(item)
        yield "]"
    else:
        yield json_text(value)


def payload_digest(payload) -> str:
    """The envelope checksum of a payload, without building its whole text at once."""
    digest = hashlib.sha256()
    for chunk in payload_chunks(payload):
        digest.update(chunk.encode())
    return digest.hexdigest()


def _pages(tx, name: str) -> Iterator[str]:
    """Every row of a type as canonical text, paged natively by the active storage backend."""
    if hasattr(tx, "iter_backup_pages") and callable(tx.iter_backup_pages):
        yield from tx.iter_backup_pages(name, page_rows=PAGE_ROWS, page_bytes=PAGE_BYTES)
        return
    from cairnmind.storage.storage_query import asc, db_find, gt
    after: str | None = None
    while True:
        where = [gt("id", after)] if after is not None else ()
        rows = db_find(tx, name, where, order_by=(asc("id"),), limit=PAGE_ROWS)
        for row in rows:
            yield json_text(clean_row(row))
        if len(rows) < PAGE_ROWS:
            return
        after = rows[-1].get("id")
        if not isinstance(after, str):
            return


def streamed_export(tx, collections: Iterable[str], format_version: int) -> dict:
    """The whole-database payload of ``Store._export_snapshot`` with rows read only while it is written.

    Must be written while ``tx`` is open. Vectors are left out (#187): ``embeddings`` and ``vector_indexes``
    are empty and ``vector_data`` is an empty object, which every earlier restore reads; a restore
    rebuilds the vectors from the text (``Store.restore`` sets the embed jobs back to pending).
    """
    payload = {"format_version": format_version, "backend": "arcadedb", "exported_at": utc_now()}
    for collection in collections:
        payload[collection] = [] if collection in VECTOR_COLLECTIONS else Rows(lambda collection=collection: _pages(tx, collection))
    payload["vector_data"] = {}
    return payload


def write_temporary(destination, payload: dict) -> Path:
    """Compress ``payload`` into a new temporary file next to ``destination`` and return its path.

    The caller moves it into place with ``publish`` or removes it. On any failure here it is removed.
    """
    destination = Path(destination).expanduser().resolve()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=TEMPORARY_PREFIX, dir=destination.parent)
    except OSError as exc:
        # Said plainly, without an errno or the name of the temporary file (#238).
        raise ValueError(f"Cannot write a backup into {destination.parent}: {exc.strerror or exc}") from None
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb") as compressed:
                digest = hashlib.sha256()
                compressed.write(('{"format":' + json_text(BACKUP_FORMATS[0]) + ',"payload":').encode())
                pending, size = [], 0
                for chunk in payload_chunks(payload):
                    data = chunk.encode()
                    digest.update(data)
                    pending.append(data)
                    size += len(data)
                    if size >= WRITE_BYTES:
                        compressed.write(b"".join(pending))
                        pending, size = [], 0
                compressed.write(b"".join(pending))
                compressed.write((',"sha256":' + json_text(digest.hexdigest()) + ',"version":1}').encode())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def publish(temporary: Path, destination) -> Path:
    """Sync a complete temporary backup to disk and move it to ``destination``; it is removed on failure."""
    from cairnmind.pal.private_files import atomic_replace, fsync_directory, make_private
    destination = Path(destination).expanduser().resolve()
    try:
        with open(temporary, "r+b") as handle:
            os.fsync(handle.fileno())
        make_private(temporary)
        atomic_replace(temporary, destination)
        fsync_directory(destination.parent)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def write_backup(destination, payload: dict) -> Path:
    """Write ``payload`` (rows in memory, or ``Rows`` read on the way) as a backup at ``destination``."""
    return publish(write_temporary(destination, payload), destination)


def streamed_backup(transaction, destination, collections: Iterable[str], format_version: int) -> Path:
    """Read the store inside ``transaction()`` into a temporary file; move it into place only after the commit.

    A transaction that fails to commit leaves no file under ``destination``, and the transaction is not
    held open while the file is synced to disk.
    """
    temporary = None
    try:
        with transaction() as tx:
            temporary = write_temporary(destination, streamed_export(tx, collections, format_version))
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return publish(temporary, destination)


def stale_temporaries(directory, older_than: float) -> list[Path]:
    """Temporary files a killed backup left in ``directory`` and last written more than ``older_than`` seconds ago."""
    directory, cutoff = Path(directory), time.time() - older_than
    if not directory.is_dir() or directory.is_symlink():
        return []
    return [path for path in directory.glob(TEMPORARY_PREFIX + "*")
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff]
