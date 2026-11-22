"""Which sensitivity classes may leave for a memory model server on another computer (#181).

A role (chat or embedding) is remote when its server is not loopback (memory_models.is_loopback): only
`cairnmind models set --allow-remote` saves such a role. A remote role receives text of the classes
listed in the setting memory_remote_classes (default: project only; see sensitivity.py). This is
enforced at the three places where source text leaves for a memory model:

- extraction (KnowledgeAgent.extract): a source of a held class is left unextracted with diagnostics
  code remote_not_allowed. The job is not failed; release_held_extractions queues it again once its
  class is allowed, so nothing has to be imported again;
- embedding (Retriever.embed_pending, service.schedule_embeddings): its chunks are left unembedded and
  stay findable by text search; the next repair embeds them once allowed;
- the context `ask` builds (agent.py, retrieval.py while ASK holds a Gate): its chunks, records, plans
  and sources are left out, and the answer's diagnostics count them (held_back).

With the local Ollama of the settings nothing changes. Not gated here: the question itself.

The same setting holds classes back from the built-in agent when its model is hosted (coding_held_classes,
#183): its read_source and memory_records (and what its remember and checkpoint hand back), the quotes of the ask answers it gets and the record previews of
its resume packet. Codex, Claude Code and external MCP clients are not gated: ask answers and resume packets
go to their own provider (docs/EXTERNAL_CLIENTS.md).
"""
from __future__ import annotations

from contextvars import ContextVar
import json
from urllib.parse import urlsplit

from .memory_models import is_loopback, profile
from .sensitivity import CLASSES, earlier_source_ids, rollup, source_rows, source_sensitivity
from .sql import clean_row, update_document, utc_now

REMOTE_NOT_ALLOWED = "remote_not_allowed"
HELD_BACK_UNKNOWN = ("Some project memory was not sent to the remote model server because of its sensitivity "
                     "class (memory_remote_classes).")
HELD_BACK_SOURCE = ("This source is held back from the remote model server by its sensitivity class "
                    "(memory_remote_classes).")
INVALID_SETTING = ("memory_remote_classes must be a list of distinct classes from: project, conversation, "
                   "third_party. Until it is, nothing is sent to a remote model server.")


def allowed_classes(settings) -> tuple[frozenset, str | None]:
    """The classes a remote role may receive, and an error when the setting is invalid (then none)."""
    value = getattr(settings, "memory_remote_classes", None)
    if (not isinstance(value, list) or not all(isinstance(item, str) and item in CLASSES for item in value)
            or len(set(value)) != len(value)):
        return frozenset(), INVALID_SETTING
    return frozenset(value), None


def is_remote(settings, role: str) -> bool:
    """Whether the role's server is on another computer. An invalid profile counts as remote (fail closed)."""
    try:
        row = profile(settings, role) or {"base_url": settings.ollama_url}
    except ValueError:
        return True
    return not is_loopback(row["base_url"])


def held_classes(settings, role: str) -> frozenset:
    """The classes the role must not receive: none for a server on this computer."""
    if not is_remote(settings, role):
        return frozenset()
    return frozenset(CLASSES) - allowed_classes(settings)[0]


def ordered(classes) -> list[str]:
    return [name for name in CLASSES if name in classes]


HOSTED_READ_REFUSED = ("This memory is of the class {name}, which the setting memory_remote_classes does not allow "
                       "for a hosted model. Nothing was read. Ask the person to add {name} to that setting if you "
                       "need it.")
HOSTED_RECORDS_HELD = ("Records of classes the setting memory_remote_classes does not allow for a hosted model are "
                       "listed without their content: they are marked held_back.")
HOSTED_ASK_UNKNOWN = ("Passages of some sensitivity classes were left out of this answer, because the setting "
                      "memory_remote_classes does not allow them for a hosted model; diagnostics.held_back counts them.")
HOSTED_PACKET_NOTE = ("Record previews of some sensitivity classes are left out, because the setting "
                      "memory_remote_classes does not allow them for a hosted model; held_back counts them.")


def coding_held_classes(settings, client: str, profile) -> frozenset:
    """The classes a built-in agent run must not receive (#183): none from a model server on this computer;
    from a hosted one (every Copilot connection, every API profile whose address is not loopback, or not
    readable) all but the allowed. Codex, Claude Code and external MCP clients are never asked."""
    if client != "copilot":
        try:
            if is_loopback(profile["base_url"]):
                return frozenset()
        except (KeyError, TypeError, AttributeError, ValueError):
            pass  # an address that cannot be read is not the local one
    return frozenset(CLASSES) - allowed_classes(settings)[0]


