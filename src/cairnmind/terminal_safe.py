"""Render archived data as literal terminal text, never live ANSI/OSC controls."""
from __future__ import annotations

from rich.text import Text

_BIDI = {*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x061C, 0x200E, 0x200F}


def safe_text(value: object, *, limit: int | None = None) -> str:
    text = str(value)
    output = []
    for character in text:
        code = ord(character)
        if character in {"\n", "\t"}:
            output.append(character)
        elif code < 32 or code == 127 or 128 <= code <= 159 or code in _BIDI:
            output.append(f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}")
        else:
            output.append(character)
    result = "".join(output)
    if limit is not None and len(result) > limit:
        return result[:limit] + "…"
    return result


def plain(value: object, *, limit: int | None = None) -> Text:
    return Text(safe_text(value, limit=limit))
