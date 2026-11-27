"""Definitions for the initial evidence: where a name the evidence uses is defined.

Retrieval finds text that uses a setting (a call with a fallback value, a slice, a sentence
about another field) more easily than the line that defines it. The code graph holds a node
for every definition (constant, field, function, class) but no link from a use to it, so names
are resolved by exact name:

- code names are the identifiers written as code (with "_", "." or an inner capital) in the
  text of the initial hits and in the question, whatever their case (``claim_job``,
  ``SCHEMA_VERSION``); word names are the question's other search terms, alone and adjacent
  pairs joined by "_" (``schema version`` -> ``schema_version``), each also upper-cased;
- a candidate is a current symbol node of the project whose label equals a name, or, for a
  field or a parameter default (``connect.port`` for ``def connect(port=7311)``), whose member
  (the bare name after ``Owner.``) does; a code name finds any kind of definition, a word name
  constants and fields only, since a plain word such as "version" or "write" names many
  functions; code-name matches are read first, word-name matches fill up the rest of the bound;
- a constant, field or parameter default is also a candidate when its snake_case name (or
  hyphenated TOML key, ``requires-python``) is made only of question words and FILLER_WORDS,
  with at least one question word and two words in all (``answer_tokens`` for "How many tokens may an answer have?", ``MAX_REPORTED`` for "... a
  drift report list at most?"). UNIT_WORDS stand in a name like filler words
  (``ROWS_PER_BATCH`` for "How many rows ... in one batch?", ``refresh_seconds`` for
  "How often does the page refresh?"). Words compare by a light stem (a trailing "ing", "ed" or
  "s" is dropped), and a word of a name of PREFIX_CHARS letters or more also matches a question
  word that begins with it (``postgres_version`` for "PostgreSQL version").
  The question words used are those that can be name words: NAME_STOPWORDS and words
  under three letters are left out, the longest MAX_PATTERN_WORDS are kept. The database applies
  this as a regular expression in its own bounded read, so only such names come back;
- a constant or field is also a candidate when one word of its name is a question word and its
  file name (or, for a field, its class) has a question word too (``FLUSH_TIMEOUT`` in
  ``journal.py`` for "What is the timeout of the journal?"; see ``context_patterns``);
- each kind of match has its own read of at most MAX_ROWS rows, after the worktree filter:
  code names (any kind), word names (constants and fields), names made of question words, names
  with one question word in a file or class with one;
- candidates are ranked: a name the question writes as code first; then a full match, a name
  of at least two question words with nothing else but filler words (``answer_tokens``; not
  ``MAX_REPORTED``, which has one); then a primary file before a test, lock or minified file;
  then by how many question words the name shares; then a value (constant, field, parameter
  default) before a function or a class, since the question words of a class name
  (``BatchWriter``) say what the question is about, not which setting it asks for; then by
  how many question words its owner and file name share; then the name whose question word
  the fewest other candidate names have.
  A candidate whose name shares no word is dropped, and a name defined in a primary
  file is never shown from a secondary one;
- each shown definition is the whole definition with neighbouring lines of its scope, within
  the excerpt size (see ``definition_lines``).
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from cairnmind.agents.retrieval import json_value, secondary_source, terms

MAX_DEFINITIONS = 4
# Definitions whose file is checked for freshness, shown or not: each check costs about twelve queries.
MAX_DEFINITION_ATTEMPTS = 8
MAX_NAMES = 64
MAX_ROWS = 200  # candidate definitions read per question, after the worktree filter
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
_KIND_ORDER = {"constant": 0, "field": 0, "parameter": 1, "function": 2, "method": 2, "class": 3}
# Words a setting's name carries without naming what it is about (MAX_REPORTED, num_ctx).
FILLER_WORDS = ("max", "min", "default", "num")
# Words a name carries to say how its value is counted, or to join two words: a question about
# ROWS_PER_BATCH, refresh_seconds or MAX_BLOB_BYTES rarely uses them. In a name they stand
# beside the question's words like filler words; they never count as a match by themselves.
UNIT_WORDS = ("per", "of", "seconds", "ms", "bytes", "chars", "tokens", "count")
# A word of a name also matches a question word that begins with it, from this length on
# (``postgres_version`` for "PostgreSQL version"): product names are written joined in prose.
PREFIX_CHARS = 5
MAX_PATTERN_WORDS = 8  # question words used to look up names by their words
# Question words that do not name what a setting is about.
NAME_STOPWORDS = frozenset("""
about all and any are can could did does doing done for from get give has have how into its know
least many may might more most much must need not one our please should some tell than that the
their there these this those use used was were what when where which who why will with would you
your
""".split())


def _stem(word: str) -> str:
    """A light stem: "reported" and "report", "events" and "event" compare equal."""
    word = word.casefold()
    for suffix, keep in (("ing", 5), ("ed", 4), ("s", 4)):
        if word.endswith(suffix) and len(word) >= keep and not word.endswith("ss"):
            return word[:-len(suffix)]
    return word


def _code_like(name: str) -> bool:
    """A name that is written as code: with "_" or ".", or with a capital after its first letter."""
    return len(name) >= 3 and ("_" in name or "." in name or any(char.isupper() for char in name[1:]))


def candidate_names(question: str, texts: list[str]) -> tuple[list[str], list[str]]:
    """(code names, word names) to look up, together at most MAX_NAMES.

    A name written as code in the question or the hits is a code name, also when a search term
    of the question has the same spelling; the question's names come first, then the hits' in
    their order. Word names are the remaining search terms and their joined pairs.
    """
    code: list[str] = []
    for text in [question, *texts]:
        for match in _IDENTIFIER.findall(text):
            parts = [match, *match.split(".")[1:]] if "." in match else [match]
            code += [part for part in parts if _code_like(part)]
    code = list(dict.fromkeys(code))[:MAX_NAMES // 2]
    words = terms(question)
    plain: list[str] = []
    for word in [*words, *("_".join(pair) for pair in zip(words, words[1:]))]:
        plain += [word, word.upper()]
    plain = [name for name in dict.fromkeys(plain) if len(name) >= 2 and name not in code]
    return code, plain[:MAX_NAMES - len(code)]


def _words(text: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {word.casefold() for word in re.split(r"[^A-Za-z0-9]+", spaced) if word}


def relevance(question: str, symbol: dict, path: str) -> tuple[int, int, int, int]:
    """(named, full, name words, context words) for a definition and a question.

    ``named`` is 1 when the question writes the definition's name or bare member as code, in any
    case (``ask_timeout``, ``SCHEMA_VERSION``, ``Settings.port``); a plain word such as "port" is
    not a name written as code. ``full`` is 1 when the bare name has at least two words and every
    one of them, filler words left out, is a question word (``answer_tokens``, ``MAX_REPORTED``). The counts are the question words found among the words of the bare
    name and among those of its owner and file name. Words are compared by ``_stem``; the
    question's search terms are split into words (``schema_version`` is schema and version).
    """
    named, full, words, context, _ = _match(question, symbol, path)
    return named, full, words, context


def _match(question: str, symbol: dict, path: str) -> tuple[int, int, int, int, frozenset]:
    """``relevance`` and, last, the words of the bare name that are question words."""
    asked = terms(question)
    spelled = {str(symbol.get(key, "")).casefold() for key in ("name", "member") if symbol.get(key)}
    named = int(any(match.casefold() in spelled for match in _IDENTIFIER.findall(question) if _code_like(match)))
    asked_words = {_stem(word) for term in asked for word in _words(term)}

    def is_asked(word: str) -> bool:
        return word in asked_words or (len(word) >= PREFIX_CHARS and any(
            other.startswith(word) for other in asked_words))

    own = {_stem(word) for word in _words(str(symbol.get("member") or symbol.get("name", "")))}
    content = own - {_stem(word) for word in FILLER_WORDS}
    matched = {word for word in content if is_asked(word)}
    unmatched = content - matched - {_stem(word) for word in UNIT_WORDS}
    # A full match needs two question words: port, question, MAX_TIMEOUT or DEFAULT_PORT is a
    # plain word match, not a name the question spells out.
    full = int(len(matched) >= 2 and not unmatched)
    context = {_stem(word) for word in _words(str(symbol.get("owner", "")))
               | _words(PurePosixPath(str(path).replace("\\", "/")).stem)}
    # A filler word counts beside a question word of the name (DEFAULT_PORT), never by itself: a
    # constant called DEFAULT or MAX is not what a question about "the default ..." asks for.
    shared = frozenset(word for word in own if is_asked(word)) if matched else frozenset()
    return named, full, len(shared), sum(is_asked(word) for word in context), shared


def rank_definitions(question: str, rows: list[dict]) -> list[dict]:
    """Rows ``{"symbol": node metadata, "path": project-relative path, ...}`` in the order shown.

    Drops rows whose bare name shares no word with the question (unless the question writes the
    name), parameter defaults that are neither named nor a full match (a parameter's name is
    local to its function: ``size`` or ``timeout`` alone says little), and, per bare name (a
    field's member), secondary rows when a primary file defines that name. Order: a name the
    question writes first, then a name made only of question words, primary before secondary,
    then question words in the name, a value before a function or a class, then question words
    in its owner and file name, then the name whose question word the fewest other candidate
    names have (``RETRY_WINDOW_NS`` for "window" before ``MAX_ROWS`` and ``MAX_ROW_BYTES`` for
    "row": the rare word says more about what is asked), values before functions before
    classes (a parameter default after a constant or field), then path and line for a stable
    result. So a one-word overlap (SUPPORT_TOKENS for "tokens") never outranks a full match.
    """
    def bare(row: dict) -> str:
        return str(row["symbol"].get("member") or row["symbol"].get("name") or "")

    scored = []
    for row in rows:
        named, full, words, context, matched = _match(question, row["symbol"], row["path"])
        if named or (words and (full or row["symbol"].get("kind") != "parameter")):
            scored.append({**row, "named": named, "full": full, "relevance": words, "file_relevance": context,
                           "_matched": matched, "secondary": secondary_source(row["path"])})
    primary_names = {bare(row) for row in scored if not row["secondary"]}
    kept = [row for row in scored if not (row["secondary"] and bare(row) in primary_names)]
    # How many candidate names have each question word; a name counts once however many files define it.
    sharing: dict[str, set[str]] = {}
    for row in kept:
        for word in row["_matched"]:
            sharing.setdefault(word, set()).add(bare(row).casefold())
    for row in kept:
        row["shared_by"] = min((len(sharing[word]) for word in row.pop("_matched")), default=0)
    return sorted(kept, key=lambda row: (-row["named"], -row["full"], row["secondary"], -row["relevance"],
                                         _KIND_ORDER.get(row["symbol"].get("kind"), 3) >= 2,
                                         -row["file_relevance"], row["shared_by"],
                                         _KIND_ORDER.get(row["symbol"].get("kind"), 3),
                                         row["path"], int(row["symbol"].get("line_start") or 0)))


def pattern_words(question: str) -> list[str]:
    """Stems of the question words that can be words of a name, longest first, at most MAX_PATTERN_WORDS."""
    stems = dict.fromkeys(
        _stem(word) for term in terms(question) for word in _words(term)
        if word.isascii() and word.isalnum() and word not in NAME_STOPWORDS and word not in FILLER_WORDS)
    chosen = [stem for stem in stems if len(stem) >= 3]
    return sorted(chosen, key=len, reverse=True)[:MAX_PATTERN_WORDS]


def _question_word(words: list[str]) -> str:
    """A regular expression for one word of a name that is a question word: one of ``words`` with
    an ending, or the beginning of one, at least PREFIX_CHARS long (``arcade`` for "arcadedb")."""
    forms = dict.fromkeys(form for word in words
                          for form in [word, *(word[:size] for size in range(PREFIX_CHARS, len(word)))])
    return "(?:" + "|".join(re.escape(form) for form in forms) + ")(?:s|es|ed|ing)?"


def name_word_pattern(question: str) -> str | None:
    """A regular expression for a lower-case name made of question words, filler words and unit
    words, with at least one question word and two words in all, joined by "_" (snake_case) or
    "-" (a TOML key such as ``requires-python``); None without a usable word."""
    words = pattern_words(question)
    if not words:
        return None
    real = _question_word(words)
    plain = dict.fromkeys([*FILLER_WORDS, *UNIT_WORDS, *(_stem(word) for word in UNIT_WORDS)])
    word = "(?:" + real + "|" + "|".join(plain) + ")"
    return f"^(?=(?:.*[_-])?{real}(?:[_-]|$))(?:{word}[_-])+{word}$"


def context_patterns(question: str) -> tuple[str, str, str] | None:
    """Regular expressions (name, file, owner) for a name that has one question word, in a file
    or a class whose name has one too; None without a usable word.

    The name pattern wants a question word as a whole word of a lower-case name (``flush_timeout``
    for "timeout"); the file pattern a question word as a whole word of the last part of a
    lower-case path (``journal.py``, ``journal_writer.py``); the owner pattern a question word
    anywhere in a lower-case class name (``journalwriter``), which ``relevance`` then compares
    by its words.
    """
    words = pattern_words(question)
    if not words:
        return None
    real = _question_word(words)
    return (f"^(?:.*[_-])?{real}(?:[_-].*)?$",
            f"^(?:.*/)?(?:[^/]*[^a-z0-9/])?{real}(?:[^a-z0-9/][^/]*)?$",
            f".*{real}.*")


def find_definitions(store, retriever, project_id: str, question: str, texts: list[str], worktree=None) -> list[dict]:
    """Ranked definition rows of this worktree for the names in ``question`` and ``texts``.

    The worktree's current sources are read first (as search reads them), and the node queries
    keep only symbols of those sources, ordered by label, path and line, each at most MAX_ROWS:
    the matches of code names (any kind), of word names (constants and fields), of names made
    of question words (``name_word_pattern``), and of names with one question word in a file or
    class with one (``context_patterns``). Every bound applies after the worktree filter and
    to one kind of match, so one kind cannot crowd out another, and the same index gives the same
    rows; four queries at most. A row is
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
    from cairnmind.storage.arcade_backend import ArcadeBackend
    backend = getattr(store, "backend", None) or ArcadeBackend(store)
    rows = backend.search_definition_nodes(
        project_id, eligible, mode="code", names=code, limit=MAX_ROWS
    ) if code else []
    known = {row["id"] for row in rows}
    if plain:
        # Word names in their own bounded read, so that names made of question words below
        # cannot be crowded out by them, nor they by those.
        rows += [row for row in backend.search_definition_nodes(
            project_id, eligible, mode="plain", names=plain, limit=MAX_ROWS
        ) if row["id"] not in known]
        known = {row["id"] for row in rows}
    pattern = name_word_pattern(question)
    if pattern:
        rows += [row for row in backend.search_definition_nodes(
            project_id, eligible, mode="pattern", pattern=pattern, limit=MAX_ROWS
        ) if row["id"] not in known]
        known = {row["id"] for row in rows}
    context = context_patterns(question)
    if context:
        rows += [row for row in backend.search_definition_nodes(
            project_id, eligible, mode="context", pattern=context[0], file_pattern=context[1],
            owner=context[2], limit=MAX_ROWS
        ) if row["id"] not in known]
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
