"""A quote that the model copied with another layout is recorded as the source's own passage (issue #104).

The texts are about parcels on purpose: this file is indexed by `cairnmind eval repository`, and
a fixture that looked like a setting of this project could be quoted as an answer about it.
"""
from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest

from test_knowledge import FakeOllama, document, final, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.agents.agent import CITATIONS_LEFT_OUT, MIN_QUOTE_CHARS, KnowledgeAgent
from cairnmind.agents.citation_match import locate_quote

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


def test_a_list_copied_with_other_spacing_gives_the_lines_of_the_source():
    source = 'command = [van, "--crates=12", "--straps=4",\n           "--route=north",\n           "--seal"]\n'
    quote = 'command = [van, "--crates=12","--straps=4", "--route=north", "--seal"]'
    assert found(quote, source) == source.strip()
    assert found('command = [van, "--crates=12", "--straps=5",', source) is None
    assert found('command = [van, "--crates=12", "--route=north"', source) is None


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
    assert found("PARCELS_A_VAN = 37\nLABEL_GAP = 3\nVANS_A_SHIFT = 2900", CODE) == "PARCELS_A_VAN = 37"
    assert found("LABEL_GAP = 3\nVANS_A", CODE, minimum=14) is None
    assert found("LABEL_GAP = 3\nVANS_A", CODE, minimum=13) == "LABEL_GAP = 3"


@pytest.mark.parametrize("quote", [
    "PARCELS_A_VAN = 37\nPALLET_ROWS = 999",                    # the second line with another value
    "PARCELS_A_VAN = 37\nLABEL_GAP = 3_000_000",
    "LABEL_GAP = 3\ninvented line of text",                    # a line the source does not have
    "invented line of text\nPARCELS_A_VAN = 37\nLABEL_GAP = 3",
    "PARCELS_A_VAN = 37\nZone codes are 0 for local",          # a line of another source
    "VANS_A_SHIFT = 2900\nPARCELS_A_VAN = 37",                 # lines in another order than the source's
])
def test_a_quote_of_several_lines_with_a_line_the_source_does_not_have_is_not_found(quote):
    assert found(quote, CODE, minimum=1) is None


@pytest.mark.parametrize("quote, source", [
    ("the limit is 1000 rows", "the limit is 1 000 rows"),
    ("the limit is 1 0 rows", "the limit is 10 rows"),
    ("retries = 15 workers", "retries = 1\n5 workers"),
    ("the token is in valid state", "the token is invalid state"),
    ("there is nowhere to go", "there is now here to go"),
    ("there is now here to go", "there is nowhere\nto go"),
])
def test_words_and_numbers_are_not_joined_or_split(quote, source):
    assert found(quote, source, minimum=1) is None


def test_spacing_next_to_a_sign_is_layout():
    assert found("retries=3, pause = 2", "retries = 3,\n    pause=2\n") == "retries = 3,\n    pause=2"
    assert found("see the bulky (zone 7 ) rule", "see the `bulky`\n(zone 7) rule") == "see the `bulky`\n(zone 7) rule"


def test_the_shortest_quote_is_measured_without_the_layout_of_the_source(workspace):  # noqa: F811
    nested = "def load(vans):\n    for van in vans:\n        if van:\n            pack({\n                van: {\n                        }\n                    }\n            )\n"
    for quote in ("}\n}", "} }"):
        assert len(found(quote, nested)) > MIN_QUOTE_CHARS  # the span holds the source's indentation
        result, _ = _validated(workspace, [(0, quote)], [nested])
        assert result["status"] == "partial" and not result["sources"], quote
        assert "The quoted passage is too short to be evidence." in result["unknowns"]
    # A source shorter than the minimum may still be quoted whole.
    result, _ = _validated(workspace, [(0, "GAP = 3")], ["GAP = 3\n"])
    assert result["status"] == "complete" and result["sources"][0]["quote"] == "GAP = 3"


def test_a_citation_with_an_invented_second_line_fails_validation(workspace):  # noqa: F811
    result, _ = _validated(workspace, [(0, "PARCELS_A_VAN = 37\nPALLET_ROWS = 999")], [CODE],
                           answer="There are 999 pallet rows.")
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert not result["sources"] and "999" not in result["answer"]


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


