"""Replace a record with a new text in the inspector (#222).

The form collects the new text, prefilled with the current one, and an optional reason; its button says Continue
and sends nothing. The consent dialog shows the current and the new text whole, what the act does and the reason,
and only its approval sends the request (#332). The service stores the new text as a new record confirmed by the
person; the record shown becomes superseded and stays in its history. The workspace's key travels with the request
(terminal_client).
"""
from __future__ import annotations

from .findings import FINDING_KINDS, MAX_FINDING_CHARS
from .record_replace import HISTORICAL, NOT_REPLACEABLE, SAME_TEXT
from .terminal_notices import about
from .terminal_person_act import agreed, typed

NOTE = ("Write the text that replaces this record. It is saved as a new record confirmed by you; this record "
        "becomes superseded and stays in its history.")
EFFECT = ("Replacing saves the new text as a new record confirmed by you on this computer, with an audit note. The "
          "current record becomes superseded and stays in its history with its sources; nothing is deleted.")
TOO_LONG = f"The new text has at most {MAX_FINDING_CHARS} characters."
DONE = "Record replaced. The new text is saved as a record confirmed by you."
FAILED = "The record was not replaced. The current version was reloaded."
MESSAGES = {
    "Replace": "Zastąp",
    "Replace record": "Zastąp ustalenie",
    "New text": "Nowy tekst",
    "Current text": "Obecny tekst",
    "Replace this record?": "Zastąpić to ustalenie?",
    "Replaced by": "Zastąpione przez",
    NOTE: "Wpisz tekst, który zastąpi to ustalenie. Zostanie zapisany jako nowe ustalenie potwierdzone przez Ciebie; "
          "to ustalenie zostanie zastąpione i pozostanie w swojej historii.",
    EFFECT: "Zastąpienie zapisuje nowy tekst jako nowe ustalenie potwierdzone przez Ciebie na tym komputerze, z "
            "notatką audytu. Obecne ustalenie zostaje zastąpione i pozostaje w swojej historii razem ze źródłami; "
            "nic nie jest usuwane.",
    TOO_LONG: f"Nowy tekst może mieć najwyżej {MAX_FINDING_CHARS} znaków.",
    DONE: "Ustalenie zastąpione. Nowy tekst zapisano jako ustalenie potwierdzone przez Ciebie.",
    FAILED: "Ustalenie nie zostało zastąpione. Wczytano obecną wersję.",
}


def replace_text(record, content, reason, t) -> str:
    """What the act does: the record's version and kind, the current and the new text whole, the effect, the reason."""
    return "\n".join([f"v{record['version']} · {t(record.get('kind') or '')}", "",
                      typed(t, "Current text", record.get("content")), "", typed(t, "New text", content), "",
                      t(EFFECT), typed(t, "Reason", reason)])


async def replace_record(app):
    # Imported here: the catalog (terminal_i18n) reads MESSAGES from this module, and these import the catalog.
    from .terminal_i18n_messages import broker_error_text
    from .terminal_ui import Field, FormModal
    if app.entity_kind != "records" or not app.entity:
        app.notice(app.t("Open a record first."))
        return
    record = dict(app.entity)
    if record.get("status") == "superseded":
        app.notice(app.t(HISTORICAL))
        return
    if record.get("kind") not in FINDING_KINDS:
        app.notice(app.t(NOT_REPLACEABLE))
        return
    scope, project_id = app._scope_version, app.workspace_data.get("project_id")
    current = str(record.get("content") or "")
    form = await app.push_screen_wait(FormModal(
        app.t("Replace record"),
        [Field("content", app.t("New text"), value=current, required=True, multiline=True,
               validator=lambda value: TOO_LONG if len(value) > MAX_FINDING_CHARS
               else SAME_TEXT if value == current.strip() else None),
         Field("reason", app.t("Reason"), multiline=True)],
        note=f"v{record['version']} · " + app.t(NOTE), submit=app.t("Continue")))
    if form is None:
        return
    if not await agreed(app, "Replace this record?", replace_text(record, form["content"], form["reason"], app.t),
                        "Replace"):
        return
    if scope != app._scope_version:
        app.notice_for(about(project_id), app.t("The project changed during review. Nothing was saved."), "warn")
        return
    payload = {"project": project_id, "content": form["content"], "expected_version": record["version"],
               "reason": form["reason"], "worktree": app.workspace_data.get("worktree")}
    try:
        result = await app.request("POST", f"/api/inspector/records/{record['id']}/replace", json=payload)
        new = result.get("record") if isinstance(result, dict) else None
        if not isinstance(new, dict) or not isinstance(new.get("id"), str):
            raise RuntimeError("The memory service gave an invalid answer.")  # never shown as a success
        if scope == app._scope_version:
            await app.open_entity(new["id"], "records")  # the new record, confirmed
            app.notice(app.t(DONE))
    except (OSError, ValueError, RuntimeError) as exc:
        if scope == app._scope_version:
            await app.open_entity(record["id"], "records")
            app.notice(app.t(FAILED) + " " + broker_error_text(exc, app.t), "error")
