"""Render archived data as literal terminal text, never live ANSI/OSC controls."""
from __future__ import annotations

import unicodedata

from rich.text import Text

_BIDI = {*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x061C, 0x200E, 0x200F}
# Characters that render as empty cells without being whitespace could push forged text onto a new terminal row.
_BLANK = {0x2800, 0x3164, 0x115F, 0x1160, 0xFFA0}


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


def one_line(value: object, limit: int = 160) -> str:
    raw = "".join(" " if ord(char) in _BLANK or unicodedata.category(char) in ("Cf", "Zs", "Zl", "Zp") else char
                  for char in str(value))
    text = " ".join(safe_text(raw).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


_line = one_line


def plain(value: object, *, limit: int | None = None) -> Text:
    return Text(safe_text(value, limit=limit))
