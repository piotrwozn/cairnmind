"""Programming panels restore data without inference and keep approval human-only."""
import asyncio
from copy import deepcopy

import pytest
from textual.widgets import Button, RichLog, Static, TextArea

from cairnmind.api_worker import AgentApp, incomplete_streams, usage_text
from cairnmind.config import Settings


class Journal:
    def messages(self):
        return [{"role": "user", "content": "Earlier question"},
                {"role": "assistant", "content": "[bold]literal[/bold]\x1b]52;c;owned\x07"}]


class Engine:
    def __init__(self, settings, run, profile, **callbacks):
        self.journal = Journal()
        self.callbacks = callbacks
        self.turns = []
        self.pending = []
        self.resolved = []
        self.interrupts = 0
        self.closed = False
        self.wait = None

    async def turn(self, text):
        self.turns.append(text)
        await self.callbacks["on_event"]({"type": "message", "data": {"role": "user", "content": text}})
        if self.wait is not None:
            await self.wait.wait()
        await self.callbacks["on_delta"]("streamed ")
        await self.callbacks["on_delta"]("answer\x1b]52;unsafe\x07")
        await self.callbacks["on_event"]({"type": "message", "data": {"role": "assistant", "content": "complete answer"}})
        return {"status": "complete", "usage": {"input_tokens": 12, "output_tokens": 3}}

    def pending_actions(self):
        return deepcopy(self.pending)

    def resolve_uncertain(self, action_id, note):
        self.resolved.append((action_id, note))
        self.pending = [a for a in self.pending if a["action_id"] != action_id]

    async def interrupt(self):
        self.interrupts += 1
        if self.wait is not None:
            self.wait.set()

    async def close(self):
        self.closed = True


def make_app(tmp_path):
    run = {"id": "run-a", "run_id": "run-a", "generation": 2, "project_id": "project-a",
           "worktree": str(tmp_path / "project"), "session_id": "session-a", "client": "api"}
    profile = {"id": "profile-a", "label": "Test API", "client": "api", "model": "controlled"}
    return AgentApp(Settings(data_dir=tmp_path / "data"), run, profile, key="fixture-key", engine_factory=Engine)


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_usage_unknown_is_not_zero_cost_and_known_pricing_is_estimated():
    assert "brak danych" in usage_text({}, {}, "pl")
    assert "koszt: brak danych" in usage_text({"input_tokens": 10, "output_tokens": 3}, {}, "pl")
    value = usage_text({"input_tokens": 1_000_000, "output_tokens": 500_000}, {"pricing": {"input_per_million": 2, "output_per_million": 4}}, "pl")
    assert "4.000000" in value and "szacunkowy" in value


def test_usage_copilot_observations_do_not_become_dollar_cost():
    result = usage_text({"input_tokens": 10, "output_tokens": 3,
        "copilot": {"total_nano_aiu": 1200, "unit": "nano_aiu", "status": "partial", "reported_calls": 1, "observed_calls": 2},
        "provider_observations": {"cost": 0.03, "cache_read_tokens": 2}}, {}, "pl")
    assert "1200 nano_aiu" in result and "niepełny" in result
    assert "1/2" in result and "brak danych" in result
    assert "USD" not in result and "jednostki dostawcy" in result
    assert '"cache_read_tokens": 2' in result


def test_incomplete_usage_never_computes_a_complete_price_estimate():
    result = usage_text({"input_tokens": 100, "output_tokens": 5, "usage_complete": False,
                         "estimated_cost": None}, {"pricing": {"input_per_million": 2, "output_per_million": 4}}, "pl")
    assert "raport tokenów niepełny" in result
    assert "koszt: brak danych" in result and "USD" not in result


def test_partial_provider_output_is_labeled_and_completed_streams_are_excluded():
    events = [
        {"type": "provider_stream", "data": {"request_id": "done", "text": "already in complete message"}},
        {"type": "provider_completed", "data": {"request_id": "done"}},
        {"type": "provider_stream", "data": {"request_id": "failed", "text": "unfinished "}},
        {"type": "provider_stream", "data": {"request_id": "failed", "text": "answer"}},
    ]
    assert incomplete_streams(events) == [{"request_id": "failed", "text": "unfinished answer", "truncated": False}]


