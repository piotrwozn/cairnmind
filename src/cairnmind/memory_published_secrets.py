"""The secrets gate of published memory (#171): what must never reach a commit, found without a network.

Publication refuses instead of masking. Every text a published file would carry is scanned: record
text, keys, quotes, task criteria, plan titles, goals and constraints, every passage (workspace notes,
review audits, releases) and every string of a source reference. A hit names the file, the field and
the kind of hit, and never any part of the value.

Detected: the secrets registered with Cairnmind on this machine (its service token, the ArcadeDB
password, saved API keys and the credential variables of the environment), private key blocks,
well-known key and token shapes, bearer tokens, connection strings with a password, and long strings
that look random. Personal data (e-mail addresses, phone numbers, the project's own patterns) is off
by default, because records about who owns what legitimately name people; a project turns it on in
`.cairnmind/publication.toml`. Nothing here promises that nothing private can pass.

A person can release one record after reading it: a release names the field, the kind and the SHA-256
of each value it lets through, with a reason, and is published as a source of that record. A
registered secret is never released.
"""
from __future__ import annotations

import hashlib
import math
import re
import tomllib
import unicodedata

REGISTERED = "registered_secret"
PRIVATE_KEY = "private_key"
HIGH_ENTROPY = "high_entropy"
MIN_REGISTERED = 8
MAX_PATTERNS = 20
PERSONAL_KINDS = ("email", "phone")
_PERSONAL = {
    "email": re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}\b"),
    # A leading + or at least two separators, so that counts, sizes and times are not taken for numbers.
    "phone": re.compile(r"(?<![\w+-])(?:\+\d{1,3}[ .-]?(?:\(?\d{1,4}\)?[ .-]?){2,5}\d{2,4}"
                        r"|(?!\(?\d{4}-\d{2}-\d{2})\(?\d{2,4}\)?[ .-]\d{2,4}[ .-]\d{2,4}(?:[ .-]\d{2,4})?)(?![\w-])"),
}
# Order matters only where shapes overlap: the first kind to claim a span keeps it.
_SHAPES = (
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AROA|ANPA|ANVA|APKA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("azure_storage_key", re.compile(r"AccountKey=[A-Za-z0-9+/]{80,}={0,2}")),
    ("bearer_token", re.compile(r"(?i)\b(?:bearer|authorization:\s*(?:basic|token))\s+"
                                r"(?=[A-Za-z._~+/-]*\d)[A-Za-z0-9._~+/-]{16,}=*")),
)
_KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_KEY_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
# The lines after a BEGIN line that belong to the key: PEM headers ("Proc-Type: 4,ENCRYPTED") and base64
# lines, separated by real line breaks or by the escaped "\n" of a JSON line.
_KEY_BODY = re.compile(r"(?:(?:\r?\n|\\n)[ \t]*(?:(?:Proc-Type|DEK-Info|Comment): [^\r\n\\]*"
                       r"|[A-Za-z0-9+/]{16,}={0,2}|[A-Za-z0-9+/]{1,15}={1,2})[ \t]*(?=\r?\n|\\n|$))*")
_URL_PASSWORD = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{1,30}://[^\s/:@'\"<>]{1,256}:([^\s/@'\"<>]{1,256})@[^\s/'\"<>]+")
_PAIR_PASSWORD = re.compile(r"(?i)(?:^|[;\s\"'])(?:password|pwd)\s*=\s*([^;\s\"']{4,})")
_PAIR_CONTEXT = re.compile(r"(?i)(?:^|[;\s\"'])(?:server|host|data source|user id|uid|database)\s*=")
_PLACEHOLDER = re.compile(r"(?i)^(?:[$<{%\[].*|\*+|x+|\.+|password|passwd|pass|pwd|secret|redacted|\[redacted\])$")
_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")
_HEXISH = re.compile(r"[0-9a-fA-F]+")
# Words, identifiers and paths are pieces between separators that are short, hex (a digest or a commit),
# or runs of at least two letters of one case (or a capitalised word) and numbers. The pieces of a
# random string are long and mix cases letter by letter, so it almost never splits like that.
_SEPARATORS = re.compile(r"[/_+=.-]")
_WORDS = re.compile(r"(?:[A-Z]?[a-z]{2,}|[A-Z]{2,}|\d+)+")
SHORT_PIECE = 6
ENTROPY_BITS = 4.0
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SecretRulesError(ValueError):
    """A publication configuration that cannot be used."""


