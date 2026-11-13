"""Documentation pages that mention a definition changed since a commit (part of ``cairnmind impact``).

Records are linked to code by extraction. A page nobody extracted from has no record, so after
``refresh_token()`` changed, ``docs/login.md`` would never be named. This module reads the
documentation files of the worktree and lists, for every definition whose text changed, the pages
that mention its name, with line numbers. A mention means "worth checking", not that the page is wrong.

Read-only like the rest of ``impact``: it reads Git's file list and the files, nothing else.

Which names count. Documentation is prose, and a definition called ``get`` or ``run`` would match half
of it. A name counts only when all of these hold:

- it is at least 4 characters long and is not a dunder such as ``__init__``, which every class has;
- it is a whole identifier in the text, so ``refresh_token_v2`` is not a mention of ``refresh_token``;
- it is distinctive, or the mention is written as code. Distinctive means it contains an underscore or
  a capital letter after its first character (``refresh_token``, ``SessionStore``). Written as code means
  it stands between backticks, in a fenced block, or is directly followed by ``(``.

So ``login`` in a sentence is not listed, while `` `login` `` and ``login()`` are.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from cairnmind.brief import _git_out, _line

DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt")
MAX_DOC_BYTES = 1024 * 1024
MAX_DOCUMENTS = 50
MAX_LINES = 3
MIN_NAME = 4
# Directories that hold Cairnmind's own state or Git's: never read, whatever Git says about them.
RESERVED = {".memory", ".cairnmind", ".devbrain", ".git"}


def _doc_names(root):
    """Documentation paths of the worktree: tracked, or untracked and not ignored. None when Git failed."""
    listed = _git_out(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--")
    if listed is None:
        return None
    names = {name for name in listed.split("\0") if name.lower().endswith(DOC_SUFFIXES)}
    return sorted((name for name in names if not RESERVED & {part.lower() for part in PurePosixPath(name).parts}),
                  key=lambda name: PurePosixPath(name).parts)


def _doc_text(root, name):
    """The file's current text, or None when it is missing, not a plain file, too large, or not UTF-8."""
    path = root / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DOC_BYTES:
            return None
        raw = path.read_bytes()
        return None if b"\0" in raw else raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _names(symbols):
    """Names of definitions whose text changed (modified or removed) that are long enough to look for."""
    found = {name for per_file in symbols.values() if per_file for name, how in per_file.items()
             if how in ("modified", "removed")}
    return sorted(name for name in found if len(name) >= MIN_NAME and re.fullmatch(r"\w+", name)
                  and not (name.startswith("__") and name.endswith("__")))


def _distinctive(name):
    return "_" in name or any(char.isupper() for char in name[1:])


def _written_as_code(line, start, end, in_fence):
    """True inside a fenced block, between backticks (an odd number of them before the name) or before ``(``."""
    return in_fence or line[:start].count("`") % 2 == 1 or line[end:end + 1] == "("


def _mentions(text, pattern):
    """``{name: [line numbers]}`` for the names of ``pattern`` that count as mentions in ``text``."""
    found: dict[str, list[int]] = {}
    in_fence = False
    for number, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        for match in pattern.finditer(line):
            name = match.group(1)
            if _distinctive(name) or _written_as_code(line, match.start(1), match.end(1), in_fence):
                if not found.get(name) or found[name][-1] != number:
                    found.setdefault(name, []).append(number)
    return found


def documentation_mentions(root, changed, symbols):
    """Documents that mention a changed definition, bounded: ``{"documents", "omitted_documents", ...}``."""
    names = _names(symbols)
    result = {"documents": [], "omitted_documents": 0, "omitted_lines": 0, "skipped_documents": 0}
    docs = _doc_names(root) if names else []
    if not docs:
        return result
    pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(name) for name in names) + r")(?!\w)")
    entries = []
    for doc in docs:
        text = _doc_text(root, doc)
        if text is None:
            # A tracked page that was deleted in the worktree is not a skipped one; anything else is.
            result["skipped_documents"] += (root / doc).exists()
            continue
        found = _mentions(text, pattern)
        if found:
            entries.append({"path": doc, "changed_in_range": doc in changed,
                            "mentions": [{"name": name, "lines": lines[:MAX_LINES],
                                          "more_lines": max(len(lines) - MAX_LINES, 0)}
                                         for name, lines in sorted(found.items())]})
    result["documents"] = entries[:MAX_DOCUMENTS]
    result["omitted_documents"] = len(entries) - len(result["documents"])
    result["omitted_lines"] = sum(item["more_lines"] for entry in result["documents"] for item in entry["mentions"])
    return result


def documentation_lines(documentation):
    """The report section, as lines; empty when no document mentions a changed definition."""
    entries = documentation["documents"]
    if not entries:
        return []
    lines = ["", "Documentation that mentions a changed definition (a mention is worth checking, "
                 "not proof that the page is wrong):"]
    for entry in entries:
        shown = []
        for item in entry["mentions"]:
            numbers = ", ".join(str(number) for number in item["lines"])
            extra = f", and {item['more_lines']} more" if item["more_lines"] else ""
            word = "line" if len(item["lines"]) == 1 else "lines"
            shown.append(f"{_line(item['name'], 80)} ({word} {numbers}{extra})")
        same = " [also changed in this range]" if entry["changed_in_range"] else ""
        lines.append(f"  - {_line(entry['path'], 120)}{same}: " + "; ".join(shown))
    if documentation["omitted_documents"]:
        lines.append(f"  {documentation['omitted_documents']} more document(s) mention a changed definition "
                     f"and are not listed (at most {MAX_DOCUMENTS} are shown).")
    if documentation["omitted_lines"]:
        lines.append(f"  {documentation['omitted_lines']} more line(s) are not listed "
                     f"(at most {MAX_LINES} per document and name).")
    if documentation["skipped_documents"]:
        lines.append(f"  {documentation['skipped_documents']} documentation file(s) were not read "
                     "(over 1 MB or not UTF-8).")
    return lines
