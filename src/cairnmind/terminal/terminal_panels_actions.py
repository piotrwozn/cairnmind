"""Sidebar workflow action handlers mixed into `SidebarApp`."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from textual import work

from cairnmind.terminal.terminal_connection_hints import connection_hint
from cairnmind.terminal.terminal_i18n_messages import broker_error_text
from cairnmind.terminal.terminal_notices import about as notice_about
from cairnmind.terminal.terminal_panels_modals import (
    ConfirmModal,
    CredentialModal,
    DiscoveryModal,
    profile_fields,
    profile_from_form,
)
from cairnmind.terminal.terminal_person_act import refused
from cairnmind.terminal.terminal_published_import import archive_replaced, archive_text
from cairnmind.terminal.terminal_ui import (
    ChoiceModal,
    Field,
    FieldError,
    FormModal,
    MessageModal,
    remeasure_buttons,
    replaces_here,
)


class _SidebarActionsMixin:
    """Action workflows (projects, memory, assistants, connections, sessions, settings) for `SidebarApp`."""

    if TYPE_CHECKING:
        settings: Any
        workspace_id: str
        workspace: Any
        workspace_data: dict[str, Any]
        broker: Any
        data: dict[str, Any]
        projects: dict[str, Any]
        language: str
        notice_history: Any

        def t(self, text: str, **kwargs: Any) -> str: ...
        async def request(self, method: str, path: str, **kwargs: Any) -> Any: ...
        async def push_screen_wait(self, screen: Any) -> Any: ...
        def notice(self, value: Any, kind: str = "info") -> None: ...
        def notice_for(self, subject: Any, value: Any, kind: str = "info") -> None: ...
        def show_notice_mark(self) -> None: ...
        def show_page(self, page: str) -> None: ...
        def chosen_project(self) -> dict[str, Any]: ...
        def configured_slots(self, project_id: str | None = None) -> list[dict[str, Any]]: ...
        def assistant_scope(self) -> Any: ...
        async def remember_assistant(self, slot_id: str, scope: Any) -> None: ...
        async def load_state(self) -> None: ...
        async def reconcile(self) -> None: ...
        def refresh_language(self) -> None: ...

    async def _isolated_request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Run a long broker call on its own client so sidebar refreshes are never stalled."""
        from cairnmind.terminal.terminal_client import BrokerClient

        if isinstance(self.broker, BrokerClient):
            def call_once():
                with BrokerClient(self.settings, ensure=False) as client:
                    return client.request(method, path, **kwargs)
            return await asyncio.to_thread(call_once)
        return await asyncio.to_thread(self.broker.request, method, path, **kwargs)

    @work(group="sidebar-scan", exclusive=True, exit_on_error=False)
    async def scan_projects(self):
        try:
            roots = self.data.get("settings", {}).get("scan_roots", [])
            value = await self.push_screen_wait(FormModal(
                self.t("Find folders"),
                [Field("roots", self.t("Folders to scan, one per line"), "\n".join(roots), True, True)],
                note=self.t("Only selected folders are scanned. No client is started."),
                submit=self.t("Search")))
            if value is None:
                return
            await self.request("POST", "/api/discovery", json={"roots": [r.strip() for r in value["roots"].splitlines() if r.strip()]})
            candidate = await self.push_screen_wait(DiscoveryModal(self.broker))
            if candidate:
                root = candidate.get("root", candidate.get("path"))
                existing = next((p for p in self.projects.values() if p["root"] == root), None)
                if existing:
                    await asyncio.to_thread(self.workspace.select_project, existing, root, intent="inspect")
                    await self.reconcile()
                else:
                    await self.register_project(candidate.get("name") or Path(root).name, root)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-register", exclusive=True, exit_on_error=False)
    async def add_project(self):
        async def save(value):
            from cairnmind.terminal.cockpit_state import normalize_path
            root = await asyncio.to_thread(normalize_path, value["root"])
            await self.register_project(value["name"] or root.name, str(root))

        await self.push_screen_wait(FormModal("Add folder", [
            Field("root", "Folder path (Windows or WSL)", required=True, folder=True),
            Field("name", "Name (optional; defaults to folder name)")],
            submit="Add folder", on_submit=save))

    @work(group="sidebar-register", exclusive=True, exit_on_error=False)
    async def clone_project(self):
        from cairnmind.memory import project_clone
        roots = self.data.get("settings", {}).get("scan_roots") or []
        default_parent = roots[0] if roots and isinstance(roots[0], str) else ""
        cloned_outcome: dict = {}

        async def save(value):
            try:
                project_clone.parse_clone_source(value["repo"])
            except ValueError as exc:
                raise FieldError("repo", self.t(str(exc))) from None
            via = (value["via"] or "auto").strip().lower()
            if via not in project_clone.CLONE_TOOLS:
                raise FieldError("via", self.t(project_clone.INVALID_VIA))
            branch = value["branch"] or None
            if branch is not None:
                try:
                    branch = project_clone.validate_branch(branch)
                except ValueError as exc:
                    raise FieldError("branch", self.t(str(exc))) from None
            payload = {"repo": value["repo"], "parent": value["parent"], "via": via}
            if value["name"]:
                payload["name"] = value["name"]
            if branch is not None:
                payload["branch"] = branch
            self.notice("Cloning repository…")
            cloned_outcome["result"] = await self._isolated_request(
                "POST", "/api/projects/clone", json=payload, timeout=620)

        try:
            submitted = await self.push_screen_wait(FormModal("Clone repository", [
                Field("repo", "Repository (owner/repo, HTTPS or SSH URL)", required=True),
                Field("parent", "Parent folder (Windows or WSL)", default_parent, required=True, folder=True),
                Field("name", "Name (optional; defaults to repository name)"),
                Field("via", "Tool: auto, gh or git", "auto"),
                Field("branch", "Branch or tag (optional)")],
                note="Clones into a new subfolder named after the repository, using gh (if signed in) or git. No assistant is started.",
                submit="Clone repository", on_submit=save))
            if submitted is None or "result" not in cloned_outcome:
                return
            cloned = cloned_outcome["result"]
            extra = (self.t("Team published memory found; import it under Memory → Team memory.")
                     if cloned.get("published_memory") else None)
            await self.register_project(cloned["name"], cloned["root"], extra_notice=extra)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    async def register_project(self, name, root, *, extra_notice=None):
        inspected = await self.request("POST", "/api/projects/inspect", json={"root": root})
        root = inspected.get("root", root)
        snapshot = inspected.get("memory_snapshot")
        if snapshot:
            choice = await self.push_screen_wait(ChoiceModal(self.t("Project memory found"), [
                (self.t("Review and import — keep identity and history"), "import"),
                (self.t("New project — separate identity, without existing history"), "new")]))
            if choice == "import":
                await self.import_memory_form(snapshot, root, name)
                return
            if choice != "new":
                return
        result = await self.request("POST", "/api/projects", json={"name": name, "root": root, **({"fresh": True} if snapshot else {})})
        project = result.get("project", result)
        await asyncio.to_thread(self.workspace.select_project, project, root, intent="inspect")
        await self.load_state()
        await self.reconcile()
        self.show_page("project")
        from cairnmind.memory.project_removal import workspace_lines
        said = workspace_lines(self.t, project, root, result)  # brought back, or a removed one kept apart (#327)
        base = " ".join(said) if said else self.t("Project opened. Choose Start work when ready.")
        self.notice(f"{base} {extra_notice}" if extra_notice else base)

    async def import_memory_form(self, path="", root="", name=""):
        checked = {}

        async def validate(values):
            payload = {"path": values["path"], "root": values["root"]}
            if values["name"]:
                payload["name"] = values["name"]
            checked["preview"] = await self.request("POST", "/api/memory/import", json={**payload, "dry_run": True})
            checked["payload"] = payload

        value = await self.push_screen_wait(FormModal(self.t("Check memory import"), [
            Field("path", self.t("Snapshot or manifest.json"), path, True),
            Field("root", self.t("Destination folder"), root, True, folder=True),
            Field("name", self.t("Local name (optional)"), name)],
            note=self.t("Import preserves project identity. Checks from another computer remain historical."),
            submit=self.t("Check import"), on_submit=validate))
        if value is None:
            return
        payload, preview = checked["payload"], checked["preview"]
        approve = await self.push_screen_wait(ConfirmModal(
            self.t("Import this project memory?"), archive_text(preview, self.language),
            approve=self.t("Import memory"), consent=True))
        if not approve:
            return
        # The preview the person just approved listed what the import replaces of this machine's own verdicts.
        try:
            result = await self.request("POST", "/api/memory/import", json={
                **payload, "dry_run": False, "replace_confirmed": replaces_here(preview),
                "expected_replaced": archive_replaced(preview)})
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(self.t("Memory was not imported." if refused(exc) else
                               "The service did not confirm the import, so the memory may have been imported.")
                        + " " + broker_error_text(exc, self.t), "error")
            return
        try:  # the memory is saved from here on (#359): a failed refresh never says it was not
            project = result.get("project")
            if project:
                await asyncio.to_thread(self.workspace.select_project, project, payload["root"], intent="inspect")
            await self.load_state()
            await self.reconcile()
        except (OSError, ValueError, RuntimeError):
            self.notice(self.t("Memory imported, but the view could not be refreshed."), "warn")
            return
        self.notice(self.t("Memory imported for viewing. No clients started."))

    @work(group="sidebar-import", exclusive=True, exit_on_error=False)
    async def import_memory(self):
        try:
            await self.import_memory_form(root=self.workspace_data.get("worktree") or "")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-memory", exclusive=True, exit_on_error=False)
    async def action_memory(self):
        try:
            await asyncio.to_thread(self.workspace.update, {
                "selected_entity": None, "focus": "inspector",
                "command_request": {"id": uuid4().hex, "action": "memory", "target_role": "inspector"}})
            await self.reconcile()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-ask", exclusive=True, exit_on_error=False)
    async def action_ask(self):
        """Ask the chosen project's memory; read-only, nothing is saved."""
        from cairnmind.terminal.terminal_commands import ask_summary
        subject = None  # the project asked about, which may not be the one shown when the answer or error comes
        try:
            project = self.chosen_project()
            subject = notice_about(project["id"], project.get("name"))
            values = await self.push_screen_wait(FormModal(
                "Ask memory", [Field("question", "Question", required=True, multiline=True)],
                note="Answers come only from this project's memory, with cited sources. Nothing is saved.",
                submit="Ask"))
            if not values:
                return
            payload = {"project": project["id"], "question": values["question"]}
            if self.workspace_data.get("project_id") == project["id"] and self.workspace_data.get("worktree"):
                payload["worktree"] = self.workspace_data["worktree"]
                subject = notice_about(project["id"], project.get("name"), payload["worktree"])
            self.notice_for(subject, "Asking memory…")
            result = await self._isolated_request(
                "POST", "/api/ask", json=payload, timeout=self.settings.ask_timeout + 150)
            self.notice("")
            await self.push_screen_wait(MessageModal("Memory answer", ask_summary(result, self.t)))
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice_for(subject, exc)

    @work(group="sidebar-brief", exclusive=True, exit_on_error=False)
    async def action_brief(self):
        """Show where work stopped in the chosen project; read-only and without a model."""
        subject = None
        try:
            project = self.chosen_project()
            params = {"project": project["id"]}
            subject = notice_about(project["id"], project.get("name"))
            if self.workspace_data.get("project_id") == project["id"] and self.workspace_data.get("worktree"):
                params["worktree"] = self.workspace_data["worktree"]
                subject = notice_about(project["id"], project.get("name"), params["worktree"])
            # Check evidence hashes the worktree: allow more than the default 20 s, and use a
            # separate client so a slow brief does not hold the shared client's lock.
            result = await self._isolated_request("GET", "/api/brief", params=params, timeout=120)
            text = result.get("text") if isinstance(result, dict) else None
            await self.push_screen_wait(MessageModal("Brief", text or self.t("The memory service returned no brief.")))
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice_for(subject, exc)

    # The group of Start work: choosing another project cancels a stage in flight, and back.
    @work(group="sidebar-select", exclusive=True, exit_on_error=False)
    async def action_next_stage(self):
        """Start the assistant or run the check mapped to the next plan step; changes no task."""
        from cairnmind.terminal.workspace_stage import run_next_stage
        try:
            await run_next_stage(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-stages", exclusive=True, exit_on_error=False)
    async def edit_stages(self):
        from cairnmind.terminal.workspace_stage import edit_stages
        try:
            await edit_stages(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-mcp", exclusive=True, exit_on_error=False)
    async def edit_mcp_servers(self):
        """Only a person adds or removes a project's MCP servers, here (#126)."""
        from cairnmind.terminal.workspace_mcp import edit_mcp_servers
        try:
            await edit_mcp_servers(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-profiles", exclusive=True, exit_on_error=False)
    async def edit_profiles(self, identifier=None, *, advanced=False):
        try:
            profiles = self.data.get("profiles", [])
            if identifier is None:
                identifier = await self.push_screen_wait(ChoiceModal("Connections",
                    [(p["label"], p["id"]) for p in profiles] + [("Add connection", "+")]))
            if identifier is None:
                return
            old = next((p for p in profiles if p["id"] == identifier), None)
            client, provider = (old["client"], old.get("provider")) if old else (None, None)
            if old is None:
                kind = await self.push_screen_wait(ChoiceModal("Connection type", [
                    ("Codex — ChatGPT account", "codex"), ("Claude Code — Claude account", "claude"),
                    ("GitHub Copilot — GitHub account", "copilot"), ("Ollama — local models", "ollama"),
                    ("OpenAI — API key", "openai"), ("Anthropic — API key", "anthropic"),
                    ("OpenAI-compatible API", "openai_compatible")]))
                if kind is None:
                    return
                client = kind if kind in {"codex", "claude", "copilot"} else "api"
                provider = kind if client == "api" else None
            identifier = old["id"] if old else "profile-" + uuid4().hex[:12]
            fields = profile_fields(old, client=client, provider=provider)
            if not advanced:
                fields = [f for f in fields if f.name in {"label", "model"} or
                          (f.name == "base_url" and provider == "openai_compatible")]
            if not old:
                for index, field in enumerate(fields):
                    if field.name == "label":
                        default_label = {"api": provider or "", "copilot": "GitHub Copilot",
                                         "codex": "Codex", "claude": "Claude"}.get(client or "", "")
                        fields[index] = Field("label", "Connection name", default_label, True)
                    elif field.name == "model" and client == "copilot":
                        fields[index] = Field("model", self.t("Model"), "auto")

            async def save(values):
                from cairnmind.agents.api_profiles import _RANGES, normalize_profile
                defaults = {f.name: f.value for f in profile_fields(old, client=client, provider=provider)}
                values = {**defaults, **values}
                for field in fields:
                    if field.name in _RANGES:
                        # The range the profile check applies, named with its field, before anything is sent.
                        minimum, maximum = _RANGES[field.name]
                        try:
                            valid = minimum <= int(values[field.name]) <= maximum
                        except ValueError:
                            valid = False
                        if not valid:
                            raise FieldError(field.name, self.t(field.label) + ": " + self.t(
                                "Enter a whole number from {minimum} to {maximum}.", minimum=minimum, maximum=maximum))
                try:
                    updated = normalize_profile(profile_from_form(values, identifier=identifier, client=client, provider=provider))
                except ValueError as exc:
                    raise FieldError("model" if "model" in str(exc).lower() else fields[0].name, str(exc)) from exc
                current = await self.request("GET", "/api/state")
                rows = [updated if p["id"] == identifier else p for p in current.get("profiles", [])]
                if not any(p["id"] == identifier for p in rows):
                    rows.append(updated)
                await self.request("PUT", "/api/profiles", json={"profiles": rows})

            title = "Advanced limits and credentials" if advanced else "Edit connection" if old else "Add connection"
            value = await self.push_screen_wait(FormModal(
                title, fields, note="Changes apply to the next launch. Saving does not start a model.", on_submit=save))
            if value:
                await self.load_state()
                self.notice("Connection saved. You can connect it or choose it for your project.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    async def save_slots(self, slots, project_id):
        settings = self.data.get("settings", {})
        payload = {"project_slots": {**settings.get("project_slots", {}), project_id: slots}} if project_id else {"default_slots": slots}
        await self.request("PUT", "/api/settings", json=payload)
        await self.load_state()
        await self.reconcile()

    @work(group="assistant-add", exclusive=True, exit_on_error=False)
    async def add_assistant(self):
        try:
            scope = self.assistant_scope()
            project_id = self.workspace_data.get("project_id")
            if not project_id:
                raise ValueError(self.t("Select a visible project first."))
            profiles = self.data.get("profiles", [])
            choice = await self.push_screen_wait(ChoiceModal("Add assistant",
                [(p["label"], p["id"]) for p in profiles] + [("Add connection", "+")]))
            if scope != self.assistant_scope():
                return
            if choice == "+":
                self.edit_profiles("+")
                return
            if not choice:
                return
            slots = list(self.configured_slots(project_id))
            selected = next((s for s in slots if s["profile_id"] == choice), None)
            if selected is None:
                selected = {"id": "slot-" + uuid4().hex[:12], "profile_id": choice}
                slots.append(selected)
                await self.save_slots(slots, project_id)
            await self.remember_assistant(selected["id"], scope)
            self.notice("An assistant was added. Existing assignments were preserved.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-slots", exclusive=True, exit_on_error=False)
    async def edit_slots(self):
        try:
            scope = self.assistant_scope()
            project_id = self.workspace_data.get("project_id")
            profiles = {p["id"]: p for p in self.data.get("profiles", [])}
            slots = list(self.configured_slots(project_id))
            chosen = await self.push_screen_wait(ChoiceModal("Manage assistants",
                [(profiles.get(s["profile_id"], {}).get("label", self.t("Unavailable")), s["id"]) for s in slots]
                + [("Add assistant", "+")]))
            if scope != self.assistant_scope():
                return
            if chosen == "+":
                self.add_assistant()
                return
            if not chosen:
                return
            action = await self.push_screen_wait(ChoiceModal("Assistant", [
                ("Choose assistant", "choose"), ("Replace connection", "replace"),
                ("Move earlier", "up"), ("Remove from layout", "remove")]))
            if scope != self.assistant_scope():
                return
            index = next(i for i, slot in enumerate(slots) if slot["id"] == chosen)
            if action == "choose":
                await self.remember_assistant(chosen, scope)
                return
            if action == "replace":
                profile_id = await self.push_screen_wait(ChoiceModal("Connections", [(p["label"], p["id"]) for p in profiles.values()]))
                if not profile_id:
                    return
                slots[index] = {**slots[index], "profile_id": profile_id}
            elif action == "up" and index:
                slots[index - 1], slots[index] = slots[index], slots[index - 1]
            elif action == "remove":
                if len(slots) < 2:
                    self.notice("Keep at least one assistant in the layout.")
                    return
                if not await self.push_screen_wait(ConfirmModal("Remove from layout?",
                    "This hides the assignment. Existing processes are not stopped.", approve="Remove")):
                    return
                slots.pop(index)
            else:
                return
            await self.save_slots(slots, project_id)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-auth", exclusive=True, exit_on_error=False)
    async def login(self):
        try:
            await self.load_state()
            from cairnmind.terminal.stages import assistant_name
            choice = await self.push_screen_wait(ChoiceModal("Connections",
                [(assistant_name(p, self.t), p["id"]) for p in self.data.get("profiles", [])]
                + [("Add connection", "+")]))
            if choice == "+":
                self.edit_profiles("+")
                return
            if choice:
                profile = next(p for p in self.data["profiles"] if p["id"] == choice)
                await self.profile_connection(profile)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-more", exclusive=True, exit_on_error=False)
    async def action_more(self):
        action = await self.push_screen_wait(ChoiceModal("More", [
            ("Sessions", "sessions"), ("Manage assistants", "assistants"),
            ("Find folders", "scan"), ("Queue", "queue"),
            ("Snapshots and export", "snapshot"), ("Import memory", "import"),
            # New entries go last: keyboard paths to the existing ones keep their positions.
            ("Settings", "settings"), ("Technical details", "details"), ("Plan stages", "stages"),
            ("MCP servers", "mcp"), ("Transcript record kinds", "record_kinds"),
            ("Notices", "notices"), ("Old task groups", "legacy_groups"),
            ("Delete project memory", "forget_project"), ("Clone repository", "clone_project")]))
        actions = {"sessions": self.sessions, "assistants": self.edit_slots, "stages": self.edit_stages,
                   "mcp": self.edit_mcp_servers, "scan": self.scan_projects, "queue": self.action_queue,
                   "snapshot": self.action_snapshot, "import": self.import_memory, "settings": self.action_settings,
                   "notices": self.action_notices, "record_kinds": self.record_kinds,
                   "legacy_groups": self.legacy_groups, "forget_project": self.forget_project,
                   "clone_project": self.clone_project}
        if action == "details":
            await self.push_screen_wait(MessageModal("Status", self.workspace_data))
        elif action in actions:
            actions[action]()

    @work(group="sidebar-legacy-groups", exclusive=True, exit_on_error=False)
    async def legacy_groups(self):
        """More, then Old task groups (#305): adopt a group of tasks from before 0.11.0 into a plan."""
        from cairnmind.terminal.terminal_legacy_groups import open_view
        try:
            await open_view(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-forget-project", exclusive=True, exit_on_error=False)
    async def forget_project(self):
        """More, then Delete project memory (#347): the person deletes a project's whole memory."""
        from cairnmind.terminal.terminal_project_forget import open_view
        try:
            await open_view(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-record-kinds", exclusive=True, exit_on_error=False)
    async def record_kinds(self):
        """More, then Transcript record kinds (#210): look at an unknown kind, accept or withdraw it."""
        from cairnmind.terminal.terminal_record_kinds import open_view
        try:
            await open_view(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    @work(group="sidebar-notices", exclusive=True, exit_on_error=False)
    async def action_notices(self):
        """The notices this sidebar showed, newest first; opening clears the mark on the notice line."""
        self.notice_history.mark_seen()
        self.show_notice_mark()
        text = self.notice_history.lines(self.workspace_data, self.t)
        await self.push_screen_wait(MessageModal("Notices", text or self.t("No notices yet.")))

    @work(group="sidebar-process-memory", exclusive=True, exit_on_error=False)
    async def action_queue(self):
        subject = None
        try:
            selected = await asyncio.to_thread(self.workspace.read)
            if not selected.get("project_id"):
                raise ValueError(self.t("Select a visible project first."))
            scope = (selected["project_id"], selected.get("worktree"))
            subject = notice_about(scope[0], selected.get("project_name"), scope[1])
            status = await self.request("GET", "/api/status", params={"project": scope[0]})
            if "models" in status:  # deterministic-only (#309): nothing to consent to, say what to do instead
                from cairnmind.cli_cmds.first_start import PROCESS_OFF, PROCESS_RESTART, RESTART_FOR_MODELS
                await self.clear_deferred_launch_error(scope)
                self.notice_for(subject, PROCESS_RESTART if status.get("get_models") == RESTART_FOR_MODELS else PROCESS_OFF)
                return
            approved = await self.push_screen_wait(ConfirmModal("Queue",
                self.t("Use local models to process pending memory for this project only. No assistant is started.")
                + "\n\n" + str(selected.get("project_name") or scope[0]), approve="Process memory"))
            if not approved:
                return
            current = await asyncio.to_thread(self.workspace.read)
            if (current.get("project_id"), current.get("worktree")) != scope:
                raise ValueError(self.t("Project changed. Open Queue again."))
            result = await self.request("POST", "/api/background-inference/enable",
                                        json={"project": scope[0], "worktree": scope[1]})
            if result.get("accepted") is not True or result.get("project_id") != scope[0]:
                raise ValueError(self.t("Memory processing could not be enabled."))
            if await self.clear_deferred_launch_error(scope):
                self.notice_for(subject, "Memory processing enabled for this project." if result.get("worker_running") else
                                result.get("notice") or "Background memory processing is disabled in this service.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice_for(subject, exc)

    async def clear_deferred_launch_error(self, scope):
        current = await asyncio.to_thread(self.workspace.read)
        if (current.get("project_id"), current.get("worktree")) != scope:
            return False
        await asyncio.to_thread(self.workspace.update, {"launch_errors": [
            row for row in current.get("launch_errors", []) if row.get("code") != "background_inference_deferred"]},
            expected_revision=current["revision"])
        return True

    @work(group="sidebar-settings", exclusive=True, exit_on_error=False)
    async def action_settings(self):
        from cairnmind.terminal.cockpit_state import CockpitState
        store = CockpitState(self.settings)
        values = await asyncio.to_thread(store.load)
        choice = await self.push_screen_wait(ChoiceModal("Settings", [
            ("Language", "language"), ("Layout", "view"), ("Reduce motion", "reduced_motion"),
            ("Activity column", "activity_column"), ("Storage and models", "topology")]))
        if not choice:
            return
        if choice == "topology":  # a read of the running service: where storage and models run (#74)
            from cairnmind.terminal.terminal_setup import open_view
            try:
                await open_view(self)
            except (OSError, ValueError, RuntimeError) as exc:
                self.notice(exc)
            return
        options = [("English", "en"), ("Polski", "pl")] if choice == "language" else (
            [("Focus", "focus"), ("Cockpit", "cockpit")] if choice == "view" else [("On", "on"), ("Off", "off")])
        titles = {"language": "Language", "view": "Layout", "reduced_motion": "Reduce motion",
                  "activity_column": "Activity column"}
        selected = await self.push_screen_wait(ChoiceModal(titles[choice], options))
        if selected is None:
            return
        values = {choice: selected == "on" if choice in {"reduced_motion", "activity_column"} else selected}
        await asyncio.to_thread(store.update, values)
        self.language = values.get("language", self.language)
        await self.reconcile()
        self.refresh_language()
        remeasure_buttons(self)
        self.notice("Preferences saved.")

    @work(group="sidebar-snapshot", exclusive=True, exit_on_error=False)
    async def action_snapshot(self):
        value = {"project_id": self.workspace_data.get("project_id"), "entity_type": "memory", "id": "memory"}
        await asyncio.to_thread(self.workspace.update, {"selected_entity": value, "focus": "inspector"})
        await self.reconcile()

    async def profile_connection(self, profile):
        identifier = profile["id"]
        choices = [("Check connection", "status"), ("Edit connection", "edit"), ("Advanced limits and credentials", "advanced")]
        if profile["client"] in {"codex", "claude"}:
            choices.insert(0, ("Connect account", "native_login"))
        else:
            choices += [("Choose model", "models"), ("Test model and tools", "probe")]
        if profile["client"] == "copilot":
            choices.insert(0, ("Connect GitHub account", "login"))
            choices += [("Prepare Copilot runtime", "prepare")]
        elif profile["client"] == "api":
            choices += [("Set API key", "credential"), ("Remove saved key", "delete")]
        action = await self.push_screen_wait(ChoiceModal(profile["label"], choices))
        if action is None:
            return
        base = "/api/profiles/" + identifier
        if action in {"edit", "advanced"}:
            self.edit_profiles(identifier, advanced=action == "advanced")
        elif action == "native_login":
            from cairnmind.terminal.workspace import show_auth
            await asyncio.to_thread(show_auth, self.settings, profile["client"], self.workspace_id)
            self.notice("Account login opened. Credentials stay outside project memory.")
        elif action == "credential":
            value = await self.push_screen_wait(CredentialModal(profile["label"]))
            if value is not None:
                await self.request("POST", base + "/credential", json=value)
                value.clear()
                await self.load_state()
                self.notice(self.t("Key saved. Its value will not be displayed again."))
        elif action == "delete":
            if await self.push_screen_wait(ConfirmModal(
                self.t("Remove saved key?"), self.t("Only the local key for this connection will be removed."),
                approve=self.t("Remove saved key"))):
                await self.request("DELETE", base + "/credential")
                self.notice(self.t("Key removed."))
        elif action == "probe":
            if await self.push_screen_wait(ConfirmModal(
                self.t("Test this model?"),
                self.t("This sends a real request to the provider and can use paid tokens or Copilot quota. It does not edit your project."),
                approve=self.t("Run test"), consent=True)):
                self.notice(self.t("Testing the model and tools…"))
                result = await asyncio.to_thread(self.broker.request, "POST", base + "/probe", json={}, timeout=180)
                await self.push_screen_wait(MessageModal(self.t("Connection test result"), result))
        elif action == "prepare":
            if await self.push_screen_wait(ConfirmModal(
                self.t("Prepare Copilot?"),
                self.t("Downloads and verifies the pinned official runtime. No model request is made."),
                approve=self.t("Prepare"), consent=True)):
                self.notice(self.t("Preparing the official Copilot runtime…"))
                result = await asyncio.to_thread(self.broker.request, "POST", "/api/copilot/prepare", json={}, timeout=300)
                await self.push_screen_wait(MessageModal(self.t("Copilot setup"), result))
        elif action == "login":
            local = await self.request("GET", base + "/status")
            if local.get("runtime", {}).get("available") is False:
                consent = await self.push_screen_wait(ConfirmModal("Prepare Copilot?",
                    "Downloads and verifies the pinned official runtime. No model request is made.",
                    approve="Prepare", consent=True))
                if not consent:
                    return
                prepared = await asyncio.to_thread(self.broker.request, "POST", "/api/copilot/prepare", json={}, timeout=300)
                if prepared.get("available") is False:
                    await self.push_screen_wait(MessageModal("Copilot setup", prepared))
                    return
            result = await self.request("POST", "/api/copilot/login", json={})
            if run := result.get("run"):
                from cairnmind.terminal.workspace import select_run
                await asyncio.to_thread(select_run, self.settings, run["id"], self.workspace_id)
                await self.reconcile()
                self.notice(self.t("Copilot login opened. Credentials stay outside project memory."))
            else:
                await self.push_screen_wait(MessageModal(self.t("Copilot login"), result))
        elif action == "models":
            result = await self.request("GET", base + "/models")
            models = [model for model in result.get("models", []) if model.get("enabled", True)]
            if not models:
                await self.push_screen_wait(MessageModal(self.t("Models"), result))
                return
            choice = await self.push_screen_wait(ChoiceModal(
                self.t("Choose model — applies to the next launch"), [(m["id"], m["id"]) for m in models]))
            if choice:
                updated = {**profile, "model": choice}
                rows = [updated if p["id"] == identifier else p for p in self.data.get("profiles", [])]
                await self.request("PUT", "/api/profiles", json={"profiles": rows})
                await self.load_state()
                self.notice(self.t("Model saved. No generation started."))
        elif action == "status":
            result = (await self.request("GET", "/api/auth", params={"client": profile["client"]})
                      if profile["client"] in {"codex", "claude"} else await self.request("GET", base + "/status"))
            if profile["client"] == "copilot":
                result = {**result, "message": self.t("Local setup only. Use Connect GitHub account to sign in; Choose model checks available models.")}
            elif hint := connection_hint([result.get("code")], self.t):
                result = {**result, "message": hint}  # the bare code stays under Technical details
            await self.push_screen_wait(MessageModal("Status", result))

    @work(group="sidebar-sessions", exclusive=True, exit_on_error=False)
    async def sessions(self):
        try:
            await self.load_state()
            runs = self.data.get("runs", [])
            from cairnmind.terminal.terminal_activity_data import run_is_live, session_label
            # The memory state comes before the folder, so a narrow list cuts the path, never the state.
            choices = [(session_label(r, self.t), r["id"]) for r in runs]
            if not choices:
                self.notice(self.t("No sessions yet."))
                return
            while True:
                identifier = await self.push_screen_wait(ChoiceModal(self.t("Sessions on this computer"), choices))
                if not identifier:
                    return
                run = next(r for r in runs if r["id"] == identifier)
                # An ended run has no process: stopping, interrupting or taking control of it is not offered.
                live = [(self.t("Take control"), "takeover"), (self.t("Interrupt current operation"), "interrupt"),
                        (self.t("Stop process"), "stop")] if run_is_live(run) else []
                action = await self.push_screen_wait(ChoiceModal(
                    self.t("Session actions"),
                    [(self.t("Show / attach without a new process"), "show"), *live]
                    + ([(self.t("Resume saved session"), "resume"), (self.t("New session with project memory"), "new")]
                       if run.get("project_id") else [])))
                if action is not None:
                    break  # Esc in the actions goes back to this list, as its hint says
            from cairnmind.terminal import workspace as manager
            if action == "show":
                await asyncio.to_thread(manager.select_run, self.settings, identifier, self.workspace_id)
                if not run_is_live(run):
                    self.notice(self.t("This session has ended. Resume it or start a new one.") if run.get("project_id") else self.t("This session has ended."))
            elif action == "takeover":
                await asyncio.to_thread(manager.take_control, self.settings, identifier, self.workspace_id)
            elif action == "stop":
                await asyncio.to_thread(manager.stop_run, self.settings, identifier, run.get("generation"), self.workspace_id)
                self.notice("The session was stopped.")
            elif action == "interrupt":
                await self.request("POST", f"/api/client-runs/{identifier}/interrupt", json={"generation": run.get("generation")})
            elif action == "resume":
                await asyncio.to_thread(manager.resume_run, self.settings, identifier, self.workspace_id)
            elif action == "new":
                profile = run.get("profile_id") or run["client"]
                result = await self.request("POST", "/api/client-runs", json={
                    "project": run["project_id"], "worktree": run["worktree"],
                    "slots": [{"id": run["slot_id"], "profile_id": profile}], "resume": False, "idempotency_key": uuid4().hex})
                if result.get("errors"):
                    raise ValueError(json.dumps(result["errors"], ensure_ascii=False))
                if result.get("runs"):
                    await asyncio.to_thread(manager.select_run, self.settings, result["runs"][0]["id"], self.workspace_id)
            await self.reconcile()
            await self.load_state()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(broker_error_text(exc, self.t) + self.t(" · Starting a new session is a separate action."))
