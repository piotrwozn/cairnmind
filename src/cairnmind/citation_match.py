"""Where a model's quote is in a source, when it is not copied character for character.

A citation is accepted only as a literal passage of the checked source. Local models copy the
words faithfully and the layout less so: a hard-wrapped line comes back joined with a space, a
Markdown code span without its backticks, and a quote of several lines skips one in the middle.
``locate_quote`` finds the passage of the source that such a quote stands for and returns its
span, so the citation that is recorded and shown is always the source's own text:

- the quote as it is, if the source holds it;
- otherwise the passage that equals the quote once whitespace and backticks are left out on both
  sides: a model joins wrapped lines with a space, and re-spaces a list it copies from code
  (``"-a", "-b"`` for two lines of arguments). Every other character must still match in order,
  so no word, digit or sign of the quote may be missing from the source or added to it;
- otherwise, for a quote of several lines, its longest run of whole lines that is found by the
  two rules above and is at least ``minimum`` characters long (the first such run on a tie).

Nothing else is tolerated: a paraphrase, a changed number or a word that the source does not
have at that place is not found.
"""
from __future__ import annotations

_IGNORED = "`"


def _normalized(text: str) -> tuple[str, list[int]]:
    """``text`` without whitespace and backticks, and for every character of the result the index
    in ``text`` it came from."""
    result: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        if char in _IGNORED or char.isspace():
            continue
        result.append(char)
        origin.append(index)
    return "".join(result), origin


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


def locate_quote(quote: str, content: str, minimum: int = 1) -> tuple[int, int] | None:
    """(start, end) of the passage of ``content`` that ``quote`` stands for, or None.

    ``content[start:end]`` is what the citation records. See the module text for the rules.
    """
    if not quote.strip():
        return None
    span = _span(quote, content, None)
    if span is not None:
        return span
    normalized = _normalized(content)
    span = _span(quote, content, normalized)
    if span is not None:
        return span
    lines = quote.split("\n")
    for length in range(len(lines) - 1, 0, -1):
        for first in range(len(lines) - length + 1):
            part = "\n".join(lines[first:first + length]).strip()
            if len(part) < minimum:
                continue
            span = _span(part, content, normalized)
            if span is not None and len(content[span[0]:span[1]].strip()) >= minimum:
                return span
    return None
