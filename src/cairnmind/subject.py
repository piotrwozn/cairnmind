"""Subject gate: is the thing a question names anywhere in what the answer quotes?

The support check (support.py) asks a model whether the quotes state the answer, and a model can
accept a quote that only shares a number: the default port of a product the project never
mentions, "supported" by the line that sets the service's own port. This check does not ask a
model. The names a question uses for its subject must occur in a verified quote, in the path of a
quoted source or elsewhere in the text of that source; when one of them occurs in none of these,
the answer is not complete. It can only refuse: it never makes an answer complete. The rest of the
source counts because a right quote often leaves the name out (the line "1 GB heap" under a
heading about the JVM answers a question about the Java heap); a source that never names the
thing asked about cannot be evidence about it. Every name is needed, however many the question
has: which of them is the subject and which is context ("on Debian") cannot be told, and a source
that never says Debian does not show what holds on Debian.

What counts as a name is decided by the written shape of the word, never by a list of products:

- an identifier: a word with an underscore, a dot between letters (``pyproject.toml``), a capital
  inside (``ArcadeDB``), or letters and digits together (``qwen3``);
- a proper name: a capitalised word (``Consul``, ``Java``). The word that begins a sentence is
  capitalised by the language, so there it is a name only when it is not one of the words that
  open a question (OPENERS: question words, auxiliaries, prepositions, requests, the adjectives
  of a short question) and is not written in lower case elsewhere in the question;
- one word alone between backticks (```consul```).

Left out: words of the language and words every project has (GENERIC: default, port, file,
value, setting ...), words written in capitals only (URL, GPU, TTL are more often common words
than names, and capitals are also emphasis), numbers and versions, pasted values (a token, a
hash, an id: a long unbroken run with a digit, or a word with single characters between its
digits and capitals), and the project's own names, which the caller passes. A question without
such a word is not checked at all: the gate then does not apply. A product name written in lower
case without backticks has no shape to go by and is not read either; such a question rests on the
support check alone. So does one asked with an answer schema, which is a caller's instruction full
of field names (the caller of ``unnamed_subject`` leaves those out). In a question written in
headline case (most words capitalised) a capital says nothing; there a capitalised word is read
only when it stands directly before one of the GENERIC words ("Zookeeper Port", "Redis Version"),
the place of the name of the thing whose port or version is asked: "Listen" and "Heap" are not
names. A name elsewhere in such a question is not read, and an ordinary word before a generic one
("Local Service") is read and passes when the quoted source has it.

The names an answer introduces are read by the same shapes and must occur in a quote or the path
of its source (``unnamed_answer_names``); a lower-case name has no shape in an answer either, which
is why a separate model call (support.py, ``names_messages``) asks about the names of an answer.

A name matches without regard to case, across ``_``, ``-``, ``.`` and spaces where the name itself
has a seam (``num_ctx`` in "num ctx", ``ArcadeDB`` in "arcade_db"), split once where it has none
(``Zookeeper`` in "zoo_keeper"), as a part of an identifier (``Consul`` in ``consulPort`` or
``consul_port``), with a plural or possessive ending, and with ``db``, ``sql``, ``ql`` or ``js``
written after it (``Mongo`` in "mongodb"). A part of a compound name does not stand for the name
("a graph" and ``graph_rows`` are not GraphQL). ``.yml`` and ``.yaml`` are one ending. An identifier with underscores is also found without its first part when that part is one
of the project's own names or stands in the source as a prefix by itself (``"EXAMPLE_"`` beside
``PORT`` for EXAMPLE_PORT). Only in a question in an inflecting language (told by a Polish word or
a letter outside ASCII) does a name also match without its case ending: "Consula", "Javy",
"Consulem", "Ollamie" for Consul, Java and Ollama. In an English question the last letter belongs
to the name.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import PurePosixPath

from .extraction_input import source_text
from .support import support_citations

# What an answer refused by this check says is missing: this fixed prefix, then the names as the
# question wrote them (a person's text, kept as it is).
SUBJECT_GAP = "The quoted sources do not name what the question asks about: "
# The same for a name the answer itself introduces (unnamed_answer_names, and the model's names call).
NAMES_GAP = "The quoted sources do not state the names the answer gives: "
# The deterministic check of those names reads a name in an answer by its written shape and is
# stricter than the subject check (quote and path only), so it is built but not in the answer path
# until scripts/replay_subject_check.py shows over the kept reports that it refuses no right answer
# (the owner's rule of 15 and 22 November 2026). Then this becomes True. The names call is not gated.
CHECK_ANSWER_NAMES = False

# Words of the language that can be capitalised inside a sentence without naming anything.
STOP = frozenset("""
a an and are as at be by can did do does for from has have how i if in is it its may must no not
of on or our should than that the their then there these this those to was were what when where
which who whom whose why will with would you your e.g i.e etc ok yes
monday tuesday wednesday thursday friday saturday sunday january february march april june july
august september october november december
""".split())
# Polish words no English question has: one of them, or a letter outside ASCII, marks a question
# whose names may carry a case ending. They also open a sentence without naming anything.
POLISH = frozenset("""
czy dla ile jak jaka jaki jakie jaką jakiego jakiej jakim jakich jakiś jakaś która które który
którą którego której którym których gdzie kiedy dlaczego czemu skąd kto kogo czego czym oraz ale
więc jeśli jeżeli gdy przez przy bez między podaj pokaż wymień wyjaśnij opisz powiedz sprawdź
znajdź wypisz proszę tego tej tym tych był była było będzie może można trzeba należy mają się są
że jest domyślny domyślna domyślne domyślnie największy największa najmniejszy najmniejsza
najdłuższy najkrótszy maksymalny maksymalna minimalny minimalna aktualny aktualna obecny obecna
adres wersja nazwa wartość liczba rozmiar czas ścieżka katalog plik baza usługa ustawienie
ustawienia licencja hasło klucz
""".split())
# Words that begin a question or a request: capitalised there by the language, never a name.
OPENERS = POLISH | frozenset("""
what which who whom whose where when why how
is are was were am be been do does did has have had can could should shall would will may might must
please show list tell give find explain describe name print get check look say summarize summarise
compare count display provide return report state specify define help see read open search identify
confirm verify clarify remind enumerate let using running run use add create delete remove set make
import export prepare need want question
in on at by for from with within without under over above below after before during about against
between into through via per since until upon to of off out up down near inside outside across along
and or but so also now then currently actually just ok okay hi hello hey thanks thank quick btw fyi
again still well right sure maybe perhaps given assuming suppose if while once according regarding
concerning besides apart except despite although though because as than not no yes
the a an any all each every some most many much few several both either neither another other such
same our my your his her their its this that these those there here one two three first second
last next previous it we you they he she
largest smallest biggest highest lowest longest shortest latest newest oldest earliest fastest
slowest current present exact actual total full main primary only average typical usual
recommended required supported allowed expected new old big small long short high low
co na nie od po do ten ta te to ma pod nad
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
_HUMP = "\x1f"  # stands where an identifier changes case: consulPort is read as consul, HUMP, port
MAX_TERMS = 8
MAX_TERM_CHARS = 64
MAX_SPLIT_CHARS = 24  # a longer word is not looked for in two parts: one pattern per place to split
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


