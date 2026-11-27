"""Compatibility facade re-exporting public UI server symbols without loading mock fixtures."""
from __future__ import annotations

from typing import Any

from cairnmind.ui.event_bus import MAX_BUS_EVENTS, UiEventBus, UiEventType
from cairnmind.ui.graph import (
    MAX_CONTEXT_FILE_BYTES,
    MAX_CONTEXT_LINES,
    _build_graph_from_entities,
    _classify_reason_code,
    _find_quote_lines,
    _sha256_bytes,
    _sha256_text,
    _utc_now,
    safe_read_file_context,
)
from cairnmind.ui.live_adapter import LiveUiAdapter
from cairnmind.ui.server import (
    _bind_cockpit_routes,
    attach_ui_routes,
    create_ui_app,
    create_ui_router,
    resolve_ui_dist,
)

__all__ = [
    "MAX_BUS_EVENTS",
    "MAX_CONTEXT_FILE_BYTES",
    "MAX_CONTEXT_LINES",
    "LiveUiAdapter",
    "UiEventBus",
    "UiEventType",
    "_bind_cockpit_routes",
    "_build_graph_from_entities",
    "_classify_reason_code",
    "_find_quote_lines",
    "_sha256_bytes",
    "_sha256_text",
    "_utc_now",
    "attach_ui_routes",
    "create_ui_app",
    "create_ui_router",
    "resolve_ui_dist",
    "safe_read_file_context",
]


def __getattr__(name: str) -> Any:
    if name == "MockUiState":
        from cairnmind.ui.mock_state import MockUiState

        return MockUiState
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
