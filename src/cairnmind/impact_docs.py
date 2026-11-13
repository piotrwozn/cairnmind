"""Documentation pages that mention a definition changed since a commit (part of ``cairnmind impact``).

Records are linked to code by extraction. A page nobody extracted from has no record, so after
``refresh_token()`` changed, ``docs/login.md`` would never be named. This module reads the
documentation files of the worktree and lists, for every definition whose text changed, the pages
that mention its name, with line numbers. A mention means "worth checking", not that the page is wrong.

Read-only like the rest of ``impact``: it reads Git's file list and the files, nothing else.

Which names are looked up: those of definitions that the parser finds in Python, JavaScript and
TypeScript files and whose text changed or that were removed. Not looked up: definitions that were
added or only moved (the text is the same), definitions of a changed file that cannot be compared,
names with characters other than letters, digits, ``_`` and ``$`` (so no ``#private`` member), dunder
names such as ``__init__`` and ``constructor`` (every class has one), and names under 4 characters.

Which mentions count. Documentation is prose, and a definition called ``login`` would match half of
it. A name counts only when it is a whole identifier in the text (``refresh_token_v2`` is not a mention
of ``refresh_token``) and either it is distinctive, or it is written as code. Distinctive means it
contains an underscore or a capital letter after its first character (``refresh_token``,
``SessionStore``). Written as code means it comes after an odd number of backtick runs on its line
(inside inline code, whether the delimiter is one backtick or two as in reStructuredText), it is
inside a fenced block (Markdown files only), or it is directly followed by ``(``. So the word login in a
sentence is not listed, while login in backticks or login() is.

Never read: files under the directories the indexer skips (``EXCLUDED_DIRS``), files it treats as
secret, files whose path passes through a symbolic link or leaves the worktree, and files over 1 MB.
"""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

from cairnmind.brief import _git_out, _line
from cairnmind.ingest import EXCLUDED_DIRS, _is_secret

DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt")
FENCED_SUFFIXES = (".md", ".mdx")
MAX_DOC_BYTES = 1024 * 1024
MAX_DOCUMENTS = 50
MAX_LINES = 3
MIN_NAME = 4
# Work bounds: how many files are read, how many bytes in all, and how many names are searched for.
MAX_FILES = 2000
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_NAMES = 500
# Directories that hold Cairnmind's own state or Git's: never read, whatever Git says about them.
RESERVED = {".memory", ".cairnmind", ".devbrain", ".git"}
GENERIC = {"constructor"}
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
_TICKS = re.compile(r"`+")


def _doc_names(root):
    """Documentation paths of the worktree: tracked, or untracked and not ignored. None when Git failed."""
    listed = _git_out(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--")
    if listed is None:
        return None
    names = []
    for name in {name for name in listed.split("\0") if name.lower().endswith(DOC_SUFFIXES)}:
        parts = PurePosixPath(name).parts
        # The indexer never reads these directories or secret-looking files, so neither does this report.
        if not RESERVED & {part.lower() for part in parts} and not EXCLUDED_DIRS & set(parts[:-1]) \
                and not _is_secret(PurePosixPath(name)):
            names.append(name)
    return sorted(names, key=lambda name: PurePosixPath(name).parts)


def _doc_text(root, real_root, name):
    """``(text, bytes)`` of the current file, or None when it is missing, linked, outside, too large, not text."""
    path = root / name
    try:
        # Any link on the way (the file itself or a directory) makes the resolved path differ from the plain one.
        if path.resolve() != real_root.joinpath(*PurePosixPath(name).parts) or not path.is_file():
            return None
        if path.stat().st_size > MAX_DOC_BYTES:
            return None
        raw = path.read_bytes()
        return None if b"\0" in raw else (raw.decode("utf-8-sig"), len(raw))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _names(symbols):
    """Sorted names of definitions whose text changed (modified or removed) that are worth looking for."""
    found = {name for per_file in symbols.values() if per_file for name, how in per_file.items()
             if how in ("modified", "removed")}
    return sorted(name for name in found if len(name) >= MIN_NAME and re.fullmatch(r"[\w$]+", name)
                  and name not in GENERIC and not (name.startswith("__") and name.endswith("__")))


def _distinctive(name):
    return "_" in name or any(char.isupper() for char in name[1:])


def _closes(fence, opened):
    """True when the fence match ``opened`` ends the block that ``fence`` began."""
    return bool(opened) and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence) \
        and not opened.group(2).strip()


