"""Restore reads a whole-database backup in pieces instead of loading it (#196).

The file stays envelope 1, one JSON document: ``{"format":...,"payload":{...},"sha256":...,"version":1}``
whose payload holds one array of rows per collection. Every writer, the 0.10.0 one and the stream, writes
it in the canonical layout of ``json_text``: payload keys in sorted order. The reader walks that document
with the standard library's decoder, one value (a row, at most) at a time, and reads the file several
times, never the whole of it at once:

1. Check: every row is serialized again to compute the envelope checksum exactly as ``payload_digest``
   does on a loaded payload. Only a bounded index is kept: project ids, and for sources, graph nodes and
   records their id with the few fields the cross references need (and whether a source's content hash
   was right).
2. Validate: the rows that refer to other rows are checked against that index.

Only after both passes succeed does restore change anything; it then reads the file again for its inserts.
Every check, its order and its message are those of ``Store._validated_backup``, which still loads a file
that is valid JSON in a layout the walk does not follow (keys out of order or repeated, a document that is
not an object): such a file was not written by any version. Each later pass also compares the digest of
the bytes read with the first pass, so a file that changes during a restore is refused before the commit.
"""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import sys
from typing import Callable, Iterator

from .config import BACKUP_FORMATS
from .sql import json_text

READ_CHARS = 1 << 16
_DECODER = json.JSONDecoder()
_SPACE = " \t\n\r"
_MISSING = object()
_MISMATCH = object()
_ABSENT = object()  # no payload key in the envelope
_OBJECT = object()  # the payload is an object, read member by member
CORRUPT = "Invalid or corrupt Cairnmind backup"
# A file that was read whole and valid once, and differs when it is read again (#238).
CHANGED = ("The backup file changed while it was being read for the restore; nothing was changed. "
           "Check the file or copy it again, then restore it again")
# What the in-memory reader turns into the message above; the stream reader wraps the same kinds.
WRAPPED = (OSError, EOFError, KeyError, TypeError, json.JSONDecodeError)


class Unusual(Exception):
    """Valid JSON whose layout the walk does not follow; the in-memory reader takes it over."""


class _Text:
    """Decoded text of the file with a window that holds the value being read and little more."""

    def __init__(self, stream):
        self._stream, self.buf, self.pos = stream, "", 0

    def _more(self) -> bool:
        # The window grows geometrically while one large value is read, so reading it stays linear.
        data = self._stream.read(max(READ_CHARS, len(self.buf) - self.pos))
        if not data:
            return False
        self.buf, self.pos = self.buf[self.pos:] + data, 0
        return True

    def peek(self) -> str:
        """The next character that is not JSON whitespace, or "" at the end of the file."""
        while True:
            buf, pos = self.buf, self.pos
            while pos < len(buf) and buf[pos] in _SPACE:
                pos += 1
            self.pos = pos
            if pos < len(buf):
                return buf[pos]
            if not self._more():
                return ""

    def take(self, char: str) -> None:
        if self.peek() != char:
            raise json.JSONDecodeError(f"Expecting {char!r}", self.buf, self.pos)
        self.pos += 1

    def value(self):
        if not self.peek():
            raise json.JSONDecodeError("Expecting value", self.buf, self.pos)
        while True:
            try:
                value, end = _DECODER.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if self._more():
                    continue
                raise
            # A number at the very end of the window may go on in the next piece of the file.
            if end == len(self.buf) and self._more():
                continue
            self.pos = end
            return value


def _members(text: _Text, ordered: bool) -> Iterator[str]:
    """Keys of an object; the caller reads each value before asking for the next key."""
    text.take("{")
    if text.peek() == "}":
        text.pos += 1
        return
    seen, last = set(), None
    while True:
        if text.peek() != '"':
            raise json.JSONDecodeError("Expecting property name enclosed in double quotes", text.buf, text.pos)
        key = text.value()
        text.take(":")
        if key in seen or (ordered and last is not None and key < last):
            raise Unusual(key)
        seen.add(key)
        last = key
        yield key
        separator = text.peek()
        if separator == "}":
            text.pos += 1
            return
        if separator != ",":
            raise json.JSONDecodeError("Expecting ',' delimiter", text.buf, text.pos)
        text.pos += 1


