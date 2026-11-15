"""The whole-database backup, written as a stream (#196).

The file is the checksummed envelope 1 that every version writes and restores: the gzip of
``{"format":...,"payload":...,"sha256":...,"version":1}`` with ``sha256`` the digest of the canonical
JSON text of the payload (``json_text``). The payload is no longer built in memory. Rows are read a page
at a time inside one transaction, serialized, hashed and compressed as they come, and the envelope's
keys are written in the sorted order of ``json_text``, so the bytes are the ones the earlier writer
produced and the earlier restore reads the file unchanged. The digest is known only at the end, which
is where the sorted order puts it anyway.

The file is written under a temporary name in the destination folder and moved into place only when it
is complete and on disk; a backup that is interrupted leaves no file under the destination name, and
any partial temporary file left by a killed process ends without its gzip trailer, so restore refuses it.
"""
from __future__ import annotations

import gc
import gzip
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterable, Iterator

from .config import BACKUP_FORMATS
from .sql import clean_row, json_text, quote_name, utc_now

# Rows are read in pages whose size adapts to the size of the rows: a page whose text is larger than
# PAGE_BYTES halves the next page, a page much smaller doubles it, between 1 and PAGE_ROWS rows.
PAGE_ROWS = 1000
PAGE_BYTES = 2 * 1024 * 1024
# Serialized text is handed to the compressor in pieces of about this size.
WRITE_BYTES = 1024 * 1024


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
    """Every row of a type as canonical text, read in record id order a page at a time."""
    limit, last = PAGE_ROWS, None
    while True:
        rows = tx.query(f"SELECT FROM {quote_name(name)}" + (" WHERE @rid > :rid" if last else "")
                        + f" ORDER BY @rid LIMIT {limit}", {"rid": last} if last else {})
        size = 0
        for row in rows:
            text = json_text(clean_row(row))
            size += len(text)
            yield text
        if len(rows) < limit:
            return
        last = rows[-1]["@rid"]
        if not isinstance(last, str) or not last.startswith("#"):
            raise ValueError(f"ArcadeDB returned a row of {name} without a record id")
        # Each HTTP response is held in a reference cycle (httpx binds its stream to the response), so the
        # body of every page would otherwise stay in memory until the next full collection: measured,
        # memory then grew with the number of pages read. Collecting here keeps one page alive at a time.
        del rows
        gc.collect()
        limit =max(1, limit // 2) if size > PAGE_BYTES else min(PAGE_ROWS, limit * 2) if size < PAGE_BYTES // 4 else limit


def streamed_export(tx, collections: Iterable[str], format_version: int) -> dict:
    """The whole-database payload of ``Store._export_snapshot`` with rows read only while it is written.

    Must be written while ``tx`` is open. Only the vector type names are read here.
    """
    payload = {"format_version": format_version, "backend": "arcadedb", "exported_at": utc_now()}
    for collection in collections:
        payload[collection] = Rows(lambda collection=collection: _pages(tx, collection))
    names = {row.get("type_name") for row in tx.query("SELECT type_name FROM vector_indexes")}
    payload["vector_data"] = {name: Rows(lambda name=name: _pages(tx, name))
                              for name in names if isinstance(name, str) and name.startswith("vector_")}
    return payload


def write_backup(destination, payload: dict) -> Path:
    """Write ``payload`` (rows in memory, or ``Rows`` read on the way) as a backup at ``destination``."""
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".cairnmind-backup-", dir=destination.parent)
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