def value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _entropy(text):
    counts = {}
    for character in text:
        counts[character] = counts.get(character, 0) + 1
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def random_looking(token: str) -> bool:
    """A long string that looks random: never a hex digest, a commit hash, a UUID, a path or an identifier.

    At least 32 characters; not made only of word-like pieces (see _WORDS: this is what keeps SHA-256
    digests, commit hashes, UUIDs, `objects/<digest>` paths and snake_case or CamelCase names out); at
    least two of lower case, upper case and digits; and at least 4 bits of entropy per character."""
    core = token.strip("=")
    if len(core) < 32 or all(len(piece) <= SHORT_PIECE or _HEXISH.fullmatch(piece) or _WORDS.fullmatch(piece)
                             for piece in _SEPARATORS.split(core)):
        return False
    classes = sum((any(c.islower() for c in core), any(c.isupper() for c in core), any(c.isdigit() for c in core)))
    return classes >= 2 and _entropy(core) >= ENTROPY_BITS


class Rules:
    """What a scan looks for. Registered secrets exist only on the publishing machine; CI has none."""

    def __init__(self, registered=(), personal=(), patterns=()):
        self.registered = sorted({value for value in registered if isinstance(value, str) and len(value) >= MIN_REGISTERED},
                                 key=len, reverse=True)
        self.personal = tuple(kind for kind in PERSONAL_KINDS if kind in personal)
        self.patterns = tuple(patterns)

    def without_registered(self):
        return Rules((), self.personal, self.patterns)


def load_rules(text: str | None, registered=()) -> Rules:
    """Rules from `.cairnmind/publication.toml`; personal data stays off when the file says nothing.

    [personal]
    detect = ["email", "phone"]
    patterns = [{name = "employee-id", regex = "EMP-[0-9]{6}"}]
    """
    if text is None:
        return Rules(registered)
    try:
        value = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        raise SecretRulesError("publication.toml is not valid TOML") from None
    if set(value) - {"personal"} or not isinstance(value.get("personal", {}), dict):
        raise SecretRulesError("publication.toml may hold only a [personal] table")
    personal = value.get("personal", {})
    if set(personal) - {"detect", "patterns"}:
        raise SecretRulesError("[personal] takes only detect and patterns")
    detect, patterns = personal.get("detect", []), personal.get("patterns", [])
    if not isinstance(detect, list) or any(item not in PERSONAL_KINDS for item in detect):
        raise SecretRulesError("[personal] detect lists only \"email\" and \"phone\"")
    if not isinstance(patterns, list) or len(patterns) > MAX_PATTERNS:
        raise SecretRulesError(f"[personal] patterns is a list of at most {MAX_PATTERNS} tables")
    compiled, names = [], set()
    for item in patterns:
        if (not isinstance(item, dict) or set(item) != {"name", "regex"} or not isinstance(item["name"], str)
                or not _NAME.fullmatch(item["name"]) or item["name"] in names or not isinstance(item["regex"], str)
                or not 0 < len(item["regex"]) <= 200):
            raise SecretRulesError("each personal pattern has a distinct lowercase name and a regex of at most "
                                   "200 characters")
        try:
            compiled.append((f"personal:{item['name']}", re.compile(item["regex"])))
        except re.error:
            raise SecretRulesError(f"the personal pattern {item['name']} is not a valid regular expression") from None
        names.add(item["name"])
    return Rules(registered, detect, compiled)