def _generic(folded: str) -> bool:
    return folded in GENERIC or (folded.endswith("s") and folded[:-1] in GENERIC)


def _starts_sentence(question: str, position: int) -> bool:
    before = question[:position].rstrip(" \t\r\n\"'`([{*")
    return not before or before[-1] in ".?!:;"


def inflecting(question: str) -> bool:
    """True when ``question`` is in a language whose names take case endings, as far as this check
    can tell: it has a Polish word (in lower case or first in its sentence) or a letter outside ASCII."""
    question = str(question)[:MAX_QUESTION_CHARS]
    return any(not character.isascii() and character.isalpha() for character in question) or any(
        match.group().casefold() in POLISH
        and (match.group().islower() or _starts_sentence(question, match.start()))
        for match in _WORD.finditer(question))


def subject_terms(question: str, own_names: Iterable[str] = ()) -> list[str]:
    """The names ``question`` uses for its subject, in order, each once; empty when it has none."""
    # A long unbroken run with a digit is a pasted value (a token, a hash, an id): blanked, not read.
    question = _VALUE.sub(lambda match: " " * len(match.group()), str(question)[:MAX_QUESTION_CHARS])
    own = {_key(name) for name in own_names if name}
    alone = {match.start(1) for match in _CODE.finditer(question)
             if _WORD.fullmatch(match.group(1))}
    words = list(_WORD.finditer(question))
    # In a question written in headline case (most words capitalised) a capital names nothing.
    inner = [match.group() for match in words
             if _letters(match.group()) >= 2 and not _starts_sentence(question, match.start())]
    headline = len(inner) >= 4 and sum(word[0].isupper() for word in inner) * 2 > len(inner)
    lower = {match.group() for match in words if match.group().islower()}
    found: dict[str, str] = {}
    for index, match in enumerate(words):
        word = match.group().strip("_")
        folded = word.casefold()
        if (_letters(word) < 2 or len(word) > MAX_TERM_CHARS or folded in STOP or _generic(folded)
                or _key(word) in own):
            continue
        named = word[0].isupper() and not word.isupper()
        if named and (headline or _starts_sentence(question, match.start())):
            # The language capitalises this word: a name only when it is no opener of a question
            # and the question does not also write it as an ordinary word.
            named = _letters(word) >= 3 and folded not in OPENERS and folded not in lower
            # In headline case every word is capitalised: only one that qualifies a generic word
            # ("Zookeeper Port", "Redis Version") is read; "Listen" and "Heap" are ordinary words.
            if named and headline:
                after = words[index + 1] if index + 1 < len(words) else None
                named = bool(after and question[match.end():after.start()].isspace()
                             and _generic(after.group().casefold()))
        if _identifier(word) or named or (match.start() in alone and _letters(word) >= 3):
            found.setdefault(_key(word), word)
    return list(found.values())[:MAX_TERMS]