def _elements(text: _Text) -> Iterator:
    text.take("[")
    if text.peek() == "]":
        text.pos += 1
        return
    while True:
        yield text.value()
        separator = text.peek()
        if separator == "]":
            text.pos += 1
            return
        if separator != ",":
            raise json.JSONDecodeError("Expecting ',' delimiter", text.buf, text.pos)
        text.pos += 1


class _Canonical:
    """The envelope checksum of the payload: sha256 of its ``json_text``, in the pieces of ``payload_chunks``."""

    def __init__(self):
        self.sha, self.error = hashlib.sha256(), None

    def text(self, piece: str) -> None:
        if self.error is None:
            try:
                self.sha.update(piece.encode())
            except UnicodeEncodeError as exc:  # a lone surrogate in a key: raised in the old order
                self.error = exc

    def value(self, prefix: str, value) -> None:
        if self.error is None:
            try:
                self.sha.update((prefix + json_text(value)).encode())
            except (ValueError, TypeError, RecursionError) as exc:
                self.error = exc  # raised where the in-memory reader computes the checksum


class _Ignored:
    def text(self, piece):
        pass

    def value(self, prefix, value):
        pass


def _walk(text: _Text, canonical, collections: frozenset) -> Iterator[tuple]:
    """Events ``(kind, key, name, value)`` of an envelope; arrays of the known collections and of the
    vector types come row by row, everything else as whole values."""
    first = text.peek()
    if first != "{":
        if first and first in '["-0123456789tfnNI':
            raise Unusual("not an object")  # valid JSON perhaps, but no envelope: the in-memory reader says so
        raise json.JSONDecodeError("Expecting value", text.buf, text.pos)
    for key in _members(text, ordered=False):
        if key != "payload":
            yield "envelope", key, None, text.value()
        elif text.peek() != "{":
            value = text.value()
            canonical.value("", value)
            yield "payload", None, None, value
        else:
            yield "payload", None, None, _OBJECT
            canonical.text("{")
            for index, member in enumerate(_members(text, ordered=True)):
                canonical.text(("," if index else "") + json_text(member) + ":")
                if member in collections and text.peek() == "[":
                    yield from _rows(text, canonical, member, None)
                elif member == "vector_data" and text.peek() == "{":
                    yield "object", member, None, None
                    canonical.text("{")
                    for position, name in enumerate(_members(text, ordered=True)):
                        canonical.text(("," if position else "") + json_text(name) + ":")
                        if text.peek() == "[":
                            yield from _rows(text, canonical, member, name)
                        else:
                            value = text.value()
                            canonical.value("", value)
                            yield "value", member, name, value
                    canonical.text("}")
                else:
                    value = text.value()
                    canonical.value("", value)
                    yield "value", member, None, value
            canonical.text("}")
    if text.peek():
        raise json.JSONDecodeError("Extra data", text.buf, text.pos)


def _rows(text, canonical, key, name):
    yield "array", key, name, None
    canonical.text("[")
    for index, row in enumerate(_elements(text)):
        canonical.value("," if index else "", row)
        yield "row", key, name, row
    canonical.text("]")


class _Hashed:
    """The backup file as gzip reads it, with a digest of every byte read."""

    mode = "rb"

    def __init__(self, handle):
        self._handle, self.sha = handle, hashlib.sha256()

    def read(self, size=-1):
        data = self._handle.read(size)
        self.sha.update(data)
        return data


def _share(value):
    return sys.intern(value) if type(value) is str else value


class _Index:
    """What the first pass keeps: about one dict entry per project, source, node and record id."""

    def __init__(self, types):
        self.types = types
        self.envelope, self.payload, self.values, self.arrays = {}, _ABSENT, {}, {}
        self.projects, self.sources, self.nodes, self.records = set(), {}, {}, {}
        self.errors = {}  # the first error building each of the four, raised in the in-memory order
        self.vector_indexes, self._pairs = [], {}

    def _pair(self, first, second):
        # Rows of one project share one tuple. Equal values are interchangeable here: the checks only
        # compare them with ==.
        pair = (first, second)
        try:
            return self._pairs.setdefault(pair, pair)
        except TypeError:
            return pair  # a value that is not hashable, such as a list where a project id should be

    def add(self, kind, key, name, value):
        if kind == "envelope":
            self.envelope[key] = value
        elif kind == "payload":
            self.payload = value
        elif kind == "value" and name is None:
            self.values[key] = value
        elif kind == "array" and name is None:
            self.arrays[key] = True
        elif kind == "row" and name is None:
            if not isinstance(value, dict):
                self.arrays[key] = False
            elif key in {"projects", "sources", "nodes", "records"} and key not in self.errors:
                try:
                    self._index(key, value)
                except (KeyError, TypeError) as exc:
                    self.errors[key] = exc
            if key == "vector_indexes":
                self.vector_indexes.append(value)

    def _index(self, key, row):
        if key == "projects":
            self.projects.add(row["id"])
        elif key == "sources":
            try:
                result = _MISMATCH if hashlib.sha256(row["content"].encode()).hexdigest() != row["sha256"] else None
            except Exception as exc:  # noqa: BLE001 - kept and raised where the in-memory reader would raise it
                result = exc
            self.sources[row["id"]] = self._pair(_share(row.get("project_id")), result)
        elif key == "nodes":
            self.nodes[row["id"]] = _share(row.get("project_id", _MISSING))
        else:
            self.records[row["id"]] = self._pair(row.get("kind") == "task", _share(row.get("project_id")))


