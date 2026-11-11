"""Select private executable overrides without changing the process-wide PATH."""
from __future__ import annotations

import os
from pathlib import Path
import shutil

from .config import Settings


def resolve_tmux(settings: Settings, *, override: str | None = None) -> str | None:
    """Return the canonical executable; an invalid explicit choice never falls back.

    An explicit constructor override wins over settings. An empty setting retains
    PATH discovery, while an empty explicit override is an error.
    """
    value = settings.tmux_executable if override is None else override
    if not isinstance(value, str):
        raise ValueError("tmux_executable must be an absolute executable path")
    if value == "" and override is None:
        value = shutil.which("tmux")
        if value is None:
            return None
    path = Path(value)
    if not value or not path.is_absolute():
        raise ValueError("tmux_executable must be an absolute executable path")
    try:
        path = path.resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("tmux_executable must identify an executable file")
    except OSError as exc:
        raise ValueError("Configured tmux executable is unavailable") from exc
    return str(path)
