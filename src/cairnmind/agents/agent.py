"""Bounded local knowledge agent, with source-checked citations and no shell."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from typing import Any

from cairnmind.agents.agent_extract import (
    _RESERVED_EXTRACTION_METADATA as _RESERVED_EXTRACTION_METADATA,
)
from cairnmind.agents.agent_extract import (
    EXTRACTION_READ_FLOOR as EXTRACTION_READ_FLOOR,
)
from cairnmind.agents.agent_extract import (
    EXTRACTOR_DIGEST_FLOOR as EXTRACTOR_DIGEST_FLOOR,
)
from cairnmind.agents.agent_extract import (
    EXTRACTOR_DIGEST_SHARE as EXTRACTOR_DIGEST_SHARE,
)
from cairnmind.agents.agent_extract import (
    EXTRACTOR_DIGEST_TIMEOUT as EXTRACTOR_DIGEST_TIMEOUT,
)
from cairnmind.agents.agent_extract import (
    _extraction_failure_reason as _extraction_failure_reason,
)
from cairnmind.agents.agent_extract import (
    _extraction_output,
    _KnowledgeExtractionMixin,
)
from cairnmind.agents.agent_extract import (
    _extraction_response as _extraction_response_impl,
)
from cairnmind.agents.agent_extract import (
    _extraction_schema as _extraction_schema,
)
from cairnmind.agents.agent_tools_query import (
    _LATER as _LATER,
)
from cairnmind.agents.agent_tools_query import (
    CITATIONS_LEFT_OUT as CITATIONS_LEFT_OUT,
)
from cairnmind.agents.agent_tools_query import (
    EXCERPT_CHARS,
    INITIAL_HITS,
    NAMES_PENDING,
    NAMES_TIMEOUT,
    SECOND_LOOK_CODES,
    SYSTEM,
    TOOLS,
    TYPED_ANSWER_INSTRUCTION,
    WHOLE_CATALOG_CHARS,
    WHOLE_PASSAGE_CHARS,
    WHOLE_RESERVE_CHARS,
    _KnowledgeQueryMixin,
    _model_diagnostics,
    _show,
)
from cairnmind.agents.agent_tools_query import (
    LATER_RANKS as LATER_RANKS,
)
from cairnmind.agents.agent_tools_query import (
    MIN_QUOTE_CHARS as MIN_QUOTE_CHARS,
)
from cairnmind.agents.agent_tools_query import (
    REPAIR_CATALOG_CHARS as REPAIR_CATALOG_CHARS,
)
from cairnmind.agents.agent_tools_query import (
    _json_leaves as _json_leaves,
)
from cairnmind.agents.agent_tools_query import (
    parse_object as parse_object,
)
from cairnmind.agents.agent_tools_query import (
    question_excerpt as question_excerpt,
)
from cairnmind.agents.agent_tools_query import (
    quote_coverage as quote_coverage,
)
from cairnmind.agents.agent_tools_query import (
    tool as tool,
)
from cairnmind.agents.agent_validate import _KnowledgeValidationMixin
from cairnmind.agents.answer_contract import (
    answer_contract_errors as answer_contract_errors,
)
from cairnmind.agents.answer_contract import (
    prepare_answer_schema,
)
from cairnmind.agents.citation_match import (
    locate_quote as locate_quote,
)
from cairnmind.agents.citation_match import (
    quote_weight as quote_weight,
)
from cairnmind.agents.extraction_input import (
    EXTRACTION_SCHEMA as EXTRACTION_SCHEMA,
)
from cairnmind.agents.extraction_input import (
    EXTRACTION_TOKENS as EXTRACTION_TOKENS,
)
from cairnmind.agents.extraction_input import (
    REPAIR_TOKENS as REPAIR_TOKENS,
)
from cairnmind.agents.extraction_input import (
    extraction_request as extraction_request,
)
from cairnmind.agents.extraction_input import (
    extractor_stamp as extractor_stamp,
)
from cairnmind.agents.extraction_input import (
    repair_prompt as repair_prompt,
)
from cairnmind.agents.extraction_input import (
    source_text,
)
from cairnmind.agents.ollama import OllamaUnavailable
from cairnmind.agents.retrieval import Retriever
from cairnmind.config import Settings
from cairnmind.memory.memory_egress import (
    ASK,
    HELD_BACK_UNKNOWN,
    HOSTED_ASK_UNKNOWN,
    ask_gate,
)
from cairnmind.memory.memory_egress import (
    HELD_BACK_SOURCE as HELD_BACK_SOURCE,
)
from cairnmind.memory.memory_egress import (
    REMOTE_NOT_ALLOWED as REMOTE_NOT_ALLOWED,
)
from cairnmind.memory.memory_egress import (
    held_back_class as held_back_class,
)
from cairnmind.memory.memory_models import answer_label, context_tokens, from_settings
from cairnmind.scip.definitions import (
    MAX_DEFINITION_ATTEMPTS as MAX_DEFINITION_ATTEMPTS,
)
from cairnmind.scip.definitions import (
    MAX_DEFINITIONS as MAX_DEFINITIONS,
)
from cairnmind.scip.definitions import (
    definition_lines as definition_lines,
)
from cairnmind.scip.definitions import (
    find_definitions,
)
from cairnmind.store import validate_task_state as validate_task_state

log = logging.getLogger(__name__)

# One job: the first call and its single repair (#266). On Apple silicon a cut answer (1,200 tokens at 19 a second)
# takes about 63 s, so a cut call and its repair are about 126 s; 180 s leaves 54 s. This is the only bound of a
# job: each call waits as long as the budget has left (read limit), not ask_timeout, so a call that never answers
# ends the job as the budget (code timeout). A stuck model holds the one extraction worker 3 minutes at most.
EXTRACTION_TIMEOUT = 180.0
SECOND_LOOK_MIN_SECONDS = 5.0


def extraction_work_budget() -> float:
    """Bound the initial call and optional single repair as one operation."""
    return float(EXTRACTION_TIMEOUT)


def extraction_outer_timeout() -> float:
    """Let extraction record its own timeout before the caller watchdog fires."""
    return extraction_work_budget() + 5.0


def inference_lease_seconds() -> float:
    """Retain the original 90-second headroom and the 120-second lease floor."""
    return max(120.0, extraction_work_budget() + 90.0)


def _extraction_response(response: dict) -> tuple[list[dict] | None, str | None]:
    return _extraction_response_impl(response, parser=_extraction_output)


class KnowledgeAgent(_KnowledgeQueryMixin, _KnowledgeValidationMixin, _KnowledgeExtractionMixin):
    def __init__(self, store, settings: Settings, ollama=None, retriever=None):
        self.store = store
        self.settings = settings
        self.ollama = ollama or from_settings(settings)
        self.retriever = retriever or Retriever(store, settings, self.ollama)

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def _find_definitions(store: Any, retriever: Any, project_id: str, question: str,
                          texts: list[str], worktree: Any) -> list[dict]:
        return find_definitions(store, retriever, project_id, question, texts, worktree)

    @staticmethod
    def _extraction_work_budget() -> float:
        return extraction_work_budget()

    @staticmethod
    def _parse_extraction_response(response: dict) -> tuple[list[dict] | None, str | None]:
        return _extraction_response(response)

    async def ask(self, project_id: str, question: str, worktree=None,
                  answer_schema: dict | None = None, also_held=frozenset(),
                  *, verifier: str = "llm", nli_scorer=None) -> dict:
        if verifier not in ("llm", "nli"):
            raise ValueError(f"Unsupported verifier {verifier!r}; choose 'llm' or 'nli'.")
        diagnostics = {"stage": "ask", "repair_attempted": False, "repair_outcome": "not_needed",
                       "validation_codes": [], "model_calls": [], "verifier": verifier,
                       "budget": {"timeout_seconds": self.settings.ask_timeout, "max_steps": self.settings.max_steps,
                                  "context_limit_chars": max(12000, context_tokens(self.settings) * 2)}}
        started = time.monotonic()
        # For a chat server on another computer, memory of a class it may not receive is left out (#181).
        # The gate holds for both passes: the second look reads through the same checks.
        gate = ask_gate(self.settings, self.store, also_held)
        token = ASK.set(gate)
        try:
            result = await self._ask_impl(project_id, question, worktree, answer_schema, diagnostics,
                                          verifier=verifier, nli_scorer=nli_scorer)
            in_part = diagnostics.pop("_hits_in_part", 0)
            now = time.monotonic()
            left = self.settings.ask_timeout - (now - started)
            # The second look makes the calls the first pass made from its last discovery call on (that
            # call, a repair, the support check), over more text: with less time left than those took it
            # cannot finish, and would only hold the model (#231). Earlier tool steps are not counted.
            final = now - diagnostics.pop("_last_call_at", now)
            if (in_part and answer_schema is None and left > max(SECOND_LOOK_MIN_SECONDS, final)
                    and result.get("status") == "partial"
                    and result.get("diagnostics", {}).get("code") in SECOND_LOOK_CODES):
                # The model found no answer in the excerpts. A hit shows 900 characters of a chunk of up
                # to 6000, chosen by the question's words, and the line that answers often has none of
                # them: one more pass reads the best chunks whole. Only a complete answer replaces the
                # first result; anything else leaves it as it was.
                again = {**diagnostics, "model_calls": [], "validation_codes": [], "repair_attempted": False,
                         "repair_outcome": "not_needed"}
                second = await self._ask_impl(project_id, question, worktree, answer_schema, again,
                                              whole=True, timeout=left, verifier=verifier, nli_scorer=nli_scorer)
                again.pop("_hits_in_part", None)
                again.pop("_last_call_at", None)
                if second.get("status") == "complete":
                    diagnostics.update(again, second_look="answered")
                    result = {**second, "elapsed_seconds": round(time.monotonic() - started, 3)}
                else:
                    diagnostics["second_look"] = "no_answer"
        finally:
            ASK.reset(token)
        held = gate.summary() if gate else None
        if held:
            diagnostics["held_back"] = held
            if also_held:  # a hosted run is told in every answer, a complete one in its warnings (#183)
                if result.get("status") != "complete":
                    result = {**result, "unknowns": [HOSTED_ASK_UNKNOWN, *result.get("unknowns", [])][:8]}
                else:
                    fresh = result.get("freshness") or {}
                    result = {**result, "freshness": {**fresh, "warnings": [*fresh.get("warnings", []),
                                                                            HOSTED_ASK_UNKNOWN][-8:]}}
            elif result.get("status") != "complete":  # the answer may lack sources for that reason
                result = {**result, "unknowns": [HELD_BACK_UNKNOWN, *result.get("unknowns", [])][:8]}
        diagnostics["code"] = result.get("diagnostics", {}).get("code", "unavailable")
        label = await answer_label(self.settings, self.ollama)  # None with the release's models
        if label:
            diagnostics["model_evaluation"] = label
        diagnostics["budget"]["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return {**result, "diagnostics": diagnostics}

    async def _ask_impl(self, project_id: str, question: str, worktree, answer_schema: dict | None,
                        diagnostics: dict, *, whole: bool = False, timeout: float | None = None,  # noqa: ASYNC109
                        verifier: str = "llm", nli_scorer=None) -> dict:
        """One pass over the question. ``whole`` is the second look (see ``ask``): the evidence is
        then the whole chunks of the first hits that the first pass showed in part, no definitions."""
        answer_schema = prepare_answer_schema(answer_schema)
        project_id = self.store.resolve_project(project_id)["id"]
        if not isinstance(question, str) or not question.strip() or len(question) > 16000:
            raise ValueError("Question must contain 1–16000 characters")
        evidence: dict = {}
        repair_fallback = None
        judged = None  # a validated complete answer whose support check is running
        started = time.monotonic()
        self.retriever.warnings.clear()
        try:
            async with asyncio.timeout(self.settings.ask_timeout if timeout is None else timeout):
                # Preserve lexical evidence even when a cold/missing embedding model times out.
                chosen = await asyncio.to_thread(self.retriever.search, project_id, question, INITIAL_HITS,
                                                 worktree=worktree)
                initial = await asyncio.to_thread(self._hits, chosen, project_id, evidence, worktree, question,
                                                  False)
                hybrid = await self.retriever.asearch(project_id, question, INITIAL_HITS, worktree=worktree)
                if hybrid:
                    chosen = hybrid
                    initial = await asyncio.to_thread(self._hits, chosen, project_id, evidence, worktree, question,
                                                      False)
                # Chunks longer than their excerpt, each stretch of a file once (chunks overlap):
                # what a second look can still show.
                in_part, taken = [], []
                for hit in chosen:
                    first, last = hit.get("line_start"), hit.get("line_end")
                    if (len(str(hit.get("text", ""))) <= EXCERPT_CHARS or not isinstance(first, int)
                            or not isinstance(last, int)):
                        continue
                    if any(source == hit["source_id"] and first <= end and begin <= last
                           for source, begin, end in taken):
                        continue
                    taken.append((hit["source_id"], first, last))
                    in_part.append(hit)
                diagnostics["_hits_in_part"] = len(in_part)
                context = await asyncio.to_thread(self.retriever.context, project_id, worktree)
                system = {"role": "system", "content": SYSTEM + (TYPED_ANSWER_INSTRUCTION if answer_schema else "")}
                # We retain complete tool exchanges, but stop before the local context is flooded.
                context_chars = max(12000, context_tokens(self.settings) * 2)
                if whole:
                    room = (context_chars - len(json.dumps(system, ensure_ascii=False))
                            - len(json.dumps(context, ensure_ascii=False)) - WHOLE_RESERVE_CHARS)
                    initial = []
                    for item in await asyncio.to_thread(self._hits, in_part, project_id, evidence, worktree,
                                                        question, False, WHOLE_PASSAGE_CHARS):
                        room -= len(json.dumps(json.dumps(item, ensure_ascii=False), ensure_ascii=False))
                        if room < 0:
                            break  # in the order of the hits: the best chunks that fit
                        initial.append(item)
                    if not initial:
                        return self._result("partial", "There are not enough sources for a confirmed answer.",
                                            evidence, ["No whole passage fits the context budget."], started)
                else:
                    # Definitions of names the question and the hits use take the places of the
                    # lowest-ranked hits, so the evidence stays INITIAL_HITS excerpts within the guard.
                    # They are shown first: the line that states a value, before the passages that
                    # use or mention it, and so also first in the short catalog of this call.
                    definitions = await asyncio.to_thread(self._definitions, project_id, question, initial,
                                                          evidence, worktree)
                    if definitions:
                        initial = [*definitions, *initial[:INITIAL_HITS - len(definitions)]]
                # Only now is it known which hits the model gets: register those, in their order.
                for position, item in enumerate(initial):
                    source = evidence.get(item["source_id"])
                    if source is not None and isinstance(item.get("text"), str) and item["text"] in source_text(source):
                        _show(source, item["text"], position, item.get("id"))
                prompt = {"project": project_id, "question": question,
                          "project_state": context, "initial_evidence": initial,
                          "citation_catalog": self._citation_catalog(
                              project_id, evidence, worktree=worktree,
                              budget=WHOLE_CATALOG_CHARS if whole else 4000, excerpt_chars=600)}
                if answer_schema is not None:
                    prompt["answer_schema"] = answer_schema
                messages: list[dict[str, Any]] = [system, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]
                for _step in range(self.settings.max_steps):
                    if sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) > context_chars:
                        return self._result("partial", "The analysis reached the context budget; narrow the question.",
                                            evidence, ["The budget for read sources was exceeded."], started,
                                            code="context_budget")
                    # The second look fills the context with whole chunks: a search on top of them
                    # would end in the context guard, so that pass answers from what it is shown.
                    diagnostics["_last_call_at"] = time.monotonic()
                    response = await self.ollama.chat(messages, tools=None if whole else TOOLS)
                    diagnostics["model_calls"] = [*diagnostics["model_calls"][-1:],
                                                  _model_diagnostics(response, "discovery")]
                    message = response["message"]
                    calls = message.get("tool_calls", [])
                    if not calls:
                        candidate = str(message.get("content", ""))
                        validated, repairable = await asyncio.to_thread(
                            self._validate_final, candidate, project_id, evidence, started, worktree, answer_schema,
                        )
                        if not repairable:
                            judged = validated
                            return await self._support_gate(project_id, question, validated, evidence, started, diagnostics,
                                                            plain=answer_schema is None, verifier=verifier,
                                                            nli_scorer=nli_scorer)
                        repair_fallback = validated
                        diagnostics["validation_codes"] = [validated["diagnostics"]["code"]]
                        if _step + 1 >= self.settings.max_steps:
                            diagnostics["repair_outcome"] = "budget_exhausted"
                            return self._repair_fallback(validated, started,
                                "No steps were left to correct the answer's format or citations.")
                        catalog = self._citation_catalog(project_id, evidence, candidate, worktree=worktree)
                        if not catalog:
                            diagnostics["repair_outcome"] = "missing_evidence"
                            return validated
                        compact = self._compact_repair(question, context, candidate, validated.get("unknowns", []),
                                                       catalog, answer_schema, context_chars)
                        if compact is None:
                            diagnostics["repair_outcome"] = "budget_exhausted"
                            return self._repair_fallback(validated, started,
                                "No context budget was left to correct the answer's format or citations.")
                        repair_messages, schema = compact
                        # Structured generation is tool-free; discovery keeps its native tool protocol.
                        diagnostics["repair_attempted"] = True
                        repaired = await self.ollama.chat(repair_messages, tools=None, format=schema)
                        diagnostics["model_calls"] = [*diagnostics["model_calls"][-1:],
                                                      _model_diagnostics(repaired, "repair")]
                        repaired_message = repaired["message"]
                        if repaired_message.get("tool_calls"):
                            diagnostics["repair_outcome"] = "failed"
                            return self._repair_fallback(validated, started,
                                "The model returned tool calls instead of a corrected answer.")
                        repaired_text = self._carry_verified_citations(
                            candidate, str(repaired_message.get("content", "")), evidence)
                        result, still_repairable = await asyncio.to_thread(
                            functools.partial(self._validate_final, drop_invalid=True), repaired_text,
                            project_id, evidence, started, worktree, answer_schema)
                        if result["diagnostics"].get("citations_dropped"):
                            diagnostics["citations_dropped"] = result["diagnostics"]["citations_dropped"]
                        diagnostics["repair_outcome"] = ("failed" if still_repairable or
                            result["diagnostics"]["code"] == "source_stale" else "succeeded")
                        judged = result
                        return await self._support_gate(project_id, question, result, evidence, started, diagnostics,
                                                        plain=answer_schema is None, verifier=verifier,
                                                        nli_scorer=nli_scorer)
                    if not isinstance(calls, list) or len(calls) > 6:
                        return self._result("partial", "The model returned an invalid list of tool calls.", evidence,
                                            ["The model broke the tool protocol."], started,
                                            code="tool_protocol_invalid")
                    messages.append({"role": "assistant", "content": message.get("content", ""), "tool_calls": calls})
                    for call in calls:
                        function = call.get("function", {}) if isinstance(call, dict) else {}
                        if not isinstance(function, dict):
                            function = {}
                        name, arguments = function.get("name", ""), function.get("arguments", {})
                        try:
                            if isinstance(arguments, str):
                                arguments = json.loads(arguments)
                            if not isinstance(arguments, dict):
                                raise ValueError("Tool arguments must be an object")
                            result = await self._tool(name, arguments, project_id, evidence, worktree)
                        except (ValueError, KeyError, TypeError, OSError) as exc:
                            result = {"error": str(exc)[:250]}
                        messages.append({"role": "tool", "tool_name": name,
                                         "content": json.dumps(result, ensure_ascii=False)})
                return self._result("partial", "The analysis reached its step limit; narrow the question.", evidence,
                                    ["The agent's step limit was exceeded."], started, code="step_budget")
        except TimeoutError:
            if judged is not None and judged.get("status") == "complete":
                # The answer and its quotes were verified; only the support or names check ran out of time.
                # Not complete, as when the check gives no verdict; the repair outcome stands.
                names = diagnostics.get("support_check") == NAMES_PENDING  # the judge had accepted it
                diagnostics["support_check"] = "names_failed" if names else "failed"
                return self._result("partial", "The model's answer could not be verified in the sources.", evidence,
                                    [*judged.get("unknowns", []),
                                     NAMES_TIMEOUT if names else
                                     "The support check of the answer did not finish within the analysis time limit."],
                                    started, judged["sources"], code="support_unverified")
            diagnostics["repair_outcome"] = "budget_exhausted" if diagnostics["repair_attempted"] else "not_needed"
            if repair_fallback is not None:
                return self._repair_fallback(repair_fallback, started,
                    "Correcting the format or citations did not finish within the analysis time limit.")
            return self._result("partial" if evidence else "unavailable", "The local analysis ran out of time.",
                                evidence, ["The local model did not finish within the time limit."], started,
                                code="timeout")
        except OllamaUnavailable as exc:
            if diagnostics["repair_attempted"]:
                diagnostics["repair_outcome"] = "unavailable"
            if repair_fallback is not None:
                return self._repair_fallback(repair_fallback, started,
                    "The local model was unavailable while correcting the format or citations.")
            return self._result("unavailable", "The local knowledge model is unavailable.", evidence,
                                [str(exc)], started, code="model_unavailable")