def _mentions(text, pattern, fenced=True):
    """``{name: [line numbers]}`` for the names of ``pattern`` that count as mentions in ``text``."""
    found: dict[str, list[int]] = {}
    fence = ""  # the run of fence characters that opened the current block, empty outside a block
    for number, line in enumerate(text.split("\n"), 1):
        line = line.removesuffix("\r")
        opened = _FENCE.match(line) if fenced else None
        if fence and _closes(fence, opened):
            fence = ""
            continue
        # An opening backtick fence has no backtick after its run; otherwise it is inline code on a normal line.
        if not fence and opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            fence = opened.group(1)
            continue
        matches = list(pattern.finditer(line))
        if not matches:
            continue
        runs = [run.start() for run in _TICKS.finditer(line)]  # one pass per line, however many matches
        before = 0
        for match in matches:
            while before < len(runs) and runs[before] < match.start(1):
                before += 1
            name = match.group(1)
            if (_distinctive(name) or fence or before % 2 == 1 or line[match.end(1):match.end(1) + 1] == "(") \
                    and (not found.get(name) or found[name][-1] != number):
                found.setdefault(name, []).append(number)
    return found


def documentation_mentions(root, changed, symbols):
    """Documents that mention a changed definition, bounded; also the counters of what was not looked at."""
    names = _names(symbols)
    result = {"documents": [], "omitted_documents": 0, "omitted_lines": 0, "skipped_documents": 0,
              "not_scanned": 0, "names_not_searched": max(len(names) - MAX_NAMES, 0), "unavailable": False}
    names = names[:MAX_NAMES]
    docs = _doc_names(root) if names else []
    if docs is None:
        result["unavailable"] = True
        return result
    if not docs:
        return result
    pattern = re.compile(r"(?<![\w$])(" + "|".join(re.escape(name) for name in names) + r")(?![\w$])")
    real_root = root.resolve()
    entries, files, total = [], 0, 0
    for index, doc in enumerate(docs):
        if files >= MAX_FILES or total >= MAX_TOTAL_BYTES:
            result["not_scanned"] = len(docs) - index
            break
        read = _doc_text(root, real_root, doc)
        if read is None:
            # A tracked page that was deleted in the worktree is not a skipped one; anything else is.
            result["skipped_documents"] += os.path.lexists(root / doc)
            continue
        files, total = files + 1, total + read[1]
        found = _mentions(read[0], pattern, doc.lower().endswith(FENCED_SUFFIXES))
        if found:
            # Paths and names go through the same sanitiser as the text report, so no control character leaves in JSON.
            entries.append({"path": _line(doc, 300), "changed_in_range": doc in changed,
                            "mentions": [{"name": _line(name, 80), "lines": lines[:MAX_LINES],
                                          "more_lines": max(len(lines) - MAX_LINES, 0)}
                                         for name, lines in sorted(found.items())]})
    result["documents"] = entries[:MAX_DOCUMENTS]
    result["omitted_documents"] = len(entries) - len(result["documents"])
    result["omitted_lines"] = sum(item["more_lines"] for entry in result["documents"] for item in entry["mentions"])
    return result


def documentation_lines(documentation):
    """The report section, as lines; empty when there is nothing to list and nothing to say about the scan."""
    entries = documentation["documents"]
    notes = []
    if documentation["unavailable"]:
        notes.append("The documentation files could not be listed (Git did not answer), so no page was checked.")
    if documentation["omitted_documents"]:
        notes.append(f"{documentation['omitted_documents']} more document(s) mention a changed definition "
                     f"and are not listed (at most {MAX_DOCUMENTS} are shown).")
    if documentation["omitted_lines"]:
        notes.append(f"{documentation['omitted_lines']} more line(s) are not listed "
                     f"(at most {MAX_LINES} per document and name).")
    if documentation["skipped_documents"]:
        notes.append(f"{documentation['skipped_documents']} documentation file(s) were not read "
                     "(a link or not a plain file, over 1 MB, or not UTF-8 text).")
    if documentation["not_scanned"]:
        notes.append(f"{documentation['not_scanned']} documentation file(s) were not scanned: "
                     f"at most {MAX_FILES} files and {MAX_TOTAL_BYTES // (1024 * 1024)} MB are read per run.")
    if documentation["names_not_searched"]:
        notes.append(f"{documentation['names_not_searched']} changed name(s) were not searched for: "
                     f"at most {MAX_NAMES} names are looked up.")
    if not entries and not notes:
        return []
    lines = [""]
    if entries:
        lines.append("Documentation that mentions a changed definition (a mention is worth checking, "
                     "not proof that the page is wrong):")
    for entry in entries:
        shown = []
        for item in entry["mentions"]:
            numbers = ", ".join(str(number) for number in item["lines"])
            extra = f", and {item['more_lines']} more" if item["more_lines"] else ""
            word = "line" if len(item["lines"]) == 1 else "lines"
            shown.append(f"{item['name']} ({word} {numbers}{extra})")
        same = " [also changed in this range]" if entry["changed_in_range"] else ""
        lines.append(f"  - {entry['path']}{same}: " + "; ".join(shown))
    return lines + [f"  {note}" if entries else note for note in notes]
