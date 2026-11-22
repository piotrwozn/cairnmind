"""More, then Transcript record kinds: an unknown record kind that blocks the resume packet (#210).

The view lists the unknown kinds in the selected project's transcripts and what a person accepted. For a
kind it shows the record as it is stored (kind, client, version and its content, escaped and cut) and
offers one action: treat this kind as technical in this project, with a note. An accepted kind can be
withdrawn. Both go to the inspector route that names no session, the person's own route (#177).

When a visible session's memory is partial, the sidebar asks once whether the project's transcripts hold
an unknown kind and says so on the notice line, with where to accept it. The record's text never reaches the notice line.
"""
from __future__ import annotations

import asyncio
import time

from .terminal_notices import about as notice_about
from .terminal_person_act import agreed, refused, typed
from .terminal_safe import safe_text

CHECK_SECONDS = 60
EXAMPLE = 300
NOTE_LABEL = "Why is this kind technical? Kept with the date."
NOT_ACCEPTED = "The kind was not accepted."
ACCEPT_UNCLEAR = "The service did not confirm it, so the kind may have been accepted."
NOT_WITHDRAWN = "The acceptance was not withdrawn."
WITHDRAW_UNCLEAR = "The service did not confirm it, so the acceptance may have been withdrawn."

MESSAGES = {
    NOT_ACCEPTED: "Rodzaj nie został zaakceptowany.",
    ACCEPT_UNCLEAR: "Usługa tego nie potwierdziła, więc rodzaj mógł zostać zaakceptowany.",
    NOT_WITHDRAWN: "Akceptacja nie została wycofana.",
    WITHDRAW_UNCLEAR: "Usługa tego nie potwierdziła, więc akceptacja mogła zostać wycofana.",
    "Transcript record kinds": "Rodzaje rekordów transkrypcji",
    "No unknown transcript record kinds in this project.": "W tym projekcie nie ma nieznanych rodzajów rekordów transkrypcji.",
    "{client} · {kind} · {records} records · blocks the resume packet":
        "{client} · {kind} · rekordy: {records} · blokuje pakiet wznowienia",
    "{client} · {kind} · accepted on {day}": "{client} · {kind} · zaakceptowany dnia {day}",
    "Kind: {kind}": "Rodzaj: {kind}",
    "Client: {client} · version: {version}": "Klient: {client} · wersja: {version}",
    "version unknown": "wersja nieznana",
    "Records of this kind: {records}": "Rekordy tego rodzaju: {records}",
    "Transcript: {path}, byte {byte}": "Transkrypcja: {path}, bajt {byte}",
    "One record, as it is stored (escaped and cut):": "Jeden rekord, tak jak jest zapisany (z kodami ucieczki, przycięty):",
    "Accepting treats this kind as technical in this project only. Its packets are ready again and the "
    "records are kept as they are. A malformed record of a known kind, and a transcript that fails its "
    "checks, still block.":
        "Akceptacja traktuje ten rodzaj jako techniczny tylko w tym projekcie. Jego pakiety znów są gotowe, "
        "a rekordy zostają takie, jakie są. Uszkodzony rekord znanego rodzaju i transkrypcja, która nie przechodzi "
        "sprawdzeń, nadal blokują.",
    "Accept a transcript record kind": "Akceptuj rodzaj rekordu transkrypcji",
    "Why is this kind technical? Kept with the date.": "Dlaczego ten rodzaj jest techniczny? Zapisane z datą.",
    "Treat as technical": "Traktuj jako techniczny",
    "Treat this kind as technical?": "Traktować ten rodzaj jako techniczny?",
    "Kind {kind} from {client} is accepted as technical in this project.":
        "Rodzaj {kind} od {client} jest zaakceptowany jako techniczny w tym projekcie.",
    "Withdraw an acceptance": "Wycofaj akceptację",
    "Accepted on {day} by {name}: {note}": "Zaakceptowany dnia {day} przez {name}: {note}",
    "an unnamed person": "osobę bez podanego imienia",
    "After withdrawing, records of this kind block the resume packets of this project again.":
        "Po wycofaniu rekordy tego rodzaju znów blokują pakiety wznowienia tego projektu.",
    "Withdraw": "Wycofaj",
    "The acceptance of {kind} from {client} is withdrawn.": "Akceptacja rodzaju {kind} od {client} została wycofana.",
    "This project's transcripts hold record kind {kind} from {client}, unknown to this release; it blocks the "
    "resume packets of this project. Look at it and accept it under More → Transcript record kinds.":
        "Transkrypcje tego projektu zawierają rodzaj rekordu {kind} od {client}, nieznany tej wersji; blokuje on "
        "pakiety wznowienia tego projektu. Obejrzyj go i zaakceptuj w Więcej → Rodzaje rekordów transkrypcji.",
    "Only the first {count} unknown transcript records of this project were read, so a kind further on is not "
    "listed here. A partial packet names its kind and where a person accepts it.":
        "Odczytano tylko pierwsze {count} nieznanych rekordów transkrypcji tego projektu, więc rodzaj zapisany "
        "dalej nie jest tu wymieniony. Częściowy pakiet podaje jego rodzaj i miejsce, w którym osoba go "
        "akceptuje.",
}


