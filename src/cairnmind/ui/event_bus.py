"""In-memory typed SSE event bus and audit ring buffer for the web UI."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import suppress
from copy import deepcopy
from typing import Any, Literal
from uuid import uuid4

from cairnmind.ui.schemas import utc_now as _utc_now

MAX_BUS_EVENTS = 250

UiEventType = Literal[
    "state.updated",
    "session.ack",
    "evidence.stale",
    "mcp.write.accepted",
    "mcp.write.rejected",
    "repo_fingerprint_changed",
    "reindex.progress",
    "plan.conflict",
]


class UiEventBus:
    """In-memory typed SSE fan-out ring buffer for UI events and live MCP audit signals."""

    def __init__(self, maxlen: int = MAX_BUS_EVENTS) -> None:
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._audit: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    def publish(self, event_type: UiEventType, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            envelope = {
                "id": f"ui-{self._seq}",
                "seq": self._seq,
                "type": event_type,
                "timestamp": _utc_now(),
                "payload": deepcopy(payload),
            }
            self._events.append(envelope)
            subs = list(self._subscribers)
        for loop, queue in subs:
            if not loop.is_closed():
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._enqueue, queue, envelope)
        return envelope

    @staticmethod
    def _enqueue(queue: asyncio.Queue, envelope: dict[str, Any]) -> None:
        if queue.full():
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with suppress(asyncio.QueueFull):
            queue.put_nowait(envelope)

    def record_audit(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = {
            "id": entry.get("id") or f"aud-{uuid4().hex[:8]}",
            "timestamp": entry.get("timestamp") or _utc_now(),
            **entry,
        }
        with self._lock:
            self._audit.appendleft(item)
        event_type: UiEventType = (
            "mcp.write.accepted" if item.get("verdict") == "ACCEPTED" else "mcp.write.rejected"
        )
        self.publish(event_type, {
            "audit_id": item["id"],
            "session_id": item.get("session_id"),
            "client": item.get("client"),
            "mcp_tool": item.get("mcp_tool"),
            "verdict": item.get("verdict"),
            "reason_code": item.get("reason_code"),
            "message": item.get("message"),
            "target_id": item.get("target_id"),
            "entry": item,
        })
        return item

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._audit)[:limit]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add((loop, queue))
        return loop, queue

    def unsubscribe(self, token: tuple[asyncio.AbstractEventLoop, asyncio.Queue]) -> None:
        with self._lock:
            self._subscribers.discard(token)
