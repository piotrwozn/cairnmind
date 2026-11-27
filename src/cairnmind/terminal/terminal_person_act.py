"""A person's act from a workspace panel goes out only from a consent dialog (#332).

The rule: a panel sends a request to a path of person_proof.PERSON_BROKER_PATH that writes only after `agreed`
returned True. `agreed` shows ConfirmModal(consent=True), which counts only input made after it was drawn (#318),
with what will be done; a form before it only collects text and its button says Continue. Not covered, on purpose:
the quick finding popup (workspace_finding), which sends without a dialog by the owner's decision of 22 November
2026 (the person typed the text and sends it, so the form is the consent), and the dry run of an import, which
writes nothing.
"""
from __future__ import annotations

from cairnmind.pal.terminal_safe import safe_text

TEXT = 200  # a record's text in a dialog, on one line, escaped and cut
RECORD = "Record: {record}"  # not {text}: the translator's first parameter is named text
NOT_GIVEN = "{label}: none given."
CONFIRMING = "Confirming makes this record confirmed by you on this computer, with an audit note."
DISPUTING = "Disputing marks this record as disputed by you on this computer, with an audit note."
NOT_RELOADED = "The view could not be reloaded."
MESSAGES = {
    NOT_RELOADED: "Nie udało się wczytać widoku ponownie.",
    RECORD: "Ustalenie: {record}",
    NOT_GIVEN: "{label}: nie podano.",
    "Confirm this record?": "Potwierdzić to ustalenie?",
    "Dispute this record?": "Zakwestionować to ustalenie?",
    CONFIRMING: "Potwierdzenie czyni to ustalenie potwierdzonym przez Ciebie na tym komputerze, z notatką audytu.",
    DISPUTING: "Zakwestionowanie oznacza to ustalenie jako zakwestionowane przez Ciebie na tym komputerze, "
               "z notatką audytu.",
    "Save this decision?": "Zapisać tę decyzję?",
    "Save this verdict?": "Zapisać tę ocenę?",
}


def refused(exc) -> bool:
    """True only for a 4xx answer of the service. A 5xx, an answer that could not be decoded and a lost connection
    may follow a commit, so they are unclear, never "not done" (#354)."""
    from cairnmind.terminal.terminal_client import BrokerError, BrokerUnreachable
    return isinstance(exc, BrokerError) and not isinstance(exc, BrokerUnreachable) and 400 <= exc.status_code < 500


async def reloaded(reload) -> bool:
    """Run the reload of a view after a person's act; False when it failed. What the act did never depends on it (#359)."""
    try:
        await reload()
    except (OSError, ValueError, RuntimeError):
        return False
    return True


async def tell_saved(app, scope, project_id, done, reload, **fields) -> None:
    """The act got a 2xx answer: tell it done (for its own project after a switch, #354). The view is reloaded only
    while that project is shown, and a reload that fails adds its own sentence instead of replacing the outcome."""
    from cairnmind.terminal.terminal_notices import about
    ok = scope != app._scope_version or await reloaded(reload)
    app.tell(scope, about(project_id), app.t(done, **fields) + ("" if ok else " " + app.t(NOT_RELOADED)),
             "info" if ok else "warn")


def record_line(item) -> str:
    """A record's (or a plan's) text on one line, escaped and cut, for the dialog's first line."""
    value = (item.get("content") or item.get("title") or "") if isinstance(item, dict) else ""
    return safe_text(" ".join(str(value).split()), limit=TEXT)


def typed(t, label, value) -> str:
    """What the person typed in the form, whole and escaped, under the label it was typed for."""
    value = str(value or "").strip()
    return f"{t(label)}: {safe_text(value)}" if value else t(NOT_GIVEN, label=t(label))


def review_text(record, action, plan_version, reason, t) -> str:
    """What a review does (inspector, Confirm or Dispute): the record, its versions, the effect, the reason."""
    versions = f"v{record['version']}" + (f" · plan v{plan_version}" if plan_version else "")
    return "\n".join([t(RECORD, record=record_line(record)),
                      versions + " · " + t("Review changes confidence; it does not complete a task."), "",
                      t(CONFIRMING if action == "confirm" else DISPUTING), typed(t, "Reason", reason)])


def resolve_text(pair, choice_label, action, reason, t) -> str:
    """What a conflict decision does: the choice as offered, both findings with their versions, the reason."""
    first, second = pair["candidate"], pair["existing"]
    return "\n".join([choice_label,
                      f"A v{first['version']}: {record_line(first)}", f"B v{second['version']}: {record_line(second)}",
                      "", t("The decision is saved with an audit note; every source is kept."),
                      typed(t, "Describe the two scopes" if action == "different_scopes" else "Reason", reason)])


async def agreed(app, title, text, approve) -> bool:
    """Show the consent dialog of a person's act; True only when the person chose `approve` in it (#318)."""
    from cairnmind.terminal.terminal_panels import ConfirmModal  # imported here: terminal_panels imports the catalog, which reads MESSAGES
    return await app.push_screen_wait(ConfirmModal(title, text, approve=approve, consent=True)) is True
