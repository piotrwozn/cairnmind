"""A quote that the model copied with another layout is recorded as the source's own passage (issue #104).

The texts are about parcels on purpose: this file is indexed by `cairnmind eval repository`, and
a fixture that looked like a setting of this project could be quoted as an answer about it.
"""
from __future__ import annotations

import json
import time

import pytest

from test_knowledge import FakeOllama, document, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.agent import MIN_QUOTE_CHARS, KnowledgeAgent
from cairnmind.citation_match import locate_quote

MARKDOWN = ("# Shipping\n\nZone codes are 0 for local, 1 for national and 7 when a parcel or pallet is\n"
            "oversized (`bulky`). A label is printed in every case.\n")
CODE = "PARCELS_A_VAN = 37\nPALLET_ROWS = 410\nLABEL_GAP = 3\n# Depot bounds: vans loaded per shift.\nVANS_A_SHIFT = 2900\n"


def found(quote, content, minimum=MIN_QUOTE_CHARS):
    span = locate_quote(quote, content, minimum)
    return None if span is None else content[span[0]:span[1]]


def test_a_literal_quote_is_found_where_it_is():
    assert found("PALLET_ROWS = 410", CODE) == "PALLET_ROWS = 410"
    assert locate_quote("PALLET_ROWS = 410", CODE) == (19, 36)


def test_joined_lines_and_missing_backticks_give_the_passage_of_the_source():
    quote = "7 when a parcel or pallet is oversized (bulky). A label"
    assert quote not in MARKDOWN
    assert found(quote, MARKDOWN) == "7 when a parcel or pallet is\noversized (`bulky`). A label"


@pytest.mark.parametrize("quote", [
    "7 when a parcel or pallet was oversized",                 # another word
    "Zone codes are 0 for local, 2 for national",              # another number
    "7 when a pallet is oversized",                            # a word left out
    "Zone codes are 0 for local and also 1 for national",      # words added
    "PARCELS_A_VAN = 370",                                     # a digit added
    "   \n  ", "```",
])
def test_changed_words_or_numbers_are_not_found(quote):
    assert found(quote, MARKDOWN) is None and found(quote, CODE) is None


def test_a_quote_that_skips_lines_keeps_its_longest_literal_run_of_whole_lines():
    quote = "PARCELS_A_VAN = 37\n# Depot bounds: vans loaded per shift.\nVANS_A_SHIFT = 2900"
    assert quote not in CODE
    assert found(quote, CODE) == "# Depot bounds: vans loaded per shift.\nVANS_A_SHIFT = 2900"
    # Equal runs: the first one. A run shorter than the minimum is not evidence.
    assert found("PARCELS_A_VAN = 37\nLABEL_GAP = 3_000_000", CODE) == "PARCELS_A_VAN = 37"
    assert found("LABEL_GAP = 3\ninvented line of text", CODE, minimum=14) is None
    assert found("LABEL_GAP = 3\ninvented line of text", CODE, minimum=13) == "LABEL_GAP = 3"


def _validated(workspace, citations, texts, drop_invalid=False, answer="Seven."):  # noqa: F811
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
    quote = "7 when a parcel or pallet is oversized (bulky)."
    result, sources = _validated(workspace, [(0, quote)], [MARKDOWN])
    assert result["status"] == "complete"
    cited = result["sources"][0]
    assert cited["quote"] == "7 when a parcel or pallet is\noversized (`bulky`)."
    assert cited["quote"] in sources[0]["content"]
    assert (cited["line_start"], cited["line_end"]) == (3, 4)


def test_a_quote_cited_under_another_shown_source_is_recorded_for_the_source_that_holds_it(workspace):  # noqa: F811
    result, sources = _validated(workspace, [(0, "PARCELS_A_VAN = 37")], [MARKDOWN, CODE])
    assert result["status"] == "complete"
    assert result["sources"][0]["id"] == sources[1]["id"] and result["sources"][0]["quote"] == "PARCELS_A_VAN = 37"
    assert result["sources"][0]["sha256"] == sources[1]["sha256"]


def test_a_quote_two_other_sources_hold_is_not_assigned_by_guess(workspace):  # noqa: F811
    result, _ = _validated(workspace, [(0, "PARCELS_A_VAN = 37")], [MARKDOWN, CODE, "# copy\n" + CODE])
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert not result["sources"]


def test_a_quote_from_a_source_the_model_was_not_shown_is_not_assigned(workspace):  # noqa: F811
    store, project, settings = workspace
    shown = store.snapshot(project["id"], "note://shown", MARKDOWN, "note")
    hidden = store.snapshot(project["id"], "note://hidden", CODE, "note")
    shown["_catalog_excerpt"] = shown["content"]
    output = json.dumps({"answer": "Thirty-seven.", "unknowns": [],
                         "citations": [{"source_id": shown["id"], "quote": "PARCELS_A_VAN = 37"}]})
    result = KnowledgeAgent(store, settings, FakeOllama())._final(
        output, project["id"], {shown["id"]: shown, hidden["id"]: hidden}, time.monotonic())
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"


def test_one_failed_citation_fails_the_answer_until_the_repair_was_tried(workspace):  # noqa: F811
    citations = [(0, "PARCELS_A_VAN = 37"), (0, "PARCELS_A_VAN = 38 invented")]
    result, _ = _validated(workspace, citations, [CODE])
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert "Seven." not in result["answer"]
    result, sources = _validated(workspace, citations, [CODE], drop_invalid=True)
    assert result["status"] == "complete" and result["answer"] == "Seven."
    assert [item["quote"] for item in result["sources"]] == ["PARCELS_A_VAN = 37"]
    assert result["diagnostics"]["citations_dropped"] == 1


def test_citations_are_never_dropped_when_none_verified(workspace):  # noqa: F811
    result, _ = _validated(workspace, [(0, "PARCELS_A_VAN = 38 invented")], [CODE], drop_invalid=True)
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert not result["sources"] and "Seven." not in result["answer"]


async def test_after_the_repair_the_answer_rests_on_its_verified_citations_and_is_still_judged(workspace):  # noqa: F811
    store, project, settings = workspace
    source, _ = document(store, project, CODE)
    reply = {"content": json.dumps({"answer": "Thirty-seven parcels.", "unknowns": [], "citations": [
        {"source_id": source["id"], "quote": "PARCELS_A_VAN = 37"},
        {"source_id": source["id"], "quote": "A van takes thirty-seven parcels."}]})}
    model = FakeOllama([reply, reply])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "PARCELS_A_VAN parcels")
    assert len(model.calls) == 2  # the answer and its one repair
    assert result["status"] == "complete" and result["answer"] == "Thirty-seven parcels."
    assert [item["quote"] for item in result["sources"]] == ["PARCELS_A_VAN = 37"]
    assert result["diagnostics"]["citations_dropped"] == 1 and result["diagnostics"]["repair_attempted"]
    judged = json.loads(model.support_calls[0]["messages"][1]["content"])
    assert [item["quote"] for item in judged["citations"]] == ["PARCELS_A_VAN = 37"]
    # The support check still decides: a "no" withholds the answer.
    refusing = FakeOllama([reply, reply], support={"content": json.dumps({"supported": False, "reason": "Other."})})
    refused = await KnowledgeAgent(store, settings, refusing).ask(project["id"], "PARCELS_A_VAN parcels")
    assert refused["status"] == "partial" and refused["diagnostics"]["code"] == "answer_unsupported"
    assert "Thirty-seven parcels." not in refused["answer"]
