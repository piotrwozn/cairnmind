"""Owner-local workspace selection; viewing never grants memory permissions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .cockpit_state import _atomic, _private_directory, _private_lock, normalize_path

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_PANES = {"sidebar", "client1", "client2", "inspector", "graph"}
_FIELDS = {"project_id", "project_name", "worktree", "intent", "work_request", "handled_work_request",
           "selected_entity", "slots", "visible_slot_ids", "run_ids", "focus", "layout", "zoom",
           "pane_ids", "cached_projects", "launch_errors", "last_error", "broker_available", "auth_runs",
           "requested_slot_ids", "command_request", "assistant_selections"}


def _identifier(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError("Niepoprawny identyfikator elementu środowiska.")
    return value


class WorkspaceState:
    def __init__(self, settings, workspace_id="default"):
        if not isinstance(workspace_id, str) or not _IDENTIFIER.fullmatch(workspace_id):
            raise ValueError("Niepoprawny identyfikator środowiska.")
        self.settings, self.workspace_id = settings, workspace_id
        self.directory = settings.data_dir / "workspaces" / workspace_id
        self.path = self.directory / "state.json"

    def defaults(self):
        return {"version": 1, "workspace_id": self.workspace_id, "revision": 0,
                "project_id": None, "project_name": None, "worktree": None, "intent": "inspect",
                "work_request": None, "handled_work_request": None, "selected_entity": None,
                "slots": [], "visible_slot_ids": [], "run_ids": [], "focus": "sidebar", "layout": "wide",
                "zoom": False, "pane_ids": {}, "cached_projects": [], "launch_errors": [],
                "last_error": None, "broker_available": None, "auth_runs": [], "updated_at": None,
                "requested_slot_ids": [], "command_request": None, "assistant_selections": []}

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return self.defaults()
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2 * 1024 * 1024):
                raise ValueError("Stan środowiska musi być prywatnym plikiem użytkownika.")
            value = json.load(handle)
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("workspace_id") != self.workspace_id or type(value.get("revision")) is not int):
            raise ValueError("Nieobsługiwany lub uszkodzony stan środowiska.")
        result = self.defaults()
        result.update(value)
        self._validate(result)
        return result

    def _validate(self, value):
        for key in ("project_id", "project_name", "work_request", "handled_work_request"):
            _identifier(value[key], nullable=True)
        if value["worktree"] is not None and (not isinstance(value["worktree"], str)
                                              or not Path(value["worktree"]).is_absolute() or "\0" in value["worktree"]):
            raise ValueError("Katalog pracy musi być ścieżką bezwzględną.")
        if value["intent"] not in {"inspect", "work"} or value["focus"] not in _PANES:
            raise ValueError("Niepoprawny tryb lub wybrany panel środowiska.")
        if value["layout"] not in {"wide", "compact"} or type(value["zoom"]) is not bool:
            raise ValueError("Niepoprawny układ paneli.")
        selections = value["assistant_selections"]
        if not isinstance(selections, list) or len(selections) > 1000:
            raise ValueError("Invalid assistant selections")
        scopes = set()
        for row in selections:
            if not isinstance(row, dict) or set(row) != {"project_id", "worktree", "slot_id"}:
                raise ValueError("Invalid assistant selection")
            _identifier(row["project_id"])
            _identifier(row["slot_id"])
            root = row["worktree"]
            if (not isinstance(root, str) or not Path(root).is_absolute() or "\0" in root
                    or os.path.normpath(root) != root):
                raise ValueError("Invalid assistant worktree")
            scope = (row["project_id"], root)
            if scope in scopes:
                raise ValueError("Duplicate assistant selection scope")
            scopes.add(scope)
        selected = value["selected_entity"]
        if selected is not None:
            allowed = {"project_id", "entity_type", "id", "revision", "owner_type", "owner_id"}
            if not isinstance(selected, dict) or set(selected) - allowed or not {"project_id", "entity_type", "id"} <= set(selected):
                raise ValueError("Niepoprawny wybór wpisu pamięci.")
            if selected["project_id"] != value["project_id"]:
                raise ValueError("Wpis należy do innego projektu niż wybrany panel.")
            for key in ("entity_type", "id"):
                _identifier(selected[key])
            if "revision" in selected and (type(selected["revision"]) is not int or selected["revision"] < 1):
                raise ValueError("Niepoprawna wersja historyczna.")
            for key in ("owner_type", "owner_id"):
                if key in selected:
                    _identifier(selected[key])
        if not isinstance(value["slots"], list) or len(value["slots"]) > 32:
            raise ValueError("Niepoprawny zestaw paneli klientów.")
        seen = set()
        for slot in value["slots"]:
            if not isinstance(slot, dict) or not {"id", "profile_id"} <= set(slot):
                raise ValueError("Niepoprawny panel klienta.")
            _identifier(slot["id"])
            _identifier(slot["profile_id"])
            if slot["id"] in seen:
                raise ValueError("Panel klienta występuje więcej niż raz.")
            seen.add(slot["id"])
        for key, maximum in (("run_ids", 1000), ("visible_slot_ids", 2), ("auth_runs", 20), ("requested_slot_ids", 32)):
            if not isinstance(value[key], list) or len(value[key]) > maximum:
                raise ValueError("Niepoprawna lista paneli środowiska.")
            for item in value[key]:
                _identifier(item)
            if len(set(value[key])) != len(value[key]):
                raise ValueError("Panel występuje więcej niż raz.")
        if any(item not in seen for item in value["visible_slot_ids"]):
            raise ValueError("Wybrany panel nie należy do bieżącego zestawu klientów.")
        if (not isinstance(value["pane_ids"], dict) or set(value["pane_ids"]) - _PANES
                or any(not isinstance(p, str) or not re.fullmatch(r"%[0-9]+", p) for p in value["pane_ids"].values())):
            raise ValueError("Niepoprawna tożsamość panelu terminala.")
        if not isinstance(value["cached_projects"], list) or len(value["cached_projects"]) > 10000:
            raise ValueError("Niepoprawna lista projektów.")
        if not isinstance(value["launch_errors"], list) or len(value["launch_errors"]) > 32:
            raise ValueError("Niepoprawna lista błędów klientów.")
        if value["last_error"] is not None and (not isinstance(value["last_error"], str) or len(value["last_error"]) > 1000):
            raise ValueError("Niepoprawny komunikat środowiska.")
        if value["broker_available"] is not None and type(value["broker_available"]) is not bool:
            raise ValueError("Niepoprawny stan usługi klientów.")
        command = value["command_request"]
        if command is not None:
            if (not isinstance(command, dict) or set(command) != {"id", "action", "target_role"}
                    or not isinstance(command["target_role"], str) or command["target_role"] not in {"sidebar", "inspector", "graph"}
                    or not isinstance(command["action"], str) or command["action"] not in {"search", "add_project", "start_work", "connections", "sessions",
                                                  "memory", "more", "settings", "next_stage"}):
                raise ValueError("Invalid workspace command")
            _identifier(command["id"])
        if len(json.dumps(value, allow_nan=False).encode()) > 2 * 1024 * 1024:
            raise ValueError("Stan środowiska przekracza limit.")

    def update(self, changes, expected_revision=None):
        if not isinstance(changes, dict) or set(changes) - _FIELDS:
            raise ValueError("Nieobsługiwane pole stanu środowiska.")
        _private_directory(self.directory.parent)
        with _private_lock(self.directory / "state.lock"):
            return self._commit(self.read(), changes, expected_revision)

    def _commit(self, value, changes, expected_revision=None):
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != value["revision"]):
            raise ValueError("stale_workspace_revision: odczytaj bieżący stan i ponów operację.")
        proposed = {**value, **deepcopy(changes)}
        self._validate(proposed)
        if all(proposed[key] == value[key] for key in changes):
            return value
        proposed["revision"] += 1
        proposed["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic(self.path, proposed)
        return proposed

    def merge_ids(self, key, add=(), *, remove=()):
        if key not in {"run_ids", "auth_runs"}:
            raise ValueError("Nieobsługiwana lista sesji środowiska.")
        for identifier in (*add, *remove):
            _identifier(identifier)
        _private_directory(self.directory.parent)
        with _private_lock(self.directory / "state.lock"):
            current = self.read()
            merged = list(dict.fromkeys([*current[key], *add]))
            merged = [identifier for identifier in merged if identifier not in remove]
            maximum = 1000 if key == "run_ids" else 20
            return self._commit(current, {key: merged[-maximum:]})

    def claim_work_request(self, expected):
        """Linearize the exact start intent before crossing the broker boundary.

        Navigation or cancellation that commits before this claim wins. Once
        claimed, an uncertain network result must never cause an automatic retry.
        """
        keys = ("project_id", "worktree", "intent", "work_request", "handled_work_request", "requested_slot_ids")
        _private_directory(self.directory.parent)
        with _private_lock(self.directory / "state.lock"):
            current = self.read()
            if (current["intent"] != "work" or not current["work_request"]
                    or current["work_request"] == current["handled_work_request"]
                    or any(current.get(key) != expected.get(key) for key in keys)):
                return False
            self._commit(current, {"handled_work_request": current["work_request"]})
            return True

    def select_project(self, project, worktree=None, *, intent="inspect", requested_slot_ids=None):
        if intent not in {"inspect", "work"} or (project is None and intent == "work"):
            raise ValueError("Praca wymaga jawnie wybranego projektu.")
        if project is not None and (not isinstance(project, dict) or not {"id", "root"} <= set(project)):
            raise ValueError("Wybierz zarejestrowany projekt.")
        requested = [] if requested_slot_ids is None else requested_slot_ids
        if (not isinstance(requested, list) or not all(isinstance(item, str) for item in requested)
                or len(requested) > 32 or len(set(requested)) != len(requested)
                or (intent == "work" and not requested) or (intent == "inspect" and requested)):
            raise ValueError("Choose the assistant to start explicitly.")
        for identifier in requested:
            _identifier(identifier)
        for _ in range(5):
            current = self.read()
            root = str(normalize_path(worktree or project["root"])) if project else None
            changes = {"project_id": project["id"] if project else None,
                       "project_name": project.get("name", project["id"]) if project else None,
                       "worktree": root, "selected_entity": None, "intent": intent, "last_error": None,
                       "work_request": str(uuid4()) if intent == "work" else None,
                       "requested_slot_ids": list(requested), "command_request": None}
            if current["project_id"] != changes["project_id"] or current["worktree"] != root:
                changes.update(slots=[], visible_slot_ids=[])
            try:
                return self.update(changes, expected_revision=current["revision"])
            except ValueError as error:
                if "stale_workspace_revision" not in str(error):
                    raise
        raise ValueError("stale_workspace_revision: stan środowiska zmienia się, ponów wybór.")
