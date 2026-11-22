"""More, then Old task groups: adopt a group of tasks from before 0.11.0 into a plan (#305).

Up to 0.10.0 `task add --plan` stored every task as confirmed, so the tasks of an old group count as confirmed
before the rule, and `plan adopt` on the command line, which makes no person's act, refuses such a group. Here the
person reads the group's tasks in the order the plan will have them and adopts the group in one act, through the
inspector route that needs the open workspace's key (terminal_client adds it), with the task versions shown.
"""
from __future__ import annotations

import asyncio

from .confirmation import LIST_WORDS
from .terminal_notices import about as notice_about
from .terminal_safe import safe_text

TEXT = 200  # a task's text in the dialog, escaped and cut
TITLE = 500  # the route's limit of a plan title
ADOPTING = (
    "Adopting makes these tasks the steps of a new plan, in this order and with their dependencies. Every step is "
    "proposed until you confirm the plan in the inspector (Plans, then Confirm): a task that was confirmed, by you "
    "or before only a person could confirm, loses that confirmation now. Progress and reported evidence are kept. "
    "If a task of the group changes before you choose Adopt, nothing is adopted.")
TOO_LONG = f"A plan title has at most {TITLE} characters."
MESSAGES = {
    "Old task groups": "Stare grupy zadań",
    "No old task groups in this project.": "W tym projekcie nie ma starych grup zadań.",
    "{group} · tasks: {count} · confirmed: {confirmed}": "{group} · zadania: {count} · potwierdzone: {confirmed}",
    "Adopt a task group into a plan": "Włącz grupę zadań do planu",
    "Group: {group}": "Grupa: {group}",
    "Its tasks, in the order the plan will have them:": "Jej zadania, w kolejności, jaką będzie miał plan:",
    ADOPTING:
        "Włączenie do planu czyni te zadania krokami nowego planu, w tej kolejności i z ich zależnościami. Każdy "
        "krok jest proponowany, dopóki nie potwierdzisz planu w inspektorze (Plany, potem Potwierdź): zadanie, "
        "które było potwierdzone, przez Ciebie albo zanim tylko osoba mogła potwierdzać, traci teraz to "
        "potwierdzenie. Postęp i zgłoszone dowody zostają. Jeśli zadanie tej grupy zmieni się, zanim wybierzesz "
        "Włącz do planu, nic nie zostanie włączone.",
    "Plan title": "Tytuł planu",
    "Adopt": "Włącz do planu",
    TOO_LONG: f"Tytuł planu może mieć najwyżej {TITLE} znaków.",
    "Group {group} is adopted as plan {title} with {count} steps. Confirm the plan in the inspector: Plans, then "
    "Confirm.":
        "Grupa {group} została włączona jako plan {title}; kroki: {count}. Potwierdź plan w inspektorze: Plany, "
        "potem Potwierdź.",
}


def _line(number, task, t) -> str:
    from .terminal_i18n_messages import display_word  # imported here: that module imports the catalog
    words = [display_word(task.get("status"), t)]
    if task.get("confirmed_by") in LIST_WORDS:
        words.append(t(LIST_WORDS[task["confirmed_by"]]))
    words.append(display_word(task.get("state") or "todo", t))
    text = safe_text(" ".join(str(task.get("content") or "").split()), limit=TEXT)
    return f"{number}. {text} · " + " · ".join(words)


def describe(group, t) -> str:
    """What the adoption will do, for the person to read before Adopt: every task, never cut from the list."""
    return "\n".join([t("Group: {group}", group=safe_text(group["group"], limit=TEXT)), "",
                      t("Its tasks, in the order the plan will have them:"),
                      *(_line(number, task, t) for number, task in enumerate(group["tasks"], 1)), "",
                      t(ADOPTING)])


async def open_view(app):
    from .terminal_ui import ChoiceModal, Field, FormModal
    selected = await asyncio.to_thread(app.workspace.read)
    project = selected.get("project_id")
    if not project:
        raise ValueError(app.t("Select a visible project first."))
    subject = notice_about(project, selected.get("project_name"), selected.get("worktree"))
    view = await app.request("GET", "/api/inspector/legacy-groups", params={"project": project})
    groups = [group for group in view.get("groups") or [] if isinstance(group, dict)
              and isinstance(group.get("group"), str)
              and isinstance(group.get("tasks"), list) and group["tasks"]
              and all(isinstance(task, dict) for task in group["tasks"])]
    if not groups:
        app.notice_for(subject, app.t("No old task groups in this project."))
        return
    choice = await app.push_screen_wait(ChoiceModal(app.t("Old task groups"), [
        (app.t("{group} · tasks: {count} · confirmed: {confirmed}", group=safe_text(group["group"], limit=80),
               count=len(group["tasks"]),
               confirmed=group["confirmed"] if type(group.get("confirmed")) is int else "?"), str(index))
        for index, group in enumerate(groups)]))
    if not choice:
        return
    group = groups[int(choice)]
    versions = {task.get("id"): task.get("version") for task in group["tasks"]}
    adopted = {}

    async def save(values):
        adopted.update(await app.request("POST", "/api/inspector/legacy-groups/adopt", json={
            "project": project, "group": group["group"], "title": values["title"], "task_versions": versions,
            "worktree": selected.get("worktree")}))

    values = await app.push_screen_wait(FormModal(
        app.t("Adopt a task group into a plan"),
        [Field("title", app.t("Plan title"), value=safe_text(group["group"], limit=TITLE)[:TITLE], required=True,
               validator=lambda value: TOO_LONG if len(value) > TITLE else None)],
        note=describe(group, app.t), submit=app.t("Adopt"), on_submit=save))
    if values:
        app.notice_for(subject, app.t(
            "Group {group} is adopted as plan {title} with {count} steps. Confirm the plan in the inspector: Plans, "
            "then Confirm.", group=safe_text(group["group"], limit=80), title=safe_text(values["title"], limit=80),
            count=len(adopted.get("tasks") or [])))
