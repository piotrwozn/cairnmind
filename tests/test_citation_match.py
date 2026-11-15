"""A quote that the model copied with another layout is recorded as the source's own passage (issue #104)."""
from __future__ import annotations

import json
import time

import pytest

from test_knowledge import FakeOllama, document, final, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.agent import MIN_QUOTE_CHARS, KnowledgeAgent
from cairnmind.citation_match import locate_quote

MARKDOWN = ("# Evaluation\n\nExit codes are 0 for passed, 1 for failed and 3 when a required model or client is\n"
            "unavailable (`not_run`). A report is written in every case.\n")
CODE = "MAX_DOCUMENTS = 50\nMAX_LINES = 400\nMIN_NAME = 3\n# Work bounds: files read per question.\nMAX_FILES = 2000\n"


def found(quote, content, minimum=MIN_QUOTE_CHARS):
    span = locate_quote(quote, content, minimum)
    return None if span is None else content[span[0]:span[1]]


def test_a_literal_quote_is_found_where_it_is():
    assert found("MAX_LINES = 400", CODE) == "MAX_LINES = 400"
    assert locate_quote("MAX_LINES = 400", CODE) == (19, 34)


def test_joined_lines_and_missing_backticks_give_the_passage_of_the_source():
    quote = "3 when a required model or client is unavailable (not_run). A report"
    assert quote not in MARKDOWN
    assert found(quote, MARKDOWN) == "3 when a required model or client is\nunavailable (`not_run`). A report"


@pytest.mark.parametrize("quote", [
    "3 when a required model or client was unavailable",       # another word
    "Exit codes are 0 for passed, 2 for failed",               # another number
    "3 when a required client is unavailable",                 # a word left out
    "Exit codes are 0 for passed and also 1 for failed",       # words added
    "MAX_DOCUMENTS = 500",                                     # a digit added
    "   \n  ", "```",
])
def test_changed_words_or_numbers_are_not_found(quote):
    assert found(quote, MARKDOWN) is None and found(quote, CODE) is None


def test_a_quote_that_skips_lines_keeps_its_longest_literal_run_of_whole_lines():
    quote = "MAX_DOCUMENTS = 50\n# Work bounds: files read per question.\nMAX_FILES = 2000"
    assert quote not in CODE
    assert found(quote, CODE) == "# Work bounds: files read per question.\nMAX_FILES = 2000"
    # Equal runs: the first one. A run shorter than the minimum is not evidence.
    assert found("MAX_DOCUMENTS = 50\nMIN_NAME = 3_000_000", CODE) == "MAX_DOCUMENTS = 50"
    assert found("MIN_NAME = 3\ninvented line of text", CODE, minimum=13) is None
    assert found("MIN_NAME = 3\ninvented line of text", CODE, minimum=12) == "MIN_NAME = 3"


def _validated(workspace, citations, texts, drop_invalid=False, answer="Three."):  # noqa: F811
    store, project, settings = workspace
    sources = [store.snapshot(project["id"], f"note://{index}", text, "note") for index, text in enumerate(texts)]
    evidence = {}
    for source in sources:
        source["_catalog_excerpt"] = source["content"]
        evidence[source["id"]] = source
    output = json.dumps({"answer": answer, "unknowns": [], "citations": [
        {"source_id": sources[index]["id"], "quote": quote} for index, quote in citations]})
    result, _ = KnowledgeAgent(store, settings, FakeOllama())._validate_final(
        output, project["id"], evidence, time.monotonic(), drop_invalid=drop_invalid)
    return result, sources


def test_validation_records_the_literal_passage_and_its_lines(workspace):  # noqa: F811
    quote = "3 when a required model or client is unavailable (not_run)."
    result, sources = _validated(workspace, [(0, quote)], [MARKDOWN])
    assert result["status"] == "complete"
    cited = result["sources"][0]
    assert cited["quote"] == "3 when a required model or client is\nunavailable (`not_run`)."
    assert cited["quote"] in sources[0]["content"]
    assert (cited["line_start"], cited["line_end"]) == (3, 4)