def coding_read(name: str, result, held: frozenset, session_id: str) -> dict:
    """What a hosted run is given of a read_source or memory_records result: for a held source the refusal
    naming its class, for held records the listing without their content. A source this run's own session
    archived stays readable. A result whose class cannot be read counts as held (fail closed)."""
    def refused(class_name):
        return {"ok": False, "error_code": "held_back_by_class", "class": class_name,
                "message": HOSTED_READ_REFUSED.format(name=class_name)}
    result = result if isinstance(result, dict) else {}
    if name == "read_source":
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        found = result.get("sensitivity")
        if (found in CLASSES and found not in held) or (
                source.get("kind") == "agent_transcript" and metadata.get("session_id") == session_id):
            return result
        return refused(found if found in CLASSES else "conversation")
    items = result.get("items")
    if not isinstance(items, list):
        return refused("conversation")
    shown, count = [], 0
    for item in items:
        item = item if isinstance(item, dict) else {}
        if item.get("sensitivity") in CLASSES and item["sensitivity"] not in held:
            shown.append(item)
        else:
            count += 1
            shown.append({key: item[key] for key in ("id", "kind", "status", "version", "sensitivity")
                          if key in item} | {"held_back": True})
    if not count:
        return result
    return {**result, "items": shown,
            "held_back": {"classes": ordered(held), "records": count, "message": HOSTED_RECORDS_HELD}}


def coding_written(result) -> dict:
    """What a hosted run is given back of a remember or checkpoint result: the record without its content,
    metadata and key (a key is often text written from a conversation, and coding_read hides it for a held
    record too). An update that sends no content keeps the old version's, which may be of a held class."""
    return {key: value for key, value in result.items() if key not in ("content", "metadata", "key", "active_key")} \
        if isinstance(result, dict) else {}


def held_back_class(settings, role: str, source) -> str | None:
    """The class of ``source`` when the role must not receive it, else None."""
    held = held_classes(settings, role)
    if not held:
        return None
    name = source_sensitivity(source)
    return name if name in held else None


def describe(settings, role: str) -> dict:
    """What doctor and `models set` say about one role: the classes it receives and those held back."""
    held = held_classes(settings, role)
    entry = {"sends": ordered(set(CLASSES) - held), "held_back": ordered(held)}
    if is_remote(settings, role):
        try:
            row = profile(settings, role) or {"base_url": settings.ollama_url}
            host = urlsplit(row["base_url"]).hostname
        except ValueError:
            host = "the configured server"
        text = (f"Memory text of the classes {', '.join(entry['sends'])}, and questions, are sent to {host} for {role}."
                if entry["sends"] else f"Questions are sent to {host} for {role}; no memory text is.")
        if held:
            text += (f" Text of the classes {', '.join(entry['held_back'])} stays on this computer: "
                     + ("extraction leaves it pending." if role == "chat" else
                        "it is not embedded and is found by text search only."))
            if role == "chat":
                text += " Answers leave it out of their context and say so."
        entry["content_sent_to"] = text
    return entry


def doctor_block(settings) -> dict:
    """The setting as doctor shows it, with the reason when it is invalid."""
    allowed, error = allowed_classes(settings)
    block = {"setting": ordered(allowed) if error is None else settings.memory_remote_classes}
    if error:
        block["error"] = error
    return block


