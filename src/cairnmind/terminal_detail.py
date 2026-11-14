"""The text of a record and of a possible conflict, built as `(role, string)` parts.

The words are decided here once: `joined(parts)` is the plain text a view keeps and tests read, and
`terminal_theme.assemble(parts)` is the same text with a style on each part. A part is styled only
because this module wrote it (a label, a state it read from a field) or to keep it apart from those
(stored text is `stored`, behind its bar where the view has one). Nothing is styled by looking at
finished text, so stored text that imitates a label keeps the plain colour.
"""
from __future__ import annotations

from datetime import date

from .terminal_theme import evidence_style, state_style

# (evidence_state, quote_survives) as returned by the inspector for one record.
EVIDENCE_LABELS = {
    ("anchored", None): "at least one supporting source still stands",
    ("evidence_replaced", True): "the source changed; the quoted passage is still there",
    ("evidence_replaced", False): "the source changed; the quoted passage is gone",
    ("evidence_replaced", None): "the source changed; there is no verified quoted passage to compare",
    ("orphaned", None): "orphaned: no supporting source has a current version",
    ("unverified", None): "imported file evidence has not been compared with local files yet",
    ("degraded", None): "written by an external client's session, or resting only on one or on a third-party tool result; never verified",
}
_EVIDENCE_TIME = {"stated": "Stated", "observed": "Observed no later than", "mixed": "Mixed evidence"}
BAR = ("quote-bar", "  | ")


def joined(parts) -> str:
    return "".join(text for _, text in parts)


def join_lines(lines, separator="\n"):
    """Lines, each a list of parts, as one list with `separator` between them."""
    parts = []
    for index, line in enumerate(lines):
        if index:
            parts.append(("value", separator))
        parts.extend(line)
    return parts


def labelled(label, value, role="value"):
    """`Label: value`: the label up to its colon is ours; `role` is the style of the value."""
    return [("label", label + ":"), ("value", " "), (role, value)]


def state_role(word):
    """Style of a state read from a field; only an exact known word gets a colour."""
    return state_style(word if isinstance(word, str) else "")


def evidence_parts(item, translate):
    """The `Evidence:` line of a record, or nothing when its state has no sentence."""
    label = EVIDENCE_LABELS.get((item.get("evidence_state"), item.get("quote_survives")))
    if not label:
        return []
    return [labelled(translate("Evidence"), translate(label), evidence_style(item.get("evidence_state")))]


def _evidence_date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat() if len(value) >= 10 else None
    except ValueError:
        return None


def record_parts(value, translate):
    """What a record, source, plan, revision or job says about itself; None when it names nothing readable."""
    if not isinstance(value, dict):
        return None
    item = value.get("record", value.get("item", value.get("source", value)))
    if not isinstance(item, dict):
        return None

    lines = []
    for name in ("title", "content", "uri", "message"):
        if isinstance(item.get(name), str):
            if lines:
                lines.append([("stored", item[name])])
            else:  # the first line names the entry; the rest of the stored text stays plain
                first, newline, rest = item[name].partition("\n")
                lines.append([("title", first), ("stored", newline + rest)])

    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    state = item.get("state", metadata.get("state", item.get("status")))
    if state:
        lines.append(labelled(translate("Status"), str(state), state_role(state)))
    if item.get("version"):
        lines.append(labelled(translate("Version"), str(item["version"])))
    if item.get("source_ids"):
        lines.append(labelled(translate("Sources"), str(len(item["source_ids"]))))

    if isinstance(value.get("record"), dict):
        # Any record detail, including a task opened from the task list.
        earliest = _evidence_date(metadata.get("evidence_earliest"))
        latest = _evidence_date(metadata.get("evidence_latest"))
        basis = metadata.get("evidence_basis")

        if basis == "stated":
            evidence_time = latest or earliest
            if evidence_time:
                lines.append(labelled(translate("Stated"), evidence_time))
        elif basis == "observed":
            evidence_time = latest or earliest
            if evidence_time:
                lines.append(labelled(translate("Observed no later than"), evidence_time))
        elif basis == "mixed":
            if earliest and latest:
                evidence_range = earliest if earliest == latest else f"{earliest} – {latest}"
                lines.append(labelled(translate("Mixed evidence"), evidence_range))

        if metadata.get("supersede_warning") == "anachronistic_supersede":
            replaced_date = _evidence_date(metadata.get("supersede_replaced_evidence_latest"))
            warning = translate("The superseded finding had newer evidence")
            if replaced_date:
                warning += f" ({replaced_date})"
            lines.append([("warn-strong", translate("Warning") + ": " + warning)])

    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    lines += evidence_parts(evidence, translate)
    if not lines:
        return None
    return join_lines(lines, "\n\n")


def _finding_parts(letter, role, item, translate):
    # Stored text is indented behind a bar and loses its blank lines, so it can neither
    # start a line that looks like one of this view's own labels nor fake a second finding.
    content = [[BAR, ("stored", line)] for line in str(item.get("content", "")).splitlines() if line.strip()]
    if item.get("content_truncated"):
        content.append([BAR, ("muted", translate("(the text is longer; open the record to read all of it)"))])
    lines = [[("accent", f"{letter} ({translate(role)})"), ("muted", f"  [{item.get('kind')}] "),
              (state_role(item.get("status")), f"{item.get('status')}"),
              ("muted", f", {translate('Version').lower()} {item.get('version')}")], *content]
    latest, basis = str(item.get("evidence_latest") or "")[:10], item.get("evidence_basis")
    if latest and basis in _EVIDENCE_TIME:
        lines.append(labelled(translate(_EVIDENCE_TIME[basis]), latest))
    lines.append(labelled(translate("Scope"), str(item.get("scope") or translate("whole project"))))
    if item.get("sources"):
        more = item.get("sources_total", 0) - len(item["sources"])
        lines.append(labelled(translate("Sources"), ", ".join(map(str, item["sources"])) + (f" (+{more})" if more > 0 else "")))
    lines += evidence_parts(item, translate)
    return join_lines(lines)


def conflict_parts(pair, translate):
    """Both findings of a possible conflict, one after the other, with what is known about their order."""
    first, second = pair.get("candidate") or {}, pair.get("existing") or {}
    newer = pair.get("newer_evidence")
    letter = "A" if newer == first.get("id") else "B" if newer == second.get("id") else None
    if letter and pair.get("newer_evidence_basis") == "stated":
        order = translate("{letter} has the newer evidence; both times were stated by their sources.", letter=letter)
    elif letter:
        order = translate("{letter} was recorded later, but its evidence time is only an upper bound.", letter=letter)
    else:
        order = translate("The order of the evidence cannot be compared.")
    return join_lines([[("title", translate("Possible conflict"))], _finding_parts("A", "new finding", first, translate),
                       _finding_parts("B", "existing finding", second, translate), [("warn", order)]], "\n\n")


def conflict_text(pair, translate):
    return joined(conflict_parts(pair, translate))