def test_a_quote_cited_under_another_shown_source_is_recorded_for_the_source_that_holds_it(workspace):  # noqa: F811
    result, sources = _validated(workspace, [(0, "MAX_DOCUMENTS = 50")], [MARKDOWN, CODE])
    assert result["status"] == "complete"
    assert result["sources"][0]["id"] == sources[1]["id"] and result["sources"][0]["quote"] == "MAX_DOCUMENTS = 50"
    assert result["sources"][0]["sha256"] == sources[1]["sha256"]


def test_a_quote_two_other_sources_hold_is_not_assigned_by_guess(workspace):  # noqa: F811
    result, _ = _validated(workspace, [(0, "MAX_DOCUMENTS = 50")], [MARKDOWN, CODE, "# copy\n" + CODE])
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert not result["sources"]


def test_a_quote_from_a_source_the_model_was_not_shown_is_not_assigned(workspace):  # noqa: F811
    store, project, settings = workspace
    shown = store.snapshot(project["id"], "note://shown", MARKDOWN, "note")
    hidden = store.snapshot(project["id"], "note://hidden", CODE, "note")
    shown["_catalog_excerpt"] = shown["content"]
    output = json.dumps({"answer": "Fifty.", "unknowns": [],
                         "citations": [{"source_id": shown["id"], "quote": "MAX_DOCUMENTS = 50"}]})
    result = KnowledgeAgent(store, settings, FakeOllama())._final(
        output, project["id"], {shown["id"]: shown, hidden["id"]: hidden}, time.monotonic())
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"


def test_one_failed_citation_fails_the_answer_until_the_repair_was_tried(workspace):  # noqa: F811
    citations = [(0, "MAX_DOCUMENTS = 50"), (0, "MAX_DOCUMENTS = 51 invented")]
    result, _ = _validated(workspace, citations, [CODE])
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert "Three." not in result["answer"]
    result, sources = _validated(workspace, citations, [CODE], drop_invalid=True)
    assert result["status"] == "complete" and result["answer"] == "Three."
    assert [item["quote"] for item in result["sources"]] == ["MAX_DOCUMENTS = 50"]
    assert result["diagnostics"]["citations_dropped"] == 1


def test_citations_are_never_dropped_when_none_verified(workspace):  # noqa: F811
    result, _ = _validated(workspace, [(0, "MAX_DOCUMENTS = 51 invented")], [CODE], drop_invalid=True)
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert not result["sources"] and "Three." not in result["answer"]


async def test_after_the_repair_the_answer_rests_on_its_verified_citations_and_is_still_judged(workspace):  # noqa: F811
    store, project, settings = workspace
    source, _ = document(store, project, CODE)
    reply = {"content": json.dumps({"answer": "Fifty documents.", "unknowns": [], "citations": [
        {"source_id": source["id"], "quote": "MAX_DOCUMENTS = 50"},
        {"source_id": source["id"], "quote": "The limit of documents is fifty."}]})}
    model = FakeOllama([reply, reply])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "MAX_DOCUMENTS documents")
    assert len(model.calls) == 2  # the answer and its one repair
    assert result["status"] == "complete" and result["answer"] == "Fifty documents."
    assert [item["quote"] for item in result["sources"]] == ["MAX_DOCUMENTS = 50"]
    assert result["diagnostics"]["citations_dropped"] == 1 and result["diagnostics"]["repair_attempted"]
    judged = json.loads(model.support_calls[0]["messages"][1]["content"])
    assert [item["quote"] for item in judged["citations"]] == ["MAX_DOCUMENTS = 50"]
    # The support check still decides: a "no" withholds the answer.
    refusing = FakeOllama([reply, reply], support={"content": json.dumps({"supported": False, "reason": "Other."})})
    refused = await KnowledgeAgent(store, settings, refusing).ask(project["id"], "MAX_DOCUMENTS documents")
    assert refused["status"] == "partial" and refused["diagnostics"]["code"] == "answer_unsupported"
    assert "Fifty documents." not in refused["answer"]
