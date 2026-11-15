"""When the excerpts hold no answer, the best chunks are read whole once (issue #104)."""
from __future__ import annotations

import json

import pytest

from test_knowledge import FakeOllama, document, final, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.agent import EXCERPT_CHARS, WHOLE_PASSAGE_CHARS, KnowledgeAgent

ANSWER = 'ARGUMENTS = ["-Xmx1024m", "-server"]'
UNKNOWN = {"content": json.dumps({"answer": "unknown", "citations": [], "unknowns": ["Not stated."]})}


def long_chunk(answer_line=ANSWER):
    """A chunk whose lines about the question are far from the line that answers it."""
    lines = ([f"# engine heap notes {number}: the database engine and its Java heap" for number in range(12)]
             + [f"filler_{number} = {number}  # unrelated setting" for number in range(40)] + [answer_line]
             + [f"other_{number} = {number}" for number in range(5)])
    text = "\n".join(lines)
    assert EXCERPT_CHARS < len(text) < WHOLE_PASSAGE_CHARS
    return text


def evidence_of(call):
    return json.loads(call["messages"][1]["content"])["initial_evidence"]


async def test_an_unknown_answer_gets_one_look_at_the_whole_chunks(workspace):  # noqa: F811
    store, project, settings = workspace
    text = long_chunk()
    source, _ = document(store, project, text, uri="notes://engine")
    model = FakeOllama([UNKNOWN, UNKNOWN, final(source["id"], ANSWER, "1024 MB.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "How much Java heap does the engine get?")
    assert result["status"] == "complete" and result["answer"] == "1024 MB."
    assert result["sources"][0]["quote"] == ANSWER
    assert result["diagnostics"]["second_look"] == "answered" and result["diagnostics"]["code"] == "ok"
    assert len(model.calls) == 3  # the answer, its repair, the second look
    first, second = evidence_of(model.calls[0]), evidence_of(model.calls[2])
    assert all(ANSWER not in item["text"] for item in first)
    assert [item["text"] for item in second] == [text]
    assert second[0]["line_start"] == 1 and second[0]["line_end"] == text.count("\n") + 1
    assert len(model.support_calls) == 1  # the second answer is judged like any other
    assert "_hits_in_part" not in result["diagnostics"]


async def test_a_second_look_that_finds_nothing_leaves_the_first_result(workspace):  # noqa: F811
    store, project, settings = workspace
    document(store, project, long_chunk(), uri="notes://engine")
    partial = {"content": json.dumps({"answer": "unknown", "citations": [], "unknowns": ["First pass."]})}
    model = FakeOllama([partial, partial, {"content": "no JSON at all"}, {"content": "still none"}])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "How much Java heap does the engine get?")
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "missing_evidence"
    assert result["diagnostics"]["second_look"] == "no_answer"
    assert result["diagnostics"]["repair_attempted"] is True  # the first pass's own diagnostics
    assert len(model.calls) == 4


@pytest.mark.parametrize("case", ["short", "complete", "refused", "typed"])
async def test_no_second_look_without_cause(workspace, case):  # noqa: F811
    store, project, settings = workspace
    question = "How much Java heap does the engine get?"
    if case == "short":
        # Every hit was shown whole: there is nothing more to read.
        document(store, project, "The engine and its Java heap are described elsewhere.", uri="notes://engine")
        replies = [UNKNOWN, UNKNOWN]
    else:
        source, _ = document(store, project, long_chunk("# the Java heap of the engine: -Xmx1024m"),
                             uri="notes://engine")
        quote = "# engine heap notes 0: the database engine and its Java heap"
        replies = {"complete": [final(source["id"], quote, "It is described.")],
                   # A refused citation or an unsupported answer is not "the evidence holds no answer".
                   "refused": [final(source["id"], "an invented quote", "1024."),
                               final(source["id"], "an invented quote", "1024.")],
                   "typed": [UNKNOWN, UNKNOWN]}[case]
    model = FakeOllama(list(replies))
    schema = {"type": "string"} if case == "typed" else None
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], question, answer_schema=schema)
    assert "second_look" not in result["diagnostics"] and "_hits_in_part" not in result["diagnostics"]
    assert len(model.calls) == len(replies) and not model.replies


async def test_the_second_look_keeps_to_the_time_limit_of_the_question(workspace):  # noqa: F811
    store, project, settings = workspace
    document(store, project, long_chunk(), uri="notes://engine")
    settings.ask_timeout = 4.0  # less than a second look needs
    model = FakeOllama([UNKNOWN, UNKNOWN])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "How much Java heap does the engine get?")
    assert result["status"] == "partial" and "second_look" not in result["diagnostics"]
    assert len(model.calls) == 2


async def test_whole_passages_stay_within_the_context_guard(workspace):  # noqa: F811
    store, project, settings = workspace
    for index in range(8):
        document(store, project, long_chunk(f"HEAP_{index} = {index}").replace("filler_", f"filler_{index}_") + "\n"
                 + "\n".join(f"padding_{index}_{line} = 'engine heap'" for line in range(120)), uri=f"notes://engine-{index}")
    model = FakeOllama([UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "How much Java heap does the engine get?")
    assert result["diagnostics"]["second_look"] == "no_answer"
    second = model.calls[2]["messages"]
    shown = evidence_of(model.calls[2])
    assert 1 <= len(shown) < 8 and all(len(item["text"]) > EXCERPT_CHARS for item in shown)
    limit = max(12000, settings.num_ctx * 2)
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in second) <= limit
    # The repair of the second look still fits too.
    assert sum(len(json.dumps(message, ensure_ascii=False)) for message in model.calls[3]["messages"]) <= limit
