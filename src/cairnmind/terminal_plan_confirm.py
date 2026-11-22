"""Confirm a plan in the inspector (#214): the person's own way to confirm a plan and its active steps.

The command line confirms nothing any more, so this form and its consent dialog are where a plan is confirmed.
The form only collects the reason; the request goes out from the dialog (#332). It sends the exact plan version
the inspector read; the inspector route checks it, and the workspace's key travels with the request
(terminal_client).
"""
from __future__ import annotations

from .terminal_notices import about
from .terminal_person_act import agreed, typed
from .terminal_safe import safe_text

MESSAGES = {
    "Confirm plan": "Potwierdź plan",
    "v{version} · Confirms the plan's title, goal and constraints with every active step.":
        "v{version} · Potwierdza tytuł, cel i ograniczenia planu razem z każdym aktywnym krokiem.",
    "Confirm this plan?": "Potwierdzić ten plan?",
    "Title: {title}": "Tytuł: {title}",
    "Plan confirmed with an audit note.": "Plan potwierdzony z notatką audytu.",
    "The plan was not confirmed.": "Plan nie został potwierdzony.",
    "The plan was not confirmed. The current version was reloaded.":
        "Plan nie został potwierdzony. Wczytano bieżącą wersję.",
}


async def confirm_plan(app):
    # Imported here: the catalog (terminal_i18n) reads MESSAGES from this module, and these import the catalog.
    from .terminal_i18n_messages import broker_error_text
    from .terminal_ui import Field, FormModal
    plan = dict(app.entity)
    scope = app._scope_version
    project_id = app.workspace_data.get("project_id")
    form = await app.push_screen_wait(FormModal(
        app.t("Confirm plan"), [Field("reason", app.t("Reason"), multiline=True)],
        note=app.t("v{version} · Confirms the plan's title, goal and constraints with every active step.",
                   version=plan["version"]), submit=app.t("Continue")))
    if form is None:
        return
    text = "\n".join([app.t("Title: {title}", title=safe_text(str(plan.get("title") or ""), limit=200)),
                      app.t("v{version} · Confirms the plan's title, goal and constraints with every active step.",
                            version=plan["version"]), "", typed(app.t, "Reason", form["reason"])])
    if not await agreed(app, "Confirm this plan?", text, "Confirm plan"):
        return
    if scope != app._scope_version:
        app.notice_for(about(project_id), app.t("The project changed during review. Nothing was saved."), "warn")
        return
    payload = {"project": project_id, "expected_plan_version": plan["version"], "reason": form["reason"],
               "worktree": app.workspace_data.get("worktree")}
    try:
        await app.request("POST", f"/api/inspector/plans/{plan['id']}/confirm", json=payload)
        if scope == app._scope_version:
            await app.open_entity(plan["id"], "plans")
        app.tell(scope, about(project_id), app.t("Plan confirmed with an audit note."))
    except (OSError, ValueError, RuntimeError) as exc:
        reloaded = scope == app._scope_version
        if reloaded:
            await app.open_entity(plan["id"], "plans")
        app.tell(scope, about(project_id), app.t(
            "The plan was not confirmed. The current version was reloaded." if reloaded
            else "The plan was not confirmed.") + " " + broker_error_text(exc, app.t), "error")
