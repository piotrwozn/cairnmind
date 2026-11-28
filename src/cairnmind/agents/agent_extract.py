"""Grounded extraction pipeline and persistence mixin for KnowledgeAgent."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from copy import deepcopy
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from cairnmind.agents.agent_tools_query import _model_diagnostics, parse_object
from cairnmind.agents.extraction_input import (
    EXTRACTION_SCHEMA,
    EXTRACTION_TOKENS,
    REPAIR_TOKENS,
    extraction_request,
    extractor_stamp,
    repair_prompt,
    source_text,
)
from cairnmind.agents.extraction_input import extraction_schema as _extraction_schema
from cairnmind.agents.ollama import OllamaUnavailable
from cairnmind.agents.retrieval import json_value
from cairnmind.memory.memory_egress import REMOTE_NOT_ALLOWED, held_back_class
from cairnmind.storage.storage_query import any_of, asc, db_find, eq, is_in, is_null, like, not_in
from cairnmind.store import validate_task_state

if TYPE_CHECKING:
    from cairnmind.config import Settings

log = logging.getLogger(__name__)

# One job: the first call and its single repair (#266). On Apple silicon a cut answer (1,200 tokens at 19 a second)
# takes about 63 s, so a cut call and its repair are about 126 s; 180 s leaves 54 s. This is the only bound of a
# job: each call waits as long as the budget has left (read limit), not ask_timeout, so a call that never answers
# ends the job as the budget (code timeout). A stuck model holds the one extraction worker 3 minutes at most.
EXTRACTION_TIMEOUT = 180.0
# The least a call may wait, so that a repair started with almost nothing left fails as the budget, not as a 0-s read.
EXTRACTION_READ_FLOOR = 1.0
# How long the job waits for the model list that gives the chat model's digest for the extractor stamp.
EXTRACTOR_DIGEST_TIMEOUT = 10.0
# Within an extract job the lookup takes at most this share of the time left, and is skipped below the floor.
EXTRACTOR_DIGEST_SHARE = 0.5
EXTRACTOR_DIGEST_FLOOR = 0.05
_RESERVED_EXTRACTION_METADATA = {
    "plan_ref", "plan_context_hash", "step_key", "plan_archived", "worktree", "worktree_root",
    "check_run_ids", "test_passed", "test_source_ids", "review_passed", "review_source_ids",
    "acceptance_passed", "acceptance_source_ids", "verified", "machine_verified", "verification_status",
    "extractor",
}


def _extraction_output(text: str) -> tuple[list[dict] | None, str | None]:
    if not isinstance(text, str):
        return None, "invalid_json"
    try:
        output = parse_object(text)
        json.dumps(output, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None, "invalid_json"
    if not Draft202012Validator(EXTRACTION_SCHEMA).is_valid(output):
        return None, "schema_invalid"
    for record in output["records"]:
        metadata = record.get("metadata", {})
        if record["kind"] == "task" and "state" in metadata:
            try:
                validate_task_state(metadata["state"])
            except ValueError:
                return None, "task_state_invalid"
    return output["records"], None


def _extraction_response(response: dict, *, parser=None) -> tuple[list[dict] | None, str | None]:
    # A syntactically valid prefix is still incomplete when generation hit its
    # token limit. Never parse or commit it as a complete extraction.
    if response.get("done_reason") == "length":
        return None, "output_truncated"
    parse_fn = _extraction_output if parser is None else parser
    records, failure = parse_fn(response["message"].get("content", ""))
    if response["message"].get("tool_calls"):
        return None, "tool_protocol_invalid"
    return records, failure


def _extraction_failure_reason(error: Exception) -> str:
    """Classify a persistence failure without retaining private exception text."""
    from cairnmind.storage.arcade import ArcadeError

    if isinstance(error, ArcadeError):
        return "storage_error"
    if isinstance(error, ValueError):
        message = str(error)
        if message.startswith("Active record key already exists"):
            return "record_key_conflict"
        if message.startswith("Task state must be one of:"):
            return "task_state_invalid"
        if message in {
            "Task depends_on must be a list of task IDs",
            "Task dependencies must be active tasks from the same project",
            "Task dependencies must not contain a cycle",
        }:
            return "task_dependencies_invalid"
        if message in {
            "acceptance_criteria must be a list of nonempty strings",
            "required_profiles must be a list of nonempty strings",
            "requires_test must be a boolean", "requires_review must be a boolean",
            "step_index must be a nonnegative integer",
        }:
            return "task_metadata_invalid"
        return "record_validation_failed"
    return "unexpected_persistence_error"


class _KnowledgeExtractionMixin:
    """Background grounded extraction, stamping, link discovery, and transactional commit."""

    if TYPE_CHECKING:
        store: Any
        settings: Settings
        ollama: Any

        @staticmethod
        def _monotonic() -> float: ...

    @staticmethod
    def _extraction_work_budget() -> float:
        return float(EXTRACTION_TIMEOUT)

    @staticmethod
    def _parse_extraction_response(response: dict) -> tuple[list[dict] | None, str | None]:
        return _extraction_response(response)

    async def extract(self, job: dict) -> None:
        started = self._monotonic()
        work_budget = self._extraction_work_budget()
        deadline = started + work_budget
        diagnostics = {"stage": "extract", "code": "source_invalid", "repair_attempted": False,
                       "repair_outcome": "not_needed", "validation_codes": [], "model_calls": [],
                       "budget": {"timeout_seconds": work_budget},
                       "rejection_counts": {"blank_content": 0, "blank_evidence": 0,
                                            "quote_absent_source": 0, "reserved_metadata": 0},
                       "counts": {"candidate_records": 0, "accepted_records": 0,
                                  "skipped_records": 0, "written_records": 0}}

        def save():
            diagnostics["budget"]["elapsed_seconds"] = round(self._monotonic() - started, 3)
            job_id = job.get("id")
            if isinstance(job_id, str):
                return self.store.set_job_diagnostics(
                    job_id, diagnostics, expected_attempts=job.get("attempts"), worker=job.get("worker")) is not False
            return True

        try:
            async with asyncio.timeout(work_budget):
                payload = json_value(job.get("payload"), {})
                source = self.store.get_source(payload["source_id"])
                project_id = job["project_id"]
                if source["project_id"] != project_id:
                    raise ValueError("Extraction source belongs to another project")
                if held_back_class(self.settings, "chat", source):
                    # Not failed: memory_egress.release_held_extractions queues it once its class is allowed.
                    diagnostics["code"] = REMOTE_NOT_ALLOWED
                    save()
                    return
                accepted = await self.propose(source, diagnostics, deadline)
                if accepted is None and not payload.get("reextract"):
                    save()
                    return
                stamp = await self.extractor_stamp(deadline) if accepted is not None else None
                diagnostics["code"] = "persistence_failed"
                diagnostics["persistence_stage"] = "begin_transaction"
                # All awaits end before the transaction. Cancellation never leaves
                # an executor thread committing after the job has been failed.
                self._extraction_deadline(deadline)
                with self.store.transaction():
                    if not save():
                        diagnostics["code"] = "stale_job"
                        raise ValueError("Extraction job attempt is no longer current")
                    try:
                        current = self.store.get_source(source["id"])
                    except ValueError:
                        diagnostics["code"] = "source_changed"
                        raise
                    # Immutable historical snapshots remain usable, but an
                    # evidence horizon cannot change during this model call.
                    if any(current.get(key) != source.get(key) for key in
                           ("project_id", "sha256", "content", "is_current", "kind")) or source_text(current) != source_text(source):
                        diagnostics["code"] = "source_changed"
                        raise ValueError("Extraction source changed during inference")
                    if accepted is None:
                        # A re-extraction (#297) of a source the extractor now skips: its old untouched proposals go.
                        from cairnmind.agents.extraction_input import EXTRACTOR_VERSION
                        from cairnmind.memory.reextract import supersede_unextracted
                        diagnostics["persistence_stage"] = "supersede_records"
                        gone = supersede_unextracted(self.store, project_id, source, EXTRACTOR_VERSION,
                                                     lambda: self._extraction_deadline(deadline))
                        if gone:
                            diagnostics["counts"]["superseded_records"] = gone
                    else:
                        if stamp is None:
                            raise ValueError("Extractor stamp unavailable")
                        self._commit_extraction(project_id, source, accepted, deadline, diagnostics, stamp)
                    self._extraction_deadline(deadline)
                    diagnostics["persistence_stage"] = "save_diagnostics"
                    diagnostics["code"] = ("source_skipped" if accepted is None else "completed_with_omissions"
                                           if diagnostics["counts"]["skipped_records"] else "ok")
                    if not save():
                        diagnostics["code"] = "stale_job"
                        raise ValueError("Extraction job attempt changed before commit")
                    self._extraction_deadline(deadline)
                    diagnostics["persistence_stage"] = "commit_transaction"
        except asyncio.CancelledError:
            diagnostics.update(code="cancelled", repair_outcome="cancelled" if diagnostics["repair_attempted"] else "not_needed")
            diagnostics["counts"]["written_records"] = 0
            diagnostics["counts"].pop("superseded_records", None)  # rolled back with the rest
            save()
            raise
        except TimeoutError:
            diagnostics.update(code="timeout", repair_outcome="budget_exhausted" if diagnostics["repair_attempted"] else "not_needed")
            diagnostics["counts"]["written_records"] = 0
            diagnostics["counts"].pop("superseded_records", None)  # rolled back with the rest
            save()
            raise
        except OllamaUnavailable:
            diagnostics["code"] = "model_unavailable"
            if diagnostics["repair_attempted"]:
                diagnostics["repair_outcome"] = "unavailable"
            diagnostics["counts"]["written_records"] = 0
            diagnostics["counts"].pop("superseded_records", None)  # rolled back with the rest
            save()
            raise ValueError("Extraction local model was unavailable; no proposals committed") from None
        except Exception as error:
            if diagnostics["code"] in {"persistence_failed", "ok", "completed_with_omissions"}:
                diagnostics["code"] = "persistence_failed"
                diagnostics["failure_reason"] = _extraction_failure_reason(error)
            diagnostics["counts"]["written_records"] = 0
            diagnostics["counts"].pop("superseded_records", None)  # rolled back with the rest
            save()
            raise ValueError("Extraction failed safely (" + diagnostics["code"] + "); no proposals committed") from None

    async def extractor_stamp(self, deadline: float | None = None) -> dict:
        """The ``metadata.extractor`` stamp of what this agent extracts now (#192).

        The model is the chat model's name and the digest its server lists for it. A server that lists
        none (one that is not Ollama), a model list that cannot be read, or a client without the lookup
        gives name@unknown: a missing digest never stops extraction. Cancellation still propagates.
        With the job's ``deadline`` the lookup takes at most half of the time that is left, so a slow
        model list cannot use up the time the commit needs; with too little left it is not made.
        """
        digest = None
        lookup = getattr(self.ollama, "model_digest", None)
        timeout = EXTRACTOR_DIGEST_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, (deadline - self._monotonic()) * EXTRACTOR_DIGEST_SHARE)
        if lookup is not None and timeout >= EXTRACTOR_DIGEST_FLOOR:
            try:
                digest = await asyncio.wait_for(lookup(self.settings.chat_model), timeout)
            except Exception as exc:
                log.debug("Model digest lookup skipped (%s)", type(exc).__name__)
                digest = None
        return extractor_stamp(self.settings.chat_model, digest)

    def _secret_mask(self, source: dict):
        """The ingestion masker's ``mask`` for a source whose view was masked (#182), else None. Raises
        ValueError when the secrets cannot be read, so the job fails and is retried instead of the source
        being skipped for good."""
        metadata = json_value(source.get("metadata"), {})
        if not isinstance(metadata, dict) or not metadata.get("masked"):
            return None
        from cairnmind.memory.secret_mask import ingest_masker
        return ingest_masker(self.settings).mask

    async def _extraction_chat(self, deadline: float, messages: list[dict], **kwargs) -> dict:
        """One extraction call that waits as long as the job's budget has left (#266), not ask_timeout."""
        left = max(EXTRACTION_READ_FLOOR, deadline - self._monotonic())
        try:
            return await self.ollama.chat(messages, tools=None, read_timeout=left, **kwargs)
        except OllamaUnavailable as exc:
            if exc.error_code == "read_timeout":  # the limit is the budget: the budget ended the call
                raise TimeoutError("Extraction call timed out at its time budget") from None
            raise

    async def propose(self, source: dict, diagnostics: dict, deadline: float | None = None) -> list[dict] | None:
        """The grounded records the model proposes for one source, before anything is stored.

        None when the source is not given to extraction at all (diagnostics code source_skipped).
        Raises ValueError, with the diagnostics code set, when the output stays invalid. The
        extraction job and ``cairnmind eval extraction`` both read proposals from here.
        """
        diagnostics["code"] = "secrets_unreadable"
        mask = self._secret_mask(source)
        diagnostics["code"] = "masked_view_changed"
        request = extraction_request(source, mask)
        if request is None:
            diagnostics["code"] = "source_skipped"
            return None
        visible_text, system, prompt = request["text"], request["system"], request["prompt"]
        extraction_schema = _extraction_schema(visible_text, request["segments"])
        diagnostics["code"] = "generation_budget_invalid"
        if type(self.settings.answer_tokens) is not int or self.settings.answer_tokens <= 0:
            raise ValueError("Extraction requires a finite positive integer token budget")
        diagnostics["code"] = "model_unavailable"
        options = {"num_predict": min(EXTRACTION_TOKENS, self.settings.answer_tokens)}
        if deadline is None:  # an evaluation run: each source gets a budget of its own
            deadline = self._monotonic() + self._extraction_work_budget()
        response = await self._extraction_chat(deadline, [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ], format=deepcopy(extraction_schema), options=options)
        diagnostics["model_calls"].append(_model_diagnostics(response, "initial"))
        records, failure = self._parse_extraction_response(response)
        if failure:
            diagnostics["validation_codes"] = [failure]
            diagnostics["repair_attempted"] = True
            diagnostics["repair_outcome"] = "failed"
            repair_options = dict(options)
            if failure == "output_truncated":
                # Only this one repair may grow; wall-clock and context
                # limits remain unchanged and still fail closed.
                repair_options["num_predict"] = min(2 * options["num_predict"], REPAIR_TOKENS)
            response = await self._extraction_chat(deadline, [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(repair_prompt(
                    source["id"], visible_text, failure, response["message"].get("content", "")),
                    ensure_ascii=False)},
            ], format=deepcopy(extraction_schema), options=repair_options)
            diagnostics["model_calls"].append(_model_diagnostics(response, "repair"))
            records, failure = self._parse_extraction_response(response)
            if failure:
                diagnostics["code"] = "invalid_output" if failure in {"task_state_invalid", "output_truncated"} else failure
                diagnostics["validation_codes"] = list(dict.fromkeys([
                    *diagnostics["validation_codes"], failure]))
                raise ValueError("Extraction format remained invalid after one correction")
            diagnostics["repair_outcome"] = "succeeded"
        if records is None:
            raise ValueError("Extraction returned no candidate records")
        diagnostics["counts"]["candidate_records"] = len(records)
        accepted = []
        for item in records:
            metadata = item.get("metadata", {})
            rejected = {"blank_content": not item["content"].strip(),
                        "blank_evidence": not item["evidence"].strip(),
                        "quote_absent_source": item["evidence"] not in visible_text,
                        "reserved_metadata": bool(_RESERVED_EXTRACTION_METADATA.intersection(metadata))}
            if any(rejected.values()):
                diagnostics["counts"]["skipped_records"] += 1
                reasons = [key for key, invalid in rejected.items() if invalid]
                for reason in reasons:
                    diagnostics["rejection_counts"][reason] += 1
                diagnostics["validation_codes"] = list(dict.fromkeys([
                    *diagnostics["validation_codes"], "proposal_rejected", *reasons]))[:8]
                continue
            accepted.append(item)
        diagnostics["counts"]["accepted_records"] = len(accepted)
        return accepted

    @classmethod
    def _extraction_deadline(cls, deadline: float) -> None:
        task = asyncio.current_task()
        if task and task.cancelling():
            raise asyncio.CancelledError
        if cls._monotonic() >= deadline:
            raise TimeoutError("Extraction exceeded its existing time budget")

    def _link_candidates(self, project_id: str, quote: str) -> list[dict]:
        """Bounded file/symbol nodes whose label or file name is named in the quote.

        The parser writes ``file`` nodes labelled by path and ``symbol`` nodes labelled
        by name. Lookup is by identifiers taken from the quote, so a large project
        cannot truncate the candidates before the relevant node is reached.
        """
        tokens = set(re.findall(r"[A-Za-z_][\w./-]*\w", quote))
        for token in list(tokens):
            tokens.update(part for part in re.split(r"[./-]", token) if part)
        names = sorted(token for token in tokens if len(token) >= 4)[:128]
        if not names:
            return []
        # A constant or field is linked only when the quote writes a compound name (with "_" or
        # "."): every module may define a "logger" or "client", and prose uses PROJECT or ROOT
        # as placeholders, but SCHEMA_VERSION or Settings.port names one definition.
        compound = [name for name in names if "_" in name or "." in name]
        symbol_filter = [
            eq("kind", "file"),
            is_null("metadata.kind"),
            not_in("metadata.kind", ("constant", "field")),
        ]
        if compound:
            symbol_filter.append(is_in("label", compound))
        rows = db_find(
            self.store,
            "nodes",
            [
                eq("project_id", project_id),
                eq("is_current", True),
                is_in("kind", ("file", "symbol")),
                is_in("label", names),
                any_of(*symbol_filter),
            ],
            columns=("id", "label"),
            order_by=(asc("label"), asc("id")),
            limit=200,
        )
        seen = {row["id"] for row in rows}
        file_names = sorted({Path(token).name for token in tokens
                             if "." in Path(token).name and len(Path(token).name) >= 4})[:16]
        for name in file_names:
            for row in db_find(
                self.store,
                "nodes",
                [
                    eq("project_id", project_id),
                    eq("is_current", True),
                    eq("kind", "file"),
                    like("label", "%" + name),
                ],
                columns=("id", "label"),
                limit=20,
            ):
                # LIKE treats '_' as a wildcard and ignores separators; confirm the exact name.
                if row["id"] not in seen and PureWindowsPath(row["label"]).name == name:
                    seen.add(row["id"])
                    rows.append(row)
        return rows

    def _commit_extraction(self, project_id: str, source: dict, records: list[dict], deadline: float,
                           diagnostics: dict, stamp: dict) -> None:
        diagnostics["persistence_stage"] = "read_records"
        # A re-extraction replaces the source's untouched proposals of an older extractor version (#194), in this
        # transaction: they neither block a same record (content, key) nor survive it. Nothing is replaced when the
        # extractor proposed nothing, or when this write fails: the transaction then rolls back.
        from cairnmind.memory.reextract import replaceable
        older = replaceable(self.store, project_id, source, stamp["version"]) if records else []
        older_ids = {r["id"] for r in older}
        existing = [r for r in self.store.list_records(project_id) if r["id"] not in older_ids]
        seen = {r["content"].strip().casefold() for r in existing}
        existing_keys = {r["key"]: r for r in existing if r.get("status") != "superseded"}
        for item in records[:6]:
            self._extraction_deadline(deadline)
            if not isinstance(item, dict):
                continue
            quote, content = item.get("evidence"), item.get("content")
            if (not isinstance(quote, str) or not quote.strip() or quote not in source_text(source)
                    or not isinstance(content, str) or not content.strip() or len(content) > 4000):
                continue
            if content.strip().casefold() in seen:
                diagnostics["counts"]["skipped_records"] += 1
                continue
            kind = item.get("kind")
            if kind not in {"decision", "task", "fact", "constraint", "question", "preference"}:
                continue
            item_metadata = deepcopy(item.get("metadata", {}))
            if not isinstance(item_metadata, dict):
                item_metadata = {}
            item_metadata.update({"evidence": quote, "extracted_by": self.settings.chat_model,
                                  "origin": "automatic_extraction"})
            if kind == "task" and item_metadata.get("state") == "done":
                # A sentence claiming completion is not independently verified testing.
                # Preserve the claim, while leaving the task at the required gate.
                if item_metadata.get("requires_test") or item_metadata.get("acceptance_criteria"):
                    item_metadata.update(reported_state="done", state="needs_test")
                elif item_metadata.get("requires_review"):
                    item_metadata.update(reported_state="done", state="needs_review")
                else:
                    item_metadata.update(reported_state="done", state="todo")
            suffix = hashlib.sha256((source["id"] + content).encode()).hexdigest()[:16]
            raw_key = item.get("key")
            key = raw_key.strip()[:200] if isinstance(raw_key, str) and raw_key.strip() else f"auto:{suffix}"
            if key in existing_keys:
                item_metadata["possible_conflicts"] = [existing_keys[key]["id"]]
                item_metadata["suggested_key"] = key
                key = f"{key[:170]}~{suffix}"
            candidate = {
                "kind": kind, "content": content, "key": key,
                "status": "proposed", "metadata": item_metadata,
            }
            # The holder of the key first: superseding another old proposal that says the same would leave the key taken.
            same = [r for r in older if r["id"] in older_ids and r["kind"] == kind]
            twin = next((r for r in same if r["key"] == key), None) or next(
                (r for r in same if r["content"].strip().casefold() == content.strip().casefold()), None)
            if twin:
                candidate["supersedes"] = twin["id"]  # one write: the old proposal is superseded by this record
                older_ids.discard(twin["id"])
            diagnostics["persistence_stage"] = "write_record"
            try:
                record = self.store.remember(project_id, candidate, [source["id"]], extracted_from=source["id"],
                                             extractor=stamp)
            except ValueError as exc:
                # The key is taken, by a record a person confirmed (VerdictKept "key") or by another one.
                if "Active record key already exists" not in str(exc) and getattr(exc, "change", None) != "key":
                    raise
                # A foreground client may have inserted the key after our snapshot.
                latest = self.store.list_records(project_id)
                if any(r["content"].strip().casefold() == content.strip().casefold() for r in latest):
                    diagnostics["counts"]["skipped_records"] += 1
                    continue
                conflicts = [r["id"] for r in latest if r["key"] == key]
                candidate.pop("supersedes", None)  # the failed write already superseded the twin in this transaction
                candidate["metadata"]["possible_conflicts"] = conflicts
                candidate["key"] = f"{key[:150]}~{suffix}"
                record = self.store.remember(project_id, candidate, [source["id"]], extracted_from=source["id"],
                                             extractor=stamp)
            existing_keys[record["key"]] = record
            seen.add(content.strip().casefold())
            diagnostics["counts"]["written_records"] += 1
            # Evidence-backed semantic links complement parser-derived code dependencies.
            node_id = f"record:{record['id']}"
            # Store.remember owns canonical vertices and revision/task metadata.
            # Query bounded candidates in ArcadeDB; never scan the whole graph in Python.
            diagnostics["persistence_stage"] = "read_graph"
            candidates = self._link_candidates(project_id, quote)
            for candidate in candidates:
                self._extraction_deadline(deadline)
                label = candidate["label"]
                names = {label, PureWindowsPath(label).name}
                if any(len(name) >= 4 and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", quote)
                       for name in names):
                    diagnostics["persistence_stage"] = "write_graph"
                    link_id = hashlib.sha256(f"{node_id}:relates_to:{candidate['id']}".encode()).hexdigest()
                    self.store.upsert_edge({
                        "id": link_id, "project_id": project_id, "from_id": node_id, "to_id": candidate["id"],
                        "kind": "relates_to", "source_id": source["id"], "status": "proposed",
                    })
        diagnostics["persistence_stage"] = "supersede_records"
        if diagnostics["counts"]["written_records"]:  # answers that wrote nothing erase nothing
            for old in older:  # no new record says the same: superseded all the same, and kept in its history
                if old["id"] in older_ids:
                    self._extraction_deadline(deadline)
                    self.store.remember(project_id, {"id": old["id"], "expected_version": old["version"],
                                                     "status": "superseded"}, old["source_ids"])
                    older_ids.discard(old["id"])
        if len(older) > len(older_ids):
            diagnostics["counts"]["superseded_records"] = len(older) - len(older_ids)
