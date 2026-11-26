"""Fast-tier edge-case tests for memory_published.py, terminal_ui.py, and agent.py."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Button, Input, Static

from cairnmind import agent as agent_mod, memory_published as mp, terminal_ui
from cairnmind.config import Settings
from cairnmind.memory import MemoryConflict


def test_memory_published_helpers_close_and_publish_edges(tmp_path, monkeypatch):
    # source_ref: note too long -> Held; bidi/control -> Held
    with pytest.raises(mp.Held, match="longer than"):
        mp.source_ref(
            {"id": "s1", "kind": "manual", "uri": "manual://1", "created_at": "2026-01-01",
             "content": "x" * (mp.MAX_NOTE_CHARS + 1), "metadata": {"origin": "workspace_form"}},
            tmp_path,
        )
    with pytest.raises(mp.Held, match="control or bidi"):
        mp.source_ref(
            {"id": "s2", "kind": "manual", "uri": "manual://2", "created_at": "2026-01-01",
             "content": "bad\x00char", "metadata": {"origin": "workspace_form"}},
            tmp_path,
        )
    ref, passage = mp.source_ref(
        {"id": "s3", "kind": "release", "uri": "release://1", "created_at": "2026-01-01",
         "content": "released", "metadata": {"origin": "local_user_release"}},
        tmp_path,
    )
    assert ref["kind"] == "release" and passage == "released"

    # quote_of: too long/bidi -> None; missing source skipped; published_quotes hit; miss -> None
    assert mp.quote_of("bad\x00quote", ["s1"], {}) is None
    assert mp.quote_of("hello", ["missing"], {}) is None
    q_sha = mp.sha256_text("matched quote")
    pub_src = {"id": "s1", "kind": "published", "metadata": {"published_quotes": [{"sha256": q_sha, "lines": [3, 4]}]}}
    assert mp.quote_of("matched quote", ["s1"], {"s1": pub_src}) == {"source": "s1", "lines": [3, 4], "text": "matched quote"}
    assert mp.quote_of("unmatched", ["s1"], {"s1": pub_src}) is None

    # plan_definitions empty plan_sources returns []
    assert mp.plan_definitions({"id": "pl1"}, [], {}, lambda s: ({}, None), []) == []

    # Publisher._close: holds record naming unpublished plan, pops unreplaced mechanical supersession, holds plan missing step
    histories = {
        "r-plan": [{"supersedes": None, "plan": "pl-missing", "covered": {"by": "review"}}],
        "r-sup": [{"supersedes": None, "covered": {"by": "superseded"}}],
    }
    held = {}
    definitions = {"pl-ok": [{"steps": [["missing-step", "k"]]}]}
    held_plans = {}
    heads = {
        "r-plan": {"kind": "task", "metadata": {}},
        "r-sup": {"kind": "fact", "metadata": {}},
    }
    mp.Publisher._close(histories, held, definitions, held_plans, heads)
    assert "r-plan" in held and "r-sup" in held and "pl-ok" in held_plans

    # Publisher.publish conflict errors (read_tree errors, files without FORMAT.json, invalid merged tree)
    pub_dir = tmp_path / ".memory" / "published"
    pub_dir.mkdir(parents=True)
    fake_store = SimpleNamespace(data_dir=tmp_path / "data", settings=Settings(data_dir=tmp_path / "data"))
    pub = mp.Publisher(fake_store)
    monkeypatch.setattr(pub, "build", lambda *a, **k: {
        "project": {"id": "p1"}, "root": tmp_path, "folder": pub_dir,
        "files": {}, "models": [], "held": {}, "labels": {}, "heads": {},
    })
    monkeypatch.setattr(mp, "publication_rules", lambda r, s: [])
    monkeypatch.setattr(mp, "scan", lambda tree, rules: [])
    monkeypatch.setattr(mp, "unbound_in_checkout", lambda *a, **k: None)

    monkeypatch.setattr(mp, "read_tree", lambda f: ({}, [("corrupt.md", "invalid")]))
    with pytest.raises(MemoryConflict, match="cannot be read"):
        pub.publish("p1")

    monkeypatch.setattr(mp, "read_tree", lambda f: ({"stray.md": b"hi"}, []))
    with pytest.raises(MemoryConflict, match="no FORMAT.json"):
        pub.publish("p1")

    monkeypatch.setattr(mp, "read_tree", lambda f: ({}, []))
    monkeypatch.setattr(mp, "check_files", lambda m, secrets=False, whole=True: ({}, [("", "broken tree")]))
    with pytest.raises(MemoryConflict, match="invalid tree"):
        pub.publish("p1")


@pytest.mark.asyncio
async def test_terminal_ui_summary_text_and_form_modal_edges(monkeypatch):
    # summary_text edge cases
    assert terminal_ui.summary_text(123, "en") == "Operation completed."
    txt = terminal_ui.summary_text({"authenticated": True, "errors": ["e1", "e2"], "warnings": "w"}, "en")
    assert "Connected" in txt and "(2)" in txt and "(1)" in txt

    # FormModal: browse button fills field, validator raises FieldError, saving guard skips double submit
    class _Host(App):
        def __init__(self, modal):
            super().__init__()
            self.modal = modal
            self.language = "en"
            self.res = "UNSET"

        def on_mount(self):
            self.push_screen(self.modal, lambda v: setattr(self, "res", v))

    modal = terminal_ui.FormModal(
        "Demo",
        [terminal_ui.Field("folder", "Folder", "", required=True, folder=True,
                           validator=lambda v: "Bad folder" if v == "bad" else None)],
    )
    host = _Host(modal)
    async with host.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(host, "push_screen_wait", lambda s: asyncio.sleep(0, result="/picked/dir"))
        modal.query_one("#browse-folder", Button).press()
        await pilot.pause()
        await host.workers.wait_for_complete()
        await pilot.pause()
        assert modal.query_one("#field-folder", Input).value == "/picked/dir"

        # Trigger validator error
        modal.query_one("#field-folder", Input).value = "bad"
        modal.action_submit()
        await host.workers.wait_for_complete()
        await pilot.pause()
        assert "Bad folder" in str(modal.query_one("#form-error", Static).render())

        # Saving guard no-op when saving=True
        modal.saving = True
        modal.action_submit()
        await host.workers.wait_for_complete()


def test_knowledge_agent_validation_and_source_edge_branches(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    warnings = []
    fake_retriever = SimpleNamespace(warnings=warnings, source_in_scope=lambda s, p, w: True)
    fake_store = SimpleNamespace(
        get_source=lambda sid: {"id": sid, "project_id": "p1", "kind": "file", "uri": "file://a.py", "sha256": "h1", "content": "hello world"},
        query=lambda sql, params=None: [],
    )
    ka = agent_mod.KnowledgeAgent(fake_store, settings, ollama=SimpleNamespace(), retriever=fake_retriever)

    # _carry_verified_citations(draft, repaired, evidence): non-list returns repaired; skips non-dict; caps at 8
    raw = json.dumps({"answer": "ok", "citations": "bad"})
    assert ka._carry_verified_citations(json.dumps({"citations": []}), raw, {}) == raw
    orig_cits = ["not-dict", *[{"source_id": "s1", "quote": f"quote {i:02d} text"} for i in range(10)]]
    repaired = json.dumps({"answer": "ok", "citations": []})
    ev = {"s1": {"id": "s1", "kind": "file", "content": "\n".join(f"quote {i:02d} text" for i in range(10)), "metadata": {}}}
    merged = json.loads(ka._carry_verified_citations(json.dumps({"citations": orig_cits}), repaired, ev))
    assert len(merged["citations"]) == 8

    # _source: current_source raising OSError appends warning & re-raises; wrong project_id raises
    monkeypatch.setattr("cairnmind.ingest.current_source", lambda st, sid: (_ for _ in ()).throw(OSError("disk err")))
    with pytest.raises(OSError):
        ka._source("s1", "p1", {})
    assert any("Cannot verify current source" in w for w in warnings)

    monkeypatch.setattr(
        "cairnmind.ingest.current_source",
        lambda st, sid: {"id": sid, "project_id": "other", "kind": "file", "uri": "file://a.py", "sha256": "h1", "is_current": True},
    )
    with pytest.raises(ValueError, match="outside this project"):
        ka._source("s1", "p1", {})

    # _definitions catches ValueError from find_definitions
    monkeypatch.setattr(agent_mod, "find_definitions", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    assert ka._definitions("p1", "q", [], {}) == []

    # _check_status rejects empty task_id
    with pytest.raises(ValueError, match="nonempty string"):
        ka._check_status("p1", "", {})

    # _validate_final: verification check error -> source_stale; missing_answer; answer_schema missing answer;
    # non-list unknowns & non-list citations & non-dict citation & source OSError during final check
    ev_with_check = {"s1": {"id": "s1", "uri": "file://a.py", "kind": "file", "content": "abc", "metadata": {}, "sha256": "h1", "_checked_machine_scopes": {"k": {"task_id": "t1", "worktree": None, "run_ids": ["r1"]}}}}
    res_stale, retry = ka._validate_final("{}", "p1", ev_with_check, 0.0)
    assert res_stale["diagnostics"]["code"] == "source_stale" and retry is False

    res_no_ans, retry = ka._validate_final(json.dumps({"answer": "   "}), "p1", {}, 0.0)
    assert res_no_ans["diagnostics"]["code"] == "missing_answer" and retry is True

    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"], "additionalProperties": False}
    res_missing_field, _ = ka._validate_final(json.dumps({"citations": "bad", "unknowns": "bad"}), "p1", {}, 0.0, answer_schema=schema)
    assert res_missing_field["status"] == "partial"

    # Non-dict citation & _source error on final check
    ev_simple = {"s1": {"id": "s1", "uri": "file://a.py", "sha256": "h1", "kind": "file", "content": "abc", "metadata": {}}}
    monkeypatch.setattr(ka, "_source", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    res_cits, _ = ka._validate_final(
        json.dumps({"answer": "ok", "citations": ["not-dict", {"source_id": "s1", "quote": "abc"}]}),
        "p1",
        ev_simple,
        0.0,
    )
    assert res_cits["status"] == "partial"