class StreamedBackup:
    """A backup validated by two passes over its file; ``rows`` reads it again for the inserts."""

    def __init__(self, handle, collections, types):
        self._handle, self._collections, self._types = handle, frozenset(collections), types
        index, canonical = _Index(types), _Canonical()
        self._digest = None
        for event in self._pass(canonical):
            index.add(*event)
        self._digest = self._last_digest
        self.skipped = _check_index(index, canonical)
        failures = {}
        try:
            for kind, key, name, row in self._pass():
                if kind == "row" and name is None and key in types["export"] and key not in self.skipped:
                    _check_row(index, failures, key, row, types)
        except (*WRAPPED, Unusual) as exc:
            # The first pass read the whole file: one the second cannot read has changed since.
            raise ValueError(CHANGED) from exc
        _raise_first(index, failures, types)
        self.vector_indexes = index.vector_indexes

    @property
    def digest(self) -> str:
        """The SHA-256 of the file's bytes, as the first pass read them."""
        return self._digest

    def _pass(self, canonical=None) -> Iterator[tuple]:
        self._handle.seek(0)
        raw = _Hashed(self._handle)
        with gzip.GzipFile(fileobj=raw, mode="rb") as compressed:
            text = io.TextIOWrapper(compressed, encoding="utf-8")
            yield from _walk(_Text(text), canonical or _Ignored(), self._collections)
        while raw.read(1 << 20):
            pass
        self._last_digest = raw.sha.hexdigest()
        if self._digest is not None and self._last_digest != self._digest:
            raise ValueError(CHANGED)

    def rows(self, collections, vectors=False) -> Iterator[tuple]:
        """``("row", collection, row)`` in file order, then with ``vectors`` ``("vector", type, row)``, a row
        of ``None`` first for each vector type; a file that changed since it was validated raises at the end."""
        wanted = set(collections) - self.skipped
        try:
            for kind, key, name, value in self._pass():
                if name is None:
                    if kind == "row" and key in wanted:
                        yield "row", key, value
                elif vectors and key == "vector_data" and kind in {"array", "row", "value"}:
                    yield "vector", name, value if kind == "row" else None
                    if kind == "value":
                        for row in value:
                            yield "vector", name, row
        except (OSError, EOFError, json.JSONDecodeError, Unusual) as exc:
            # This file was validated whole before: one that cannot be read now has changed since.
            raise ValueError(CHANGED) from exc


class LoadedBackup:
    """A backup the in-memory reader loaded and validated, read in the order restore always used."""

    digest = None  # none: a copy this version wrote is never read this way, and None equals no digest (fails closed)

    def __init__(self, payload):
        self._payload, self.vector_indexes = payload, payload["vector_indexes"]

    def rows(self, collections, vectors=False) -> Iterator[tuple]:
        for collection in collections:
            for row in self._payload[collection]:
                yield "row", collection, row
        if vectors:
            for name, rows in self._payload.get("vector_data", {}).items():
                yield "vector", name, None
                for row in rows:
                    yield "vector", name, row


