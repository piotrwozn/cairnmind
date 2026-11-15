"""Subject gate: is the thing a question names anywhere in what the answer quotes?

The support check (support.py) asks a model whether the quotes state the answer, and a model can
accept a quote that only shares a number: the default port of a product the project never
mentions, "supported" by the line that sets the service's own port. This check does not ask a
model. The names a question uses for its subject must occur in a verified quote, in the path of a
quoted source or elsewhere in the text of that source; when one of them occurs in none of these,
the answer is not complete. It can only refuse: it never makes an answer complete. The rest of the
source counts because a right quote often leaves the name out (the line "1 GB heap" under a
heading about the JVM answers a question about the Java heap); a source that never names the
thing asked about cannot be evidence about it.

What counts as a name is decided by the written shape of the word, never by a list of products:

- an identifier: a word with an underscore, a dot between letters (``pyproject.toml``), a capital
  inside (``ArcadeDB``), or letters and digits together (``qwen3``);
- a proper name: a capitalised word that does not begin a sentence (``Consul``, ``Java``);
- one word alone between backticks (```consul```).

Left out: words of the language and words every project has (GENERIC: default, port, file,
value, setting ...), abbreviations written in capitals only (URL, GPU, TTL are more often common
words than names), numbers and versions, pasted values (a token, a hash, an id: a long unbroken
run with a digit, or a word with single characters between its digits and capitals), and the
project's own names, which the caller passes. A question without such a word is not checked at
all: the gate then does not apply. A product name written in lower case without backticks has no
shape to go by and is not read either; such a question rests on the support check alone. So does
a question written in headline case, where a capital says nothing, and one asked with an answer
schema, which is a caller's instruction full of field names (the caller of ``unnamed_subject``
leaves those out). A question with one or two names needs every one of them in a quoted source;
with three or more, which is a question with context, at least half.

A name matches without regard to case, across ``_``, ``-``, ``.`` and spaces where the name itself
has a seam (``num_ctx`` in "num ctx", ``ArcadeDB`` in "arcade_db"), split once where it has none
(``Zookeeper`` in "zoo_keeper"), as a part of an identifier (``Consul`` in ``consulPort`` or
``consul_port``), with a plural or possessive ending, and with the one-letter ending of an inflected
form ("Consula", "Javy"). A compound name whose first part has five letters or more is also found
by that part alone, because identifiers shorten it that way (``arcade_url`` for ArcadeDB).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from .extraction_input import source_text
from .support import support_citations

# What an answer refused by this check says is missing: this fixed prefix, then the names as the
# question wrote them (a person's text, kept as it is).
SUBJECT_GAP = "The quoted sources do not name what the question asks about: "

# Words of the language that can be capitalised inside a sentence without naming anything.
STOP = frozenset("""
a an and are as at be by can did do does for from has have how i if in is it its may must no not
of on or our should than that the their then there these this those to was were what when where
which who whom whose why will with would you your e.g i.e etc ok yes
monday tuesday wednesday thursday friday saturday sunday january february march april june july
august september october november december
co czy dla ile jak jaka jaki jakie jest która które który na nie od po się są ten to w z że
""".split())
# Words every project has: naming one of them does not name a subject.
GENERIC = frozenset("""
address api app application argument arguments array bool boolean branch bug build byte bytes cache
changelog class classes cli client code command commands commit config configuration constant
constants count data database db default defaults dict dir directory doc docs documentation engine
enum env environment error event events example false field fields file files flag flags float
folder format function functions guide header host id index input int integer interface item items
key keys length library limit line lines list log logs max maximum memory message method methods
min minimum model models module modules name names none null number object option options output
package packages parameter parameters path paths port ports process project property queue readme
record records release repo repository request response result root row rows schema script server
service session setting settings size source sources state status str string table task test tests
text thread time timeout token tokens tool tools true type types uri url user users value values
variable variables version versions window worker
base64 http2 ipv4 ipv6 md5 oauth2 sha1 sha256 sha512 utf8 utf16 x64 x86 x86_64
""".split())

_WORD = re.compile(r"\w+(?:\.\w+)*")
_CODE = re.compile(r"`([^`\s]+)`")
_SEAM = re.compile(r"[\W_]+|(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])|(?<=[a-zà-ÿ])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-zà-ÿ])")
_CAMEL = re.compile(r"(?<=[a-zà-ÿ])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-zà-ÿ])")
_VALUE = re.compile(r"(?<![\w+=-])(?=[\w+=-]*\d)[\w+=-]{24,}(?![\w+=-])")
_VOWELS = "aeiouyąęó"
_LETTER = r"[^\W\d_]"
MAX_TERMS = 8
MAX_TERM_CHARS = 64
MAX_QUESTION_CHARS = 4000


def _letters(word: str) -> int:
    return sum(character.isalpha() for character in word)


def _identifier(word: str) -> bool:
    # A name is made of parts one can read: a hash, a key or an id (d489cee, x7Kq9) has parts of
    # one character between its digits and capitals, and is a value, not a name.
    if not all(len(part) >= 2 or part.isdigit() for part in _SEAM.split(word) if part):
        return False
    if "_" in word.strip("_") or re.search(_LETTER + r"\." + _LETTER, word):
        return True
    if any(character.isdigit() for character in word):
        return True
    return bool(_CAMEL.search(word)) and not word.isupper()


def _starts_sentence(question: str, position: int) -> bool:
    before = question[:position].rstrip(" \t\r\n\"'`([{*")
    return not before or before[-1] in ".?!:;"


def subject_terms(question: str, own_names: Iterable[str] = ()) -> list[str]:
    """The names ``question`` uses for its subject, in order, each once; empty when it has none."""
    # A long unbroken run with a digit is a pasted value (a token, a hash, an id): blanked, not read.
    question = _VALUE.sub(lambda match: " " * len(match.group()), str(question)[:MAX_QUESTION_CHARS])
    own = {_key(name) for name in own_names if name}
    alone = {match.start(1) for match in _CODE.finditer(question)
             if _WORD.fullmatch(match.group(1))}
    # In a question written in headline case (most words capitalised) a capital names nothing.
    inner = [match.group() for match in _WORD.finditer(question)
             if _letters(match.group()) >= 2 and not _starts_sentence(question, match.start())]
    headline = len(inner) >= 4 and sum(word[0].isupper() for word in inner) * 2 > len(inner)
    found: dict[str, str] = {}
    for match in _WORD.finditer(question):
        word = match.group().strip("_")
        folded = word.casefold()
        if (_letters(word) < 2 or len(word) > MAX_TERM_CHARS or folded in STOP or folded in GENERIC
                or _key(word) in own):
            continue
        if folded.endswith("s") and folded[:-1] in GENERIC:
            continue
        named = (word[0].isupper() and not word.isupper() and not headline
                 and not _starts_sentence(question, match.start()))
        if _identifier(word) or named or (match.start() in alone and _letters(word) >= 3):
            found.setdefault(_key(word), word)
    return list(found.values())[:MAX_TERMS]


def _key(word: str) -> str:
    return re.sub(r"[\W_]+", "", str(word).casefold())


def _spread(text: str) -> str:
    """``text`` folded, with a space where an identifier changes case (consulPort: consul port)."""
    return _CAMEL.sub(" ", str(text)).casefold()


def _patterns(term: str) -> list[str]:
    parts = [part.casefold() for part in _SEAM.split(term) if part]
    if not parts:
        return []
    key = "".join(parts)
    whole = r"[\W_]*".join(re.escape(part) for part in parts)
    patterns = [whole + r"(?:e?s)?"]
    plain = len(parts) == 1 and key.isalpha()
    if plain and len(key) >= 6:
        # A name without a seam of its own, written in two parts: zoo_keeper, Zoo Keeper.
        patterns += [re.escape(key[:cut]) + r"[\s_.\-]" + re.escape(key[cut:]) + r"(?:e?s)?"
                     for cut in range(3, len(key) - 2)]
    if plain and len(key) >= 5:
        patterns.append(re.escape(key[:-1]))  # an inflected name: Consula for Consul
    if plain and len(key) >= 4 and key[-1] in _VOWELS:
        patterns.append(re.escape(key[:-1]) + "[" + _VOWELS + "]")  # Javy for Java
    camel = [part for part in _CAMEL.split(term) if part]
    if len(camel) > 1 and "_" not in term and "." not in term and camel[0].isalpha() and len(camel[0]) >= 5:
        patterns.append(re.escape(camel[0].casefold()))  # arcade_url for ArcadeDB
    return patterns


def mentions(term: str, text: str) -> bool:
    """True when ``text`` names ``term``; see the module docstring for what counts as the same name."""
    spread = _spread(text)
    return any(re.search(rf"(?<!{_LETTER})(?:{pattern})(?!{_LETTER})", spread) for pattern in _patterns(term))


def names_directory(term: str, paths: Iterable[str]) -> bool:
    """True when a directory on one of ``paths`` (relative, POSIX) is called ``term``: the name of
    the project itself or of one of its parts, which every answer is about."""
    key = _key(term)
    return any(_key(part) == key for path in paths for part in str(path).replace("\\", "/").split("/")[:-1])


def _absent(terms: list[str], passages: Iterable[str]) -> list[str]:
    texts = [str(passage) for passage in passages if passage]
    return [term for term in terms if not any(mentions(term, text) for text in texts)]


def missing_subject(question: str, passages: Iterable[str], own_names: Iterable[str] = ()) -> list[str]:
    """The subject names of ``question`` that none of ``passages`` mentions, when they are too many
    for the answer to stand; otherwise empty.

    With one or two names every one must be there. A question with three or more names carries
    context besides its subject (where it was seen, what else was tried), and which of them is the
    subject cannot be told: then at least half of them must be there.
    """
    terms = subject_terms(question, own_names)
    missing = _absent(terms, passages)
    return missing if len(missing) > (0 if len(terms) < 3 else len(terms) // 2) else []


def unnamed_subject(store, project_id: str, question: str, sources: list[dict], evidence: dict) -> list[str]:
    """The subject names of ``question`` that no source quoted by an answer has; empty lets it pass.

    ``sources`` are the answer's verified citations and ``evidence`` the sources the agent read.
    A name is looked for in each quote, in the path of its source and in the rest of that source's
    text. A name that is the project's own (its registered name, the name of its root or of a
    directory that holds current sources) is never missing: every answer is about the project.
    The names come from the question, which a person typed; they are returned as written.
    """
    whole = {source["id"]: source for source in evidence.values()}
    passages = [text for cite in support_citations(sources) for text in (cite["quote"], cite["path"])]
    passages += [source_text(whole[source["id"]]) for source in sources if source.get("id") in whole]
    if not _absent(subject_terms(question), passages):
        return []
    project = store.query("SELECT name, root FROM projects WHERE id=:id", {"id": project_id})
    name, root = (str(project[0].get("name") or ""), str(project[0].get("root") or "")) if project else ("", "")
    own = [name, *name.split(), PurePosixPath(root).name]
    for term in _absent(subject_terms(question, own), passages):
        rows = store.query(
            "SELECT uri FROM sources WHERE project_id=:project AND is_current=true "
            "AND uri.toLowerCase() LIKE :part LIMIT 50",
            {"project": project_id, "part": "%/" + term.casefold() + "/%"}) if root else []
        # LIKE treats '_' as a wildcard, and the directories above the root are not parts of the project.
        inside = [str(row.get("uri", ""))[len(root):] for row in rows if str(row.get("uri", "")).startswith(root)]
        if names_directory(term, inside):
            own.append(term)
    return missing_subject(question, passages, own)
