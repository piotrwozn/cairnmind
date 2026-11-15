"""The initial evidence shows a passage once, however many overlapping chunks hold it (issue #104)."""
from __future__ import annotations

import json
from uuid import uuid4

from test_knowledge import FakeOllama, final, workspace  # noqa: F401  (workspace is a fixture)

from cairnmind.agent import EXCERPT_CHARS, INITIAL_HITS, KnowledgeAgent, distinct_hits


def hit(source, text, line_start=1):
    return {"id": str(uuid4()), "source_id": source, "text": text, "line_start": line_start,
            "line_end": line_start + text.count("\n")}


def test_a_hit_that_shows_the_same_passage_of_the_same_source_gives_its_place_to_the_next():
    hits = [hit("a", "retry budget: five"), hit("a", "retry budget: five", 1), hit("b", "retry budget: five"),
            hit("a", "another line about the retry", 7), hit("c", "the last one")]
    kept = distinct_hits(hits, "retry budget", 3)
    assert [item["id"] for item in kept] == [hits[0]["id"], hits[2]["id"], hits[3]["id"]]
    assert distinct_hits(hits, "retry budget", 10) == [hits[0], hits[2], hits[3], hits[4]]
    assert distinct_hits([], "retry budget", 3) == []


def test_overlapping_chunks_count_once_when_their_excerpts_are_the_same_lines():
    lines = [f"line {number}: " + "filler " * 20 for number in range(40)]
    lines[20] = "RETRY_BUDGET = 5  # the retry budget"
    first = hit("a", "\n".join(lines[10:30]), 11)
    second = hit("a", "\n".join(lines[12:32]), 13)   # the next chunk of the file, two lines on
    apart = hit("a", "\n".join(lines[0:5]), 1)       # other lines of the same file
    assert len(first["text"]) > EXCERPT_CHARS
    kept = distinct_hits([first, second, apart], "What is the retry budget?", 3)
    assert kept == [first, apart]
    # A record hit has no lines: only an identical text of the same source repeats it.
    record = {"id": "record:1", "source_id": "a", "text": "retry budget decided", "line_start": None}
    assert distinct_hits([record, {**record, "id": "record:2"}, {**record, "source_id": "b"}], "retry", 5) == [
        record, {**record, "source_id": "b"}]


async def test_the_model_is_shown_ten_distinct_passages_when_chunks_repeat(workspace):  # noqa: F811
    store, project, settings = workspace
    lines = [f"note {number}: " + "background " * 12 for number in range(30)]
    lines[15] = "The retry budget is five attempts per request."
    source = store.snapshot(project["id"], "notes://retry", "\n".join(lines), "document")
    # Five overlapping chunks of one file, each starting one line later, all holding the answer.
    for start in range(5):
        store.command("INSERT INTO chunks CONTENT :doc", {"doc": {
            "id": str(uuid4()), "project_id": project["id"], "source_id": source["id"],
            "text": "\n".join(lines[start + 4:start + 24]), "line_start": start + 5, "line_end": start + 24,
            "metadata": {}, "is_current": True}})
    others = []
    for index in range(INITIAL_HITS + 2):
        text = f"Retry budget of service {index} is discussed in ticket {index}."
        other = store.snapshot(project["id"], f"notes://other-{index}", text, "document")
        store.command("INSERT INTO chunks CONTENT :doc", {"doc": {
            "id": str(uuid4()), "project_id": project["id"], "source_id": other["id"], "text": text,
            "line_start": 1, "line_end": 1, "metadata": {}, "is_current": True}})
        others.append(other["id"])
    model = FakeOllama([final(source["id"], lines[15], "Five attempts.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "What is the retry budget per request?")
    assert result["status"] == "complete"
    shown = json.loads(model.calls[0]["messages"][1]["content"])["initial_evidence"]
    assert len(shown) == INITIAL_HITS
    passages = [(item["source_id"], item["text"]) for item in shown]
    assert len(set(passages)) == len(passages)
    from_file = [item for item in shown if item["source_id"] == source["id"]]
    assert len(from_file) < 5 and any(lines[15] in item["text"] for item in from_file)
    assert len({item["source_id"] for item in shown} & set(others)) >= INITIAL_HITS - len(from_file)