class Gate:
    """The classes one answer must not send, and what it left out because of them (counted once each)."""

    def __init__(self, store, held: frozenset):
        self.store, self.held = store, held
        self._classes: dict[str, str] = {}
        self._earlier: dict[str, set[str]] = {}  # source ids earlier versions of a record cited
        self.chunks: set[str] = set()
        self.records: set[str] = set()
        self.sources: set[str] = set()

    def _class_of(self, source_id) -> str:
        if source_id not in self._classes:
            rows = source_rows(self.store, [source_id])
            self._classes[source_id] = source_sensitivity(rows[source_id]) if source_id in rows else "conversation"
        return self._classes[source_id]

    def _read_earlier(self, records) -> None:
        missing = [str(record.get("id")) for record in records if str(record.get("id")) not in self._earlier]
        if missing:
            found = earlier_source_ids(self.store, missing)
            self._earlier.update({i: found.get(i, set()) for i in missing})

    def prime(self, records) -> None:
        """Read the classes of every source behind ``records``, now or before, in bounded queries."""
        records = list(records)
        self._read_earlier(records)
        wanted = {i for record in records if isinstance(record.get("source_ids"), list)
                  for i in record["source_ids"] if isinstance(i, str) and i not in self._classes}
        wanted |= {i for record in records for i in self._earlier[str(record.get("id"))] if i not in self._classes}
        rows = source_rows(self.store, wanted)
        self._classes.update({i: source_sensitivity(rows[i]) if i in rows else "conversation" for i in wanted})

    def source_id_held(self, source_id) -> bool:
        """For a graph node that would name a source."""
        if self._class_of(source_id) in self.held:
            self.sources.add(str(source_id))
            return True
        return False

    def chunk_held(self, chunk_id, source) -> bool:
        if source_sensitivity(source) in self.held:
            self.chunks.add(str(chunk_id))
            return True
        return False

    def source_held(self, source) -> bool:
        """For a source about to be read into the prompt."""
        if source_sensitivity(source) in self.held:
            self.sources.add(str(source.get("id")))
            return True
        return False

    def record_held(self, record) -> bool:
        """For a record (or a revision, or a plan) about to go into the prompt."""
        self._read_earlier([record])
        if rollup(record, self._class_of, self._earlier[str(record.get("id"))]) in self.held:
            self.records.add(str(record.get("id")))
            return True
        return False

    def summary(self) -> dict | None:
        if not (self.chunks or self.records or self.sources):
            return None
        return {"classes": ordered(self.held), "chunks": len(self.chunks), "records": len(self.records),
                "sources": len(self.sources)}


# Set by KnowledgeAgent.ask for the length of one answer; asyncio.to_thread carries it into threads.
ASK: ContextVar[Gate | None] = ContextVar("cairnmind_ask_gate", default=None)


def ask_gate(settings, store, also_held=frozenset()) -> Gate | None:
    """``also_held``: classes the asking hosted run must not receive (#183); they only add to the chat role's."""
    held = held_classes(settings, "chat") | frozenset(also_held)
    return Gate(store, held) if held else None


def held_chunk_ids(store, settings, rows, known: dict | None = None) -> set[str]:
    """Of chunk rows (id), those the embedding role must not receive.

    ``known``: a dict the caller keeps between calls (the service's repair loop). A held chunk stays
    missing and comes back on every repair; its class cannot change (a chunk keeps its source, a
    source its kind and mark), so it is looked up once per set of held classes, not on every repair.
    """
    held = held_classes(settings, "embedding")
    ids = [row["id"] for row in rows]
    if not held or not ids:
        return set()
    if known is not None:
        seen = known.setdefault(held, set())
        new = held_chunk_ids(store, settings, [row for row in rows if row["id"] not in seen])
        seen |= new
        return {i for i in ids if i in seen}
    owners: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        for row in store.query("SELECT id,source_id FROM chunks WHERE id IN :ids", {"ids": ids[start:start + 500]}):
            owners[row["id"]] = row["source_id"]
    sources = source_rows(store, owners.values())
    return {chunk for chunk, source in owners.items()
            if source not in sources or source_sensitivity(sources[source]) in held}


def release_held_extractions(store, settings) -> int:
    """Queue again the extraction jobs held back for a class the chat role may now receive."""
    held = held_classes(settings, "chat")
    rows = [clean_row(row) for row in store.query(
        "SELECT FROM jobs WHERE kind='extract' AND state='completed' AND diagnostics.code=:code",
        {"code": REMOTE_NOT_ALLOWED})]
    released = 0
    for job in rows:
        payload = job.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        source_id = payload.get("source_id") if isinstance(payload, dict) else None
        if held:
            sources = source_rows(store, [source_id])
            if source_id not in sources or source_sensitivity(sources[source_id]) in held:
                continue
        with store.transaction() as tx:
            update_document(tx, "jobs", {"state": "pending", "error": None, "lease_until": None, "worker": None,
                                         "updated_at": utc_now(),
                                         "attempt_history": store._job_event(job, "remote_allowed")},
                            "id=:id AND state='completed'", {"id": job["id"]})
        released += 1
    return released
