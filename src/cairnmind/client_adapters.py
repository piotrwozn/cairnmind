"""Typed native execution boundary, independent of browser transport.

Only native Codex and Claude adapters are registered in 0.5. A future Cairnmind
API engine must implement this boundary and its verification tests before it
can be registered; a name alone never grants verified-memory capability.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


NativeClient = Literal["codex", "claude"]
RunState = Literal["preparing", "starting", "running", "stopping", "exited", "failed", "interrupted"]
MemoryReadiness = Literal["unavailable", "partial", "ready", "acknowledged", "stale", "unknown"]


@dataclass(frozen=True)
class AdapterCapabilities:
    """Observed capabilities, not a promise derived from an executable name."""

    terminal: bool = False
    native_login: bool = False
    verified_resume: bool = False
    session_binding: bool = False
    interrupt: bool = False
    resize: bool = False
    history: bool = False
    api_tools: bool = False

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class NativeAdapterDescriptor:
    client: NativeClient
    adapter_id: str
    title: str
    runtime: Literal["native"] = "native"


NATIVE_ADAPTERS: Mapping[str, NativeAdapterDescriptor] = {
    "codex": NativeAdapterDescriptor("codex", "native.codex", "Codex CLI"),
    "claude": NativeAdapterDescriptor("claude", "native.claude", "Claude Code"),
}


def native_adapter(client: str) -> NativeAdapterDescriptor:
    try:
        return NATIVE_ADAPTERS[client]
    except (KeyError, TypeError):
        raise ValueError("Only registered native Codex and Claude adapters are available") from None


def observed_capabilities(client: str, probe: Mapping[str, Any]) -> AdapterCapabilities:
    native_adapter(client)
    flags = probe.get("capabilities") or {}
    if not isinstance(flags, Mapping):
        flags = {}
    installed = probe.get("installed") is True
    return AdapterCapabilities(
        terminal=installed,
        native_login=installed and probe.get("login_supported") is True,
        verified_resume=installed and flags.get("verified_resume") is True,
        session_binding=installed and flags.get("session_binding") is True,
        interrupt=installed,
        resize=installed,
        history=installed,
    )


@runtime_checkable
class NativeAdapter(Protocol):
    """Lifecycle methods used by the supervisor; every method stays run-scoped.

    Runtime implementations bind requests to a project, worktree and launch
    generation before touching a process. An attachment is transport only and
    cannot create a resume acknowledgement or native permission approval.
    """

    def prepare(self, profile: Mapping[str, Any], project: Mapping[str, Any],
                worktree: str | None = None) -> dict[str, Any]: ...

    def start(self, prepared: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]: ...

    def attach(self, run_id: str, *, generation: str) -> Any: ...

    def input(self, run_id: str, data: bytes, *, generation: str, controller: str) -> None: ...

    def resize(self, run_id: str, cols: int, rows: int, *, generation: str, controller: str) -> None: ...

    def interrupt(self, run_id: str, *, generation: str) -> dict[str, Any]: ...

    def stop(self, run_id: str, *, generation: str) -> dict[str, Any]: ...

    def status(self, run_id: str) -> dict[str, Any]: ...

    def history(self, run_id: str, *, limit: int = 5000) -> dict[str, Any]: ...
