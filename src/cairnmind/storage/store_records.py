"""Source snapshot, record persistence, task validation, and confirmation rules for Store."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from cairnmind.storage.domain_types import MemoryRecord, SourceRecord
from cairnmind.storage.sql import clean_row, evidence_time, json_text, parse_utc, utc_now
from cairnmind.storage.storage_query import (
    asc,
    db_delete_where,
    db_find,
    db_find_one,
    db_insert_document,
    db_update_where,
    desc,
    eq,
    is_in,
    ne,
    not_in,
)
from cairnmind.storage.store_validation import (
    ADVISORY_DEFINITION_FIELDS,
    CONFLICT_REVIEW_METADATA,
    DERIVED_RECORD_METADATA,
    EXTRACTION_MARK,
    EXTRACTOR_STAMP,
    KINDS,
    MANAGED_DEFINITION_METADATA_FIELDS,
    ORIGIN_METADATA,
    STATUSES,
    KeyTaken,
    RecordRefused,
    degraded_origin,
    normalise_criteria,
    outcome_changed,
    validate_criterion_links,
    validate_extractor_stamp,
    validate_scope,
    validate_task_state,
)


class _StoreRecordsMixin:
    """Mixin implementing source snapshots, record writes, task checks, and confirmations for Store."""

    if TYPE_CHECKING:
        _local: Any
        settings: Any

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

        def current_origin(self) -> Any: ...

        def _proven_act(self) -> Any: ...

        def _kept(self, record: Any) -> bool: ...

        def _keep_verdict(self, record: Any, change: Any, status: Any = None, differs: Any = ()) -> None: ...

        @staticmethod
        def _require_project(tx: Any, project_id: Any) -> None: ...

        @staticmethod
        def _load_checked_sources(tx: Any, project_id: Any, sources: Any) -> dict[str, Any]: ...

        def _delete_old_vectors(self, tx: Any, source_ids: Any) -> None: ...

        def _revision(self, tx: Any, record: Any, *, source_rows: Any = None) -> None: ...

        def _after_commit(self, call: Any) -> None: ...

    def _confirmation(
        self,
        tx: Any,
        project_id: str,
        old: Mapping[str, Any] | None,
        asked: Any,
        status: str,
        statement: Any,
        sources: list[str],
        version: int,
    ) -> tuple[str, dict[str, Any] | None]:
        """The one rule for the status confirmed (#177): (status, mark) as this write stores them.

        A write stores `confirmed` only (a) as a person's act the store checks here, or (b) by keeping a
        confirmation whose statement it does not change (progress on a task, a note of a conflict decision).
        Anything else that asks for `confirmed` is refused. A write that is not a person's act never
        reaches this point on a record a person confirmed (_keep_verdict refuses it first). On a
        confirmation that is not a person's verdict given here (imported, seed data, the checkpoint record)
        a write that does not ask for a status and changes what was confirmed is stored as proposed, with a
        mark that says the confirmation lapsed; the same mark is left when such a write sets another status.
        A person's act leaves no such mark.
        """
        from cairnmind.memory.confirmation import (
            MARK,
            NOT_PROVEN,
            PERSON_ROUTES,
            SEED_ROUTE,
            SEED_SOURCE,
            ConfirmationRefused,
            lowered,
            person_mark,
            said,
        )
        act = self._proven_act()
        if status != "confirmed":
            # A person's act (a dispute, a conflict decision) leaves no such mark and clears an earlier one.
            was = ((old or {}).get("metadata") or {}).get(MARK)
            return status, None if act else lowered(old, status, version) or (
                was if isinstance(was, dict) and was.get("by") == "lapsed" else None)
        if asked == "confirmed" and not act and getattr(self._local, "person_act", None):
            raise ConfirmationRefused(NOT_PROVEN)  # an act is open, but not proven by the workspace's key
        if asked == "confirmed" and act:
            if self.current_origin() or getattr(self._local, "session_write", None):
                raise ConfirmationRefused()
            prefix, origin = SEED_SOURCE if act["route"] == SEED_ROUTE else PERSON_ROUTES[act["route"]]
            source = db_find_one(
                tx,
                "sources",
                {"id": act["source_id"]},
                columns=("id", "project_id", "uri", "metadata", "content"),
            ) if act["source_id"] in sources else None
            if (
                not source
                or source.get("project_id") != project_id
                or not str(source.get("uri", "")).startswith(prefix)
                or (source.get("metadata") or {}).get("origin") != origin
            ):
                raise ConfirmationRefused()
            return status, person_mark(act["route"], source, version, utc_now())
        if old and old["status"] == "confirmed" and statement == said(
            old["kind"], old["content"], old["key"], old.get("supersedes"), old.get("metadata")
        ):
            kept = (old.get("metadata") or {}).get(MARK)
            return status, kept if isinstance(kept, dict) and kept.get("by") != "lapsed" else None
        if asked == "confirmed" or not old or old["status"] != "confirmed":
            raise ConfirmationRefused()
        return "proposed", None if act else lowered(old, "proposed", version)

    def _uuid4_fn(self) -> Any:
        return uuid4

    def _insert_doc(self, tx: Any, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        return db_insert_document(tx, collection, data)

    def _validate_scope(self, scope: Any) -> None:
        validate_scope(scope)

    @staticmethod
    def drop_chunks(tx: Any, source_ids: Iterable[str]) -> None:
        """Delete the chunks of sources that stop being current, in the transaction that replaces them (#186).

        Nothing reads a chunk of a source that is not current: search, vectors and the reparse of a transcript take
        current chunks only, and an import rebuilds chunks from the source text. The source itself, its text and
        its hash stay, so evidence that cites the old version can still be read and checked."""
        ids = list(source_ids)
        if ids:
            db_delete_where(tx, "chunks", is_in("source_id", ids))

    def snapshot(
        self,
        project_id: str,
        uri: str,
        content: str,
        kind: str = "document",
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceRecord:
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        digest, key = hashlib.sha256(content.encode()).hexdigest(), json_text([project_id, uri])
        with self.transaction() as tx:
            self._require_project(tx, project_id)
            existing = db_find_one(tx, "sources", {"current_key": key, "is_current": True})
            if existing and existing["sha256"] == digest:
                return cast(SourceRecord, existing)
            if existing:
                db_update_where(
                    tx, "sources", set_fields={"is_current": False, "current_key": None}, where={"id": existing["id"]}
                )
                self.drop_chunks(tx, [existing["id"]])
                db_update_where(
                    tx,
                    "nodes",
                    set_fields={"is_current": False},
                    where=[
                        eq("source_id", existing["id"]),
                        not_in(
                            "kind",
                            ("source", "decision", "task", "fact", "constraint", "question", "preference", "checkpoint"),
                        ),
                    ],
                )
                db_update_where(
                    tx,
                    "edges",
                    set_fields={"is_current": False},
                    where=[
                        eq("source_id", existing["id"]),
                        not_in("kind", ("supported_by", "depends_on", "supersedes")),
                    ],
                )
                self._delete_old_vectors(tx, [existing["id"]])
            source: SourceRecord = {
                "id": str(self._uuid4_fn()()),
                "project_id": project_id,
                "uri": uri,
                "kind": kind,
                "content": content,
                "sha256": digest,
                "metadata": {**(metadata or {}), **(self.current_origin() or {})},
                "is_current": True,
                "current_key": key,
                "created_at": utc_now(),
            }
            self._insert_doc(tx, "sources", cast(dict[str, Any], source))
            return source

    def snapshot_new(
        self,
        tx: Any,
        project_id: str,
        uri: str,
        content: str,
        kind: str = "document",
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceRecord | None:
        """The source of a URI that has no current one, written inside the caller's transaction.

        Returns None, having written nothing, when the URI has a current source: the caller then
        takes ``snapshot``, which knows how to keep or replace it. The row is the one ``snapshot`` writes.
        """
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        if db_find_one(
            tx, "sources", {"current_key": json_text([project_id, uri]), "is_current": True}, columns=("id",)
        ):
            return None
        return self.new_source(tx, project_id, uri, content, kind, metadata)

    def new_source(
        self,
        tx: Any,
        project_id: str,
        uri: str,
        content: str,
        kind: str = "document",
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceRecord:
        """Insert the current source of a URI whose earlier one, if any, this transaction already deactivated.

        The row is the one ``snapshot`` writes; the unique index on the current key refuses a second current one.
        """
        if not isinstance(content, str) or not isinstance(uri, str) or not uri:
            raise ValueError("Source requires a URI and text content")
        source: SourceRecord = {
            "id": str(self._uuid4_fn()()),
            "project_id": project_id,
            "uri": uri,
            "kind": kind,
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "metadata": {**(metadata or {}), **(self.current_origin() or {})},
            "is_current": True,
            "current_key": json_text([project_id, uri]),
            "created_at": utc_now(),
        }
        self._insert_doc(tx, "sources", cast(dict[str, Any], source))
        return source

    def get_source(self, source_id: str) -> dict[str, Any]:
        row = db_find_one(self, "sources", {"id": source_id}, required=True)
        if row is None:
            raise ValueError(f"Unknown source: {source_id}")
        return row

    @staticmethod
    def _validate_sources(tx: Any, project_id: str, sources: Any) -> None:
        if not isinstance(sources, list) or not sources or not all(isinstance(item, str) for item in sources):
            raise ValueError(
                "A memory record requires at least one source ID: cite a source you read, "
                "or the source_ids a checkpoint returns"
            )
        for source_id in sources:
            source = db_find_one(tx, "sources", {"id": source_id}, columns=("project_id",))
            if not source or source.get("project_id") != project_id:
                raise ValueError("Sources must exist and belong to the same project")

    def _validate_task(
        self,
        tx: Any,
        project_id: str,
        record_id: str,
        metadata: dict[str, Any],
        sources: list[str],
        *,
        assert_completion: bool = True,
        validate_dependencies: bool = True,
    ) -> None:
        for key, value in {
            "state": "todo",
            "depends_on": [],
            "acceptance_criteria": [],
            "requires_test": False,
            "requires_review": False,
        }.items():
            metadata.setdefault(key, value)
        validate_task_state(metadata["state"])
        if not isinstance(metadata["depends_on"], list) or not all(isinstance(x, str) for x in metadata["depends_on"]):
            raise ValueError("Task depends_on must be a list of task IDs")
        criteria = metadata["acceptance_criteria"]
        if not isinstance(criteria, list) or not all(isinstance(x, str) and x.strip() for x in criteria):
            raise ValueError("acceptance_criteria must be a list of nonempty strings")
        for key in ("required_profiles", "check_run_ids"):
            values = metadata.get(key, [])
            if not isinstance(values, list) or not all(isinstance(x, str) and x.strip() for x in values):
                raise ValueError(f"{key} must be a list of nonempty strings")
        validate_criterion_links(metadata, criteria)
        for run_id in metadata.get("check_run_ids", []):
            run = db_find_one(tx, "check_runs", {"id": run_id}, columns=("task_id", "project_id"))
            if not run or run.get("project_id") != project_id or run.get("task_id") != record_id:
                raise ValueError("check_run_ids must reference machine runs for this task and project")
        for flag in ("requires_test", "requires_review", "test_passed", "review_passed", "acceptance_passed"):
            if flag in metadata and not isinstance(metadata[flag], bool):
                raise ValueError(f"{flag} must be a boolean")
        if "step_index" in metadata and (type(metadata["step_index"]) is not int or metadata["step_index"] < 0):
            raise ValueError("step_index must be a nonnegative integer")
        unfinished: list[str] = []
        seen: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id == record_id:
                raise ValueError("Task dependencies must not contain a cycle")
            if task_id in seen:
                return
            seen.add(task_id)
            task = db_find_one(
                tx,
                "records",
                [eq("id", task_id), eq("project_id", project_id), eq("kind", "task"), ne("status", "superseded")],
            )
            if not task:
                raise ValueError("Task dependencies must be active tasks from the same project")
            if task["metadata"].get("state", "todo") != "done":
                unfinished.append(task_id)
            for dependency in task["metadata"].get("depends_on", []):
                visit(dependency)

        if validate_dependencies:
            for dependency in metadata["depends_on"]:
                visit(dependency)
        if metadata["state"] != "done" or not assert_completion:
            return
        if unfinished:
            raise ValueError("Task cannot be done while dependencies are unfinished")
        for needed, flag, field in (
            (bool(criteria), "acceptance_passed", "acceptance_source_ids"),
            (metadata["requires_review"], "review_passed", "review_source_ids"),
        ):
            if needed:
                evidence = metadata.get(field)
                if metadata.get(flag) is not True or not isinstance(evidence, list) or not evidence:
                    raise ValueError(f"Task completion requires {flag}=true and {field}")
                if not all(isinstance(x, str) and x in sources for x in evidence):
                    raise ValueError(f"{field} must reference this record's source IDs")

    def _derive_symbol_anchor(
        self,
        tx: Any,
        project_id: str,
        sources: list[str],
        metadata: Mapping[str, Any],
        *,
        source_rows: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Derive a cross-OS symbol anchor from cited code sources when metadata names a symbol or quote."""
        from cairnmind.memory.symbol_anchor import posix_path, resolve_symbol_anchor
        from cairnmind.scip.parsing import analyze_code
        symbol_name = metadata.get("symbol") if isinstance(metadata.get("symbol"), str) else None
        quote = metadata.get("evidence") if isinstance(metadata.get("evidence"), str) else None
        if not (symbol_name and symbol_name.strip()) and not (quote and quote.strip()):
            return None
        if source_rows is not None:
            code_sources = [
                source_rows[sid]
                for sid in dict.fromkeys(sources)
                if sid in source_rows and source_rows[sid].get("kind") == "code"
            ]
        else:
            code_sources = db_find(
                tx,
                "sources",
                [eq("project_id", project_id), is_in("id", list(dict.fromkeys(sources))), eq("kind", "code")],
                columns=("id", "uri", "kind", "content", "metadata"),
            )
        if not code_sources:
            return None
        by_id = {row["id"]: row for row in code_sources}
        for source_id in sources:
            src = by_id.get(source_id)
            if not src:
                continue
            raw_meta = src.get("metadata")
            src_meta = raw_meta if isinstance(raw_meta, dict) else {}
            rel = src_meta.get("relative_path")
            if not isinstance(rel, str) or not rel.strip():
                raw_path = src_meta.get("path") or src.get("uri") or ""
                raw_root = src_meta.get("root")
                if isinstance(raw_path, str) and isinstance(raw_root, str) and raw_path and raw_root:
                    try:
                        rel = Path(raw_path).relative_to(Path(raw_root)).as_posix()
                    except ValueError:
                        rel = Path(raw_path).name
                else:
                    rel = Path(str(raw_path)).name
            rel = posix_path(rel)
            if not rel:
                continue
            raw_analysis = src_meta.get("analysis")
            analysis = raw_analysis if isinstance(raw_analysis, dict) else None
            has_hashes = bool(
                analysis
                and any(isinstance(sym.get("body_hash"), str) for sym in (analysis.get("symbols") or []))
            )
            if not has_hashes:
                analysis = analyze_code(Path(rel), str(src.get("content") or ""))
            resolved = resolve_symbol_anchor(
                rel,
                str(src.get("content") or ""),
                analysis,
                quote=quote,
                symbol_name=symbol_name,
            )
            if resolved is not None:
                return resolved
        return None

    def _remember(
        self,
        tx: Any,
        project_id: str,
        record: Any,
        sources: list[str],
        *,
        managed_batch: bool = False,
        conflict_review: bool = False,
        replace_origin: bool = False,
        replace_metadata: bool = False,
        mark_only_changes: bool = False,
        extracted_from: str | None = None,
        extractor: Any = None,
        slot: bool = False,
        new_id: str | None = None,
        symbol_anchor: Any = None,
        comment_anchor: Any = None,
        adr_anchor: Any = None,
        trailer_anchor: Any = None,
        quote_ref: Any = None,
    ) -> MemoryRecord:
        """Write one record version.

        new_id: the ID of a new record, chosen by a person's act whose audit names it before the write (#222).

        slot: the write replaces the content of the one checkpoint record, which is a slot for the latest
        state and not a statement: a confirmation of an earlier state lapses instead of refusing the write.

        replace_origin: the origin mark follows this writer instead of the old version (checkpoints).
        replace_metadata: the metadata is built from this write alone, not merged onto the old version's.
        Used when a non-external checkpoint replaces an external one, so no external text stays behind
        unmarked; state that earlier native checkpoints set and neither checkpoint repeats is dropped too.
        mark_only_changes: stamp the current origin only if the stored result differs (plan steps).
        extracted_from: the extraction worker's new record from this one source (EXTRACTION_MARK).
        extractor: that record's extractor stamp (EXTRACTOR_STAMP), only together with extracted_from.
        """
        self._require_project(tx, project_id)
        source_rows = self._load_checked_sources(tx, project_id, sources)
        if not isinstance(record, dict):
            raise RecordRefused("record must be an object")
        old = None
        if record.get("id"):
            old = db_find_one(tx, "records", {"id": record["id"], "project_id": project_id})
            if not old and not managed_batch:
                raise ValueError("Cannot update an unknown record")
            if old and (type(record.get("expected_version")) is not int or record["expected_version"] != old["version"]):
                raise ValueError("Record version conflict; read the current version and retry")
            if not old and record.get("expected_version") is not None:
                raise ValueError("New managed records must not provide expected_version")
            if old and old["status"] == "superseded" and not managed_batch:
                raise ValueError("A superseded record is immutable; create a new record")
        if new_id is not None and (old is not None or record.get("id")):
            raise ValueError("new_id names a new record; it cannot update one")
        # A write that is not a person's act and reaches a record a person confirmed (it can only leave the
        # statement as it is: progress on a task, or nothing) does not make that confirmation worth less: the
        # record keeps every source it had, the note of the person's act among them, and the write only adds.
        kept = old is not None and not slot and self._kept(old)
        if kept and old is not None:
            held = old.get("source_ids") or []
            sources = list(dict.fromkeys([*(json.loads(held) if isinstance(held, str) else held), *sources]))
            extra_sources = [sid for sid in sources if sid not in source_rows]
            for start in range(0, len(extra_sources), 500):
                for row in db_find(tx, "sources", is_in("id", extra_sources[start:start + 500])):
                    cleaned = clean_row(row)
                    source_rows[cleaned["id"]] = cleaned
        kind = record.get("kind", old["kind"] if old else None)
        status = record.get("status", old["status"] if old else "proposed")
        content = record.get("content", old["content"] if old else None)
        if not isinstance(kind, str) or not isinstance(status, str) or kind not in KINDS or status not in STATUSES:
            raise RecordRefused("Unknown record kind or status")
        if old and kind != old["kind"]:
            raise RecordRefused("Record kind cannot change")
        if not isinstance(content, str) or not content.strip():
            raise RecordRefused("Record content must be nonempty text")
        record_id = old["id"] if old else new_id or record.get("id") or str(self._uuid4_fn()())
        key = record.get("key", old["key"] if old else None)
        key = record_id if key is None else key
        if not isinstance(key, str) or not key.strip():
            raise RecordRefused("Record key must be nonempty text")
        metadata = dict(old.get("metadata", {})) if old and not replace_metadata else {}
        updates = record.get("metadata", {})
        if not isinstance(updates, dict):
            raise RecordRefused("Record metadata must be an object")
        if not conflict_review:
            # An agent may mark a possible conflict when it creates a finding, but it can
            # neither unmark one later nor record a decision about it.
            updates = {
                k: v
                for k, v in updates.items()
                if k not in CONFLICT_REVIEW_METADATA and not (old and k == "possible_conflicts")
            }
        metadata.update({k: v for k, v in updates.items() if k not in DERIVED_RECORD_METADATA})
        if quote_ref is not None:
            metadata["quote_ref"] = quote_ref
        elif old and "evidence" in updates and updates["evidence"] != (old.get("metadata") or {}).get("evidence"):
            metadata.pop("quote_ref", None)
        if "depends_on" in metadata and metadata["depends_on"] is None:
            # A model's metadata may say depends_on: null; that is no dependencies, not a failed write (#238).
            del metadata["depends_on"]
        if kind == "task":
            if "acceptance_criteria" in updates:
                # A criteria list that is declared again says everything about its links: what the
                # record held before is dropped unless the caller sends the stored form as well.
                for stored in ("criterion_checks", "check_proposals"):
                    if stored not in updates:
                        metadata.pop(stored, None)
            normalise_criteria(metadata)
            # Checked when it is set, not whenever the task is written: a task that already carries
            # something else under this name stays writable, and is simply never compared.
            if "scope" in updates and updates["scope"] != (old or {}).get("metadata", {}).get("scope"):
                self._validate_scope(updates["scope"])
        evidence = evidence_time(tx, sources, source_rows=source_rows)
        metadata.update(
            evidence_earliest=evidence["earliest"],
            evidence_latest=evidence["latest"],
            evidence_basis=evidence["basis"],
        )
        if symbol_anchor is not None:
            from cairnmind.memory.symbol_anchor import validate_symbol_anchor
            validated_anchor = validate_symbol_anchor(symbol_anchor)
            if validated_anchor is None:
                raise ValueError("Invalid symbol_anchor")
            metadata["symbol_anchor"] = validated_anchor
        elif "symbol_anchor" not in metadata or any(field in updates for field in ("symbol", "evidence")):
            derived_anchor = self._derive_symbol_anchor(tx, project_id, sources, metadata, source_rows=source_rows)
            if derived_anchor is not None:
                metadata["symbol_anchor"] = derived_anchor
        if comment_anchor is not None:
            if not isinstance(comment_anchor, dict) or not comment_anchor.get("tag") or not comment_anchor.get("text_sha256"):
                raise ValueError("Invalid comment_anchor")
            metadata["comment_anchor"] = dict(comment_anchor)
        if adr_anchor is not None:
            if not isinstance(adr_anchor, dict) or not adr_anchor.get("path") or not adr_anchor.get("text_sha256"):
                raise ValueError("Invalid adr_anchor")
            metadata["adr_anchor"] = dict(adr_anchor)
        if trailer_anchor is not None:
            if not isinstance(trailer_anchor, dict) or not trailer_anchor.get("commit") or not trailer_anchor.get("text_sha256"):
                raise ValueError("Invalid trailer_anchor")
            metadata["trailer_anchor"] = dict(trailer_anchor)
        # Set after the client's metadata, which can neither forge nor clear it; kept from the old version,
        # except when a checkpoint replaces the checkpoint record: it replaces its content and its only source,
        # so there the mark follows the writer of the current checkpoint.
        if replace_origin:
            for name in ORIGIN_METADATA:
                metadata.pop(name, None)
        explicit_completion = kind == "task" and metadata.get("state") == "done" and (not old or updates.get("state") == "done")
        if managed_batch and old and old.get("metadata", {}).get("state") == "done" and metadata.get("state") == "done":
            explicit_completion = False  # Internal plan batches preserve progress; they do not assert a new check.
        managed = kind == "task" and bool((old or {}).get("metadata", {}).get("plan_ref") or metadata.get("plan_ref"))
        if kind == "task":
            if any(field in updates for field in ("verification_status", "machine_verified", "verified")):
                raise ValueError("Machine verification status is derived from immutable check runs")
            if metadata.get("plan_ref") is not None and not isinstance(metadata["plan_ref"], str):
                raise ValueError("plan_ref must be a managed plan ID")
            if managed and "worktree" in updates:
                raise ValueError("Managed task worktree is transient; supply record.worktree")
            definition_changed = bool(old) and (
                content != old["content"]
                or key != old["key"]
                or any(metadata.get(k) != old.get("metadata", {}).get(k) for k in MANAGED_DEFINITION_METADATA_FIELDS)
            )
            if managed and not managed_batch:
                if not old or definition_changed or status == "superseded":
                    raise ValueError("Managed task definitions and membership must be changed through PlanManager")
                if type(record.get("expected_plan_version")) is not int or record["expected_plan_version"] < 1:
                    raise ValueError("Managed task updates require expected_plan_version")
            # A proposed command is not part of what a task is: changing one keeps reported evidence.
            proof_changed = bool(old) and (
                content != old["content"]
                or key != old["key"]
                or any(
                    metadata.get(k) != old.get("metadata", {}).get(k)
                    for k in MANAGED_DEFINITION_METADATA_FIELDS - ADVISORY_DEFINITION_FIELDS
                )
            )
            if proof_changed:
                for field in (
                    "acceptance_passed", "acceptance_source_ids", "test_passed",
                    "test_source_ids", "review_passed", "review_source_ids",
                ):
                    if field not in updates:
                        metadata.pop(field, None)
            self._validate_task(
                tx,
                project_id,
                record_id,
                metadata,
                sources,
                assert_completion=explicit_completion,
                validate_dependencies=not managed_batch and status != "superseded",  # retiring needs none
            )
        # Decided on the final metadata, after every field this write drops (criteria links, reported proof).
        origin = self.current_origin()
        if origin and (mark_only_changes or kept) and not outcome_changed(old, content, key, metadata):
            # A plan save rewrites every step; one it leaves as it was keeps its own mark, or none. The same
            # for a record a person confirmed: a write that stores nothing new does not stamp its origin on it.
            origin = None
        metadata.update(origin or {})
        metadata.pop(EXTRACTION_MARK, None)
        if extracted_from is not None:
            if old or list(sources) != [extracted_from]:
                raise ValueError("An extraction mark belongs only to a new record from its one source")
            metadata[EXTRACTION_MARK] = extracted_from
        if extractor is not None:
            # After the client's metadata, which can neither set nor clear it; a later write keeps it.
            if extracted_from is None:
                raise ValueError("An extractor stamp belongs only to the extraction worker's new record")
            metadata[EXTRACTOR_STAMP] = validate_extractor_stamp(extractor)
        supersedes = record.get("supersedes", old.get("supersedes") if old else None)
        if supersedes and (not old or supersedes != old.get("supersedes")):
            previous = db_find_one(tx, "records", {"id": supersedes, "project_id": project_id})
            if not previous or previous["id"] == record_id or previous["status"] == "superseded":
                raise ValueError("supersedes must identify an active record in the same project")
            if previous["kind"] != kind:
                raise ValueError("Replacement and replaced record must have the same kind")
            if previous["kind"] == "task" and previous.get("metadata", {}).get("plan_ref") and not managed_batch:
                raise ValueError("Managed task membership must be changed through PlanManager")
            if not old and "expected_version" in record and record["expected_version"] != previous["version"]:
                raise ValueError("Record version conflict; replaced record has changed")
            from cairnmind.memory.confirmation import MARK as mark_name
            from cairnmind.memory.confirmation import lowered
            self._keep_verdict(previous, "replace")
            act = self._proven_act()
            if not act and previous["status"] == "confirmed":
                # A replacement that is not a person's act takes a confirmed record out of use: it says so.
                previous["metadata"] = {
                    **(previous.get("metadata") or {}),
                    mark_name: lowered(previous, "superseded", previous["version"] + 1),
                }
            if conflict_review and act:
                # A conflict decision judged both findings (#239): the replaced one's last version cites its audit,
                # so that version travels as the person's act and not as a mechanical supersession.
                cited = previous.get("source_ids") or []
                cited = json.loads(cited) if isinstance(cited, str) else cited
                previous["source_ids"] = list(dict.fromkeys([*cited, act["source_id"]]))
            previous.update(status="superseded", active_key=None, version=previous["version"] + 1, updated_at=utc_now())
            db_update_where(tx, "records", set_fields=previous, where={"id": supersedes})
            self._revision(tx, previous)
        # Recomputed on every write of a replacement, so a later source change never
        # leaves a stale warning behind. Warn, never block.
        metadata.pop("supersede_warning", None)
        metadata.pop("supersede_replaced_evidence_latest", None)
        if supersedes:
            replaced = db_find_one(
                tx, "records", {"id": supersedes, "project_id": project_id}, columns=("source_ids",)
            ) or {}
            replaced_sources = replaced.get("source_ids") or []
            if isinstance(replaced_sources, str):
                replaced_sources = json.loads(replaced_sources)
            replaced_time = evidence_time(tx, replaced_sources)
            # Only a stated time is a real evidence time; an observed time is only an
            # upper bound, so comparing against it would flag snapshot order, not age.
            replaced_latest = parse_utc(replaced_time["latest"]) if replaced_time["basis"] == "stated" else None
            latest = parse_utc(metadata["evidence_latest"])
            if replaced_latest and latest and latest < replaced_latest:
                metadata["supersede_warning"] = "anachronistic_supersede"
                metadata["supersede_replaced_evidence_latest"] = replaced_time["latest"]
        # The rule for `confirmed`, on the final statement of this write and for every route (#177).
        from cairnmind.memory.confirmation import MARK, said, said_difference
        statement = said(kind, content, key, supersedes, metadata)
        if old and status != "confirmed":
            self._keep_verdict(old, "status", status)
        elif old and not slot:
            before = said(old["kind"], old["content"], old["key"], old.get("supersedes"), old.get("metadata"))
            if statement != before:
                self._keep_verdict(old, "change", differs=said_difference(before, statement))
        status, confirmation = self._confirmation(
            tx, project_id, old, record.get("status"), status, statement, sources, old["version"] + 1 if old else 1
        )
        metadata.pop(MARK, None)
        if confirmation:
            metadata[MARK] = confirmation
        active_key = json_text([project_id, kind, key]) if status != "superseded" else None
        collision = db_find_one(tx, "records", {"active_key": active_key}, columns=("id",)) if active_key else None
        if collision and collision["id"] != record_id:
            # A record a person confirmed is refused as such: updating it by id would only be refused again.
            self._keep_verdict(db_find_one(tx, "records", {"id": collision["id"]}, required=True), "key")
            raise KeyTaken(collision["id"])
        now = utc_now()
        result: dict[str, Any] = {
            "id": record_id,
            "project_id": project_id,
            "kind": kind,
            "key": key,
            "content": content,
            "status": status,
            "source_ids": list(dict.fromkeys(sources)),
            "version": old["version"] + 1 if old else 1,
            "supersedes": supersedes,
            "metadata": metadata,
            "created_at": old["created_at"] if old else now,
            "updated_at": now,
            "active_key": active_key,
        }
        if explicit_completion and metadata.get("requires_test"):
            from cairnmind.memory.verification import validate_completion
            validate_completion(self, tx, project_id, {**result, "_completion_worktree": record.get("worktree")})
        if old:
            updated = db_update_where(
                tx, "records", set_fields=result, where={"id": record_id, "version": old["version"]}
            )
            if isinstance(updated, list) and (
                not updated
                or (isinstance(updated[0], dict) and type(updated[0].get("count")) is int and updated[0]["count"] != 1)
            ):
                raise ValueError("Record version conflict; read the current version and retry")
        else:
            self._insert_doc(tx, "records", result)
        self._revision(tx, result, source_rows=source_rows)
        if managed and not managed_batch:
            from cairnmind.memory.plans import note_task_mutation
            note_task_mutation(self, tx, result, record["expected_plan_version"])
        if kind == "task" and metadata.get("scope"):
            before = old.get("metadata", {}).get("state", "todo") if old else None
            after = metadata.get("state")
            working = after in ("in_progress", "needs_test", "needs_review", "blocked")
            declared_now = working and not (old and old.get("metadata", {}).get("scope"))
            # A start is entering in_progress, taking a finished task up again, or declaring a scope
            # on a task that is already being worked on. A note that is still open is never replaced.
            change = (
                "start"
                if (after == "in_progress" and before != "in_progress") or (before == "done" and working) or declared_now
                else "finish"
                if after == "done" and before not in (None, "done")
                else None
            )
            if change:
                # A scoped task starts or ends: the tree is noted for the scope-drift warning. Only after
                # the whole write is committed and the writer lock is released, and never as part of it.
                from cairnmind.memory.scope import note_progress
                worktree = record.get("worktree")
                change_kind: str = change
                self._after_commit(lambda: note_progress(self, result, worktree, change_kind))
        return cast(MemoryRecord, result)

    def remember(
        self,
        project_id: str,
        record: Any,
        sources: list[str],
        *,
        conflict_review: bool = False,
        extracted_from: str | None = None,
        extractor: Any = None,
        symbol_anchor: Any = None,
        comment_anchor: Any = None,
        adr_anchor: Any = None,
        trailer_anchor: Any = None,
    ) -> dict[str, Any]:
        with self.transaction() as tx:
            return cast(
                dict[str, Any],
                self._remember(
                    tx,
                    project_id,
                    record,
                    sources,
                    conflict_review=conflict_review,
                    extracted_from=extracted_from,
                    extractor=extractor,
                    symbol_anchor=symbol_anchor,
                    comment_anchor=comment_anchor,
                    adr_anchor=adr_anchor,
                    trailer_anchor=trailer_anchor,
                ),
            )

    def checkpoint(self, project_id: str, state: Any) -> MemoryRecord:
        if not isinstance(state, dict):
            raise ValueError("Checkpoint state must be an object")
        content = json_text(state)
        from cairnmind.memory.secret_mask import (
            note_metadata,  # indexed with its known secrets replaced (#182)
        )
        source = self.snapshot(
            project_id,
            f"checkpoint://{self._uuid4_fn()()}",
            content,
            "checkpoint",
            note_metadata(self.settings, content, {"origin": "client"}),
        )
        with self.transaction() as tx:
            old = db_find_one(
                tx,
                "records",
                [eq("project_id", project_id), eq("kind", "checkpoint"), eq("key", "latest"), ne("status", "superseded")],
            )
            record = {"kind": "checkpoint", "key": "latest", "content": content, "metadata": state}
            if old:
                record.update(id=old["id"], expected_version=old["version"])
            # The record's metadata accumulates the fields of earlier checkpoints. When a writer that is not
            # an external session replaces an external checkpoint, none of the external fields may stay behind
            # unmarked: the metadata is then built from this state alone. Any other succession merges as before.
            fresh = bool(old) and degraded_origin(old.get("metadata")) and not self.current_origin()
            return self._remember(
                tx, project_id, record, [source["id"]], replace_origin=True, replace_metadata=fresh, slot=True
            )

    def list_records(self, project_id: str, include_history: bool = False) -> list[dict[str, Any]]:
        self._require_project(self, project_id)
        conds = [eq("project_id", project_id)]
        if not include_history:
            conds.append(ne("status", "superseded"))
        return [
            clean_row(row)
            for row in self.find("records", conds, order_by=(desc("updated_at"), asc("id")))
        ]

    def record_history(self, record_id: str) -> list[MemoryRecord]:
        return [
            cast(MemoryRecord, row["snapshot"])
            for row in self.find("record_revisions", {"record_id": record_id}, columns=("snapshot",), order_by=(asc("version"),))
        ]
