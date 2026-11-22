"""Release a record in the inspector (#306): the person lets a record's false alarms of the secrets scan be
published, with a reason that is published too.

The command line releases nothing any more, so this form is where a record is released. The inspector route checks
the record and the reason; the workspace's key travels with the request (terminal_client).
"""
from __future__ import annotations

from .memory_published_secrets import MAX_RELEASE_REASON
from .terminal_notices import about

TOO_LONG = f"A reason has at most {MAX_RELEASE_REASON} characters."
MESSAGES = {
    "Release": "Przepuść",
    "Release record": "Przepuść ustalenie",
    "Why these hits are not secrets (published with the record)":
        "Dlaczego te trafienia nie są sekretami (publikowane razem z ustaleniem)",
    "Lets this record's current hits of the secrets scan be published. Your reason is published with the record. "
    "A registered secret is never released.":
        "Pozwala opublikować obecne trafienia skanu sekretów w tym ustaleniu. Twoje uzasadnienie zostanie "
        "opublikowane razem z ustaleniem. Zarejestrowany sekret nigdy nie jest przepuszczany.",
    "Release saved with your reason. Hits of the secrets scan let through in this record: {count}.":
        "Zapisano przepuszczenie z Twoim uzasadnieniem. Przepuszczone trafienia skanu sekretów w tym ustaleniu: "
        "{count}.",
    "The record was not released.": "Ustalenie nie zostało przepuszczone.",
    TOO_LONG: f"Uzasadnienie może mieć najwyżej {MAX_RELEASE_REASON} znaków.",
}


async def release_record(app):
    # Imported here: the catalog (terminal_i18n) reads MESSAGES from this module, and these import the catalog.
    from .terminal_i18n_messages import broker_error_text
    from .terminal_ui import Field, FormModal
    if app.entity_kind != "records" or not app.entity:
        app.notice(app.t("Open a record first."))
        return
    record = dict(app.entity)
    scope = app._scope_version
    project_id = app.workspace_data.get("project_id")
    form = await app.push_screen_wait(FormModal(
        app.t("Release record"),
        [Field("reason", app.t("Why these hits are not secrets (published with the record)"), required=True,
               multiline=True, validator=lambda value: TOO_LONG if len(value) > MAX_RELEASE_REASON else None)],
        note=app.t("Lets this record's current hits of the secrets scan be published. Your reason is published with "
                   "the record. A registered secret is never released."),
        submit=app.t("Release")))
    if form is None:
        return
    if scope != app._scope_version:
        app.notice_for(about(project_id), app.t("The project changed during review. Nothing was saved."), "warn")
        return
    payload = {"project": project_id, "reason": form["reason"], "worktree": app.workspace_data.get("worktree")}
    try:
        result = await app.request("POST", f"/api/inspector/records/{record['id']}/release", json=payload)
    except (OSError, ValueError, RuntimeError) as exc:
        if scope == app._scope_version:
            app.notice(app.t("The record was not released.") + " " + broker_error_text(exc, app.t), "error")
        return
    if scope == app._scope_version:
        count = len(result.get("released") or []) if isinstance(result, dict) else 0
        app.notice(app.t("Release saved with your reason. Hits of the secrets scan let through in this record: "
                         "{count}.", count=count))