def _key(word: str) -> str:
    return re.sub(r"[\W_]+", "", str(word).casefold())


def _spread(text: str) -> str:
    """``text`` folded, with a mark where an identifier changes case (consulPort: consul, mark, port)."""
    return _CAMEL.sub(_HUMP, str(text)).casefold()


def _parts(term: str) -> list[str]:
    return [part.casefold() for part in _SEAM.split(term) if part]


@lru_cache(maxsize=512)
def _matcher(term: str, inflected: bool) -> tuple[str, re.Pattern] | None:
    """The first letters every written form of ``term`` begins with, and the pattern of all its forms."""
    parts = _parts(term)
    if not parts:
        return None
    key = "".join(parts)
    last = "ya?ml" if len(parts) > 1 and parts[-1] in ("yml", "yaml") else re.escape(parts[-1])
    patterns = [r"[\W_]*".join([*(re.escape(part) for part in parts[:-1]), last]) + r"(?:e?s)?"]
    plain = len(parts) == 1 and key.isalpha()
    if plain and len(key) >= 4:
        patterns.append(re.escape(key) + r"(?:db|sql|ql|js)")  # mongodb for Mongo, postgresql for Postgres
    if plain and 6 <= len(key) <= MAX_SPLIT_CHARS:
        # A name without a seam of its own, written in two parts: zoo_keeper, Zoo Keeper, zooKeeper.
        patterns += [re.escape(key[:cut]) + r"[\s_.\-" + _HUMP + "]" + re.escape(key[cut:]) + r"(?:e?s)?"
                     for cut in range(3, len(key) - 2)]
    if plain and inflected:
        if len(key) >= 5:
            patterns.append(re.escape(key[:-1]))  # Consula for Consul
        if len(key) >= 4 and key[-1] in _VOWELS:
            patterns.append(re.escape(key[:-1]) + "[" + _VOWELS + "]")  # Javy for Java
        # Consulem for Consul, Ollamie for Ollama: an ending of two or three letters on five or more.
        patterns += [re.escape(key[:-cut]) + "[" + _VOWELS + "]?" for cut in (2, 3) if len(key) - cut >= 5]
    return parts[0][:3], re.compile(rf"(?<!{_LETTER})(?:{'|'.join(patterns)})(?!{_LETTER})")


def _in(term: str, spread: str, inflected: bool = False) -> bool:
    matcher = _matcher(term, inflected)
    return bool(matcher and matcher[0] in spread and matcher[1].search(spread))


