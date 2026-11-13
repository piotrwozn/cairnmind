"""Atomic, versioned project plans shared by independent coding clients.

Plan definitions own task identity and dependencies. Progress remains a regular
memory-record mutation, with a compare-and-swap on both task and plan versions.
"""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import hashlib
import heapq
import json
from pathlib import Path
from uuid import uuid4

from .store import ORIGIN_METADATA, _clean, _insert, _json, _now, _one, _set


_EVIDENCE_FIELDS = {
    "test_passed", "test_source_ids", "review_passed", "review_source_ids",
    "acceptance_passed", "acceptance_source_ids", "check_run_ids",
    "verification_status", "verified", "machine_verified",
}
_MANAGED_FIELDS = {"plan_ref", "step_key", "step_index", "plan_context_hash", "plan_archived", "worktree"}


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _version(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _tasks(tx, project_id, plan):
    rows = [_clean(row) for row in tx.query(
        "SELECT FROM records WHERE project_id=:project AND id IN :ids",
        {"project": project_id, "ids": plan["step_ids"]},
    )] if plan["step_ids"] else []
    by_id = {task["id"]: task for task in rows}
    if set(by_id) != set(plan["step_ids"]):
        raise ValueError("Plan references missing tasks")
    return [by_id[task_id] for task_id in plan["step_ids"]]


def _plan_graph(store, tx, plan, tasks):
    node_id = f"plan:{plan['id']}"
    sources = plan.get("source_ids", [])
    store.upsert_node({"id": node_id, "project_id": plan["project_id"], "kind": "plan",
                       "label": plan["title"], "source_id": sources[-1] if sources else None,
                       "metadata": {"plan_id": plan["id"], "version": plan["version"],
                                    "goal": plan["goal"], "constraints": plan["constraints"]}}, tx)
    tx.command("DELETE FROM edges WHERE from_id=:id AND kind IN ['has_step','supported_by']", {"id": node_id})
    for source_id in sources:
        source = _one(tx, "SELECT FROM sources WHERE id=:id", {"id": source_id})
        target = f"source:{source_id}"
        store.upsert_node({"id": target, "project_id": plan["project_id"], "kind": "source",
                           "label": source["uri"], "source_id": source_id,
                           "metadata": {"source_id": source_id, "source_kind": source["kind"]}}, tx)
        store.upsert_edge({"id": _digest([node_id, "supported_by", target]),
                           "project_id": plan["project_id"], "from_id": node_id, "to_id": target,
                           "kind": "supported_by", "source_id": source_id}, tx)
    for task in tasks:
        target = f"record:{task['id']}"
        store.upsert_edge({"id": _digest([node_id, "has_step", target]),
                           "project_id": plan["project_id"], "from_id": node_id, "to_id": target,
                           "kind": "has_step", "source_id": sources[-1] if sources else None}, tx)


def _revision(store, tx, plan, tasks, reason):
    _insert(tx, "plan_revisions", {
        "id": f"{plan['id']}:{plan['version']}", "project_id": plan["project_id"],
        "plan_id": plan["id"], "version": plan["version"], "reason": reason,
        "snapshot": {"plan": plan, "tasks": tasks}, "recorded_at": _now(),
    })
    _plan_graph(store, tx, plan, tasks)


def note_task_mutation(store, tx, task: dict, expected_plan_version: int) -> None:
    """Called inside Store's task transaction; a failed plan CAS rolls it all back."""
    plan_id = task.get("metadata", {}).get("plan_ref")
    if not plan_id:
        return
    plan = _one(tx, "SELECT FROM plans WHERE id=:id AND project_id=:project",
                {"id": plan_id, "project": task["project_id"]}, False)
    _version(expected_plan_version, "expected_plan_version")
    if not plan or plan["version"] != expected_plan_version:
        raise ValueError("Plan version conflict; read the current plan and retry")
    if task["id"] not in plan["step_ids"]:
        raise ValueError("Task is not an active member of its plan")
    plan.update(version=plan["version"] + 1, updated_at=_now(), **(store.current_origin() or {}))
    _set(tx, "plans", plan, "id=:id AND version=:version",
         {"id": plan_id, "version": expected_plan_version})
    _revision(store, tx, plan, _tasks(tx, task["project_id"], plan), "task_progress")


class PlanManager:
    def __init__(self, store):
        self.store = store

    def _worktree(self, tx, project_id, worktree):
        project = _one(tx, "SELECT FROM projects WHERE id=:id", {"id": project_id})
        root = str(Path(worktree if worktree is not None else project["root"]).expanduser().resolve())
        if not tx.query("SELECT id FROM project_roots WHERE project_id=:project AND path=:path",
                        {"project": project_id, "path": root}):
            raise ValueError("Worktree must be a registered root of this project")
        return root

    def get(self, project_id, plan_id):
        with self.store.transaction() as tx:
            self.store._require_project(tx, project_id)
            plan = _one(tx, "SELECT FROM plans WHERE id=:id AND project_id=:project",
                        {"id": plan_id, "project": project_id})
            return {"plan": plan, "tasks": _tasks(tx, project_id, plan)}

    def list(self, project_id):
        self.store._require_project(self.store, project_id)
        return [_clean(row) for row in self.store.query(
            "SELECT FROM plans WHERE project_id=:project ORDER BY created_at,id", {"project": project_id})]

    def activate(self, project_id, plan_id, worktree=None):
        with self.store.transaction() as tx:
            root = self._worktree(tx, project_id, worktree)
            _one(tx, "SELECT id FROM plans WHERE id=:id AND project_id=:project",
                 {"id": plan_id, "project": project_id})
            state_id = _digest([project_id, root])
            old = _one(tx, "SELECT FROM worktree_states WHERE id=:id", {"id": state_id}, False)
            if old and old["active_plan_id"] == plan_id:
                return old
            now = _now()
            state = {"id": state_id, "project_id": project_id, "worktree": root,
                     "active_plan_id": plan_id, "version": old["version"] + 1 if old else 1,
                     "created_at": old["created_at"] if old else now, "updated_at": now}
            if old:
                _set(tx, "worktree_states", state, "id=:id", {"id": state_id})
            else:
                _insert(tx, "worktree_states", state)
            return state

    def save(self, project_id, plan: dict, steps: list[dict], idempotency_key: str,
             expected_version=None, task_versions=None, worktree=None, activate=False):
        return self._save(project_id, plan, steps, idempotency_key, expected_version,
                          task_versions, worktree, activate)

    def _save(self, project_id, plan, steps, idempotency_key, expected_version,
              task_versions, worktree, activate, adoption=None):
        if not isinstance(plan, dict) or not isinstance(steps, list):
            raise ValueError("Plan must be an object and steps a list")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be nonempty text")
        if type(activate) is not bool:
            raise ValueError("activate must be a boolean")
        if task_versions is not None and not isinstance(task_versions, dict):
            raise ValueError("task_versions must map task IDs to versions")
        # Hash the caller's full request, not allocated IDs or resulting timestamps.
        payload = {"project_id": project_id, "plan": plan, "steps": steps,
                   "expected_version": expected_version, "task_versions": task_versions,
                   "worktree": str(worktree) if worktree is not None else None,
                   "activate": activate, "adoption": adoption["group"] if adoption else None}
        payload_hash = _digest(payload)
        request_id = _digest([project_id, idempotency_key])
        with self.store.transaction() as tx:
            self.store._require_project(tx, project_id)
            root = self._worktree(tx, project_id, worktree)
            request = _one(tx, "SELECT FROM plan_requests WHERE id=:id", {"id": request_id}, False)
            if request:
                if request["payload_hash"] != payload_hash:
                    raise ValueError("Idempotency key was already used for a different payload")
                return {**deepcopy(request["result"]), "replayed": True}
            old = None
            if plan.get("id"):
                old = _one(tx, "SELECT FROM plans WHERE id=:id AND project_id=:project",
                           {"id": plan["id"], "project": project_id}, False)
                if not old:
                    raise ValueError("Cannot update an unknown plan")
            if old:
                _version(expected_version, "expected_version")
                if expected_version != old["version"]:
                    raise ValueError("Plan version conflict; read the current plan and retry")
            elif expected_version is not None:
                raise ValueError("A new plan cannot have expected_version")
            title = plan.get("title", old["title"] if old else None)
            goal = plan.get("goal", old["goal"] if old else "")
            constraints = plan.get("constraints", old["constraints"] if old else [])
            if not isinstance(title, str) or not title.strip() or not isinstance(goal, str):
                raise ValueError("Plan requires a nonempty title and a text goal")
            if not isinstance(constraints, list) or not all(isinstance(c, str) and c.strip() for c in constraints):
                raise ValueError("Plan constraints must be nonempty strings")
            plan_id = old["id"] if old else str(uuid4())
            previous = _tasks(tx, project_id, old) if old else []
            existing_by_id = {task["id"]: task for task in previous}
            if adoption:
                existing_by_id.update(adoption["tasks"])
            existing_by_key = {task["metadata"].get("step_key", task["key"]): task
                               for task in existing_by_id.values()}
            supplied_versions = task_versions or {}
            if set(supplied_versions) - set(existing_by_id):
                raise ValueError("task_versions contains unknown task IDs")
            for task_id, task in existing_by_id.items():
                expected = supplied_versions.get(task_id)
                if adoption and task_versions is None:
                    expected = task["version"]
                _version(expected, f"task_versions[{task_id}]")
                if expected != task["version"]:
                    raise ValueError("Task version conflict; read the current tasks and retry")
            definitions, ids, used_ids = {}, {}, set()
            for step in steps:
                if not isinstance(step, dict):
                    raise ValueError("Each plan step must be an object")
                key = step.get("key")
                if not isinstance(key, str) or not key.strip() or key in definitions:
                    raise ValueError("Plan step keys must be unique nonempty strings")
                content, dependencies = step.get("content"), step.get("depends_on", [])
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Step content must be nonempty text")
                if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
                    raise ValueError("Step depends_on must contain local step keys")
                if len(set(dependencies)) != len(dependencies):
                    raise ValueError("Step dependencies must not contain duplicates")
                existing = existing_by_id.get(step["id"]) if step.get("id") else existing_by_key.get(key)
                if step.get("id") and not existing:
                    raise ValueError("Step ID must identify an existing member of this plan")
                task_id = existing["id"] if existing else str(uuid4())
                if task_id in used_ids:
                    raise ValueError("A task may occur only once in a plan")
                used_ids.add(task_id)
                metadata = step.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError("Step metadata must be an object")
                if _MANAGED_FIELDS.intersection(metadata) or "depends_on" in metadata:
                    raise ValueError("Managed step fields and depends_on are assigned by PlanManager")
                for field in _EVIDENCE_FIELDS:
                    if field in metadata and (not existing or metadata[field] != existing["metadata"].get(field)):
                        raise ValueError("Plan definitions cannot inject verification or completion evidence")
                if "state" in metadata and metadata["state"] != (existing["metadata"].get("state", "todo") if existing else "todo"):
                    raise ValueError("Plan definitions preserve progress; new steps must start todo")
                updates = {k: deepcopy(v) for k, v in metadata.items()
                           if k not in _EVIDENCE_FIELDS and k != "state"}
                definitions[key] = {"step": step, "existing": existing, "metadata": updates}
                ids[key] = task_id
            # Validate the entire final graph before touching any record. Ordering of
            # old edges cannot turn a valid reversal into a transient false cycle.
            order, keys = [], list(definitions)
            indices = {key: index for index, key in enumerate(keys)}
            remaining, dependents = {}, {key: [] for key in keys}
            for key, definition in definitions.items():
                dependencies = definition["step"].get("depends_on", [])
                remaining[key] = len(dependencies)
                for dependency in dependencies:
                    if dependency not in definitions:
                        raise ValueError("Plan dependency must identify a step in the final plan")
                    dependents[dependency].append(key)
            ready = [indices[key] for key in keys if not remaining[key]]
            heapq.heapify(ready)
            while ready:
                key = keys[heapq.heappop(ready)]
                order.append(key)
                for dependent in dependents[key]:
                    remaining[dependent] -= 1
                    if not remaining[dependent]:
                        heapq.heappush(ready, indices[dependent])
            if len(order) != len(definitions):
                raise ValueError("Plan dependencies must not contain a cycle")
            removed = {task["id"]: task for task in previous if task["id"] not in used_ids}
            if removed:
                for row in tx.query("SELECT FROM records WHERE project_id=:project AND kind='task' AND status!='superseded'",
                                    {"project": project_id}):
                    if row["id"] not in existing_by_id and set(row["metadata"].get("depends_on", [])).intersection(removed):
                        raise ValueError("Cannot archive a step still required by an active task outside this plan")
            source = self.store.snapshot(project_id, f"plan://{plan_id}", _json(payload),
                                         "plan", {"origin": "plan_request", "plan_id": plan_id,
                                                  "idempotency_key": idempotency_key})
            context_hash = _digest({"title": title, "goal": goal, "constraints": constraints})
            # Final keys are unique, but a valid simultaneous key swap must not
            # collide with the old ownership while records are being rewritten.
            for task in existing_by_id.values():
                fields = {"active_key": None}
                if "worktree" in task["metadata"]:
                    fields["metadata"] = {key: value for key, value in task["metadata"].items()
                                          if key != "worktree"}
                _set(tx, "records", fields, "id=:id", {"id": task["id"]})
            saved, changed_steps = {}, False
            for key in order:
                definition = definitions[key]
                step, existing = definition["step"], definition["existing"]
                metadata = {**definition["metadata"], "plan_ref": plan_id, "step_key": key,
                            "step_index": indices[key], "depends_on": [ids[d] for d in step.get("depends_on", [])],
                            "plan_context_hash": context_hash, "plan_archived": False}
                if not existing:
                    metadata["state"] = "todo"
                elif adoption:
                    # Adoption retains old *reported* evidence. It creates no
                    # check_run and never upgrades those reports to verification.
                    for field in ("acceptance_passed", "acceptance_source_ids", "test_passed",
                                  "test_source_ids", "review_passed", "review_source_ids"):
                        if field in existing["metadata"]:
                            metadata[field] = deepcopy(existing["metadata"][field])
                record = {"id": ids[key], "kind": "task", "key": f"plan:{plan_id}:{key}",
                          "content": step["content"], "metadata": metadata}
                if existing:
                    record["expected_version"] = existing["version"]
                sources = list(dict.fromkeys([source["id"]] + (existing["source_ids"] if existing else [])))
                # Only a step this save defines anew carries the writer's mark; an unchanged step keeps its own.
                # The plan-level context hash is the plan row's, which is marked below.
                changed = not existing or bool(adoption) or record["key"] != existing["key"] or (
                    step["content"] != existing["content"] or any(
                        existing["metadata"].get(name) != value for name, value in metadata.items()
                        if name not in ("plan_context_hash", "plan_archived", "state")))
                changed_steps |= changed
                with nullcontext() if changed else self.store.write_origin(None):
                    saved[key] = self.store._remember(tx, project_id, record, sources, managed_batch=True)
            for task_id, task in removed.items():
                # Archiving a step changes nothing it says; its own mark, or none, stays.
                with self.store.write_origin(None):
                    self.store._remember(tx, project_id, {"id": task_id, "expected_version": task["version"],
                        "status": "superseded", "metadata": {"plan_archived": True}},
                        list(dict.fromkeys(task["source_ids"] + [source["id"]])), managed_batch=True)
            tasks = [saved[key] for key in definitions]
            # All vertices now exist; rebuild batch dependency edges as a final pass.
            for task in tasks:
                self.store._record_graph(tx, task)
            now = _now()
            result_plan = {"id": plan_id, "project_id": project_id, "version": old["version"] + 1 if old else 1,
                           "title": title, "goal": goal, "constraints": constraints,
                           "step_ids": [task["id"] for task in tasks], "context_hash": context_hash,
                           "source_ids": list(dict.fromkeys((old.get("source_ids", []) if old else []) + [source["id"]])),
                           "created_at": old["created_at"] if old else now, "updated_at": now}
            # Like a record, a plan an external session ever changed keeps that mark; a native plan has none.
            plan_changed = (not old or changed_steps or bool(removed)
                            or (title, goal, constraints, result_plan["step_ids"])
                            != (old["title"], old["goal"], old["constraints"], old["step_ids"]))
            kept = {key: old[key] for key in ORIGIN_METADATA if old and key in old}
            result_plan.update((plan_changed and self.store.current_origin()) or kept)
            if old:
                _set(tx, "plans", result_plan, "id=:id AND version=:version", {"id": plan_id, "version": expected_version})
            else:
                _insert(tx, "plans", result_plan)
            _revision(self.store, tx, result_plan, tasks, "legacy_adoption" if adoption else "save")
            if activate:
                self.activate(project_id, plan_id, root)
            result = {"plan": result_plan, "tasks": tasks, "replayed": False}
            _insert(tx, "plan_requests", {"id": request_id, "project_id": project_id, "key": idempotency_key,
                                          "payload_hash": payload_hash, "result": result, "created_at": now})
            return result

    def adopt_legacy(self, project_id, group: str, *, plan=None, idempotency_key,
                     task_versions=None, worktree=None, activate=False):
        """Explicitly turn an old arbitrary plan_id group into a managed plan."""
        if not isinstance(group, str) or not group.strip():
            raise ValueError("Legacy group must be nonempty text")
        with self.store.transaction() as tx:
            self.store._require_project(tx, project_id)
            tasks = [_clean(row) for row in tx.query(
                "SELECT FROM records WHERE project_id=:project AND kind='task' AND status!='superseded'",
                {"project": project_id}) if row["metadata"].get("plan_id") == group]
            if not tasks or any(task["metadata"].get("plan_ref") for task in tasks):
                # A repeated adoption request must still replay after membership
                # changed, rather than fail before its idempotency record is read.
                request = _one(tx, "SELECT FROM plan_requests WHERE id=:id",
                               {"id": _digest([project_id, idempotency_key])}, False)
                if request and request.get("result"):
                    original = _one(tx, "SELECT FROM sources WHERE id=:id",
                                    {"id": request["result"]["plan"]["source_ids"][-1]})
                    payload = json.loads(original["content"])
                    expected_plan = plan or {"title": group}
                    if payload.get("adoption") == group and payload["plan"] == expected_plan and payload["task_versions"] == task_versions and payload["worktree"] == (str(worktree) if worktree is not None else None) and payload["activate"] == activate:
                        return {**deepcopy(request["result"]), "replayed": True}
                raise ValueError("Legacy group is empty or already managed")
            tasks.sort(key=lambda task: (task["metadata"].get("step_index", 2**63), task["created_at"], task["id"]))
            by_id = {task["id"]: task for task in tasks}
            keys = {task["id"]: task["key"] for task in tasks}
            steps = []
            for task in tasks:
                dependencies = task["metadata"].get("depends_on", [])
                if any(dependency not in by_id for dependency in dependencies):
                    raise ValueError("Legacy adoption requires all dependency tasks in the same group")
                metadata = {k: deepcopy(v) for k, v in task["metadata"].items()
                            if k not in _MANAGED_FIELDS | _EVIDENCE_FIELDS | {"state", "depends_on"}}
                steps.append({"id": task["id"], "key": keys[task["id"]], "content": task["content"],
                              "depends_on": [keys[d] for d in dependencies], "metadata": metadata})
            return self._save(project_id, plan or {"title": group}, steps, idempotency_key, None,
                              task_versions, worktree, activate, {"group": group, "tasks": by_id})

    def next_task(self, project_id, plan_id=None, worktree=None):
        with self.store.transaction() as tx:
            root = self._worktree(tx, project_id, worktree)
            if plan_id is None:
                state = _one(tx, "SELECT FROM worktree_states WHERE id=:id",
                             {"id": _digest([project_id, root])}, False)
                if not state:
                    return {"plan": None, "task": None, "reason": "no_active_plan", "blocked": []}
                plan_id = state["active_plan_id"]
            current = self.get(project_id, plan_id)
            tasks, blocked = current["tasks"], []
            by_id = {task["id"]: task for task in tasks}
            available = []
            for task in tasks:
                metadata = task["metadata"]
                if task["status"] == "superseded" or metadata.get("plan_archived") or metadata.get("state", "todo") == "done":
                    continue
                unfinished = [dependency for dependency in metadata.get("depends_on", [])
                              if dependency not in by_id or by_id[dependency]["metadata"].get("state", "todo") != "done"]
                reasons = (["explicitly_blocked"] if metadata.get("state") == "blocked" else [])
                if unfinished:
                    reasons.append("unfinished_dependencies")
                if reasons:
                    blocked.append({"task_id": task["id"], "reasons": reasons, "dependency_ids": unfinished})
                    continue
                available.append(task)
            if available:
                task = available[0]
                metadata = task["metadata"]
                state = metadata.get("state", "todo")
                return {"plan": current["plan"], "task": task,
                        "reason": state if state in {"in_progress", "needs_test", "needs_review"} else "ready", "blocked": blocked}
            return {"plan": current["plan"], "task": None,
                    "reason": "blocked" if blocked else "completed", "blocked": blocked}
