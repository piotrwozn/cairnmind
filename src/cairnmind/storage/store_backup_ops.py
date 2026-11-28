"""Cache stamp, export, backup, restore, and project deletion operations for Store."""
from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cairnmind.config import BACKUP_FORMATS, BACKUP_SUFFIXES
from cairnmind.storage.backup_reader import opened_backup
from cairnmind.storage.sql import clean_row, json_text, utc_now
from cairnmind.storage.storage_query import (
    asc,
    db_delete_where,
    db_find,
    db_find_one,
    db_update_where,
    eq,
    gt,
    is_in,
    lte,
)
from cairnmind.storage.store_backup import VECTOR_COLLECTIONS, payload_digest, streamed_backup, write_backup
from cairnmind.storage.store_validation import (
    CACHE_STAMP_IGNORED,
    DOCUMENT_TYPES,
    EXPORT_TYPES,
    RESTORE_TYPES,
    V03_DOCUMENT_TYPES,
    V04_DOCUMENT_TYPES,
    V05_DOCUMENT_TYPES,
)


class _StoreBackupMixin:
    """Mixin implementing cache_stamp, export, backup, restore, and forget_project for Store."""

    if TYPE_CHECKING:
        client: Any
        _stamp_lock: Any
        _stamp_cursor: Any
        _stamp_base: int
        _stamp_global: int
        _stamp_seq: dict[Any, Any]
        confirmations_before_rule: Any
        embed_jobs_requeued: Any

        @property
        def backend(self) -> Any: ...

        @backend.setter
        def backend(self, value: Any) -> None: ...

        def transaction(self, commit_timeout: Any = ...) -> AbstractContextManager[Any]: ...

        def find(
            self,
            collection: str,
            where: Any = (),
            *,
            columns: Any = None,
            order_by: Any = (),
            limit: int | None = None,
            distinct: bool = False,
        ) -> list[dict[str, Any]]: ...

        def find_one(
            self,
            collection: str,
            where: Any = (),
            *,
            columns: Any = None,
            order_by: Any = (),
            required: bool = False,
        ) -> dict[str, Any] | None: ...

        @staticmethod
        def _require_project(tx: Any, project_id: Any) -> None: ...

        def _delete_old_vectors(self, tx: Any, source_ids: Any) -> None: ...

    def cache_stamp(self, project_id: str) -> str:
        """A value that changes whenever a committed write could change an answer for the project.

        It is a counter read from the change journal, which every mutation of the store
        advances in its own transaction, so no collection is scanned: an unchanged store
        costs one point read, and otherwise only the events added since the last call are
        read. Writes that cannot affect an answer (jobs, sessions, imports, snapshots) do
        not move it. The value is comparable only within one Store object.
        """
        # Plain reads, never the writer lock: a question must not wait behind a long write
        # transaction of another project. Events are committed together with the journal
        # state, so every event up to the head read first is visible to the queries below.
        with self._stamp_lock:
            state = self.find_one("journal_state", {"id": "current"}, columns=("epoch", "seq", "first_seq"))
            if not state:
                raise ValueError("Journal state is unavailable")
            head = {"epoch": state["epoch"], "seq": state["seq"]}
            known = self._stamp_cursor
            if head != known:
                position = known["seq"] if known and known["epoch"] == head["epoch"] else None
                if position is not None and not state.get("first_seq", 1) - 1 <= position <= head["seq"]:
                    position = None
                seq, shared, complete = dict(self._stamp_seq), self._stamp_global, position is not None
                while complete and position is not None and position < head["seq"]:
                    rows = self.find(
                        "journal_events",
                        [eq("epoch", head["epoch"]), gt("seq", position), lte("seq", head["seq"])],
                        columns=("project_id", "entity_type", "seq"),
                        order_by=(asc("seq"),),
                        limit=1000,
                    )
                    # Sequence numbers are contiguous. A gap means the events were pruned or
                    # the journal was reset while we read.
                    if not rows or [row["seq"] for row in rows] != list(range(position + 1, position + 1 + len(rows))):
                        complete = False
                        break
                    for event in rows:
                        if event["entity_type"] in CACHE_STAMP_IGNORED:
                            continue
                        if event.get("project_id") is None:
                            shared = event["seq"]
                        else:
                            seq[event["project_id"]] = event["seq"]
                    position = rows[-1]["seq"]
                if complete:
                    self._stamp_seq, self._stamp_global = seq, shared
                else:
                    # First call, a new journal epoch, or events no longer available: nothing
                    # is known about the gap, so every project counts as changed.
                    self._stamp_base, self._stamp_global, self._stamp_seq = self._stamp_base + 1, 0, {}
                self._stamp_cursor = head
            parts = [head["epoch"], self._stamp_base, self._stamp_global, self._stamp_seq.get(project_id, 0)]
        return hashlib.sha256(json_text(parts).encode()).hexdigest()

    def export(self, project_id: str | None = None) -> dict[str, Any]:
        from cairnmind.storage.restore_staged import refuse_if_unfinished
        refuse_if_unfinished(self.client)  # part of a backup is not an export of the store (#362)
        with self.transaction() as tx:
            return self._export_snapshot(tx, project_id, EXPORT_TYPES, 5)

    def _export_snapshot(
        self,
        tx: Any,
        project_id: str | None,
        collections: Iterable[str],
        format_version: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"format_version": format_version, "backend": "arcadedb", "exported_at": utc_now()}
        self._require_project(tx, project_id)
        for collection in collections:
            field = "id" if collection == "projects" else "project_id"
            result[collection] = [] if collection in VECTOR_COLLECTIONS or (collection == "ingestion_inbox" and project_id) else [
                clean_row(row) for row in db_find(tx, collection, {field: project_id} if project_id else ())
            ]
        result["vector_data"] = {}  # no vectors in an export, as in a backup (#187)
        return result

    def backup(self, destination: Path | str) -> Path:
        from cairnmind.storage.restore_staged import refuse_if_unfinished
        refuse_if_unfinished(self.client)  # a backup of part of a restore would pass for a whole one (#362)
        destination = Path(destination).expanduser().resolve()
        if destination.is_dir():
            destination /= f"cairnmind-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}{BACKUP_SUFFIXES[0]}"
        # Rows are read, hashed and compressed a page at a time inside one transaction; the file is moved
        # into place only after that transaction commits (#196).
        return streamed_backup(self.transaction, destination, EXPORT_TYPES, 5)

    @staticmethod
    def _write_backup(destination: Path | str, payload: dict[str, Any]) -> Path:
        return write_backup(destination, payload)

    @staticmethod
    def _validated_backup(path: Path | str) -> dict[str, Any]:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
            if not isinstance(envelope, dict) or envelope.get("format") not in BACKUP_FORMATS or envelope.get("version") != 1:
                raise ValueError("This file is not a supported Cairnmind ArcadeDB backup")
            payload = envelope["payload"]
            if payload_digest(payload) != envelope.get("sha256"):
                raise ValueError("Backup checksum mismatch")
            if not isinstance(payload, dict):
                raise ValueError("Backup payload must be an object")
            payload_version = payload.get("format_version")
            if payload_version not in {2, 3, 4, 5} or payload.get("backend") != "arcadedb":
                raise ValueError("Backup schema version is incompatible")
            if payload_version == 2:
                # Validate the original checksum above before any legacy transformation.
                # Legacy plan_id labels and historical evidence stay untouched.
                payload = dict(payload)
                for collection in V03_DOCUMENT_TYPES:
                    payload[collection] = []
            if payload_version in {2, 3}:
                payload = dict(payload)
                for collection in V04_DOCUMENT_TYPES:
                    payload[collection] = []
            if payload_version in {2, 3, 4}:
                payload = dict(payload)
                for collection in V05_DOCUMENT_TYPES:
                    payload[collection] = []
            payload["format_version"] = 5
            for collection in EXPORT_TYPES:
                if not isinstance(payload.get(collection), list) or not all(isinstance(r, dict) for r in payload[collection]):
                    raise ValueError(f"Backup is missing collection {collection}")
            projects = {r["id"] for r in payload["projects"]}
            sources = {r["id"]: r for r in payload["sources"]}
            nodes = {r["id"]: r for r in payload["nodes"]}
            records = {r["id"]: r for r in payload["records"]}
            for collection in EXPORT_TYPES:
                for row in payload[collection]:
                    if collection in V05_DOCUMENT_TYPES:
                        continue  # Tombstones may legitimately refer to removed projects.
                    if row.get("project_id") is not None and row["project_id"] not in projects:
                        raise ValueError("Backup contains an invalid project reference")
            for source in sources.values():
                if hashlib.sha256(source["content"].encode()).hexdigest() != source["sha256"]:
                    raise ValueError("Backup contains a corrupt source snapshot")
            for record in payload["records"]:
                if any(sid not in sources or sources[sid].get("project_id") != record["project_id"] for sid in record["source_ids"]):
                    raise ValueError("Backup contains an invalid evidence reference")
            for edge in payload["edges"]:
                if any(nid not in nodes or nodes[nid]["project_id"] != edge["project_id"] for nid in (edge["from_id"], edge["to_id"])):
                    raise ValueError("Backup contains an invalid graph endpoint")
            for plan in payload["plans"]:
                if not isinstance(plan.get("step_ids"), list) or any(
                    task_id not in records or records[task_id].get("kind") != "task"
                    or records[task_id].get("project_id") != plan["project_id"]
                    for task_id in plan["step_ids"]
                ):
                    raise ValueError("Backup contains invalid managed plan steps")
            for run in payload["check_runs"]:
                record = records.get(run.get("task_id"))
                if not record or record.get("kind") != "task" or record.get("project_id") != run.get("project_id"):
                    raise ValueError("Backup contains an invalid machine check task reference")
                evidence = run.get("source_ids", {})
                if not isinstance(evidence, dict):
                    raise ValueError("Backup contains invalid machine check evidence")
                evidence_ids = [*evidence.values(), run.get("profile_source_id")]
                if any(
                    not isinstance(sid, str) or sid not in sources or sources[sid].get("project_id") != run.get("project_id")
                    for sid in evidence_ids
                ):
                    raise ValueError("Backup contains an invalid machine check source reference")
            if not isinstance(payload.get("vector_data", {}), dict):
                raise ValueError("Backup vector data is invalid")
            return payload
        except (OSError, EOFError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid or corrupt Cairnmind backup") from exc

    def restore(self, backup: Path | str, *, claims: Any = None) -> None:
        """Restore validated data as one outcome: the whole backup, or the store as it was. Stop Cairnmind workers
        before calling. The rows are written in transactions of bounded size, so a failure is undone from the safety
        copy, and a restore that was cut off leaves a mark that refuses the store until a restore finishes (#362).

        With `claims` (restore_claims.BackupClaims) the restore keeps no person's act of the backup: each
        confirmation becomes the backup's claim and accepted record kinds are withdrawn (#214). Without it the
        rows are written as they are; `cairnmind restore` decides by the open workspace's key."""
        # The file is validated in two passes over it before anything changes, and read a third time for
        # the inserts; it is never held in memory whole (#196, backup_reader).
        from cairnmind.storage.restore_staged import staged_restore
        from cairnmind.storage.store_writer import require_writer
        path = Path(backup).expanduser().resolve()
        with opened_backup(path, self._validated_backup, RESTORE_TYPES) as source:
            require_writer(self)  # only the writer restores, and the restored data keeps naming it (#70)
            # The safety copy, the mark, the bounded transactions and, when one fails, the way back (#362).
            staged_restore(self, source, path, claims)
        if claims is not None:
            self.confirmations_before_rule = None  # nothing restored is a person's, so nothing is marked as one
            return
        # A backup of a store before 0.11.0 brings records confirmed before the rule back (#215).
        from cairnmind.memory.confirmation_upgrade import mark_confirmed_before_rule
        self.confirmations_before_rule = mark_confirmed_before_rule(self, force=True)

    def _restore_from(self, source: Any, claims: Any = None) -> None:
        # DDL is deliberately outside the data transaction; no live data has been removed.
        self.backend.ensure_restored_vector_types(source.vector_indexes)
        # Rows are deleted and written in transactions of bounded size, the engine's heap holding a transaction (#362).
        from cairnmind.storage.restore_staged import replace
        self.embed_jobs_requeued = replace(self, source, claims)

    @staticmethod
    def _requeue_embed_jobs(batches: Any) -> int | None:
        """A backup holds no vectors (#187): set each completed embed job of a current chunk back to pending, in the
        restore's transactions, and return how many. None when the backup brought vectors, which stay as they are."""
        tx = batches.tx
        if db_find_one(tx, "embeddings", columns=("id",)):
            return None
        from cairnmind.agents.retrieval import json_value  # retrieval imports this module
        current = {row["id"] for row in db_find(tx, "chunks", {"is_current": True}, columns=("id",))}
        first: dict[Any, Any] = {}  # one job per chunk: a retry can leave two completed jobs of the same chunk
        for row in db_find(tx, "jobs", {"kind": "embed", "state": "completed"}, columns=("id", "payload")):
            chunk = (json_value(row.get("payload"), {}) or {}).get("chunk_id")
            if chunk in current:
                first.setdefault(chunk, row["id"])
        ids = list(first.values())
        for start in range(0, len(ids), 500):
            db_update_where(
                batches.tx,
                "jobs",
                set_fields={"state": "pending", "error": None, "worker": None, "lease_until": None, "updated_at": utc_now()},
                where=is_in("id", ids[start:start + 500]),
            )
            batches.wrote(len(ids[start:start + 500]))
        return len(ids)

    def forget_project(self, project_id: str) -> None:
        from cairnmind.memory.confirmation import ConfirmationRefused
        from cairnmind.terminal.person_proof import FORGET_REFUSED, present
        if not present():
            raise ConfirmationRefused(FORGET_REFUSED)  # only the workspace's route deletes a project (#347)
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            from cairnmind.storage.journal import Journal
            Journal.reset(tx, reason="forget")
            self._delete_old_vectors(
                tx, [r["id"] for r in db_find(tx, "sources", {"project_id": project_id}, columns=("id",))]
            )
            db_delete_where(tx, "edges", {"project_id": project_id})
            db_delete_where(tx, "nodes", {"project_id": project_id})
            for collection in reversed(DOCUMENT_TYPES):
                if collection != "projects":
                    db_delete_where(tx, collection, {"project_id": project_id})
            db_delete_where(tx, "projects", {"id": project_id})
