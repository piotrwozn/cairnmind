"""Edge-case tests for api_worker (AgentApp events, errors, resolve, interrupt, main)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import RichLog, Static, TextArea

from cairnmind.api_worker import AgentApp, incomplete_streams, main as worker_main, usage_text
from cairnmind.config import Settings
from test_api_worker import Engine, settle


class _RichJournal:
    def messages(self):
        return [{"role": "user", "content": {"structured": 1}, "name": "alice"}]

    def events(self):
        return [
            {"type": "provider_stream", "data": {"request_id": "req-1", "text": "unfinished response"}},
        ]


class _EdgeEngine(Engine):
    def __init__(self, settings, run, profile, **callbacks):
        super().__init__(settings, run, profile, **callbacks)
        self.journal = _RichJournal()
        self.turn_result = {
            "status": "partial",
            "error_code": "limit",
            "message": "stopped early",
            "unknowns": ["Missing evidence"],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }
        self.turn_error = None

    async def turn(self, text):
        self.turns.append(text)
        if self.turn_error is not None:
            raise self.turn_error
        await self.callbacks["on_delta"]("partial output")
        return dict(self.turn_result)

    async def pending_actions(self):
        return list(self.pending)

    async def resolve_uncertain(self, action_id, note):
        self.resolved.append((action_id, note))
        self.pending = [a for a in self.pending if a["action_id"] != action_id]


def _make_edge_app(tmp_path, *, factory=_EdgeEngine, mcp_problems=None, report=None):
    run = {
        "id": "run-e", "run_id": "run-e", "generation": 1, "project_id": "p",
        "worktree": str(tmp_path / "proj"), "session_id": "sess-e", "client": "api",
    }
    profile = {"id": "prof-e", "label": "Edge API", "client": "api", "model": "m"}
    return AgentApp(
        Settings(data_dir=tmp_path / "data"),
        run,
        profile,
        key="secret-val",
        engine_factory=factory,
        mcp_problems=mcp_problems,
        report=report,
    )


def test_usage_text_and_incomplete_streams_edge_cases():
    out = usage_text({"copilot": {"status": "unavailable"}, "premium_requests": 2}, {}, "en")
    assert "Copilot: usage unavailable" in out and "premium_requests" in out
    assert incomplete_streams([
        "bad",
        {"type": "provider_stream", "data": {"request_id": ""}},
        {"type": "provider_stream", "data": {"request_id": "big", "text": "a" * 140000}},
    ])[0]["truncated"] is True


@pytest.mark.asyncio
async def test_agent_app_events_and_mcp_reporting(tmp_path, monkeypatch):
    reported = []
    app = _make_edge_app(
        tmp_path,
        mcp_problems=[{"server_id": "srv1", "code": "start_failed"}],
        report=lambda sid, code: reported.append((sid, code)),
    )
    async with app.run_test(size=(100, 38)) as pilot:
        await settle(app, pilot)
        history_text = "\n".join(line.text for line in app.query_one("#agent-history", RichLog).lines)
        assert "req-1" in history_text

        app.write_message("not-a-dict")
        await app.agent_event("not-a-dict")
        await app.agent_event({"type": "provider_event", "data": {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 4}}})
        await app.agent_event({"type": "usage", "data": {"input_tokens": 20, "output_tokens": 8}})
        await app.agent_event({"type": "context_compacted", "data": {}})
        await app.agent_event({"type": "mcp_server_failed", "data": {"server_id": "srv2", "code": "start_timeout"}})
        await app.agent_event({"type": "mcp_tool_withdrawn", "data": {}})
        await app.agent_event({"type": "mcp_tools_withdrawn", "data": {"code": "tools_rejected"}})
        await app.agent_event({"type": "mcp_tools_kept", "data": {"code": "provider_error"}})
        await settle(app, pilot)
        assert ("srv2", "start_timeout") in reported

        notified = []
        monkeypatch.setattr(app, "notify", lambda msg, **kw: notified.append(msg))
        await app.action_quit()
        assert notified

    # After unmount, app._closed is True and late callbacks are safely ignored.
    await app.on_delta("ignored")
    await app.agent_event({"type": "error", "data": {"code": "ignored"}})


@pytest.mark.asyncio
async def test_agent_app_turn_errors_interrupt_and_resolve(tmp_path, monkeypatch):
    app = _make_edge_app(tmp_path)
    async with app.run_test(size=(100, 38)) as pilot:
        await settle(app, pilot)
        app.query_one("#agent-prompt", TextArea).text = "Run partial"
        await pilot.click("#agent-send")
        await settle(app, pilot)
        assert "Incomplete response" in str(app.query_one("#agent-stream", Static).render())

        app.engine.turn_error = RuntimeError("boom")
        app.query_one("#agent-prompt", TextArea).text = "Explode"
        await pilot.click("#agent-send")
        await settle(app, pilot)
        assert "did not finish" in str(app.query_one("#agent-status", Static).render())
        app.engine.turn_error = None

        app._queued = True
        app.query_one("#agent-prompt", TextArea).text = "   "
        app._submit_prompt()
        assert app._queued is False

        dismissed = []
        app._queued = True
        app._approval_screen = SimpleNamespace(dismiss=lambda val: dismissed.append(val))
        app.action_interrupt_turn()
        await settle(app, pilot)
        assert app._queued is False and dismissed == [False]
        app._approval_screen = None

        app.engine.pending = []
        app.action_resolve()
        await settle(app, pilot)
        app.engine.pending = [{"action_id": "act-1", "command": "ls"}]
        monkeypatch.setattr(app, "push_screen_wait", lambda s: asyncio.sleep(0, result=None))
        app.resolve_pending()
        await settle(app, pilot)
        assert len(app.engine.pending) == 1

        seq = iter(["act-1", {"note": "checked ok"}])

        async def drive_resolve(screen):
            val = next(seq)
            if getattr(screen, "on_submit", None):
                await screen.on_submit(val)
            return val

        monkeypatch.setattr(app, "push_screen_wait", drive_resolve)
        app.resolve_pending()
        await settle(app, pilot)
        assert app.engine.resolved == [("act-1", "checked ok")]


@pytest.mark.asyncio
async def test_agent_app_mount_failure_sets_error_status(tmp_path):
    def bad_factory(*args, **kwargs):
        raise RuntimeError("cannot start")

    app = _make_edge_app(tmp_path, factory=bad_factory)
    async with app.run_test(size=(100, 36)) as pilot:
        await settle(app, pilot)
        assert app.engine is None
        assert "Cannot prepare" in str(app.query_one("#agent-status", Static).render())
        app.execute_turn("ignored")
        app.action_send()
        app._queued = True
        app._submit_prompt()
        app.resolve_pending()
        await settle(app, pilot)


def test_worker_main_tty_and_error_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["worker", "--data-dir", str(tmp_path), "--run-id", "r1", "--generation", "1"])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("sys.stdout", SimpleNamespace(isatty=lambda: True))
    with pytest.raises(SystemExit, match="terminal is required"):
        worker_main()

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(
        "cairnmind.api_runtime.api_worker_config",
        lambda s, rid, gen: {"run": {"id": "other", "generation": 1}, "profile": {"id": "p", "client": "api"}},
    )
    with pytest.raises(SystemExit):
        worker_main()

    ran = []

    class DummyAgentApp:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            ran.append(True)

    monkeypatch.setattr("cairnmind.api_worker.AgentApp", DummyAgentApp)
    monkeypatch.setattr(
        "cairnmind.api_runtime.api_worker_config",
        lambda s, rid, gen: {"run": {"id": "r1", "generation": 1}, "profile": {"id": "p", "client": "api"}, "key": "k"},
    )
    worker_main()
    assert ran == [True]
