"""The sentence for a check profile a worktree does not declare, in English and Polish (#243).

`cairnmind check run` says it in English (the source language); the workspace reads that sentence back
from the check's error and says it again in the panel's language. Only names made of plain short ASCII
reach either sentence, so nothing else of an error is ever re-rendered as a translated sentence.
"""
from __future__ import annotations

import re

UNKNOWN = ("Unknown check profile: {profile}. This worktree declares: {declared}. "
           "See cairnmind check profiles PROJECT.")
UNKNOWN_NONE = ("Unknown check profile: {profile}. This worktree declares no check profiles. "
                "See cairnmind check profiles PROJECT.")
MESSAGES = {
    UNKNOWN: "Nieznany profil testu: {profile}. Ten katalog roboczy deklaruje: {declared}. "
             "Zobacz cairnmind check profiles PROJECT.",
    UNKNOWN_NONE: "Nieznany profil testu: {profile}. Ten katalog roboczy nie deklaruje profili testów. "
                  "Zobacz cairnmind check profiles PROJECT.",
}
_NAME = r"(?:[A-Za-z0-9._-]{1,64}|\?)"
_PATTERN = re.compile(r"Unknown check profile: (" + _NAME + r")\. This worktree declares(?:: (" + _NAME
                      + r"(?:, " + _NAME + r"){0,9})| no check profiles)\. See cairnmind check profiles PROJECT\.")


def name(value) -> str:
    """A profile name as a message may show it: a plain short name, or a placeholder (names come from input)."""
    text = value if isinstance(value, str) else ""
    return text if 0 < len(text) <= 64 and all(c.isascii() and (c.isalnum() or c in "._-") for c in text) else "?"


def unknown_profile_message(profile_id, profiles) -> str:
    """Names the profile asked for and the ones this worktree declares, so the fix is in the message."""
    declared = ", ".join(sorted(name(each) for each in profiles)[:10])
    if not declared:
        return UNKNOWN_NONE.format(profile=name(profile_id))
    return UNKNOWN.format(profile=name(profile_id), declared=declared)


def check_error_text(error, translate) -> str:
    """A check's error as the panel shows it: the unknown-profile sentence in the panel's language,
    any other error as it is (the caller's notice line makes it safe to show)."""
    text = str(error)
    found = _PATTERN.search(text)
    if not found:
        return text
    profile, declared = found.groups()
    if declared is None:
        return translate(UNKNOWN_NONE).format(profile=profile)
    return translate(UNKNOWN).format(profile=profile, declared=declared)
