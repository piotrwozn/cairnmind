"""Lightweight CLI entrypoint with a fast path for `cairnmind hook` invocations."""
from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import sys


def _parse_hook_argv(argv: Sequence[str]) -> tuple[str | None, str] | None:
    """Return ``(data_dir, client)`` only for plain ``hook --client <NAME>`` calls."""
    data_dir: str | None = None
    index = 0
    total = len(argv)
    while index < total:
        arg = argv[index]
        if arg == "hook":
            index += 1
            break
        if arg == "--data-dir" and index + 1 < total and data_dir is None:
            value = argv[index + 1]
            if not value or value.startswith("-"):
                return None
            data_dir = value
            index += 2
            continue
        if arg.startswith("--data-dir=") and data_dir is None:
            value = arg.partition("=")[2]
            if not value or value.startswith("-"):
                return None
            data_dir = value
            index += 1
            continue
        return None
    else:
        return None
    if index >= total:
        return None
    arg = argv[index]
    if arg == "--client" and index + 2 == total:
        client = argv[index + 1]
    elif arg.startswith("--client=") and index + 1 == total:
        client = arg.partition("=")[2]
    else:
        return None
    if not client or client.startswith("-"):
        return None
    return data_dir, client


def _run_hook(data_dir: str | None, client: str) -> None:
    from cairnmind.config import Settings
    from cairnmind.integrations import hook as handle_hook

    try:
        settings = Settings.load(Path(data_dir) if data_dir is not None else None, create=True)
    except (ValueError, OSError):
        from cairnmind.cli import entrypoint as cli_entrypoint
        cli_entrypoint()
        return
    if settings._origin.get("connect_saved"):
        from cairnmind.database_mode import saved_note
        print(f"Cairnmind: {saved_note(settings)}", file=sys.stderr)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = handle_hook(settings, payload, client)
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - match cli.hook: never block a client turn
        print(f"Cairnmind hook: {exc}", file=sys.stderr)


def entrypoint(argv: Sequence[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    parsed = _parse_hook_argv(args)
    if parsed is not None:
        _run_hook(*parsed)
        return
    from cairnmind.cli import entrypoint as cli_entrypoint
    cli_entrypoint()