def _overlaps(span, taken):
    return any(span[0] < end and start < span[1] for start, end in taken)


def _visible(text: str) -> str:
    """The text without format characters (Unicode category Cf: zero-width spaces and joiners, soft hyphens,
    word joiners), which pass the published format's character rule but cannot be seen in a diff."""
    if text.isascii():
        return text
    return "".join(character for character in text if unicodedata.category(character) != "Cf")


def well_known_spans(text: str, bounded: bool = False):
    """(kind, start, end) of every private key block and well-known key or token shape in ``text``, in the
    order the scan claims them. Ingestion (#182) replaces exactly these, beside the registered secrets.

    A BEGIN line without its END line claims the rest of the text, which is right for refusing a publication.
    ``bounded`` (ingestion, which rewrites conversation text) ends such a block after the key lines that
    follow its BEGIN line instead, so a message that only names the header keeps the rest of its words."""
    for begin in _KEY_BEGIN.finditer(text):
        end = _KEY_END.search(text, begin.end())
        if end:
            yield PRIVATE_KEY, begin.start(), end.end()
        else:
            yield PRIVATE_KEY, begin.start(), _KEY_BODY.match(text, begin.end()).end() if bounded else len(text)
    for kind, pattern in _SHAPES:
        for match in pattern.finditer(text):
            yield kind, match.start(), match.end()


def scan_text(text: str, rules: Rules):
    """[(kind, value)] found in one text. Each span is claimed by the first kind that finds it.

    The scan reads the text as a reader sees it: a secret split by an invisible character is still found."""
    text = _visible(text)
    found, taken = [], []

    def claim(kind, start, end):
        if end > start and not _overlaps((start, end), taken):
            taken.append((start, end))
            found.append((kind, text[start:end]))

    for secret in rules.registered:
        start = text.find(secret)
        while start >= 0:
            claim(REGISTERED, start, start + len(secret))
            start = text.find(secret, start + 1)
    for kind, start, end in well_known_spans(text):
        claim(kind, start, end)
    for match in _URL_PASSWORD.finditer(text):
        if not _PLACEHOLDER.fullmatch(match.group(1)):
            claim("connection_password", match.start(), match.end())
    if _PAIR_CONTEXT.search(text):
        for match in _PAIR_PASSWORD.finditer(text):
            if not _PLACEHOLDER.fullmatch(match.group(1)):
                claim("connection_password", match.start(1), match.end(1))
    for match in _CANDIDATE.finditer(text):
        if random_looking(match.group(0)):
            claim(HIGH_ENTROPY, match.start(), match.end())
    for kind in rules.personal:
        for match in _PERSONAL[kind].finditer(text):
            claim(kind, match.start(), match.end())
    for kind, pattern in rules.patterns:
        for match in pattern.finditer(text):
            claim(kind, match.start(), match.end())
    return found


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)


def record_texts(model):
    """(field, text) of every text a published record or plan file carries, named so that a field means the
    same thing in every revision: a release made for one value holds wherever that value stays."""
    for source_id in sorted(model["sources"]):
        source = model["sources"][source_id]
        for text in _strings(source["ref"]):
            yield f"source {source_id} reference", text
        if source["passage"] is not None:
            yield f"source {source_id} passage", source["passage"]
    if model["kind"] == "plan":
        for definition in model["definitions"]:
            yield "title", definition["title"]
            yield "goal", definition["goal"]
            for text in _strings(definition["constraints"]):
                yield "constraints", text
            for text in _strings([step[1] for step in definition["steps"]]):
                yield "steps", text
        return
    for revision in model["revisions"]:
        yield "text", revision["text"]
        yield "key", revision["key"]
        if revision["quote"] is not None:
            yield "quote", revision["quote"]["text"]
        for name in ("step_key", "required_profiles", "acceptance_criteria", "criterion_checks", "scope"):
            for text in _strings(revision.get(name)):
                yield name, text


