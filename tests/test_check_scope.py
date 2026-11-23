"""The scope of a question is worked out once and handed to every check of its files (issue #416).

The check of a hit's file on disk read the project, its folders, the memory bindings and the imported sources again for
each file: a dozen database requests a file. Now they are read when the question starts, and a check takes one request
(the file's current source) with the rows of the hits read together. The verdict of each check stays what it was.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from test_knowledge import FakeOllama, workspace  # noqa: F401  (workspace is a fixture)
from test_round_trips import requests_made  # noqa: F401  (a fixture)

from cairnmind.agent import KnowledgeAgent
from cairnmind.ingest import index_project


def _module(name: str, extra: str = "") -> str:
    return f'"""The {name} module."""\n\nLIMIT_{name.upper()} = "{name}"\n\n\ndef {name}_budget():\n    return 1\n{extra}'


def _hit(store, source_id: str) -> dict:
    chunk = store.query("SELECT FROM chunks WHERE source_id=:s ORDER BY line_start LIMIT 1", {"s": source_id})[0]
    return {"id": chunk["id"], "source_id": source_id, "text": chunk["text"], "line_start": chunk["line_start"],
            "line_end": chunk["line_end"], "uri": store.get_source(source_id)["uri"], "kind": "code"}


def _current(store, project, path: Path) -> str:
    return store.query("SELECT id FROM sources WHERE project_id=:p AND uri=:uri AND is_current=true",
                       {"p": project["id"], "uri": str(path)})[0]["id"]


def _synthetic(store, project, path: Path, text: str) -> dict:
    """A code source and chunk for a path the indexer never saw (a file outside the root, a link, another spelling)."""
    source = store.snapshot(project["id"], str(path), text, "code", metadata={"path": str(path)})
    chunk = str(uuid4())
    store.command("INSERT INTO chunks CONTENT :doc", {"doc": {
        "id": chunk, "project_id": project["id"], "source_id": source["id"], "text": text, "line_start": 1,
        "line_end": len(text.splitlines()), "metadata": {}, "is_current": True}})
    return {"id": chunk, "source_id": source["id"], "text": text, "line_start": 1, "line_end": len(text.splitlines()),
            "uri": str(path), "kind": "code"}


def _checked(agent, project, hit, worktree=None, with_scope=True):
    """What a check of one hit gives: the hits it shows, the warnings, and the error that escaped (None)."""
    agent.retriever.warnings.clear()
    kwargs = {"scope": agent._check_scope(project["id"], worktree)} if with_scope else {}
    try:
        shown = agent._hits([hit], project["id"], {}, worktree, "budget", False, checked={}, **kwargs)
        error = None
    except Exception as exc:  # compared, not hidden
        shown, error = None, f"{type(exc).__name__}: {exc}"
    return shown, list(agent.retriever.warnings), error


def test_the_checks_of_a_question_take_a_request_each_not_a_dozen(workspace, requests_made):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    (root / "pkg").mkdir()
    names = [f"limit_{index:02d}" for index in range(12)]
    for name in names:
        (root / "pkg" / f"{name}.py").write_text(_module(name))
    index_project(store, project["id"])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    hits = [_hit(store, _current(store, project, root / "pkg" / f"{name}.py")) for name in names]
    requests_made.clear()
    scope = agent._check_scope(project["id"])
    in_the_scope = len(requests_made)
    requests_made.clear()
    shown = agent._hits(hits, project["id"], {}, None, "budget", False, checked={}, scope=scope)
    in_the_checks = len(requests_made)
    requests_made.clear()
    old = agent._hits(hits, project["id"], {}, None, "budget", False, checked={})
    without = len(requests_made)
    assert len(shown) == len(names) and shown == old
    # The scope: the project, its roots, the bindings, the imported sources. The checks: one read of the sources of
    # all the hits, then for each file the request for its current source (a changed file also writes).
    assert in_the_scope <= 6, in_the_scope
    assert in_the_checks <= 1 + len(names), requests_made
    assert without >= 8 * len(names)  # what a check cost: the scope again for each file
    print(f"{len(names)} files: scope {in_the_scope} requests, checks {in_the_checks} (without {without})")


def test_a_question_asks_for_its_scope_once_and_the_next_question_reads_it_again(workspace):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    (root / "pkg").mkdir()
    (root / "pkg" / "limit.py").write_text(_module("limit"))
    second = root.parent / "a-second-worktree"
    (second / "pkg").mkdir(parents=True)
    (second / "pkg" / "other.py").write_text(_module("other"))
    index_project(store, project["id"])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    scopes, make = [], agent._check_scope
    agent._check_scope = lambda *args: scopes.append(make(*args)) or scopes[-1]
    asyncio.run(agent.ask(project["id"], "limit budget"))
    store.add_project_root(project["id"], second)  # a root added between two questions
    index_project(store, project["id"])
    asyncio.run(agent.ask(project["id"], "other budget", worktree=str(second)))
    assert len(scopes) == 2 and scopes[0] is not scopes[1]
    assert second.resolve() not in scopes[0].roots and second.resolve() in scopes[1].roots
    assert scopes[1].root == second.resolve()


def test_the_model_tools_and_the_repair_catalog_use_the_scope_of_the_question(workspace):  # noqa: F811
    from test_knowledge import calls
    store, project, settings = workspace
    root = Path(project["root"])
    (root / "limit.py").write_text(_module("limit"))
    index_project(store, project["id"])
    source_id = _current(store, project, root / "limit.py")
    agent = KnowledgeAgent(store, settings, FakeOllama([
        calls(("search", {"query": "limit budget"}), ("read_source", {"source_id": source_id})),
        {"content": "not a typed answer"}]))
    made, tools, catalogs = [], [], []
    make, tool, catalog = agent._check_scope, agent._tool, agent._citation_catalog
    agent._check_scope = lambda *args: made.append(make(*args)) or made[-1]
    agent._tool = lambda *args, **kwargs: tools.append(args[5:] + tuple(kwargs.values())) or tool(*args, **kwargs)
    agent._citation_catalog = lambda *args, **kwargs: catalogs.append(kwargs.get("scope")) or catalog(*args, **kwargs)
    asyncio.run(agent.ask(project["id"], "limit budget"))
    assert len(made) == 2  # the question's, and one for the tool step (#423)
    assert tools == [(made[1],)] * 2  # search and read_source of one step share the step's scope
    # the first catalog is the question's; the repair catalog comes after the step and uses the latest
    assert len(catalogs) >= 2 and catalogs[0] is made[0] and all(scope is made[1] for scope in catalogs[1:])


def test_the_ask_without_models_uses_the_scope_of_its_question(workspace, requests_made):  # noqa: F811
    from cairnmind.knowledge_mode import text_matches
    store, project, settings = workspace
    root = Path(project["root"])
    (root / "pkg").mkdir()
    for index in range(12):
        (root / "pkg" / f"limit_{index:02d}.py").write_text(_module(f"limit_{index:02d}", "\nNOTE = 'retry budget'\n"))
    index_project(store, project["id"])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    made, make = [], agent._check_scope
    agent._check_scope = lambda *args: made.append(make(*args)) or made[-1]
    requests_made.clear()
    found = asyncio.run(text_matches(agent, project["id"], "retry budget"))
    with_scope = len(requests_made)
    assert len(made) == 1 and len(found["matches"]) == 8
    agent._check_scope = lambda *args: None  # the checks work the scope out for every file, as they did
    requests_made.clear()
    before = asyncio.run(text_matches(agent, project["id"], "retry budget"))
    assert [match["id"] for match in found["matches"]] == [match["id"] for match in before["matches"]]
    assert [match["excerpt"] for match in found["matches"]] == [match["excerpt"] for match in before["matches"]]
    print(f"deterministic ask: {with_scope} requests with the scope of its question, {len(requests_made)} without")
    assert with_scope <= 30 and len(requests_made) - with_scope >= 5 * len(found["matches"]), (with_scope, len(requests_made))


def test_a_folder_removed_between_two_tool_steps_is_not_read_from_by_the_second(workspace):  # noqa: F811
    from test_knowledge import calls
    store, project, settings = workspace
    root = Path(project["root"])
    second = root.parent / "a-second-worktree"
    second.mkdir()
    (second / "other.py").write_text(_module("other"))
    store.add_project_root(project["id"], second)
    index_project(store, project["id"])
    source_id = _current(store, project, second / "other.py")
    read = ("read_source", {"source_id": source_id})

    def remove_the_root(messages, tools):  # the owner removes the folder while the model thinks
        store.command("DELETE FROM project_roots WHERE project_id=:p AND path=:path",
                      {"p": project["id"], "path": str(second)})
        return calls(read)
    model = FakeOllama([calls(read), remove_the_root, {"content": "not a typed answer"}])
    agent = KnowledgeAgent(store, settings, model)
    made, make = [], agent._check_scope
    agent._check_scope = lambda *args: made.append(make(*args)) or made[-1]
    asyncio.run(agent.ask(project["id"], "other budget", worktree=str(second)))
    tools = [json.loads(message["content"]) for message in model.calls[2]["messages"] if message.get("role") == "tool"]
    assert "LIMIT_OTHER" in tools[0]["content"]  # accepted while the folder was registered
    assert "error" in tools[1] and "content" not in tools[1]  # refused once it was removed
    assert len(made) == 2 and made[0] is not made[1]  # the first checks share one scope, each tool step has its own


def test_every_check_of_a_file_gives_the_verdict_it_gave_without_the_scope(workspace, tmp_path):  # noqa: F811
    store, project, settings = workspace
    root = Path(project["root"])
    second, outside = tmp_path / "a-second-worktree", tmp_path / "outside"
    for folder in (root / "pkg", second / "pkg", outside):
        folder.mkdir(parents=True)
    for name in ("stable", "edited", "deleted", "replaced", "shared"):
        (root / "pkg" / f"{name}.py").write_text(_module(name))
    (second / "pkg" / "shared.py").write_text(_module("shared", "\nSECOND = 2\n"))  # another version in a worktree
    store.add_project_root(project["id"], second)
    index_project(store, project["id"])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    hits = {name: _hit(store, _current(store, project, root / "pkg" / f"{name}.py"))
            for name in ("stable", "edited", "deleted", "replaced", "shared")}
    hits["shared_second"] = _hit(store, _current(store, project, second / "pkg" / "shared.py"))
    (root / "pkg" / "edited.py").write_text(_module("edited", "\nEDITED = True\n"))  # changed since indexing
    (root / "pkg" / "deleted.py").unlink()  # missing on disk
    (root / "pkg" / "replaced.py").write_text(_module("replaced", "\nNEWER = True\n"))
    index_project(store, project["id"])  # a newer indexed version: the hit still names the replaced source
    (outside / "far.py").write_text(_module("far"))
    hits["outside"] = _synthetic(store, project, outside / "far.py", _module("far"))
    hits["case"] = _synthetic(store, project, root / "PKG" / "Stable.py", _module("stable"))
    (root / "pkg" / "link.py").symlink_to(root / "pkg" / "stable.py")
    hits["symlink"] = _synthetic(store, project, root / "pkg" / "link.py", _module("stable"))
    cases = [(name, None) for name in hits] + [("shared", str(second)), ("shared_second", str(second)),
                                               ("shared_second", str(root)), ("stable", str(second))]
    verdicts = {}
    for name, worktree in cases:
        verdicts[name, worktree] = _checked(agent, project, hits[name], worktree)
        assert verdicts[name, worktree] == _checked(agent, project, hits[name], worktree, with_scope=False), name
    shown = {key: value[0] for key, value in verdicts.items()}
    assert shown["stable", None] and shown["stable", None][0]["text"] == hits["stable"]["text"]
    assert "EDITED = True" in shown["edited", None][0]["text"] and shown["edited", None][0]["refreshed"]
    assert shown["deleted", None] == [] and any(
        "missing or cannot be checked" in warning for warning in verdicts["deleted", None][1])
    assert shown["replaced", None][0]["source_id"] != hits["replaced"]["source_id"]  # the current version, not the old
    assert shown["outside", None] == [] and shown["symlink", None] == []
    if not (root / "PKG" / "Stable.py").exists():  # a file system that tells the spellings apart
        assert shown["case", None] == []
    assert shown["shared", str(second)] == [] and shown["stable", str(second)] == []  # the other worktree's rule
    assert shown["shared_second", str(root)] == [] and shown["shared_second", str(second)]
