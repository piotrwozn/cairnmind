"""Measure real panel input/render latency while its synchronous broker is slow.

This headless check covers Textual's event loop, input routing and refresh. It
does not claim to measure Windows Terminal's display or human task completion.
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time

import pytest
from textual import events, on
from textual.widgets import DataTable, Input

from devbrain.terminal_panels import SidebarApp
from test_terminal_panels import settle
from test_terminal_ux080 import setup_ui

# Budgets target the developer workstation. Shared CI runners set a larger scale; it stays
# below the 3 s controlled backend delay, so a UI blocked on the backend still fails.
SCALE = float(os.environ.get("DEVBRAIN_LATENCY_SCALE", "1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (180, 55)])
async def test_local_input_remains_responsive_during_three_second_backend_delay(
    tmp_path, monkeypatch, record_property, size,
):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch)
    # Exercise search and layout with actual long Unicode project rows.
    broker.projects.extend({
        "id": f"project-{index}",
        "name": f"Project {index:03} — Zażółć 日本語 " + "long name " * 8,
        "root": str(tmp_path / f"project-{index}"),
    } for index in range(250))
    original_request = broker.request
    slow = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    delay_claim = threading.Lock()

    def request(method, path, **kwargs):
        with delay_claim:
            delay_this_request = (slow.is_set() and not started.is_set()
                                  and method == "GET" and path == "/api/state")
            if delay_this_request:
                started.set()
        try:
            if delay_this_request:
                time.sleep(3)
            return original_request(method, path, **kwargs)
        finally:
            if delay_this_request:
                finished.set()

    monkeypatch.setattr(broker, "request", request)

    class MeasuredSidebar(SidebarApp):
        sent_at = 0.0
        expected_value = None

        @on(Input.Changed, "#project-search")
        def measure_input(self, message):
            self.call_after_refresh(self.measure_refresh, message.value)

        def measure_refresh(self, value):
            if value == self.expected_value and self.sent_at:
                latencies.append(time.perf_counter() - self.sent_at)
                self.sent_at = 0.0
                acknowledged.set()

    latencies = []
    loop_intervals = []
    acknowledged = asyncio.Event()
    app = MeasuredSidebar(settings, broker=broker, workspace=workspace)
    monitor = None

    async def monitor_loop():
        while True:
            before = time.perf_counter()
            await asyncio.sleep(.01)
            loop_intervals.append(time.perf_counter() - before)

    try:
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            search = app.query_one("#project-search", Input)
            search.focus()
            await pilot.pause()
            monitor = asyncio.create_task(monitor_loop())
            # Ten samples per three-second window allow even the permitted
            # P95=150ms plus two <=500ms outliers across the full 50 samples.
            # A single window for all 50 would silently impose a stricter 60ms
            # average instead of the user's actual latency budget.
            for _ in range(5):
                started.clear()
                finished.clear()
                slow.set()
                app.refresh_sidebar()
                assert await asyncio.to_thread(started.wait, 1), "Slow backend request never started"
                for index in range(10):
                    assert not finished.is_set(), "Input samples exceeded the controlled backend delay"
                    key, character = ("ż", "ż") if index % 2 == 0 else ("backspace", None)
                    acknowledged.clear()
                    app.expected_value = search.value + "ż" if character else search.value[:-1]
                    app.sent_at = time.perf_counter()
                    # Use the same App → focused widget route as received keys.
                    app.post_message(events.Key(key, character))
                    await asyncio.wait_for(acknowledged.wait(), timeout=.5 * SCALE)
                    assert search.value == app.expected_value
                    # Both the Unicode query and its removal retain the long
                    # rows, rather than benchmarking an empty search result.
                    assert app.query_one("#project-rows", DataTable).row_count >= 250
                assert await asyncio.to_thread(finished.wait, 4), "Slow backend did not finish"
                slow.clear()
                await settle(app, pilot)
    finally:
        slow.clear()
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        # Never leave an unobserved delayed worker running after an assertion.
        if started.is_set():
            assert await asyncio.to_thread(finished.wait, 4), "Backend worker remained alive"

    assert len(latencies) == 50
    p95 = sorted(latencies)[math.ceil(.95 * len(latencies)) - 1]
    maximum_loop_interval = max(loop_intervals)
    record_property("keyboard_samples", len(latencies))
    record_property("keyboard_to_refresh_p95_ms", round(p95 * 1000, 3))
    record_property("maximum_ui_loop_interval_ms", round(maximum_loop_interval * 1000, 3))
    record_property("controlled_backend_delay_seconds", 3)
    assert p95 <= .150 * SCALE, (
        f"Keyboard-to-refresh P95 {p95 * 1000:.1f} ms exceeds {150 * SCALE:.0f} ms"
    )
    assert maximum_loop_interval <= .500 * SCALE, (
        f"UI loop stalled for {maximum_loop_interval * 1000:.1f} ms, exceeding {500 * SCALE:.0f} ms"
    )