def mentions(term: str, text: str, inflected: bool = False) -> bool:
    """True when ``text`` names ``term``; see the module docstring for what counts as the same name.
    ``inflected`` also accepts the name without a case ending (a question in Polish)."""
    return _in(term, _spread(text), inflected)


def _without_prefix(term: str, spread: str, own: set[str], inflected: bool) -> bool:
    """True when ``spread`` names an identifier with underscores without its first part, and that
    part is a name of the project or stands in the text as a prefix by itself ("EXAMPLE_")."""
    parts = _parts(term)
    if len(parts) < 2 or "_" not in term or "." in term:
        return False
    head = re.escape(parts[0])
    prefixed = parts[0] in own or (parts[0] in spread
                                   and re.search(rf"(?<!{_LETTER}){head}_(?!\w)", spread) is not None)
    return prefixed and _in("_".join(parts[1:]), spread, inflected)


def names_directory(term: str, paths: Iterable[str]) -> bool:
    """True when a directory on one of ``paths`` (relative, POSIX) is called ``term``: the name of
    the project itself or of one of its parts, which every answer is about."""
    key = _key(term)
    return any(_key(part) == key for path in paths for part in str(path).replace("\\", "/").split("/")[:-1])


def _absent(terms: list[str], spreads: list[str], own: set[str], inflected: bool) -> list[str]:
    return [term for term in terms
            if not any(_in(term, spread, inflected) or _without_prefix(term, spread, own, inflected)
                       for spread in spreads)]


def missing_subject(question: str, passages: Iterable[str], own_names: Iterable[str] = ()) -> list[str]:
    """The subject names of ``question`` that none of ``passages`` mentions; empty lets it pass."""
    own = {_key(name) for name in own_names if name}
    spreads = [_spread(passage) for passage in passages if passage]
    return _absent(subject_terms(question, own_names), spreads, own, inflecting(question))


def unnamed_subject(store, project_id: str, question: str, sources: list[dict], evidence: dict) -> list[str]:
    """The subject names of ``question`` that no source quoted by an answer has; empty lets it pass.

    ``sources`` are the answer's verified citations and ``evidence`` the sources the agent read.
    A name is looked for in each quote, in the path of its source and in the rest of that source's
    text. A name that is the project's own (its registered name, the name of its root or of a
    directory that holds current sources) is never missing: every answer is about the project.
    The names come from the question, which a person typed; they are returned as written.
    Each text is read once, and the store is asked only about names that are missing.
    """
    terms = subject_terms(question)
    if not terms:
        return []
    whole = {source["id"]: source for source in evidence.values()}
    passages = [text for cite in support_citations(sources) for text in (cite["quote"], cite["path"])]
    passages += [source_text(whole[source["id"]]) for source in sources if source.get("id") in whole]
    spreads = [_spread(passage) for passage in passages if passage]
    inflected = inflecting(question)
    return _not_own(store, project_id, _absent(terms, spreads, set(), inflected), spreads, inflected)


def unnamed_answer_names(store, project_id: str, question: str, answer: str, sources: list[dict]) -> list[str]:
    """The names ``answer`` introduces that no source it quotes has; empty lets it pass.

    A name is read from the answer by the same written shape as the question's (``subject_terms``),
    and one the question already has is not introduced by the answer. Each must occur in a verified
    quote or in the path of its source, nowhere else: the rest of the source does not count, because
    the answer asserts the name and the quote has to show it. The project's own names are never
    missing. Not in the answer path while ``CHECK_ANSWER_NAMES`` is False.
    """
    terms = _introduced(question, answer)
    if not terms:
        return []
    spreads = [_spread(text) for cite in support_citations(sources) for text in (cite["quote"], cite["path"]) if text]
    inflected = inflecting(answer)
    return _not_own(store, project_id, _absent(terms, spreads, set(), inflected), spreads, inflected)


_DOTTED = re.compile(r"(?<=[^\W\d])\.(?=[^\W\d])")  # a dot between letters: Settings.chat_model


