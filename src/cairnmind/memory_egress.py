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

With the local Ollama of the settings nothing changes. Not gated here: the question itself, and what
coding clients and the built-in agent read from memory and send to their own providers (#183).
"""
from __future__ import annotations

from contextvars import ContextVar
import json
from urllib.parse import urlsplit

from .memory_models import is_loopback, profile
from .sensitivity import CLASSES, rollup, source_rows, source_sensitivity
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
        self.chunks: set[str] = set()
        self.records: set[str] = set()
        self.sources: set[str] = set()

    def _class_of(self, source_id) -> str:
        if source_id not in self._classes:
            rows = source_rows(self.store, [source_id])
            self._classes[source_id] = source_sensitivity(rows[source_id]) if source_id in rows else "conversation"
        return self._classes[source_id]

    def prime(self, records) -> None:
        """Read the classes of every source behind ``records`` in bounded queries, before checking each."""
        wanted = {i for record in records if isinstance(record.get("source_ids"), list)
                  for i in record["source_ids"] if isinstance(i, str) and i not in self._classes}
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
        if rollup(record, self._class_of) in self.held:
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


def ask_gate(settings, store) -> Gate | None:
    held = held_classes(settings, "chat")
    return Gate(store, held) if held else None


def held_chunk_ids(store, settings, rows) -> set[str]:
    """Of chunk rows (id), those the embedding role must not receive."""
    held = held_classes(settings, "embedding")
    ids = [row["id"] for row in rows]
    if not held or not ids:
        return set()
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
