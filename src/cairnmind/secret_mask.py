"""Known secrets replaced by [REDACTED]: in the agent journal, and in conversation text before it is indexed (#182).

One string replacement serves both. The agent journal (and the answer log) replaces the secrets it was
given wherever they occur. Ingestion replaces, in the readable view of conversation text (a transcript
line's ``normalized_text``, an archived agent event's, a ``remember`` note's) and in the chunks written
from it, the secrets registered with Cairnmind on this machine and the well-known key shapes of the
secrets gate (#171): the service token, the ArcadeDB password, the keys saved in the credential store,
the values behind the ``env:`` references of the saved connection and memory model profiles, the
credential variables of the environment, private key blocks and the token shapes of
``memory_published_secrets.well_known_spans``. The archived raw text is kept as it was received, so its
SHA-256, the source identity and every passage cut from the raw text stay what they were; a source
whose view changed says so with ``metadata.masked`` and the kinds it replaced, never the values.
"""
from __future__ import annotations

import os

MASK = "[REDACTED]"
REGISTERED = "registered_secret"
# A registered value shorter than this is not looked for in conversation text: an ArcadeDB password like
# "admin" would replace an ordinary word everywhere. The gate of published memory uses the same floor.
MIN_REGISTERED = 8
# A bounded tool result may end in a truncated credential: a prefix of at least this many characters at
# the very end of a text is replaced too.
MIN_TRUNCATED = 6


def replace_known(text: str, ordered) -> str:
    """``text`` with every secret of ``ordered`` (longest first) and a truncated one at its end replaced."""
    for secret in ordered:
        text = text.replace(secret, MASK)
        for length in range(min(len(secret) - 1, len(text)), MIN_TRUNCATED - 1, -1):
            if text.endswith(secret[:length]):
                text = text[:-length] + MASK
                break
    return text


def ordered_secrets(values, minimum: int = 1) -> list[str]:
    """The distinct string values of at least ``minimum`` characters, longest first."""
    return sorted({value for value in values if isinstance(value, str) and len(value) >= minimum},
                  key=len, reverse=True)


def _env_references(rows, environ):
    for row in rows:
        reference = row.get("credential_ref") if isinstance(row, dict) else None
        if isinstance(reference, str) and reference.startswith("env:"):
            value = environ.get(reference[4:])
            if value:
                yield value


def registered_secrets(settings, environ=None) -> list[str]:
    """The secrets registered with Cairnmind on this machine, in no particular order.

    Raises ValueError when the saved keys or the saved profiles cannot be read: what cannot be read cannot be
    kept out, so a caller refuses instead of passing text through unchecked."""
    from .api_credentials import CredentialStore, credential_environment_names
    from .cockpit_state import CockpitState
    from .memory_models import FIELDS, profile
    environ = os.environ if environ is None else environ
    values = [getattr(settings, "token", None), getattr(settings, "arcade_password", None)]
    values += CredentialStore(settings).registered_values()
    try:
        profiles = CockpitState(settings).load()["profiles"]
    except (OSError, ValueError) as exc:
        raise ValueError(f"the saved connection profiles cannot be read: {exc}") from None
    values += _env_references(profiles, environ)
    values += _env_references([profile(settings, role) for role in FIELDS], environ)
    names = set(credential_environment_names())
    values += [value for name, value in environ.items()
               if name in names or name.endswith(("_API_KEY", "_AUTH_TOKEN", "_GITHUB_TOKEN"))]
    return [value for value in values if isinstance(value, str) and value]


class Masker:
    """Replaces registered secrets and well-known key shapes in conversation text.

    ``mask`` returns the text itself, unchanged, when it holds nothing to replace: a source without a secret
    is written exactly as before."""

    def __init__(self, secrets=()):
        self.secrets = ordered_secrets(secrets, MIN_REGISTERED)

    def spans(self, text: str):
        """(start, end, kind) of everything to replace, possibly overlapping."""
        from .memory_published_secrets import well_known_spans
        found = []
        for secret in self.secrets:
            start = text.find(secret)
            while start >= 0:
                found.append((start, start + len(secret), REGISTERED))
                start = text.find(secret, start + len(secret))
            for length in range(min(len(secret) - 1, len(text)), MIN_TRUNCATED - 1, -1):
                if text.endswith(secret[:length]):
                    found.append((len(text) - length, len(text), REGISTERED))
                    break
        found += [(start, end, kind) for kind, start, end in well_known_spans(text) if end > start]
        return found

    def mask(self, text: str) -> tuple[str, list[str]]:
        """(the text with every span replaced by [REDACTED], the sorted kinds replaced)."""
        if not isinstance(text, str) or not text:
            return text, []
        spans = sorted(self.spans(text))
        if not spans:
            return text, []
        pieces, position = [], 0
        for start, end, _ in spans:
            if start >= position:
                pieces += [text[position:start], MASK]
            elif end <= position:
                continue  # inside a span already replaced
            position = max(position, end)
        pieces.append(text[position:])
        return "".join(pieces), sorted({kind for _, _, kind in spans})


def masked_metadata(metadata: dict, kinds) -> dict:
    """``metadata`` marked as holding a masked view; the same dict object when nothing was replaced."""
    return {**metadata, "masked": True, "masked_kinds": list(kinds)} if kinds else metadata


def ingest_masker(settings) -> Masker:
    """The masker of conversation ingestion on this machine; raises ValueError when secrets cannot be read."""
    return Masker(registered_secrets(settings))


def note_metadata(settings, text: str, metadata: dict) -> dict:
    """The metadata of a ``remember`` note's source: with a masked ``normalized_text``, which indexing reads
    instead of the raw text, when the note holds a known secret; ``metadata`` itself when it holds none."""
    masked, kinds = ingest_masker(settings).mask(text)
    return masked_metadata({**metadata, "normalized_text": masked}, kinds) if kinds else metadata
