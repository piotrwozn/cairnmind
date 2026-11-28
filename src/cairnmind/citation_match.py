"""Where a model's quote is in a source, when it is not copied character for character.

A citation is accepted only as a literal passage of the checked source. Local models copy the
words faithfully and the layout less so: a hard-wrapped line comes back joined with a space, a
Markdown code span without its backticks, and a quote of several lines skips one in the middle.
``locate_quote`` finds the passage of the source that such a quote stands for and returns its
span, so the citation that is recorded and shown is always the source's own text:

- the quote as it is, if the source holds it;
- otherwise the passage that equals the quote once the layout is left out on both sides. A run
  of whitespace between two word characters (letters, digits, ``_``) counts as one separator
  and must be there on both sides: ``1 000`` is not ``1000`` and ``in valid`` is not
  ``invalid``. Whitespace next to any other character, and backticks, are not compared: a model
  joins wrapped lines with a space, and re-spaces a list it copies from code (``"-a","-b"`` for
  two lines of arguments). Every other character must match in order, so no word, digit or sign
  of the quote may be missing from the source or added to it;
- otherwise, for a quote of several lines of which every line is in the source by the two rules
  above, one after another in the order of the quote: its longest run of whole lines that is
  found as one passage and weighs at least ``minimum`` (the first such run on a tie). The source
  may have lines between them that the quote left out. A quote with a line the source does not
  have, or has only before the lines quoted ahead of it, is not found at all.

Nothing else is tolerated: a paraphrase, a changed number or a word that the source does not
have at that place is not found.

``quote_weight`` is the size a quote is measured by where a shortest quote is required: its
characters with the layout left out, so that the indentation and line breaks of the source do
not make two brackets into evidence.
"""
from __future__ import annotations

_IGNORED = "`\ufeff"
_SEPARATOR = " "


def _word(char: str) -> bool:
    return char.isalnum() or char == "_"


def _lf_with_map(text: str) -> tuple[str, list[int]]:
    """Convert CRLF/CR to LF and drop UTF-8 BOM, returning `(normalized, origin_indices)`."""
    chars: list[str] = []
    origin: list[int] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\ufeff":
            i += 1
            continue
        if ch == "\r":
            chars.append("\n")
            origin.append(i)
            i += 2 if (i + 1 < length and text[i + 1] == "\n") else 1
            continue
        chars.append(ch)
        origin.append(i)
        i += 1
    return "".join(chars), origin


def _span_lf(quote: str, content: str) -> tuple[int, int] | None:
    """Exact match after normalizing CRLF/CR -> LF and stripping BOM, mapped back to `content` offsets."""
    if "\r" not in quote and "\r" not in content and "\ufeff" not in quote and "\ufeff" not in content:
        return None
    q_norm, _ = _lf_with_map(quote)
    if not q_norm:
        return None
    c_norm, origin = _lf_with_map(content)
    found = c_norm.find(q_norm)
    if found < 0:
        return None
    start = origin[found]
    last_idx = found + len(q_norm) - 1
    end = origin[last_idx] + 1
    if content[origin[last_idx]:origin[last_idx] + 2] == "\r\n":
        end = origin[last_idx] + 2
    return start, end


def _normalized(text: str) -> tuple[str, list[int]]:
    """``text`` without its layout (see the module text), and for every character of the result
    the index in ``text`` it came from."""
    result: list[str] = []
    origin: list[int] = []
    gap = -1  # where the whitespace since the last kept character began
    for index, char in enumerate(text):
        if char.isspace():
            if gap < 0:
                gap = index
            continue
        if char in _IGNORED:
            continue
        if gap >= 0 and result and _word(result[-1]) and _word(char):
            result.append(_SEPARATOR)
            origin.append(gap)
        gap = -1
        result.append(char)
        origin.append(index)
    return "".join(result), origin


def quote_weight(text: str) -> int:
    """The number of characters of ``text`` that count as quoted: backticks do not, and a run of
    whitespace counts as one character inside the text and not at all at its ends."""
    return len(" ".join("".join(char for char in text if char not in _IGNORED).split()))


def _span(quote: str, content: str, normalized: tuple[str, list[int]] | None) -> tuple[int, int] | None:
    start = content.find(quote)
    if start >= 0:
        return start, start + len(quote)
    if normalized is None:
        return None
    wanted = _normalized(quote)[0]
    if not wanted:
        return None
    text, origin = normalized
    found = text.find(wanted)
    if found < 0:
        return None
    return origin[found], origin[found + len(wanted) - 1] + 1


def _lines_in_order(lines: list[str], text: str) -> bool:
    """Whether every line that has something to compare is in ``text``, each after the one before."""
    cursor = 0
    for line in lines:
        wanted = _normalized(line)[0]
        if not wanted:
            continue
        found = text.find(wanted, cursor)
        if found < 0:
            return False
        cursor = found + len(wanted)
    return True


def locate_quote(quote: str, content: str, minimum: int = 1) -> tuple[int, int] | None:
    """(start, end) of the passage of ``content`` that ``quote`` stands for, or None.

    ``content[start:end]`` is what the citation records. Handles UTF-8 BOM and CRLF/LF differences
    deterministically at Level 0. See the module text for the layout rules.
    """
    if not quote.strip().lstrip("\ufeff"):
        return None
    span = _span(quote, content, None)
    if span is not None:
        return span
    span = _span_lf(quote, content)
    if span is not None:
        return span
    normalized = _normalized(content)
    span = _span(quote, content, normalized)
    if span is not None:
        return span
    lines = quote.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) < 2 or not _lines_in_order(lines, normalized[0]):
        return None
    for length in range(len(lines) - 1, 0, -1):
        for first in range(len(lines) - length + 1):
            part = "\n".join(lines[first:first + length]).strip()
            if quote_weight(part) < minimum:
                continue
            span = _span(part, content, normalized)
            if span is not None and quote_weight(content[span[0]:span[1]]) >= minimum:
                return span
    return None