def _word(value, limit=64) -> str:
    """A kind, client or version from the service: shown literally, escaped and cut."""
    return safe_text(" ".join(str(value).split()), limit=limit)


def _label(item, t, accepted=False) -> str:
    client, kind = _word(item.get("client")), _word(item.get("kind"))
    if accepted:
        return t("{client} · {kind} · accepted on {day}", client=client, kind=kind,
                 day=_word(str(item.get("accepted_at") or "")[:10], 10))
    return t("{client} · {kind} · {records} records · blocks the resume packet", client=client, kind=kind,
             records=item.get("records") if type(item.get("records")) is int else "?")


def describe(item, t) -> str:
    """The record as stored, for the person to look at before accepting its kind."""
    lines = [t("Kind: {kind}", kind=_word(item.get("kind"))),
             t("Client: {client} · version: {version}", client=_word(item.get("client")),
               version=_word(item["version"], 40) if item.get("version") else t("version unknown")),
             t("Records of this kind: {records}", records=item.get("records") if type(item.get("records")) is int else "?")]
    if item.get("path"):
        lines.append(t("Transcript: {path}, byte {byte}", path=_word(item["path"], 400),
                       byte=item.get("byte") if type(item.get("byte")) is int else "?"))
    lines += ["", t("One record, as it is stored (escaped and cut):"),
              safe_text(str(item.get("excerpt") or ""), limit=EXAMPLE), "",
              t("Accepting treats this kind as technical in this project only. Its packets are ready again and the "
                "records are kept as they are. A malformed record of a known kind, and a transcript that fails its "
                "checks, still block.")]
    return "\n".join(lines)


async def open_view(app):
    """The view itself. Accepting and withdrawing a kind go out only from a consent dialog (#332)."""
    from .terminal_ui import ChoiceModal, Field, FormModal
    selected = await asyncio.to_thread(app.workspace.read)
    project = selected.get("project_id")
    if not project:
        raise ValueError(app.t("Select a visible project first."))
    subject = notice_about(project, selected.get("project_name"), selected.get("worktree"))
    view = await app.request("GET", "/api/inspector/record-kinds", params={"project": project})
    blocking = [item for item in view.get("blocking") or [] if isinstance(item, dict)][:50]
    standing = [item for item in view.get("accepted") or [] if isinstance(item, dict)][:64]
    choices = ([(_label(item, app.t), f"accept:{index}") for index, item in enumerate(blocking)]
               + [(_label(item, app.t, True), f"withdraw:{index}") for index, item in enumerate(standing)])
    if view.get("complete", True) is False:
        # The service read only part of the records: never say there is nothing when a kind may lie further on.
        count = view.get("scanned") if type(view.get("scanned")) is int else "?"
        app.notice_for(subject, app.t(
            "Only the first {count} unknown transcript records of this project were read, so a kind further on is "
            "not listed here. A partial packet names its kind and where a person accepts it.",
            count=count), "warn")
    elif not choices:
        app.notice_for(subject, app.t("No unknown transcript record kinds in this project."))
    if not choices:
        return
    choice = await app.push_screen_wait(ChoiceModal(app.t("Transcript record kinds"), choices))
    if not choice:
        return
    action, index = choice.split(":")
    item = (blocking if action == "accept" else standing)[int(index)]
    body = {"project": project, "client": item.get("client"), "kind": item.get("kind")}
    words = {"kind": _word(item.get("kind")), "client": _word(item.get("client"))}
    if action == "accept":
        values = await app.push_screen_wait(FormModal(
            app.t("Accept a transcript record kind"),
            [Field("note", app.t(NOTE_LABEL), required=True, multiline=True)],
            note=describe(item, app.t), submit=app.t("Continue")))
        if not values:
            return
        text = describe(item, app.t) + "\n\n" + typed(app.t, NOTE_LABEL, values["note"])
        if not await agreed(app, "Treat this kind as technical?", text, "Treat as technical"):
            return
        if await _send(app, subject, "accept", {**body, "note": values["note"]}, NOT_ACCEPTED, ACCEPT_UNCLEAR):
            app.notice_for(subject, app.t("Kind {kind} from {client} is accepted as technical in this project.", **words))
        return
    name = item.get("accepted_by")
    text = "\n".join([
        app.t("Accepted on {day} by {name}: {note}", day=_word(str(item.get("accepted_at") or "")[:10], 10),
              name=_word(name, 120) if name else app.t("an unnamed person"), note=safe_text(str(item.get("note") or ""),
                                                                                          limit=1000)),
        "", app.t("After withdrawing, records of this kind block the resume packets of this project again.")])
    if (await agreed(app, "Withdraw an acceptance", text, "Withdraw")
            and await _send(app, subject, "withdraw", body, NOT_WITHDRAWN, WITHDRAW_UNCLEAR)):
        app.notice_for(subject, app.t("The acceptance of {kind} from {client} is withdrawn.", **words))