async def test_the_id_of_a_shown_hit_stands_for_its_source(workspace):  # noqa: F811
    store, project, settings = workspace
    source, chunk_id = document(store, project, CODE)
    other, other_chunk = document(store, project, MARKDOWN, uri="notes://shipping")
    # The model writes the hit's own ID where the source ID belongs; the quote is checked as ever.
    model = FakeOllama([final(chunk_id, "PARCELS_A_VAN = 37", "Thirty-seven.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "PARCELS_A_VAN parcels shipping zone")
    assert result["status"] == "complete" and len(model.calls) == 1
    assert result["sources"][0]["id"] == source["id"] and result["sources"][0]["quote"] == "PARCELS_A_VAN = 37"
    # An ID that was never shown, and a quote the hit's source does not hold, stay refused.
    for cited, quote in ((str(uuid4()), "PARCELS_A_VAN = 37"), (chunk_id, "PARCELS_A_VAN = 38 invented")):
        refused = FakeOllama([final(cited, quote, "Thirty-seven."), final(cited, quote, "Thirty-seven.")])
        result = await KnowledgeAgent(store, settings, refused).ask(project["id"], "PARCELS_A_VAN parcels shipping zone")
        assert result["status"] == "partial" and not result["sources"]


def test_one_failed_citation_fails_the_answer_until_the_repair_was_tried(workspace):  # noqa: F811
    citations = [(0, "PARCELS_A_VAN = 37"), (0, "PARCELS_A_VAN = 38 invented")]
    result, _ = _validated(workspace, citations, [CODE])
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert "Seven." not in result["answer"]
    result, sources = _validated(workspace, citations, [CODE], drop_invalid=True)
    assert result["status"] == "complete" and result["answer"] == "Seven."
    assert [item["quote"] for item in result["sources"]] == ["PARCELS_A_VAN = 37"]
    assert result["diagnostics"]["citations_dropped"] == 1
    # The result says so where a reader looks, and stays complete.
    assert CITATIONS_LEFT_OUT in result["freshness"]["warnings"] and not result["unknowns"]
    kept, _ = _validated(workspace, citations[:1], [CODE], drop_invalid=True)
    assert CITATIONS_LEFT_OUT not in kept["freshness"]["warnings"]


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
    assert CITATIONS_LEFT_OUT in result["freshness"]["warnings"]
    judged = json.loads(model.support_calls[0]["messages"][1]["content"])
    assert [item["quote"] for item in judged["citations"]] == ["PARCELS_A_VAN = 37"]
    # The support check still decides: a "no" withholds the answer.
    refusing = FakeOllama([reply, reply], support={"content": json.dumps({"supported": False, "reason": "Other."})})
    refused = await KnowledgeAgent(store, settings, refusing).ask(project["id"], "PARCELS_A_VAN parcels")
    assert refused["status"] == "partial" and refused["diagnostics"]["code"] == "answer_unsupported"
    assert "Thirty-seven parcels." not in refused["answer"]


def test_a_whole_shown_passage_can_be_quoted_with_the_value_at_its_end(workspace):  # noqa: F811
    # Issue #241: a passage is shown at up to EXCERPT_CHARS characters, so a quote of all of it must
    # survive the schema, the prompt and validation; the value of a setting is often its last line.
    from cairnmind.agents.agent import EXCERPT_CHARS, SYSTEM
    lines = [f"    # Depot rule {index}: crates are stacked by weight and labelled on two sides." for index in range(10)]
    passage = "\n".join([*lines, "    crate_limit: int = 431"])
    assert 750 < len(passage) <= EXCERPT_CHARS
    result, _ = _validated(workspace, [(0, passage)], [f"class Depot:\n{passage}\n"], answer="431.")
    assert result["status"] == "complete"
    assert result["sources"][0]["quote"].endswith("crate_limit: int = 431")
    store, _, settings = workspace
    catalog = [{"source_id": "s", "quote_excerpt": passage}]
    schema = KnowledgeAgent(store, settings, FakeOllama())._answer_schema(catalog)
    assert schema["properties"]["citations"]["items"]["properties"]["quote"]["maxLength"] == EXCERPT_CHARS
    assert f"quotes of at most {EXCERPT_CHARS} characters" in SYSTEM
