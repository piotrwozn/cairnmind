"""A terminal workspace that keeps the original coding clients intact."""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from .config import Settings
from .store import Store


TASK_STATES = ("todo", "in_progress", "needs_test", "needs_review", "done", "blocked")
KIND_LABELS = {
    "decision": "Decyzja", "task": "Zadanie", "checkpoint": "Kontynuacja",
    "fact": "Fakt", "constraint": "Ograniczenie", "question": "Pytanie",
    "preference": "Preferencja",
}


def _object(value: Any, default: Any) -> Any:
    """Support both decoded Store results and JSON columns from older exports."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return default
    return value if isinstance(value, type(default)) else default


class DevBrainApp(App):
    """Project launcher and evidence viewer; never replaces native permission prompts."""

    TITLE = "DevBrain"
    SUB_TITLE = "Lokalna pamięć · Hybrid RAG · GraphRAG"
    BINDINGS = [
        ("q", "quit", "Zakończ"),
        ("r", "refresh", "Odśwież"),
        ("f1", "launch_codex", "Codex"),
        ("f2", "launch_claude", "Claude"),
    ]
    CSS = """
    Screen { background: $surface; }
    #workspace { height: 1fr; }
    #sidebar { width: 36; min-width: 28; padding: 1 2; border-right: solid $primary; }
    #main { width: 1fr; padding: 1 2; }
    .section { color: $accent; text-style: bold; margin-top: 1; }
    #sidebar Button { width: 1fr; margin-top: 1; }
    #sidebar Input { margin-top: 1; }
    #project-select { margin-top: 1; }
    #selected-root { height: auto; max-height: 5; color: $text-muted; margin-top: 1; }
    #model-label { height: auto; color: $text-muted; margin-bottom: 1; }
    #records { height: 12; }
    #worktree-select { margin-top: 1; }
    #plans { height: 7; }
    #coordination { height: auto; max-height: 16; overflow-y: auto; }
    #checks { height: 8; }
    #session-readiness { height: auto; max-height: 8; overflow-y: auto; margin-top: 1; }
    #record-detail { height: 1fr; min-height: 5; overflow-y: auto; margin-top: 1; }
    #task-actions { height: 3; margin-top: 1; }
    #task-state { width: 22; }
    #task-update { margin-left: 1; }
    #question { margin-top: 1; }
    #answer { height: 1fr; overflow-y: auto; margin-top: 1; }
    #status { height: auto; max-height: 10; overflow-y: auto; margin-top: 1; }
    #operation-log { height: 1fr; min-height: 5; margin-top: 1; }
    #import-path { margin-top: 1; }
    .operation-row { height: 3; margin-top: 1; }
    .operation-row Button { margin-right: 1; }
    #hint { height: auto; color: $text-muted; margin-top: 1; }
    #portable-status, #portable-detection, #portable-privacy { height: auto; margin-top: 1; }
    #portable-detail { height: auto; min-height: 5; margin-top: 1; }
    #portable-interval { width: 14; }
    #portable-tab Input { margin-top: 1; }
    """

    def __init__(self, settings: Settings, *, store: Store | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.store = store or Store(settings.data_dir)
        self.selected_project: str | None = None
        self.selected_record: str | None = None
        self.selected_worktree: str | None = None
        self.selected_plan: str | None = None
        self.plans: dict[str, dict] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self._portable_preview_scope: tuple[str | None, str | None] | None = None
        self._portable_import_preview: tuple[str, str, str] | None = None
        self._portable_auto_enabled = False
        self._portable_busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Label("PROJEKT", classes="section")
                yield Select([], prompt="Wybierz projekt", id="project-select")
                yield Static("Dodaj pierwszy projekt poniżej.", id="selected-root", markup=False)
                yield Select([], prompt="Worktree projektu", id="worktree-select")
                yield Button("Uruchom Codex  [F1]", id="launch-codex", variant="primary")
                yield Button("Uruchom Claude  [F2]", id="launch-claude")
                yield Label("NOWY PROJEKT", classes="section")
                yield Input(placeholder="Nazwa projektu", id="project-name")
                yield Input(placeholder="Katalog projektu w WSL", id="project-root")
                yield Button("Dodaj projekt", id="add-project")
                yield Static(
                    "Kodowanie odbywa się w oryginalnym CLI. "
                    "Po zakończeniu sesji wrócisz do tego ekranu.",
                    id="hint", markup=False,
                )
            with Vertical(id="main"):
                yield Static(f"Agent wiedzy: {self.settings.chat_model} · lokalna Ollama", id="model-label")
                with TabbedContent(initial="memory-tab"):
                    with TabPane("Pamięć i zadania", id="memory-tab"):
                        yield DataTable(id="records", cursor_type="row", zebra_stripes=True)
                        with Horizontal(id="task-actions"):
                            yield Select(
                                [(value, value) for value in TASK_STATES],
                                value="todo", allow_blank=False, id="task-state",
                            )
                            yield Button("Zmień stan zadania", id="task-update")
                        yield Markdown(
                            "Wybierz rekord, aby zobaczyć treść, wersję i źródła.", id="record-detail",
                        )
                    with TabPane("Plany i weryfikacja", id="plans-tab"):
                        yield DataTable(id="plans", cursor_type="row", zebra_stripes=True)
                        yield Button("Aktywuj plan dla tego worktree", id="activate-plan")
                        yield Markdown("Wybierz projekt, aby odczytać następny krok.", id="coordination")
                        yield Static("Pakiet sesji: jeszcze nieprzygotowany.", id="session-readiness", markup=False)
                        yield DataTable(id="checks", cursor_type="row", zebra_stripes=True)
                        yield Static("Test uruchamiasz jawnie: devbrain check run PROJEKT PROFIL --task ID", markup=False)
                    with TabPane("Zapytaj agenta", id="ask-tab"):
                        yield Input(
                            placeholder="Co ustaliliśmy? Gdzie jest kod? Co pozostało do zrobienia?",
                            id="question",
                        )
                        yield Button("Zapytaj — ask", id="ask", variant="primary")
                        yield Markdown(
                            "Odpowiedź lokalnego agenta pojawi się tutaj wraz ze źródłami.", id="answer",
                        )
                    with TabPane("Przenośna pamięć", id="portable-tab"):
                        with VerticalScroll():
                            yield Static(
                                "Pełny snapshot zawiera rozmowy i źródła tego projektu; mogą zawierać prywatne dane. "
                                "Pliki uwierzytelnienia i konfiguracja komputera są pomijane. "
                                "Przed zapisem sprawdź podgląd. Git i wysyłanie pozostają Twoją jawną operacją.",
                                id="portable-privacy", markup=False,
                            )
                            yield Static("Wybierz projekt do zapisania. Import jest dostępny również bez projektu.",
                                         id="portable-status", markup=False)
                            yield Static("", id="portable-detection", markup=False)
                            with Horizontal(classes="operation-row"):
                                yield Button("Podgląd zapisu", id="portable-preview", disabled=True)
                                yield Button("Zapisz pamięć", id="portable-snapshot", disabled=True)
                                yield Button("Odśwież status", id="portable-refresh")
                            with Horizontal(classes="operation-row"):
                                yield Input("15", placeholder="Minuty", type="integer", id="portable-interval")
                                yield Button("Włącz automat", id="portable-auto", disabled=True)
                            yield Label("ODTWORZENIE PROJEKTU", classes="section")
                            yield Input(placeholder="Katalog snapshotu lub manifest.json", id="portable-path")
                            yield Input(placeholder="Jawny katalog docelowego projektu w WSL", id="portable-root")
                            yield Input(placeholder="Nazwa lokalna (opcjonalna)", id="portable-name")
                            with Horizontal(classes="operation-row"):
                                yield Button("Podgląd pliku", id="portable-inspect")
                                yield Button("Sprawdź import", id="portable-dry-run")
                                yield Button("Wczytaj pamięć", id="portable-import", disabled=True)
                            yield Static(
                                "Podgląd pokazuje zakres pamięci. Sprawdź import przed wczytaniem. "
                                "Importowane testy są historyczne; świeżość wymaga lokalnego uruchomienia.",
                                id="portable-detail", markup=False,
                            )
                    with TabPane("Stan i operacje", id="operations-tab"):
                        yield Static("", id="status", markup=False)
                        with Horizontal(classes="operation-row"):
                            yield Button("Indeksuj projekt", id="index-project")
                            yield Button("Skonfiguruj oba CLI", id="install-integrations")
                        yield Input(placeholder="Ścieżka pliku historii JSONL", id="import-path")
                        with Horizontal(classes="operation-row"):
                            yield Button("Importuj historię", id="import-history")
                            yield Button("Odśwież stan", id="refresh-state")
                        yield RichLog(id="operation-log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#records", DataTable).add_columns("Typ", "Stan", "Treść", "Źródła")
        self.query_one("#plans", DataTable).add_columns("Plan", "Wersja", "Kroki", "Aktywny")
        self.query_one("#checks", DataTable).add_columns("Profil", "Wynik", "Exit", "Run ID")
        self.refresh_state()
        self.set_interval(5, self._refresh_status)

    @work(group="status-refresh", exclusive=True)
    async def _refresh_status(self) -> None:
        project_id = self.selected_project
        try:
            status = await asyncio.to_thread(self.store.status, project_id)
            if project_id == self.selected_project:
                self.query_one("#status", Static).update(json.dumps(status, ensure_ascii=False, indent=2))
                if self.query_one(TabbedContent).active == "portable-tab":
                    await self._load_portable(project_id, self.selected_worktree)
        except (ValueError, OSError, RuntimeError) as exc:
            self._log(f"Stan: {exc}")

    @work(group="workspace-refresh", exclusive=True)
    async def refresh_state(self) -> None:
        try:
            projects = await asyncio.to_thread(self.store.list_projects)
            self.projects = {str(project["id"]): project for project in projects}
            if self.selected_project not in self.projects:
                self.selected_project = next(iter(self.projects), None)
            select = self.query_one("#project-select", Select)
            select.set_options([(project["name"], str(project["id"])) for project in projects])
            select.value = self.selected_project or Select.NULL
            await self._load_project()
        except (ValueError, OSError, RuntimeError) as exc:
            self._log(f"Pamięć ArcadeDB: {exc}")
            self.notify(str(exc), severity="error")

    def action_refresh(self) -> None:
        self.refresh_state()

    @on(Select.Changed, "#project-select")
    def select_project(self, event: Select.Changed) -> None:
        # set_options posts a BLANK event before we restore the selected ID.
        # Ignore that stale event rather than accidentally clearing the project.
        if event.value != self.query_one("#project-select", Select).value:
            return
        previous_project = self.selected_project
        if event.value is Select.NULL:
            self.selected_project = None
        elif str(event.value) in self.projects:
            self.selected_project = str(event.value)
        else:
            return
        if self.selected_project == previous_project:
            return
        self.selected_record = None
        self.selected_worktree = None
        self.selected_plan = None
        self._render_project()

    @work(group="project-refresh", exclusive=True)
    async def _render_project(self) -> None:
        try:
            await self._load_project()
        except (ValueError, OSError, RuntimeError) as exc:
            self._log(f"Projekt: {exc}")
            self.notify(str(exc), severity="error")

    async def _load_project(self) -> None:
        project_id = self.selected_project
        project = self.projects.get(self.selected_project or "")
        records, status, roots = await asyncio.gather(
            asyncio.to_thread(self.store.list_records, project["id"]) if project else asyncio.sleep(0, result=[]),
            asyncio.to_thread(self.store.status, project_id),
            asyncio.to_thread(self.store.query, "SELECT path FROM project_roots WHERE project_id=:project",
                              {"project": project_id}) if project else asyncio.sleep(0, result=[]),
        )
        if project_id != self.selected_project:
            return
        self.query_one("#selected-root", Static).update(
            str(project["root"]) if project else "Dodaj lub wybierz projekt."
        )
        paths = [str(row["path"]) for row in roots]
        if self.selected_worktree not in paths:
            self.selected_worktree = str(project["root"]) if project else None
        scope = self.query_one("#worktree-select", Select)
        scope.set_options([(path, path) for path in paths])
        scope.value = self.selected_worktree or Select.NULL
        table = self.query_one("#records", DataTable)
        table.clear()
        self.records = {}
        if project:
            self.records = {str(record["id"]): record for record in records}
            for record in records:
                metadata = _object(record.get("metadata"), {})
                state = metadata.get("state", "todo") if record["kind"] == "task" else record["status"]
                sources = _object(record.get("source_ids"), [])
                text = str(record["content"]).replace("\n", " ")
                table.add_row(
                    KIND_LABELS.get(record["kind"], record["kind"]),
                    str(state), text[:110], str(len(sources)), key=str(record["id"]),
                )
        self.query_one("#status", Static).update(json.dumps(status, ensure_ascii=False, indent=2))
        self.query_one("#task-update", Button).disabled = not bool(project)
        await self._load_coordination(project_id, self.selected_worktree)
        await self._load_portable(project_id, self.selected_worktree)
        if self.selected_record in self.records:
            self.show_record(self.selected_record)
        else:
            self.selected_record = None
            self.query_one("#record-detail", Markdown).update(
                "Wybierz rekord, aby zobaczyć treść, wersję i źródła."
            )

    @on(Select.Changed, "#worktree-select")
    def select_worktree(self, event: Select.Changed) -> None:
        select = self.query_one("#worktree-select", Select)
        if event.value is Select.NULL or event.value != select.value:
            return
        if str(event.value) == self.selected_worktree:
            return
        self.selected_worktree = str(event.value)
        self._render_project()

    def _coordination_data(self, project_id: str, worktree: str) -> dict:
        from .plans import PlanManager
        from .sessions import SessionManager
        from .verification import verification_status

        manager = PlanManager(self.store)
        next_step = manager.next_task(project_id, worktree=worktree)
        task = next_step.get("task")
        verification = verification_status(self.store, task, worktree) if task else None
        sessions = self.store.query(
            "SELECT id FROM sessions WHERE project_id=:project AND worktree=:worktree "
            "ORDER BY updated_at DESC LIMIT 1", {"project": project_id, "worktree": worktree})
        session = SessionManager(self.store, self.settings).status(sessions[0]["id"]) if sessions else None
        checks = self.store.query(
            "SELECT FROM check_runs WHERE project_id=:project AND worktree=:worktree ORDER BY ended_at DESC LIMIT 30",
            {"project": project_id, "worktree": worktree})
        return {"plans": manager.list(project_id), "next": next_step, "verification": verification,
                "session": session, "checks": checks}

    async def _load_coordination(self, project_id: str | None, worktree: str | None) -> None:
        data = (await asyncio.to_thread(self._coordination_data, project_id, worktree)
                if project_id and worktree else {"plans": [], "next": {}, "checks": []})
        if (project_id, worktree) != (self.selected_project, self.selected_worktree):
            return
        self.plans = {plan["id"]: plan for plan in data["plans"]}
        table = self.query_one("#plans", DataTable)
        table.clear()
        active = data["next"].get("plan")
        for plan in self.plans.values():
            table.add_row(plan["title"], str(plan["version"]), str(len(plan["step_ids"])),
                          "tak" if active and active["id"] == plan["id"] else "", key=plan["id"])
        if self.selected_plan not in self.plans:
            self.selected_plan = active["id"] if active else next(iter(self.plans), None)
        task = data["next"].get("task")
        parts = ["### Aktywny plan: " + (active["title"] if active else "brak"),
                 "Worktree: `" + str(worktree or "brak") + "`",
                 "Następny krok: " + (task["content"] if task else str(data["next"].get("reason", "brak")))]
        if task:
            parts.append(f"Zadanie `{task['id']}` · wersja {task['version']} · stan {task['metadata']['state']}")
        if data.get("verification"):
            parts.append("Weryfikacja: **" + data["verification"]["status"] + "**")
            parts.extend(data["verification"].get("reasons", []))
        if data["next"].get("blocked"):
            parts.append("Blokady: `" + json.dumps(data["next"]["blocked"], ensure_ascii=False) + "`")
        self.query_one("#coordination", Markdown).update("\n\n".join(parts))
        session = data.get("session")
        readiness = "Pakiet sesji: jeszcze nieprzygotowany. Launcher przygotuje go przed uruchomieniem klienta."
        if session:
            readiness = (f"Pakiet sesji: {session.get('status', 'unknown')} · epoka {session.get('epoch')} · "
                         f"ACK: {'otrzymany' if session.get('acknowledged') else 'wymagany'}")
            if session.get("unknowns"):
                readiness += "\nBraki: " + "; ".join(map(str, session["unknowns"]))
            if session.get("ack_reason"):
                readiness += "\n" + str(session["ack_reason"])
        self.query_one("#session-readiness", Static).update(readiness)
        checks = self.query_one("#checks", DataTable)
        checks.clear()
        for run in data["checks"]:
            checks.add_row(str(run.get("profile_id", run.get("profile", "?"))), str(run.get("outcome", "unknown")) + (" / passed" if run.get("passed") else ""),
                           str(run.get("returncode", "—")), str(run["id"]), key=str(run["id"]))
        self.query_one("#activate-plan", Button).disabled = not bool(self.plans)

    @on(DataTable.RowHighlighted, "#plans")
    def highlight_plan(self, event: DataTable.RowHighlighted) -> None:
        candidate = str(event.row_key.value)
        if candidate in self.plans:
            self.selected_plan = candidate

    @work(group="plan-activation", exclusive=True)
    async def activate_selected_plan(self) -> None:
        try:
            from .plans import PlanManager

            project = self._project()
            if not self.selected_plan:
                raise ValueError("Wybierz plan w tabeli.")
            await asyncio.to_thread(PlanManager(self.store).activate, project["id"], self.selected_plan,
                                    worktree=self.selected_worktree)
            self.refresh_state()
            self.notify("Aktywowano plan dla wybranego worktree.")
        except (ValueError, OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error")

    @on(DataTable.RowHighlighted, "#records")
    def highlight_record(self, event: DataTable.RowHighlighted) -> None:
        self.show_record(str(event.row_key.value))

    def show_record(self, record_id: str) -> None:
        record = self.records.get(record_id)
        if not record:
            return
        self.selected_record = record_id
        metadata = _object(record.get("metadata"), {})
        if record["kind"] == "task" and metadata.get("state", "todo") in TASK_STATES:
            self.query_one("#task-state", Select).value = metadata.get("state", "todo")
        self._load_record(record_id)

    @work(group="record-detail", exclusive=True)
    async def _load_record(self, record_id: str) -> None:
        record = self.records.get(record_id)
        if not record:
            return
        project_id = self.selected_project
        worktree = self.selected_worktree
        metadata = _object(record.get("metadata"), {})
        parts = [
            f"### {KIND_LABELS.get(record['kind'], record['kind'])}",
            str(record["content"]),
            f"\nWersja: **{record.get('version', 1)}** · Ustalenie: **{record['status']}**",
        ]
        if record["kind"] == "task":
            from .verification import verification_status

            verification = await asyncio.to_thread(verification_status, self.store, record, worktree)
            parts += ["\nWeryfikacja: **" + verification["status"] + "**",
                      "\n".join(str(reason) for reason in verification.get("reasons", []))]
        if metadata:
            parts += ["\nMetadane:", "```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```"]
        parts.append("\n### Źródła")
        for source_id in _object(record.get("source_ids"), []):
            try:
                source = await asyncio.to_thread(self.store.get_source, source_id)
                parts.append(f"\n**{source.get('uri', source_id)}** · `{source_id}`")
                parts.append("\n```text\n" + str(source.get("content", ""))[:4000] + "\n```")
            except ValueError:
                parts.append(f"\nBrak źródła `{source_id}` — wymagane sprawdzenie archiwum.")
            except (OSError, RuntimeError) as exc:
                parts.append(f"\nŹródło `{source_id}` jest niedostępne: {exc}")
        if (record_id, project_id, worktree) != (self.selected_record, self.selected_project, self.selected_worktree):
            return
        self.query_one("#record-detail", Markdown).update("\n\n".join(parts))

    def _project(self) -> dict[str, Any]:
        project = self.projects.get(self.selected_project or "")
        if not project:
            raise ValueError("Najpierw dodaj lub wybierz projekt.")
        return project

    async def _ensure_service(self) -> None:
        # Lazy import avoids cli -> tui -> cli import cycles.
        from .cli import ensure_service

        await asyncio.to_thread(ensure_service, self.settings)

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        await self._ensure_service()
        async with httpx.AsyncClient(
            base_url=self.settings.url, timeout=self.settings.ask_timeout + 15,
            headers={"Authorization": f"Bearer {self.settings.token}"}, trust_env=False,
        ) as client:
            response = await client.request(method, path, json=payload)
            if response.is_error:
                try:
                    detail = response.json().get("detail", response.text)
                except (ValueError, AttributeError):
                    detail = response.text
                raise RuntimeError(f"HTTP {response.status_code}: {str(detail)[:1000]}")
            return response.json()

    def _log(self, message: str) -> None:
        self.query_one("#operation-log", RichLog).write(message)

    @on(Button.Pressed)
    async def button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        try:
            if button_id == "add-project":
                self.add_project()
            elif button_id == "ask":
                self.ask_question()
            elif button_id == "launch-codex":
                await self.action_launch_codex()
            elif button_id == "launch-claude":
                await self.action_launch_claude()
            elif button_id == "index-project":
                project = self._project()
                self.operation("/ingest", {"project": project["id"]}, "Indeksowanie")
            elif button_id == "import-history":
                project = self._project()
                path = self.query_one("#import-path", Input).value.strip()
                if not path:
                    raise ValueError("Podaj ścieżkę pliku historii.")
                self.operation(
                    "/import", {"project": project["id"], "path": path, "client": "auto"}, "Import",
                )
            elif button_id == "install-integrations":
                self.install_clients()
            elif button_id == "task-update":
                self.update_task()
            elif button_id == "activate-plan":
                self.activate_selected_plan()
            elif button_id == "refresh-state":
                self.refresh_state()
            elif button_id in {
                "portable-preview", "portable-snapshot", "portable-refresh", "portable-auto",
                "portable-inspect", "portable-dry-run", "portable-import",
            }:
                self.portable_operation(button_id)
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")

    @work(group="add-project", exclusive=True)
    async def add_project(self) -> None:
        button = self.query_one("#add-project", Button)
        try:
            name = self.query_one("#project-name", Input).value.strip()
            raw_root = self.query_one("#project-root", Input).value.strip()
            if not name or not raw_root:
                raise ValueError("Podaj nazwę i katalog projektu.")
            root = Path(raw_root).expanduser().resolve()
            if not root.is_dir():
                raise ValueError("Katalog projektu nie istnieje.")
            button.disabled = True
            project = await asyncio.to_thread(self.store.add_project, name, root)
            self.selected_project = str(project["id"])
            self.refresh_state()
            self.notify(f"Dodano projekt {name}")
        except (ValueError, OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error")
        finally:
            button.disabled = False

    @on(Input.Submitted, "#question")
    def submit_question(self) -> None:
        self.ask_question()

    @work(group="ask", exclusive=True)
    async def ask_question(self) -> None:
        button = self.query_one("#ask", Button)
        question = self.query_one("#question", Input).value.strip()
        try:
            project = self._project()
            if not question:
                raise ValueError("Wpisz pytanie do agenta wiedzy.")
            # Capture the project before awaiting: switching projects must not move an ask.
            project_id = project["id"]
            button.disabled = True
            self.query_one("#answer", Markdown).update(f"Pytam agenta o projekt **{project['name']}**…")
            result = await self._request("POST", "/ask", {"project": project_id, "question": question})
            parts = [f"### {project['name']} · {result.get('status', 'unknown')}", str(result.get("answer", ""))]
            unknowns = result.get("unknowns", [])
            if unknowns:
                parts += ["\n### Braki wiedzy", *[f"- {item}" for item in unknowns]]
            parts.append("\n### Źródła")
            for source in result.get("sources", []):
                if isinstance(source, str):
                    parts.append(f"- `{source}`")
                    continue
                uri = source.get("uri", source.get("source_id", source.get("id", "źródło")))
                lines = ""
                if source.get("line_start") is not None:
                    lines = f" · linie {source['line_start']}–{source.get('line_end', source['line_start'])}"
                parts.append(f"- `{uri}`{lines}")
            if result.get("freshness"):
                parts += ["\nAktualność:", "```json\n" + json.dumps(result["freshness"], ensure_ascii=False) + "\n```"]
            if "elapsed_seconds" in result:
                parts.append(f"\nCzas: {result['elapsed_seconds']:.2f} s")
            self.query_one("#answer", Markdown).update("\n\n".join(parts))
        except (ValueError, OSError, httpx.HTTPError, RuntimeError) as exc:
            self.query_one("#answer", Markdown).update(f"### Agent niedostępny\n\n{exc}")
            self.notify(str(exc), severity="error")
        finally:
            button.disabled = False

    @work(group="operation")
    async def operation(self, path: str, payload: dict, label: str) -> None:
        self._log(f"{label}: rozpoczęto.")
        try:
            result = await self._request("POST", path, payload)
            self._log(f"{label}: {json.dumps(result, ensure_ascii=False)}")
            self.refresh_state()
        except (ValueError, OSError, httpx.HTTPError, RuntimeError) as exc:
            self._log(f"{label}: błąd — {exc}")
            self.notify(str(exc), severity="error")

    @work(group="install", exclusive=True)
    async def install_clients(self) -> None:
        try:
            from .integrations import install_integrations

            project = self._project()
            result = await asyncio.to_thread(install_integrations, self.settings, project)
            self._log("Integracje: " + json.dumps(result, ensure_ascii=False))
            self.notify("Konfiguracja integracji zakończona; sprawdź wynik w operacjach.")
        except (ValueError, OSError, RuntimeError) as exc:
            self._log(f"Integracje: błąd — {exc}")
            self.notify(str(exc), severity="error")

    @work(group="task-update", exclusive=True)
    async def update_task(self) -> None:
        try:
            project = self._project()
            selected = self.records.get(self.selected_record or "")
            if not selected or selected.get("kind") != "task":
                raise ValueError("Wybierz zadanie w tabeli pamięci.")
            metadata = dict(_object(selected.get("metadata"), {}))
            metadata["state"] = str(self.query_one("#task-state", Select).value)
            record = {
                "id": selected["id"], "kind": "task", "key": selected.get("key"),
                "content": selected["content"], "status": selected["status"],
                "metadata": metadata, "expected_version": selected["version"],
            }
            if metadata.get("plan_ref"):
                from .plans import PlanManager

                current = await asyncio.to_thread(PlanManager(self.store).get, project["id"], metadata["plan_ref"])
                record["expected_plan_version"] = current["plan"]["version"]
            await self._request("POST", "/remember", {
                "project": project["id"], "record": record,
                "sources": _object(selected.get("source_ids"), []),
            })
            self.refresh_state()
            self.notify(f"Stan zadania: {metadata['state']}")
        except (ValueError, OSError, httpx.HTTPError, RuntimeError) as exc:
            self.notify(str(exc), severity="error")

    def _portable_status_data(self, project_id: str, worktree: str | None) -> dict:
        """Read existing data without starting the service merely to display a tab."""
        from .memory import MemoryManager

        return MemoryManager(self.store).status(project_id, worktree=worktree)

    def _portable_import_fields(self) -> tuple[str, str, str]:
        return tuple(self.query_one(selector, Input).value.strip() for selector in (
            "#portable-path", "#portable-root", "#portable-name",
        ))

    def _portable_buttons(self) -> None:
        selected = bool(self.selected_project)
        scope = (self.selected_project, self.selected_worktree)
        conditions = {
            "portable-preview": selected,
            "portable-snapshot": selected and self._portable_preview_scope == scope,
            "portable-refresh": True,
            "portable-auto": selected,
            "portable-inspect": True,
            "portable-dry-run": True,
            "portable-import": self._portable_import_preview == self._portable_import_fields(),
        }
        for button_id, allowed in conditions.items():
            self.query_one("#" + button_id, Button).disabled = self._portable_busy or not allowed

    @on(Input.Changed)
    def portable_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"portable-path", "portable-root", "portable-name"}:
            self._portable_import_preview = None
            self.query_one("#portable-import", Button).disabled = True

    async def _load_portable(self, project_id: str | None, worktree: str | None) -> None:
        if self._portable_preview_scope != (project_id, worktree):
            self._portable_preview_scope = None
        self._portable_buttons()
        if not project_id:
            self.query_one("#portable-status", Static).update(
                "Brak wybranego projektu. Możesz wczytać snapshot do jawnie wskazanego katalogu."
            )
            self.query_one("#portable-detection", Static).update("")
            return
        try:
            result = await asyncio.to_thread(self._portable_status_data, project_id, worktree)
            if (project_id, worktree) != (self.selected_project, self.selected_worktree):
                return
            auto = result.get("auto") or {}
            self._portable_auto_enabled = bool(auto.get("enabled"))
            self.query_one("#portable-auto", Button).label = (
                "Wyłącz automat" if self._portable_auto_enabled else "Włącz automat"
            )
            interval = self.query_one("#portable-interval", Input)
            if not interval.has_focus:
                interval.value = str(auto.get("interval_minutes", 15))
            lines = [
                f"Snapshot: {result.get('path', 'brak')}",
                f"Automat: {'włączony' if self._portable_auto_enabled else 'wyłączony'} · "
                f"interwał: {auto.get('interval_minutes', 15)} min",
                f"Worktree automatu: {auto.get('worktree') or 'jeszcze niewybrane'}",
                f"Ostatnia udana kontrola automatu: {auto.get('last_success_at') or 'brak'}",
            ]
            latest = result.get("latest") or {}
            if latest:
                lines.append(f"Dostępny snapshot: {latest.get('snapshot_id', 'nieznany')}")
                if latest.get("created_at"):
                    lines.append(f"Czas snapshotu: {latest['created_at']}")
                if latest.get("counts"):
                    lines.append("Zakres: " + json.dumps(latest["counts"], ensure_ascii=False))
            for label, details in (("Zaległości", result.get("processing") or latest.get("processing")),
                                   ("Kompletność", result.get("completeness") or latest.get("completeness")),
                                   ("Błąd", result.get("error") or auto.get("error"))):
                if details:
                    lines.append(label + ": " + str(details))
            self.query_one("#portable-status", Static).update("\n".join(lines))
            detected = ""
            if latest:
                detected = (
                    "Wykryto snapshot. Użyj „Podgląd pliku” i „Sprawdź import”, aby ocenić jego wczytanie. "
                    "Samo wykrycie niczego nie importuje."
                )
                if not self.query_one("#portable-path", Input).value.strip():
                    self.query_one("#portable-path", Input).value = str(result["path"])
            if worktree and not self.query_one("#portable-root", Input).value.strip():
                self.query_one("#portable-root", Input).value = worktree
            self.query_one("#portable-detection", Static).update(detected)
        except (ValueError, OSError, RuntimeError) as exc:
            self.query_one("#portable-status", Static).update(f"Pamięć przenośna: {exc}")

    def _portable_result(self, label: str, result: dict) -> None:
        # Display the manifest summary, never expand archived source/record bodies.
        keys = ("status", "project", "project_id", "worktree", "path", "manifest_path", "snapshot_id",
                "parent_id", "format_version", "counts", "bytes", "byte_size", "size_bytes",
                "processing", "completeness", "pending", "warnings", "conflicts", "verification", "auto")
        summary = {key: result[key] for key in keys if key in result}
        self.query_one("#portable-detail", Static).update(
            label + "\n" + json.dumps(summary, ensure_ascii=False, indent=2)
        )

    @work(group="portable-operation")
    async def portable_operation(self, button_id: str) -> None:
        if self._portable_busy:
            return
        self._portable_busy = True
        self._portable_buttons()
        try:
            if button_id == "portable-refresh":
                await self._load_portable(self.selected_project, self.selected_worktree)
                return
            if button_id in {"portable-inspect", "portable-dry-run", "portable-import"}:
                fields = self._portable_import_fields()
                path, root, name = fields
                if not path:
                    raise ValueError("Podaj katalog snapshotu lub plik manifest.json.")
                if button_id == "portable-inspect":
                    result = await self._request("POST", "/memory/inspect", {"path": path})
                    self._portable_result("Podgląd zapisanego snapshotu", result)
                    return
                if not root:
                    raise ValueError("Podaj jawny katalog docelowego projektu.")
                if button_id == "portable-import" and self._portable_import_preview != fields:
                    raise ValueError("Najpierw użyj „Sprawdź import” dla tych samych ścieżek i nazwy.")
                dry_run = button_id == "portable-dry-run"
                payload = {"path": path, "root": root, "dry_run": dry_run}
                if name:
                    payload["name"] = name
                result = await self._request("POST", "/memory/import", payload)
                self._portable_result("Sprawdzenie importu" if dry_run else "Import pamięci", result)
                if dry_run:
                    if result.get("status") in {"dry_run", "unchanged"} and fields == self._portable_import_fields():
                        self._portable_import_preview = fields
                else:
                    self._portable_import_preview = None
                    project = result.get("project") or {}
                    if project.get("id"):
                        self.selected_project = str(project["id"])
                        self.selected_worktree = root
                    self.refresh_state()
                    self.notify("Pamięć wczytana; importowane testy pozostają historyczne.")
                return
            project = self._project()
            scope = (project["id"], self.selected_worktree)
            payload = {"project": project["id"], "worktree": self.selected_worktree}
            if button_id == "portable-preview":
                result = await self._request("POST", "/memory/preview", payload)
                self._portable_result("Pełny snapshot projektu — podgląd przed zapisem", result)
                if scope == (self.selected_project, self.selected_worktree):
                    self._portable_preview_scope = scope
            elif button_id == "portable-snapshot":
                if self._portable_preview_scope != scope:
                    raise ValueError("Najpierw sprawdź „Podgląd zapisu” dla wybranego projektu i worktree.")
                result = await self._request("POST", "/memory/snapshot", payload)
                self._portable_result("Zapis pamięci", result)
                self.notify("Snapshot zapisany." if result.get("status") == "created" else "Pamięć bez zmian.")
                await self._load_portable(*scope)
            elif button_id == "portable-auto":
                raw_interval = self.query_one("#portable-interval", Input).value.strip()
                if not raw_interval.isdigit() or int(raw_interval) < 1:
                    raise ValueError("Interwał automatu musi być dodatnią liczbą minut.")
                result = await self._request("PATCH", "/memory/settings", {
                    **payload, "enabled": not self._portable_auto_enabled, "interval_minutes": int(raw_interval),
                })
                self._portable_result("Ustawienia automatu", result)
                await self._load_portable(*scope)
        except (ValueError, OSError, httpx.HTTPError, RuntimeError) as exc:
            self.query_one("#portable-detail", Static).update(f"Operacja nie została ukończona: {exc}")
            self.notify(str(exc), severity="error")
        finally:
            self._portable_busy = False
            self._portable_buttons()

    async def _launch(self, client: str) -> None:
        try:
            from .integrations import run_client

            project = {**self._project(), "root": self.selected_worktree or self._project()["root"]}
            await self._ensure_service()
            with self.suspend():
                code = run_client(self.settings, project, client)
            self.refresh_state()
            if code:
                self.notify(f"{client}: proces zakończył się kodem {code}.", severity="warning")
        except (ValueError, OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error")

    async def action_launch_codex(self) -> None:
        await self._launch("codex")

    async def action_launch_claude(self) -> None:
        await self._launch("claude")

    def on_unmount(self) -> None:
        # Workers are normally stopped by Textual; suppress shutdown-only UI errors.
        with suppress(Exception):
            self.workers.cancel_all()