async def _send(app, subject, route, body, not_done, unclear) -> bool:
    """The act's request alone: a refusal says it was not done, any other failure says it may have been (#354)."""
    from .terminal_i18n_messages import broker_error_text
    try:
        await app.request("POST", f"/api/inspector/record-kinds/{route}", json=body)
    except (OSError, ValueError, RuntimeError) as exc:
        app.notice_for(subject, app.t(not_done if refused(exc) else unclear) + " " + broker_error_text(exc, app.t), "error")
        return False
    return True


def _checked(app) -> dict:
    """Per project: when the sidebar last asked, and which kinds it last named."""
    if not isinstance(getattr(app, "_record_kinds_checked", None), dict):
        app._record_kinds_checked = {}
    return app._record_kinds_checked


def check_partial(app, value) -> None:
    """When a visible session's memory is partial, find out once whether an unknown kind blocks it."""
    project = value.get("project_id")
    # A launch refused for a partial packet leaves no slot with a partial memory: look anyway (#246).
    refused = any(isinstance(row, dict) and row.get("code") == "resume_partial" for row in value.get("launch_errors") or [])
    if not project or not (refused or any(isinstance(slot, dict) and (slot.get("memory") or {}).get("status") == "partial"
                                          for slot in value.get("slots") or [])):
        return
    checked = _checked(app)
    now = time.monotonic()
    if now - checked.get(project, (-CHECK_SECONDS, None))[0] < CHECK_SECONDS:
        return
    checked[project] = (now, checked.get(project, (0, None))[1])
    app.run_worker(_announce(app, project, value), group="record-kinds-check", exclusive=True, exit_on_error=False)


async def _announce(app, project, value):
    try:
        view = await app.request("GET", "/api/inspector/record-kinds", params={"project": project})
    except (OSError, ValueError, RuntimeError):
        return
    blocking = [item for item in view.get("blocking") or [] if isinstance(item, dict)]
    checked = _checked(app)
    signature = tuple(sorted((str(item.get("client")), str(item.get("kind"))) for item in blocking))
    last = checked.get(project, (time.monotonic(), None))
    if not blocking:
        # Nothing blocks now: forget what was named, so a kind that comes back (withdrawn) is named again.
        checked[project] = (last[0], None)
        return
    if last[1] == signature:
        return
    checked[project] = (last[0], signature)
    first = blocking[0]
    # The kind is found in the project's transcripts, not proved to be what makes this slot partial: say so.
    app.notice_for(notice_about(project, value.get("project_name"), value.get("worktree")),
                   app.t("This project's transcripts hold record kind {kind} from {client}, unknown to this release; "
                         "it blocks the resume packets of this project. Look at it and accept it under More → "
                         "Transcript record kinds.",
                         kind=_word(first.get("kind")), client=_word(first.get("client"))), "warn")
