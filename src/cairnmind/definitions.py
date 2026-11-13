"""Definitions for the initial evidence: where a name the evidence uses is defined.

Retrieval finds text that uses a setting (a call with a fallback value, a slice, a sentence
about another field) more easily than the line that defines it. The code graph holds a node
for every definition (constant, field, function, class) but no link from a use to it, so names
are resolved by exact name:

- code names are the identifiers written as code (with "_", "." or an inner capital) in the
  text of the initial hits and in the question; word names are the question's search terms,
  alone and adjacent pairs joined by "_" (``schema version`` -> ``schema_version``), each also
  upper-cased (``SCHEMA_VERSION``);
- a candidate is a current symbol node of the project whose label equals a name, or, for a
  field, whose member (the bare name after ``Owner.``) does; a word name finds constants and
  fields only, since a plain word such as "version" or "write" names many functions;
- candidates are ranked by how many search terms of the question occur among the words of the
  name, its owner and its file name; a candidate sharing none is dropped; then a primary file
  before a test, lock or minified file; a name defined in a primary file is never shown from a
  secondary one;
- each shown definition is the whole definition with neighbouring lines of its scope, within
  the excerpt size (see ``definition_lines``).
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .retrieval import json_value, secondary_source, terms

MAX_DEFINITIONS = 4
# Definitions whose file is checked for freshness, shown or not: each check costs about twelve queries.
MAX_DEFINITION_ATTEMPTS = 8
MAX_NAMES = 64
MAX_ROWS = 200  # candidate definitions read per question, after the worktree filter
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
_KIND_ORDER = {"constant": 0, "field": 0, "function": 1, "method": 1, "class": 2}


def _code_like(name: str) -> bool:
    """A name that is written as code: with "_" or ".", or with a capital after its first letter."""
    return len(name) >= 3 and ("_" in name or "." in name or any(char.isupper() for char in name[1:]))


def candidate_names(question: str, texts: list[str]) -> tuple[list[str], list[str]]:
    """(code names, word names) to look up, together at most MAX_NAMES, word names first."""
    words = terms(question)
    plain: list[str] = []
    for word in [*words, *("_".join(pair) for pair in zip(words, words[1:]))]:
        plain += [word, word.upper()]
    code: list[str] = []
    for text in [question, *texts]:
        for match in _IDENTIFIER.findall(text):
            parts = [match, *match.split(".")[1:]] if "." in match else [match]
            code += [part for part in parts if _code_like(part)]
    plain = [name for name in dict.fromkeys(plain) if len(name) >= 2][:MAX_NAMES]
    code = [name for name in dict.fromkeys(code) if name not in plain][:MAX_NAMES - len(plain)]
    return code, plain


def _words(text: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {word.casefold() for word in re.split(r"[^A-Za-z0-9]+", spaced) if word}


def relevance(question: str, symbol: dict, path: str) -> tuple[int, int, int]:
    """(named, words in name and owner, words in file name) for a definition and a question.

    ``named`` is 1 when the question writes the definition's name or bare member as code, in any
    case (``ask_timeout``, ``SCHEMA_VERSION``, ``Settings.port``); a plain word such as "port" is
    not a name written as code. The word counts compare the
    question's search terms split into words (``schema_version`` counts as schema and version)
    with the words of the name and its owner, and of the file name.
    """
    asked = terms(question)
    spelled = {str(symbol.get(key, "")).casefold() for key in ("name", "member") if symbol.get(key)}
    named = int(any(match.casefold() in spelled for match in _IDENTIFIER.findall(question) if _code_like(match)))
    words = _words(str(symbol.get("name", ""))) | _words(str(symbol.get("owner", "")))
    stem = _words(PurePosixPath(path).stem)
    asked_words = [word for term in asked for word in _words(term)]
    return named, sum(word in words for word in asked_words), sum(word in stem for word in asked_words)


def rank_definitions(question: str, rows: list[dict]) -> list[dict]:
    """Rows ``{"symbol": node metadata, "path": project-relative path, ...}`` in the order shown.

    Drops rows whose name and owner share no word with the question (unless the question writes
    the name) and, per bare name (a field's member), secondary rows when a primary file defines
    that name. Order: a name the question writes first, then question words in name and owner,
    then in the file name, primary before secondary, constants and fields before functions before
    classes, then path and line for a stable result.
    """
    def bare(row: dict) -> str:
        return str(row["symbol"].get("member") or row["symbol"].get("name") or "")

    scored = []
    for row in rows:
        named, words, filed = relevance(question, row["symbol"], row["path"])
        if named or words:
            scored.append({**row, "named": named, "relevance": words, "file_relevance": filed,
                           "secondary": secondary_source(row["path"])})
    primary_names = {bare(row) for row in scored if not row["secondary"]}
    kept = [row for row in scored if not (row["secondary"] and bare(row) in primary_names)]
    return sorted(kept, key=lambda row: (-row["named"], -row["relevance"], -row["file_relevance"], row["secondary"],
                                         _KIND_ORDER.get(row["symbol"].get("kind"), 3),
                                         row["path"], int(row["symbol"].get("line_start") or 0)))


def find_definitions(store, retriever, project_id: str, question: str, texts: list[str], worktree=None) -> list[dict]:
    """Ranked definition rows of this worktree for the names in ``question`` and ``texts``.

    The worktree's current sources are read first (as search reads them), and the node query
    keeps only symbols of those sources, ordered by label, path and line, at most MAX_ROWS: the
    bound applies after the worktree filter, and the same index gives the same rows. A row is
    ``{"node_id", "source_id", "symbol", "path", "named", "relevance", ...}`` with ``path``
    relative to the worktree root.
    """
    code, plain = candidate_names(question, texts)
    if not code and not plain:
        return []
    project_id, root, roots = retriever._scope(project_id, worktree)
    eligible = sorted(retriever._source_ids(project_id, root, roots))
    if not eligible:
        return []
    rows = store.query(
        "SELECT id,label,source_id,metadata FROM nodes WHERE project_id=:project AND is_current=true "
        "AND kind='symbol' AND source_id IN :eligible AND (label IN :code OR metadata.member IN :code "
        "OR ((label IN :plain OR metadata.member IN :plain) AND metadata.kind IN ['constant', 'field'])) "
        f"ORDER BY label, metadata.path, metadata.line_start, id LIMIT {MAX_ROWS}",
        {"project": project_id, "eligible": eligible, "code": code or [""], "plain": plain or [""]})
    found = []
    for row in rows:
        symbol = json_value(row.get("metadata"), {})
        symbol = symbol if isinstance(symbol, dict) else {}
        path = symbol.get("path")
        if not isinstance(path, str) or not isinstance(row.get("source_id"), str):
            continue
        absolute = Path(path)
        owners = [candidate for candidate in roots if absolute.is_relative_to(candidate)]
        if not owners or max(owners, key=lambda candidate: len(candidate.parts)) != root:
            continue
        found.append({"node_id": row["id"], "source_id": row["source_id"], "symbol": symbol,
                      "path": absolute.relative_to(root).as_posix()})
    return rank_definitions(question, found)


def definition_lines(lines: list[str], start: int, end: int, size: int) -> tuple[int, int]:
    """1-based first and last line to show for the definition at ``start``..``end``.

    The definition itself comes first; if it does not fit, its first lines that fit. Otherwise
    neighbouring lines are added, one above and one below in turn, while the text fits: below,
    only lines of the same scope (blank, or indented at least as deep as the definition); above,
    the same, and the header of the enclosing scope (the first less indented line) is the last
    line taken upwards. So a field is shown with the fields around it and its class header.
    """
    def indent(line: str) -> int:
        return len(line) - len(line.lstrip())

    def length(first: int, last: int) -> int:
        return len("\n".join(lines[first - 1:last]))

    start, end = max(1, start), min(len(lines), max(start, end))
    if length(start, end) > size:
        while end > start and length(start, end) > size:
            end -= 1
        return start, end  # the caller cuts a single line longer than size
    depth = indent(lines[start - 1])
    first, last, up, down = start, end, True, True
    while up or down:
        if up:
            if first == 1:
                up = False
            else:
                line = lines[first - 2]
                if length(first - 1, last) > size:
                    up = False
                else:
                    first -= 1
                    if line.strip() and indent(line) < depth:
                        up = False
        if down:
            if last == len(lines):
                down = False
            else:
                line = lines[last]
                if (line.strip() and indent(line) < depth) or length(first, last + 1) > size:
                    down = False
                else:
                    last += 1
    return first, last