def releases_of(model):
    """{(field, kind, value_sha256)} that the release sources of a record file let through."""
    from .memory_published_format import parse_json, ref_class
    allowed = set()
    for source in model["sources"].values():
        if ref_class(source["ref"]) != "release":
            continue
        release = parse_json(source["passage"])
        if release.get("record_id") != model["id"]:
            continue
        allowed |= {(hit["field"], hit["kind"], hit["value_sha256"]) for hit in release["hits"]
                    if hit["kind"] != REGISTERED}
    return allowed


def model_hits(model, path, rules: Rules, *, released=True):
    """[{path, field, kind, value_sha256}] of one file, without what its releases let through."""
    allowed = releases_of(model) if released and model["kind"] != "plan" else set()
    hits, seen = [], set()
    for field, text in record_texts(model):
        for kind, value in scan_text(text, rules):
            item = (field, kind, value_sha256(value))
            if item in seen or (kind != REGISTERED and item in allowed):
                continue
            seen.add(item)
            hits.append({"path": path, "field": field, "kind": kind, "value_sha256": item[2]})
    return hits


def scan_models(models, rules: Rules | None = None):
    """Every hit in a set of models ({"records", "plans", "paths"}), ordered by path, field and kind."""
    rules = rules or Rules()
    hits = []
    for identifier, model in sorted([*models["records"].items(), *models["plans"].items()]):
        hits += model_hits(model, models["paths"][identifier], rules)
    return sorted(hits, key=lambda hit: (hit["path"], hit["field"], hit["kind"], hit["value_sha256"]))


def described(hit) -> str:
    """A hit as printed: the file, the field and the kind, never the value or its hash."""
    return f"{hit['path']}: {hit['kind'].replace('_', ' ')} in {hit['field']}"


RELEASE_FIELDS = frozenset({"action", "actor", "actor_name", "at", "branch", "commit", "hits", "reason", "record_id"})
MAX_RELEASE_REASON = 2000
MAX_RELEASE_HITS = 100


def _valid_hit(hit) -> bool:
    return (isinstance(hit, dict) and set(hit) == {"field", "kind", "value_sha256"} and isinstance(hit["field"], str)
            and isinstance(hit["kind"], str) and hit["kind"] != REGISTERED and bool(hit["kind"])
            and isinstance(hit["value_sha256"], str) and bool(_SHA256.fullmatch(hit["value_sha256"])))


def release_problem(passage, record_id):
    """Why a release passage cannot stand in the file of `record_id`; None when it can.

    A release is the canonical JSON of {action "release", actor, actor_name, at, branch, commit, hits,
    reason, record_id}, with a reason and at least one hit, sorted, none of them a registered secret."""
    from .memory_published_format import J, PublishedFormatError, canonical_uuid, clean_value, parse_json, timestamp
    try:
        value = parse_json(passage)
    except PublishedFormatError:
        return "is not JSON"
    if not isinstance(value, dict) or set(value) != RELEASE_FIELDS or value["action"] != "release" \
            or value["actor"] != "local_user" or not clean_value(value) or J(value) != passage:
        return "is not a release as Cairnmind writes it"
    if value["record_id"] != record_id or not canonical_uuid(record_id):
        return "releases another record"
    if not timestamp(value["at"]) or not all(item is None or isinstance(item, str)
                                             for item in (value["actor_name"], value["branch"], value["commit"])):
        return "has no valid time or author"
    reason, hits = value["reason"], value["hits"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_RELEASE_REASON:
        return f"needs a reason of at most {MAX_RELEASE_REASON} characters"
    if (not isinstance(hits, list) or not 1 <= len(hits) <= MAX_RELEASE_HITS or not all(_valid_hit(h) for h in hits)
            or hits != sorted(hits, key=J) or len({J(h) for h in hits}) != len(hits)):
        return "must list the hits it lets through, sorted and distinct; a registered secret is never released"
    return None