def _check_index(index: _Index, canonical: _Canonical) -> frozenset:
    """The checks of the in-memory reader up to its cross references; returns the collections a payload
    of an earlier format replaces with empty ones."""
    types, envelope = index.types, index.envelope
    if envelope.get("format") not in BACKUP_FORMATS or envelope.get("version") != 1:
        raise ValueError("This file is not a supported Cairnmind ArcadeDB backup")
    if index.payload is _ABSENT:
        raise KeyError("payload")
    if canonical.error is not None:
        raise canonical.error
    if canonical.sha.hexdigest() != envelope.get("sha256"):
        raise ValueError("Backup checksum mismatch")
    if index.payload is not _OBJECT:
        raise ValueError("Backup payload must be an object")
    version = index.values.get("format_version")
    if version not in {2, 3, 4, 5} or index.values.get("backend") != "arcadedb":
        raise ValueError("Backup schema version is incompatible")
    skipped = set()
    if version == 2:
        skipped.update(types["v03"])
    if version in {2, 3}:
        skipped.update(types["v04"])
    if version in {2, 3, 4}:
        skipped.update(types["v05"])
    for collection in types["export"]:
        if collection not in skipped and index.arrays.get(collection) is not True:
            raise ValueError(f"Backup is missing collection {collection}")
    for name in ("projects", "sources", "nodes", "records"):
        if name in index.errors:
            raise index.errors[name]
    return frozenset(skipped)


def _check_row(index, failures, key, row, types):
    def check(category, test):
        if category not in failures:
            try:
                test()
            except Exception as exc:  # noqa: BLE001 - the first failure of each kind, raised in the in-memory order
                failures[category] = exc

    if key not in types["v05"]:
        check(("project", key), lambda: _project(index, row))
    if key == "records":
        check("evidence", lambda: _evidence(index, row))
    elif key == "edges":
        check("edges", lambda: _edge(index, row))
    elif key == "plans":
        check("plans", lambda: _plan(index, row))
    elif key == "check_runs":
        check("check_runs", lambda: _check_run(index, row))


def _project(index, row):
    if row.get("project_id") is not None and row["project_id"] not in index.projects:
        raise ValueError("Backup contains an invalid project reference")


def _evidence(index, record):
    sources = index.sources
    if any(sid not in sources or sources[sid][0] != record["project_id"] for sid in record["source_ids"]):
        raise ValueError("Backup contains an invalid evidence reference")


def _edge(index, edge):
    nodes = index.nodes

    def project(nid):
        value = nodes[nid]
        if value is _MISSING:
            raise KeyError("project_id")
        return value

    if any(nid not in nodes or project(nid) != edge["project_id"] for nid in (edge["from_id"], edge["to_id"])):
        raise ValueError("Backup contains an invalid graph endpoint")


def _plan(index, plan):
    records = index.records
    if not isinstance(plan.get("step_ids"), list) or any(
        task_id not in records or not records[task_id][0] or records[task_id][1] != plan["project_id"]
        for task_id in plan["step_ids"]
    ):
        raise ValueError("Backup contains invalid managed plan steps")


def _check_run(index, run):
    record = index.records.get(run.get("task_id"))
    if not record or not record[0] or record[1] != run.get("project_id"):
        raise ValueError("Backup contains an invalid machine check task reference")
    evidence = run.get("source_ids", {})
    if not isinstance(evidence, dict):
        raise ValueError("Backup contains invalid machine check evidence")
    sources = index.sources
    if any(not isinstance(sid, str) or sid not in sources or sources[sid][0] != run.get("project_id")
           for sid in [*evidence.values(), run.get("profile_source_id")]):
        raise ValueError("Backup contains an invalid machine check source reference")


def _raise_first(index, failures, types):
    for collection in types["export"]:
        if ("project", collection) in failures:
            raise failures[("project", collection)]
    for _, result in index.sources.values():
        if result is _MISMATCH:
            raise ValueError("Backup contains a corrupt source snapshot")
        if result is not None:
            raise result
    for category in ("evidence", "edges", "plans", "check_runs"):
        if category in failures:
            raise failures[category]
    if "vector_data" in index.values and not isinstance(index.values["vector_data"], dict):
        raise ValueError("Backup vector data is invalid")


@contextmanager
def opened_backup(path, load_whole: Callable, types: dict):
    """The validated backup at ``path``, read in pieces; a layout no writer produced goes to ``load_whole``.

    Everything is checked before this yields, so nothing in the store has changed when it refuses a file.
    """
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise ValueError(CORRUPT) from exc
    with handle:
        try:
            source = StreamedBackup(handle, types["export"], types)
        except Unusual:
            source = LoadedBackup(load_whole(path))
        except WRAPPED as exc:
            raise ValueError(CORRUPT) from exc
        yield source