@pytest.mark.asyncio
async def test_startup_restores_literal_history_without_inference_or_key_display(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        assert app.engine.turns == []
        assert app._key is None
        history = app.query_one("#agent-history", RichLog)
        lines = "\n".join(line.text for line in history.lines)
        assert "Earlier question" in lines
        assert "\\x1b]52" in lines and "\x1b" not in lines
        assert "fixture-key" not in lines
        assert not app.query_one("#agent-send", Button).disabled
    assert app.engine.closed


@pytest.mark.asyncio
async def test_send_streams_once_and_usage_remains_unknown_without_price(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        app.query_one("#agent-prompt", TextArea).text = "Implement safely"
        await pilot.click("#agent-send")
        await settle(app, pilot)
        assert app.engine.turns == ["Implement safely"]
        assert app.query_one("#agent-prompt", TextArea).text == ""
        assert "cost: unavailable" in str(app.query_one("#agent-usage", Static).render())
        assert app.query_one("#agent-stream", Static).render().plain == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["keys", "paste"])
async def test_submit_waits_for_preceding_textarea_input_queue_without_delay(tmp_path, delivery):
    from textual import events
    app = make_app(tmp_path)
    message = "Read all prompt characters before submitting"
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#agent-prompt", TextArea)
        if delivery == "keys":
            for character in message:
                prompt._forward_event(events.Key("space" if character == " " else character, character))
        else:
            prompt._forward_event(events.Paste(message))
        # Deliberately no pause/yield: priority App action races child input.
        app.action_send()
        app.action_send()
        await pilot.pause()
        await settle(app, pilot)
        assert app.engine.turns == [message]
        assert prompt.text == ""


@pytest.mark.asyncio
async def test_approval_rejects_stale_or_missing_identity_without_dialog(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        called = []
        async def never(screen):
            called.append(screen)
            return True
        monkeypatch.setattr(app, "push_screen_wait", never)
        request = {"action_id": "action-a", "command": "pytest", "cwd": str(tmp_path / "project"),
                   "run_id": "run-a", "generation": 2, "session_id": "session-a"}
        assert await app.approve({**request, "generation": 1}) is False
        assert await app.approve({**request, "generation": True}) is False
        assert await app.approve({k: v for k, v in request.items() if k != "generation"}) is False
        assert await app.approve({k: v for k, v in request.items() if k != "run_id"}) is False
        assert await app.approve({**request, "session_id": "foreign"}) is False
        assert called == []
        assert await app.approve(request) is True
        assert len(called) == 1
        assert "pytest" in called[0].value


@pytest.mark.asyncio
async def test_pending_action_requires_human_note_and_does_not_execute_again(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        app.engine.pending = [{"action_id": "uncertain-a", "command": "touch result"}]
        await app.refresh_pending()
        assert not app.query_one("#agent-resolve", Button).disabled
        choices = iter(["uncertain-a", {"note": "Sprawdziłem plik: istnieje."}])
        async def choose(screen):
            value = next(choices)
            if getattr(screen, "on_submit", None):
                await screen.on_submit(value)
            return value
        monkeypatch.setattr(app, "push_screen_wait", choose)
        app.resolve_pending()
        await settle(app, pilot)
        assert app.engine.resolved == [("uncertain-a", "Sprawdziłem plik: istnieje.")]
        assert app.engine.turns == []
        assert app.query_one("#agent-resolve", Button).disabled


@pytest.mark.asyncio
async def test_interrupt_stops_busy_turn_without_starting_another(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        app.engine.wait = asyncio.Event()
        app.query_one("#agent-prompt", TextArea).text = "Wait"
        await pilot.click("#agent-send")
        await pilot.pause()
        assert app._busy
        await pilot.press("ctrl+c")
        await settle(app, pilot)
        assert app.engine.interrupts == 1
        assert app.engine.turns == ["Wait"]
        assert not app._busy


@pytest.mark.asyncio
async def test_provider_and_tool_control_text_and_key_are_literal(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(95, 42)) as pilot:
        await pilot.pause()
        await app.agent_event({"type": "tool_result", "data": {"content": "fixture-key\x1b]52;c;secret\x07\u202e"}})
        lines = "\n".join(line.text for line in app.query_one("#agent-history", RichLog).lines)
        assert "fixture-key" not in lines and "[REDACTED]" in lines
        assert "\x1b" not in lines and "\u202e" not in lines


@pytest.mark.parametrize("delivery", ["keys", "bracketed_paste"])
def test_real_tmux_panel_restores_then_requires_human_command_approval(tmp_path, delivery):
    """Real terminal/keyboard path, with an injected engine and no paid calls."""
    import os
    import shutil
    import subprocess
    import sys
    import time

    tmux = os.environ.get("DEVBRAIN_TEST_TMUX") or shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux is required for the real terminal case")
    socket = tmp_path / "agent.sock"
    marker = tmp_path / "observed.txt"
    script = tmp_path / "controlled_worker.py"
    script.write_text('''import asyncio
from pathlib import Path
from cairnmind.api_worker import AgentApp
from cairnmind.config import Settings
class Journal:
    def messages(self):
        return [{"role":"assistant","content":"CONTROLLED-NO-INFERENCE"}]
class Engine:
    def __init__(self,*args,**kwargs):
        self.journal=Journal()
        self.callbacks=kwargs
    def pending_actions(self): return []
    async def turn(self,text):
        Path(MARKER).write_text("turn-started:"+text)
        result=await self.callbacks["approve"]({"action_id":"fixture-action","command":"CONTROLLED-COMMAND","cwd":ROOT,"run_id":"fixture-run","generation":1,"session_id":"fixture-session"})
        Path(MARKER).write_text("approved" if result else "denied")
        await self.callbacks["on_event"]({"type":"message","data":{"role":"assistant","content":"DECISION-RECORDED"}})
        return {"status":"complete"}
    async def interrupt(self): pass
    async def close(self): pass
ROOT=ROOT_VALUE
MARKER=MARKER_VALUE
run={"id":"fixture-run","generation":1,"worktree":ROOT,"project_id":"fixture-project","session_id":"fixture-session","client":"api"}
AgentApp(Settings(data_dir=Path(ROOT)/"data"),run,{"id":"fixture","label":"Controlled API","client":"api","model":"fixture-model"},engine_factory=Engine).run()
'''.replace("ROOT_VALUE", repr(str(tmp_path))).replace("MARKER_VALUE", repr(str(marker))))
    def command(*args):
        return subprocess.run([tmux, "-S", str(socket), *args], capture_output=True, text=True, timeout=5, check=True)
    def screen_until(text, timeout=15):
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            last = command("capture-pane", "-p", "-t", "agent").stdout
            if text in last:
                return last
            time.sleep(.05)
        raise AssertionError(f"Missing {text!r} in actual terminal:\n{last}")
    try:
        command("-f", "/dev/null", "new-session", "-d", "-x", "120", "-y", "40", "-s", "agent", sys.executable, "-I", str(script))
        startup = screen_until("CONTROLLED-NO-INFERENCE")
        (tmp_path / "startup.capture.txt").write_text(startup)
        assert "fixture-model" in startup
        assert not marker.exists()
        if delivery == "bracketed_paste":
            command("send-keys", "-t", "agent", "-H", "1b", "5b", "32", "30", "30", "7e")
        command("send-keys", "-t", "agent", "-l", "Exercise controlled approval")
        if delivery == "bracketed_paste":
            command("send-keys", "-t", "agent", "-H", "1b", "5b", "32", "30", "31", "7e")
        # CSI-u ctrl+enter works across terminal emulators without a mouse.
        command("send-keys", "-t", "agent", "-H", "1b", "5b", "31", "33", "3b", "35", "75")
        dialog = screen_until("CONTROLLED-COMMAND")
        (tmp_path / "approval.capture.txt").write_text(dialog)
        assert "Approve one command" in dialog
        assert marker.read_text() == "turn-started:Exercise controlled approval"
        # Escape is a denial. The command itself is never executed by fixture.
        command("send-keys", "-t", "agent", "Escape")
        (tmp_path / "completed.capture.txt").write_text(screen_until("DECISION-RECORDED"))
        assert marker.read_text() == "denied"
    finally:
        if socket.exists():
            subprocess.run([tmux, "-S", str(socket), "kill-server"], capture_output=True, timeout=5)


@pytest.mark.parametrize("language", ["en", "pl"])
def test_usage_language_keeps_provider_units_and_never_invents_usd(language):
    value = usage_text({"usage_complete": False, "copilot": {"total_nano_aiu": 90, "status": "partial"},
                        "provider_observations": {"credits": 3}}, {}, language)
    assert "90 nano_aiu" in value and '"credits": 3' in value and "USD" not in value
    assert ("incomplete" if language == "en" else "niepełny") in value


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_keyboard_only_send_interrupt_resolve_at_80x24(tmp_path, language):
    from cairnmind.cockpit_state import CockpitState
    app = make_app(tmp_path)
    CockpitState(app.settings).update({"language": language})
    app.language = language
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.engine.turns == []
        assert str(app.query_one("#agent-send", Button).label) == ("Send" if language == "en" else "Wyślij")
        await pilot.press(*list("First message"), "ctrl+enter")
        await settle(app, pilot)
        assert app.engine.turns == ["First message"]
        app.engine.wait = asyncio.Event()
        await pilot.press(*list("Wait"), "ctrl+enter")
        await pilot.pause()
        assert app._busy
        await pilot.press("ctrl+c")
        await settle(app, pilot)
        assert app.engine.interrupts == 1 and app.engine.turns == ["First message", "Wait"]
        app.engine.pending = [{"action_id": "uncertain-a", "command": "synthetic action"}]
        await app.refresh_pending()
        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*list("Checked: no change."), "ctrl+s")
        await settle(app, pilot)
        assert app.engine.resolved == [("uncertain-a", "Checked: no change.")]
        assert app.engine.turns == ["First message", "Wait"]
        lines = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert "fixture-key" not in lines and "Traceback" not in lines
        assert "finding was saved" in lines if language == "en" else "Zapisano ustalenie" in lines


@pytest.mark.asyncio
async def test_language_change_preserves_prompt_engine_and_updates_usage(tmp_path):
    from cairnmind.cockpit_state import CockpitState
    app = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        engine = app.engine
        await pilot.press(*list("Unsent text"))
        app.update_usage({"input_tokens": 7, "output_tokens": 2})
        CockpitState(app.settings).update({"language": "pl"})
        await app.check_language()
        assert app.engine is engine and engine.turns == []
        assert app.query_one("#agent-prompt", TextArea).text == "Unsent text"
        assert "wejście 7" in str(app.query_one("#agent-usage", Static).render())
        assert str(app.query_one("#agent-send", Button).label) == "Wyślij"


@pytest.mark.asyncio
async def test_resolution_failure_preserves_note_and_never_retries_automatically(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.engine.pending = [{"action_id": "uncertain-a", "command": "synthetic action"}]
        calls = []
        def unavailable(action_id, note):
            calls.append((action_id, note))
            raise RuntimeError("Controlled unavailable journal")
        monkeypatch.setattr(app.engine, "resolve_uncertain", unavailable)
        await app.refresh_pending()
        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*list("Keep this human finding"), "ctrl+s")
        await pilot.pause(.3)
        assert app.screen.query_one("#field-note", TextArea).text == "Keep this human finding"
        assert "Controlled unavailable" in str(app.screen.query_one("#form-error", Static).render())
        await pilot.pause(.3)
        assert calls == [("uncertain-a", "Keep this human finding")]
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.engine.turns == [] and app.engine.resolved == []