def _name_pattern(name: str) -> re.Pattern:
    """The pattern of one name (casefolded) in casefolded text; see ``name_in``."""
    core = r"[\s_\-]*".join(re.escape(word) for word in name.split())
    lead = r"(?<!\w)" + (r"(?<!\d\.)" if name[0].isdigit() else "")
    tail = r"(?!\w)" + (r"(?!\.\d)" if name[-1].isdigit() else "")
    # A flag takes its value (-Xmx2g): after "-" or in a name that starts with it, digits may follow.
    flag = r"(?![^\W\d]|_)"
    return re.compile(rf"{lead}(?:{core}{tail}|{core}{flag if name[0] == '-' else '(?!)'}|(?<=-){core}{flag})")


def name_in(name: str, text: str) -> bool:
    """True when ``text`` (any case) states ``name`` as a whole: not as a part of a longer word
    (``Java`` is not in "JavaScript", ``3.12`` not in "13.12" or "3.12.1"), with ``_`` ``-`` or
    nothing where the name has a space ("Java 21" in "java-21"). Case-insensitive, no endings, no
    model. A name of several words, or a dotted one (``Settings.chat_model``), that does not stand as
    written is found when each of its words stands on its own in ``text``; the version of
    "ArcadeDB 26.11.1" and its name need not be written together."""
    name, text = " ".join(str(name).casefold().split()), str(text).casefold()
    if not name:
        return False
    if _name_pattern(name).search(text):
        return True
    parts = [part for word in name.split() for part in _DOTTED.split(word) if part]
    return len(parts) > 1 and all(_name_pattern(part).search(text) for part in parts)


def found_names(names: Iterable[str], question: str, sources: list[dict], evidence: dict) -> list[str]:
    """Of the ``names`` a model reported as unstated, those that stand in a source the answer cites (its
    verified quotes, their paths and the text of the sources). The question is not searched: a name the
    question carries (nginx in "the nginx port") is exactly what an absent answer must not borrow from it,
    so ``question`` is kept only for the signature. The name is looked for as ``name_in`` says; a source
    that was read but not cited, or whose citation failed, is not in ``sources``."""
    whole = {source["id"]: source for source in evidence.values()}
    passages = [text for cite in support_citations(sources) for text in (cite["quote"], cite["path"])]
    passages += [source_text(whole[source["id"]]) for source in sources if source.get("id") in whole]
    texts = [text for text in passages if text]
    return [name for name in names if any(name_in(name, text) for text in texts)]


def _introduced(question: str, answer: str, own_names: Iterable[str] = ()) -> list[str]:
    asked = inflecting(question)
    return [term for term in subject_terms(answer, own_names) if not mentions(term, question, asked)]


def missing_answer_names(question: str, answer: str, passages: Iterable[str], own_names: Iterable[str] = ()) -> list[str]:
    """The names ``answer`` introduces that none of ``passages`` (the quotes and paths) mentions, without
    a store: the project's own names are passed. What the replay script (scripts/replay_subject_check.py) runs."""
    own = {_key(name) for name in own_names if name}
    spreads = [_spread(passage) for passage in passages if passage]
    return _absent(_introduced(question, answer, own_names), spreads, own, inflecting(answer))


def _not_own(store, project_id: str, absent: list[str], spreads: list[str], inflected: bool) -> list[str]:
    """Of the ``absent`` names, those that are not the project's own (its name, the name of its root
    or of a directory that holds current sources, or an identifier whose prefix is the project's)."""
    if not absent:
        return []
    project = store.query("SELECT name, root FROM projects WHERE id=:id", {"id": project_id})
    name, root = (str(project[0].get("name") or ""), str(project[0].get("root") or "")) if project else ("", "")
    own = {_key(item) for item in (name, *name.split(), PurePosixPath(root).name) if item}
    missing = []
    for term in absent:
        if _key(term) in own or any(_without_prefix(term, spread, own, inflected) for spread in spreads):
            continue
        rows = store.query(
            "SELECT uri FROM sources WHERE project_id=:project AND is_current=true "
            "AND uri.toLowerCase() LIKE :part LIMIT 50",
            {"project": project_id, "part": "%/" + term.casefold() + "/%"}) if root else []
        # LIKE treats '_' as a wildcard, and the directories above the root are not parts of the project.
        inside = [str(row.get("uri", ""))[len(root):] for row in rows if str(row.get("uri", "")).startswith(root)]
        if not names_directory(term, inside):
            missing.append(term)
    return missing
