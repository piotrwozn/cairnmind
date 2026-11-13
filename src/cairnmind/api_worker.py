"""Terminal conversation for a run-scoped API or Copilot programming agent.

This is an operational process inside a private tmux pane. It never loads a
stored key into the visible UI, performs inference on startup, or treats a
terminal attachment as a memory acknowledgement.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import replace
import inspect
import json
import math
from pathlib import Path
import re
import sys

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, RichLog, Static, TextArea

from .config import Settings
from .terminal_panels import ConfirmModal
from .terminal_safe import plain, safe_text
from .terminal_ui import ChoiceModal, Field, FormModal
from .terminal_i18n import language_for
from .terminal_agent_i18n import status_text, tr

_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")


def usage_text(usage: dict, profile: dict, language="en") -> str:
    """Show observed counters without inventing completeness or currency."""
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    def t(text, **values):
        return tr(text, language, **values)
    parts = [t("input") + " " + (str(input_tokens) if type(input_tokens) is int else t("unavailable")),
             t("output") + " " + (str(output_tokens) if type(output_tokens) is int else t("unavailable"))]
    complete = usage.get("usage_complete", True) is True
    if not complete:
        parts.append(t("token report incomplete"))
    pricing = profile.get("pricing") or {}
    estimate = usage.get("estimated_cost")
    if not (complete and usage.get("cost_currency") == "USD" and type(estimate) in (int, float)
            and math.isfinite(estimate) and estimate >= 0):
        estimate = None
        if complete and all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in (
                input_tokens, output_tokens, pricing.get("input_per_million"), pricing.get("output_per_million"))):
            estimate = (input_tokens * pricing["input_per_million"] + output_tokens * pricing["output_per_million"]) / 1_000_000
    if estimate is not None:
        parts.append(t("estimated cost {amount:.6f} USD (profile rates)", amount=estimate))
    else:
        parts.append(t("cost: unavailable"))
    copilot = usage.get("copilot")
    if isinstance(copilot, dict):
        amount = copilot.get("total_nano_aiu")
        state = t(copilot.get("status") if copilot.get("status") in {"complete", "partial", "unavailable"} else "observed")
        if type(amount) in (int, float) and math.isfinite(amount) and amount >= 0:
            parts.append(t("Copilot: {amount} nano_aiu · report {state}", amount=amount, state=state))
        else:
            parts.append(t("Copilot: usage unavailable"))
        if type(copilot.get("reported_calls")) is int and type(copilot.get("observed_calls")) is int:
            parts.append(t("Copilot reports {reported}/{observed}", reported=copilot['reported_calls'], observed=copilot['observed_calls']))
    raw_observations = usage.get("provider_observations")
    observations = dict(raw_observations) if isinstance(raw_observations, dict) else {}
    if type(usage.get("premium_requests")) in (int, float):
        observations["premium_requests"] = usage["premium_requests"]
    observed = {key: value for key, value in observations.items()
                if key in {"cost", "cache_read_tokens", "cache_write_tokens", "premium_requests", "credits"}
                and type(value) in (int, float) and math.isfinite(value) and value >= 0}
    if observed:
        parts.append(t("provider report (provider units): ") + json.dumps(observed, ensure_ascii=False))
    return " · ".join(parts)


def incomplete_streams(events):
    """Return explicitly incomplete observed output, never complete messages."""
    completed, groups = set(), {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            continue
        data = event["data"]
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        if event.get("type") == "provider_completed":
            completed.add(request_id)
        elif event.get("type") == "provider_stream" and isinstance(data.get("text"), str):
            current = groups.setdefault(request_id, {"request_id": request_id, "text": "", "truncated": False})
            combined = current["text"] + data["text"]
            current["truncated"] = current["truncated"] or len(combined) > 131072
            current["text"] = combined[-131072:]
    return [value for request_id, value in groups.items() if request_id not in completed][-20:]


class AgentApp(App):
    TITLE = "Cairnmind · Agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #101816; color: #d4dfd3; }
    Header, Footer { background: #1c2a20; color: #c3df9b; }
    #agent-main { height: 1fr; }
    #agent-scope, #agent-status, #agent-usage { height: auto; max-height: 4; padding: 0 1; }
    #agent-scope { color: #c3df9b; }
    #agent-status { color: #dcb78c; }
    #agent-history { height: 1fr; min-height: 3; background: #101816; }
    #agent-stream { height: auto; max-height: 8; padding: 0 1; }
    #agent-prompt { height: 5; min-height: 3; border: tall #344834; background: #101a15; }
    #agent-actions { height: 3; }
    #agent-actions Button { min-width: 10; width: 1fr; margin-right: 0; border: tall #344834; }
    """
    BINDINGS = [Binding("ctrl+c", "interrupt_turn", "Interrupt", priority=True),
                Binding("ctrl+enter", "send", "Send", priority=True),
                Binding("ctrl+r", "resolve", "Resolve", priority=True)]

    def __init__(self, settings, run, profile, *, key=None, engine_factory=None, mcp_servers=None, mcp_problems=None):
        super().__init__()
        self.settings, self.run_record, self.profile = settings, dict(run), dict(profile)
        # Third-party MCP servers fixed for this run by the launcher (#126); API runs only.
        self.mcp_servers = list(mcp_servers or []) if self.run_record.get("client") == "api" else []
        self.mcp_problems = [row for row in (mcp_problems or []) if isinstance(row, dict)]
        self.language = language_for(settings)
        self._status_value = ("Restoring the local journal; no model is running…", {})
        self._last_usage = None
        self._key = key
        self._engine_factory = engine_factory
        self.engine = None
        self._turn_task = None
        self._approval_screen = None
        self._stream = ""
        self._busy = False
        self._queued = False
        self._closed = False
        self._redacted_values = [key] if isinstance(key, str) and key else []

    def t(self, text, **values):
        return tr(text, self.language, **values)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="agent-main"):
            yield Static(plain(self.profile.get("label", "Agent") + " · " + (self.profile.get("model") or self.t("default model")) + "\n" + self.run_record["worktree"]), id="agent-scope")
            yield Static(self.t("Restoring the local journal; no model is running…"), id="agent-status", markup=False)
            yield RichLog(id="agent-history", wrap=True, markup=False, highlight=False, max_lines=5000)
            yield Static("", id="agent-stream", markup=False)
            yield Static(self.t("usage: unavailable · cost: unavailable"), id="agent-usage", markup=False)
            yield TextArea(id="agent-prompt")
            with Horizontal(id="agent-actions"):
                yield Button(self.t("Send"), id="agent-send", variant="primary", disabled=True)
                yield Button(self.t("Interrupt"), id="agent-interrupt", disabled=True)
                yield Button(self.t("Resolve"), id="agent-resolve", disabled=True)
        yield Footer()

    async def on_mount(self):
        self.refresh_language()
        self.set_interval(.5, self.check_language)
        try:
            if self._engine_factory is None:
                from .api_agent import AgentEngine
                factory = AgentEngine
            else:
                factory = self._engine_factory
            extra = {"mcp_servers": self.mcp_servers} if self.mcp_servers else {}
            self.engine = factory(self.settings, self.run_record, self.profile, key=self._key,
                                  approve=self.approve, on_delta=self.on_delta, on_event=self.agent_event, **extra)
            self._key = None
            # The journal is already redacted by the engine. Defend the display
            # boundary as well, including controlled/test engine callbacks.
            journal = getattr(self.engine, "journal", None)
            messages = await asyncio.to_thread(journal.messages) if journal is not None else []
            for message in messages[-200:]:
                self.write_message(message)
            if journal is not None and callable(getattr(journal, "events", None)):
                events = await asyncio.to_thread(journal.events)
                for observed in incomplete_streams(events):
                    suffix = self.t(" (visible excerpt limited)") if observed["truncated"] else ""
                    self.write_message({"role": self.t("Incomplete response") + suffix,
                        "content": self.t("Request {request_id} — this output is not a confirmed response.\n", request_id=observed["request_id"]) + observed["text"]})
            self.set_status("Ready. Send a message to start work and restore memory.")
            for problem in self.mcp_problems:
                self.mcp_server_off(problem.get("server_id"), problem.get("code"))
            await self.refresh_pending()
            self.query_one("#agent-send", Button).disabled = False
            self.query_one("#agent-prompt", TextArea).focus()
        except Exception:
            self._key = None
            self.set_status("Cannot prepare the agent. Check the profile and services in Connections.")

    async def check_language(self):
        language = await asyncio.to_thread(language_for, self.settings)
        if language != self.language:
            self.language = language
            self.refresh_language()

    def refresh_language(self):
        for name, label in (("send", "Send"), ("interrupt", "Interrupt"), ("resolve", "Resolve")):
            self.query_one("#agent-" + name, Button).label = self.t(label)
        for key, bindings in self._bindings.key_to_bindings.items():
            self._bindings.key_to_bindings[key] = [replace(binding, description=self.t(
                {"send": "Send", "interrupt_turn": "Interrupt", "resolve": "Resolve"}.get(binding.action, binding.description)))
                for binding in bindings]
        self.refresh_bindings()
        self.set_status(self._status_value[0], **self._status_value[1])
        self.update_usage(self._last_usage)

    def update_usage(self, value):
        self._last_usage = value
        text = usage_text(value, self.profile, self.language) if isinstance(value, dict) else self.t("usage: unavailable · cost: unavailable")
        self.query_one("#agent-usage", Static).update(self.display_text(text, limit=3000))

    def display_text(self, value, *, limit=65536):
        value = str(value)
        for secret in self._redacted_values:
            value = value.replace(secret, "[REDACTED]")
        return plain(value, limit=limit)

    def set_status(self, value, **values):
        self._status_value = (value, values)
        self.query_one("#agent-status", Static).update(self.display_text(self.t(value, **values), limit=3000))

    def write_message(self, message):
        if not isinstance(message, dict):
            return
        role = str(message.get("role", "event"))
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        label = self.t({"user": "You", "assistant": "Agent", "tool": "Tool", "system": "System"}.get(role, role))
        if message.get("name"):
            label += " · " + str(message["name"])
        self.query_one("#agent-history", RichLog).write(self.display_text(label + ":\n" + content))

    async def on_delta(self, text):
        if self._closed:
            return
        self._stream = (self._stream + str(text))[-131072:]
        self.query_one("#agent-stream", Static).update(self.display_text(self._stream, limit=131072))

    async def agent_event(self, event):
        if self._closed or not isinstance(event, dict):
            return
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "message":
            self.write_message(data)
            if data.get("role") == "assistant":
                self._stream = ""
                self.query_one("#agent-stream", Static).update("")
        elif kind == "provider_event" and data.get("type") == "usage" and isinstance(data.get("usage"), dict):
            self.update_usage(data["usage"])
        elif kind == "usage":
            self.update_usage(data)
        elif kind in {"status", "memory_required", "context_compacted", "error"}:
            self.set_status(data.get("message", data.get("status", data.get("code", json.dumps(data, ensure_ascii=False)))))
        elif kind == "mcp_server_failed":
            self.mcp_server_off(data.get("server_id"), data.get("code"))
        elif kind in {"tool_start", "tool_result"}:
            # Results are visible as literal data, never terminal control codes.
            label = self.t("Tool started" if kind == "tool_start" else "Tool result")
            self.query_one("#agent-history", RichLog).write(self.display_text(label + "\n" + json.dumps(data, ensure_ascii=False, default=str)))

    def mcp_server_off(self, server_id, code):
        """A fixed code and a validated id only: never text from the server."""
        server = server_id if isinstance(server_id, str) and _SERVER_ID.fullmatch(server_id) else "unknown"
        code = code if isinstance(code, str) and _CODE.fullmatch(code) else "unknown"
        text = self.t("Third-party MCP server {server} is off ({code}); the agent works without its tools.",
                      server=server, code=code)
        self.set_status(text)
        self.query_one("#agent-history", RichLog).write(self.display_text(text))

    async def approve(self, request):
        # Every decision remains bound to the exact current run and generation.
        if (not isinstance(request, dict) or request.get("run_id") != self.run_record.get("id")
                or type(request.get("generation")) is not int
                or request.get("generation") != self.run_record.get("generation")):
            return False
        if request.get("session_id") != self.run_record.get("session_id"):
            return False
        if request.get("kind") == "mcp_tool":
            # One call of a third-party tool: server, tool and the exact arguments, approved once (#126).
            if (not request.get("action_id") or not isinstance(request.get("server_id"), str)
                    or not isinstance(request.get("tool"), str) or not isinstance(request.get("name"), str)
                    or not isinstance(request.get("arguments"), dict)):
                return False
            title, note, button = ("Approve one third-party tool call",
                                   "This sends exactly these arguments to a program you configured. Its answer "
                                   "is untrusted third-party text.", "Call once")
        elif not request.get("action_id") or not isinstance(request.get("command"), str):
            return False
        else:
            title, note, button = ("Approve one command", "This command runs with your user account permissions.",
                                   "Run once")
        self._approval_screen = ConfirmModal(self.t(title), self.display_text(json.dumps(request, ensure_ascii=False, indent=2)).plain + "\n\n" + self.t(note), approve=self.t(button))
        try:
            return await self.push_screen_wait(self._approval_screen) is True
        finally:
            self._approval_screen = None

    def set_busy(self, busy):
        self._busy = busy
        self.query_one("#agent-send", Button).disabled = busy or self.engine is None
        self.query_one("#agent-interrupt", Button).disabled = not busy
        self.query_one("#agent-resolve", Button).disabled = busy

    @work(group="agent-turn", exclusive=True, exit_on_error=False)
    async def execute_turn(self, text):
        self._queued = False
        if self.engine is None or self._busy:
            return
        self._turn_task = asyncio.current_task()
        self.set_busy(True)
        self._stream = ""
        self.query_one("#agent-stream", Static).update("")
        self.set_status("Agent is working…")
        try:
            result = await self.engine.turn(text)
            if isinstance(result, dict):
                details = [status_text(result.get("status", "completed"), self.language)]
                for field in ("message", "error_code"):
                    if result.get(field):
                        details.append(str(result[field]))
                if result.get("unknowns"):
                    details.append("; ".join(str(v) for v in result["unknowns"]))
                self.set_status(self.t("Status: ") + " · ".join(details))
                if result.get("unknowns"):
                    self.query_one("#agent-history", RichLog).write(self.display_text(self.t("Missing evidence / blockers:\n") + "\n".join(str(v) for v in result["unknowns"])))
                if self._stream and result.get("status") not in {"completed", "complete"}:
                    self.query_one("#agent-stream", Static).update(self.display_text(self.t("Incomplete response — unconfirmed output:\n") + self._stream, limit=131072))
                if isinstance(result.get("usage"), dict):
                    self.update_usage(result["usage"])
        except asyncio.CancelledError:
            self.set_status("Interrupted. Check uncertain actions; they will not be retried automatically.")
        except Exception:
            self.set_status("The operation did not finish. Check the journal and connection status.")
        finally:
            self._turn_task = None
            self.set_busy(False)
            await self.refresh_pending()

    def action_send(self):
        if self._busy or self._queued or self.engine is None:
            return
        # The App's priority Ctrl+Enter binding may run before earlier Key or
        # Paste messages queued in the TextArea. Capture after that child's
        # queue barrier, never by sleeping or guessing input timing.
        self._queued = True
        prompt = self.query_one("#agent-prompt", TextArea)
        if not prompt.call_later(self._submit_prompt):
            self._queued = False

    def _submit_prompt(self):
        if not self._queued or self._busy or self.engine is None:
            self._queued = False
            return
        prompt = self.query_one("#agent-prompt", TextArea)
        text = prompt.text.strip()
        if text:
            prompt.text = ""
            self.execute_turn(text)
        else:
            self._queued = False

    @work(group="agent-interrupt", exclusive=True, exit_on_error=False)
    async def action_interrupt_turn(self):
        if self._queued and not self._busy:
            self._queued = False
            self.set_status("Message submission cancelled before the turn started.")
        if self._approval_screen is not None:
            self._approval_screen.dismiss(False)
        if self.engine is not None and self._busy:
            result = self.engine.interrupt()
            if inspect.isawaitable(result):
                await result
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()
            self.set_status("Interrupting the current operation…")

    async def refresh_pending(self):
        pending = self.engine.pending_actions() if self.engine is not None else []
        if inspect.isawaitable(pending):
            pending = await pending
        self.query_one("#agent-resolve", Button).disabled = self._busy or not pending
        if pending:
            self.set_status("Uncertain actions: {count}. Check their outcome and use Resolve before continuing.", count=len(pending))
        return pending

    @work(group="agent-resolve", exclusive=True, exit_on_error=False)
    async def resolve_pending(self):
        if self._busy or self.engine is None:
            return
        pending = await self.refresh_pending()
        if not pending:
            return
        choices = []
        for action in pending:
            action_id = action.get("action_id", action.get("id"))
            if action_id:
                choices.append((safe_text(str(action_id) + " · " + str(action.get("command", action.get("name", action.get("tool", self.t("action")))))), action_id))
        selected = await self.push_screen_wait(ChoiceModal(self.t("Action with an unconfirmed outcome"), choices))
        if selected is None:
            return
        async def save_finding(values):
            result = self.engine.resolve_uncertain(selected, values["note"])
            if inspect.isawaitable(result):
                await result
        note = await self.push_screen_wait(FormModal(self.t("Resolve after checking the outcome"), [Field("note", self.t("What did you check, and what actually happened?"), required=True, multiline=True)], note=self.t("This saves your finding. It does not repeat the command or edit."), submit=self.t("Save finding"), on_submit=save_finding))
        if note:
            await self.refresh_pending()
            self.set_status("Your finding was saved. Send a separate message to continue work.")

    def action_resolve(self):
        self.resolve_pending()

    @on(Button.Pressed)
    def button_action(self, event):
        actions = {"agent-send": self.action_send, "agent-interrupt": self.action_interrupt_turn, "agent-resolve": self.resolve_pending}
        callback = actions.get(event.button.id)
        if callback:
            callback()

    async def on_unmount(self):
        self._closed = True
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self.engine is not None:
            with suppress(Exception):
                result = self.engine.close()
                if inspect.isawaitable(result):
                    await result
        self._key = None
        self._redacted_values.clear()


def main():
    parser = argparse.ArgumentParser(description="Prywatny terminal agenta API Cairnmind")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("A terminal is required. Start cairnmind.")
    settings = None
    try:
        from .api_runtime import api_worker_config
        settings = Settings.load(args.data_dir, create=False)
        config = api_worker_config(settings, args.run_id, args.generation)
        run = config["run"]
        if run.get("id") != args.run_id or run.get("generation") != args.generation:
            raise ValueError("worker scope mismatch")
        app = AgentApp(settings, run, config["profile"], key=config.get("key"),
                       mcp_servers=config.get("mcp_servers"), mcp_problems=config.get("mcp_problems"))
        config.pop("key", None)
    except Exception:
        raise SystemExit(tr("Cannot restore this run. Check the profile status in Cairnmind.", language_for(settings) if settings else "en")) from None
    app.run()


if __name__ == "__main__":
    main()
