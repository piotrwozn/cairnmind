"""Read-only memory projections and explicit, versioned local-user reviews.

Graph nodes are a view over archived evidence and logical entities. Rendering a
message or a historical revision does not promote it to confirmed knowledge.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .agent import source_text
from .memory_paths import bound_metadata, memory_bindings
from .retrieval import Retriever, public_record, public_source_metadata
from .store import _clean, _json, _now, _one


TABLES = {"records", "sources", "plans", "check_runs", "jobs"}
NODE_LIMIT = 2000
EDGE_LIMIT = 5000


class InspectorNotFound(ValueError):
    pass


def _cursor(reader, store):
    from .journal import Journal

    return Journal(store).cursor(reader=reader)


def _limit(value, maximum=500):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Limit must be between 1 and {maximum}")
    return value


def _encode_cursor(value):
    return base64.urlsafe_b64encode(_json(value).encode()).decode().rstrip("=")


def _decode_cursor(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Invalid inspector page cursor")
    try:
        result = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid inspector page cursor") from exc
    if not isinstance(result, dict) or not isinstance(result.get("after"), str):
        raise ValueError("Invalid inspector page cursor")
    return result


class Inspector:
    def __init__(self, store):
        self.store = store

    def _project(self, project):
        try:
            return self.store.resolve_project(project)
        except ValueError as exc:
            raise InspectorNotFound("Unknown inspector project") from exc

    def _scope(self, project, worktree=None):
        project = self._project(project)
        roots = [Path(row["path"]).resolve() for row in self.store.query(
            "SELECT path FROM project_roots WHERE project_id=:project", {"project": project["id"]})]
        root = Path(worktree).expanduser().resolve() if worktree else None
        if root is not None and root not in roots:
            raise ValueError("Inspector worktree must be a registered project root")
        return project, root, roots, memory_bindings(self.store, project["id"])

    def _matches(self, scope, row, *, source=False):
        project, root, roots, bindings = scope
        if root is None:
            return True
        metadata = row.get("metadata") or {}
        return Retriever._matches_scope(bound_metadata(self.store, project["id"], metadata, bindings=bindings),
            root, roots, file_path=source and row.get("kind") in {"code", "document", "file"})

    @staticmethod
    def _header(row):
        return {key: row.get(key) for key in ("id", "project_id", "uri", "kind", "sha256", "created_at", "is_current")}

    def page(self, collection, project, *, worktree=None, kind=None, status=None, state=None, cursor=None, limit=100):
        if collection == "tasks":
            collection, kind = "records", "task"
        if collection == "checks":
            collection = "check_runs"
        if collection not in TABLES:
            raise ValueError("Unknown inspector collection")
        _limit(limit)
        scope = self._scope(project, worktree)
        pid = scope[0]["id"]
        query_key = hashlib.sha256(_json([collection, pid, str(scope[1]), kind, status, state]).encode()).hexdigest()
        after = None
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded.get("query") != query_key:
                raise ValueError("Inspector page cursor belongs to another query")
            after = decoded["after"]
        conditions, params = ["project_id=:project"], {"project": pid}
        if kind:
            conditions.append("kind=:kind")
            params["kind"] = kind
        if status:
            field = "state" if collection == "jobs" else "outcome" if collection == "check_runs" else "status"
            conditions.append(f"{field}=:status")
            params["status"] = status
        if state:
            if collection != "records" or kind != "task":
                raise ValueError("Task state filter requires the task collection")
            conditions.append("metadata.state=:task_state")
            params["task_state"] = state
        where = " AND ".join(conditions)
        with self.store.transaction() as reader:
            # Fetch only identity/scope fields for worktree filtering. Source
            # bodies and arbitrary metadata never enter list projections.
            scope_fields = "metadata.worktree AS worktree" if collection != "check_runs" else "worktree"
            rows = reader.query(f"SELECT id,kind,metadata.root AS root,{scope_fields},"
                f"metadata.path AS path FROM {collection} WHERE {where} ORDER BY id", params)
            ids = [row["id"] for row in rows if self._matches(scope, {"kind": row.get("kind"),
                "metadata": {key: row.get(key) for key in ("root", "worktree", "path")}}, source=collection == "sources")]
            available = [identifier for identifier in ids if after is None or identifier > after]
            selected = available[:limit]
            fields = "*"
            if collection == "sources":
                fields = "id,project_id,uri,kind,sha256,created_at,is_current,metadata.role AS role,metadata.client AS client,metadata.session_id AS session_id,metadata.source_timestamp AS source_timestamp,metadata.byte_offset AS byte_offset"
            rows = reader.query(f"SELECT {fields} FROM {collection} WHERE project_id=:project AND id IN :ids ORDER BY id",
                                {"project": pid, "ids": selected}) if selected else []
            items = []
            for row in rows:
                row = _clean(row)
                if collection == "records":
                    item = public_record(row, content_limit=1000, metadata_budget=2000)
                    item["project_id"] = pid
                elif collection == "sources":
                    item = row
                elif collection == "jobs":
                    item = self.store._public_job(row)
                elif collection == "check_runs":
                    item = {key: row.get(key) for key in ("id", "project_id", "task_id", "profile_id", "worktree",
                        "outcome", "passed", "returncode", "started_at", "ended_at", "origin", "source_ids")}
                else:
                    item = {key: row.get(key) for key in ("id", "project_id", "version", "title", "goal", "step_ids",
                                                        "created_at", "updated_at")}
                items.append(item)
            result = {"items": items, "total": len(ids), "next_cursor": _encode_cursor({"query": query_key,
                "after": selected[-1]}) if len(available) > limit else None, "cursor": _cursor(reader, self.store)}
        if collection == "records":
            # Derived outside the writer lock, like the detail view; the list needs no file bodies.
            from .evidence import evidence_states
            states = evidence_states(self.store, pid, rows, quotes=False)
            for item in items:
                item["evidence_state"] = states[item["id"]]["evidence_state"]
        return result

    def entity(self, collection, project, identifier, *, worktree=None):
        if collection not in {"plans", "jobs", "check_runs"}:
            raise ValueError("Unknown inspector entity collection")
        scope = self._scope(project, worktree)
        with self.store.transaction() as reader:
            row = _one(reader, f"SELECT FROM {collection} WHERE project_id=:project AND id=:id",
                       {"project": scope[0]["id"], "id": identifier}, False)
            metadata = row.get("metadata", {}) if row else {}
            if row and collection == "check_runs":
                metadata = {**metadata, "worktree": row.get("worktree")}
            if not row or not self._matches(scope, {"metadata": metadata}):
                raise InspectorNotFound("Unknown entity in requested scope")
            if collection == "jobs":
                row = self.store._public_job(row, detail=True)
            return {"item": row, "cursor": _cursor(reader, self.store)}

    def plan_revisions(self, project, plan_id, *, cursor=None, limit=100):
        _limit(limit)
        plan = self.entity("plans", project, plan_id)["item"]
        after = 0
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded.get("plan") != plan_id or decoded.get("project") != plan["project_id"] or not decoded["after"].isdigit():
                raise ValueError("Plan history cursor belongs to another plan")
            after = int(decoded["after"])
        with self.store.transaction() as reader:
            params = {"project": plan["project_id"], "id": plan_id, "offset_version": after, "limit": limit + 1}
            total = reader.query("SELECT count(*) AS n FROM plan_revisions WHERE project_id=:project AND plan_id=:id", params)[0]["n"]
            rows = reader.query("SELECT FROM plan_revisions WHERE project_id=:project AND plan_id=:id "
                                "AND version>:offset_version ORDER BY version LIMIT :limit", params)
            return {"items": [_clean(row) for row in rows[:limit]], "total": total,
                "next_cursor": _encode_cursor({"plan": plan_id, "project": plan["project_id"], "after": str(rows[limit - 1]["version"])})
                if len(rows) > limit else None, "cursor": _cursor(reader, self.store)}

    def record(self, project, record_id, worktree=None):
        scope = self._scope(project, worktree)
        with self.store.transaction() as reader:
            row = _one(reader, "SELECT FROM records WHERE id=:id AND project_id=:project",
                       {"id": record_id, "project": scope[0]["id"]}, False)
            if not row or not self._matches(scope, row):
                raise InspectorNotFound("Unknown record in requested scope")
            result = {"record": row, "cursor": _cursor(reader, self.store)}
        # Derived on every read: a source can change without any write to the record.
        from .evidence import evidence_states
        result["evidence"] = evidence_states(self.store, scope[0]["id"], [row])[row["id"]]
        if row["kind"] == "task":
            # A read projection, not the /checks recovery operation. Filesystem
            # capture never holds the shared database writer lock.
            from .verification import verification_status

            result["verification"] = verification_status(self.store, row, scope[1])
            result["verification_observed_at"] = _now()
        return result

    def revisions(self, project, record_id, *, cursor=None, limit=100, worktree=None):
        _limit(limit)
        current = self.record(project, record_id, worktree)
        pid = current["record"]["project_id"]
        after = 0
        if cursor:
            value = _decode_cursor(cursor)
            if value.get("record") != record_id or value.get("project") != pid or not value["after"].isdigit():
                raise ValueError("Inspector revision cursor belongs to another record")
            after = int(value["after"])
        with self.store.transaction() as reader:
            total = reader.query("SELECT count(*) AS n FROM record_revisions WHERE project_id=:project AND record_id=:id",
                                 {"project": pid, "id": record_id})[0]["n"]
            rows = reader.query("SELECT FROM record_revisions WHERE project_id=:project AND record_id=:id "
                "AND version>:offset_version ORDER BY version LIMIT :limit",
                {"project": pid, "id": record_id, "offset_version": after, "limit": limit + 1})
            return {"items": [_clean(row) for row in rows[:limit]], "total": total,
                "next_cursor": _encode_cursor({"record": record_id, "project": pid, "after": str(rows[limit - 1]["version"])})
                if len(rows) > limit else None, "cursor": _cursor(reader, self.store)}

    def source(self, project, source_id, *, view="raw", offset=0, limit=65536, worktree=None):
        if view not in {"raw", "normalized", "metadata"}:
            raise ValueError("Source view must be raw, normalized or metadata")
        _limit(limit, 262144)
        if type(offset) is not int or offset < 0:
            raise ValueError("Source offset must be nonnegative")
        scope = self._scope(project, worktree)
        with self.store.transaction() as reader:
            row = _one(reader, "SELECT FROM sources WHERE id=:id AND project_id=:project",
                       {"id": source_id, "project": scope[0]["id"]}, False)
            if not row or not self._matches(scope, row, source=True):
                raise InspectorNotFound("Unknown source in requested scope")
            text = row["content"] if view == "raw" else source_text(row) if view == "normalized" else _json(row["metadata"])
            end = min(len(text), offset + limit)
            return {"source": {**self._header(row), "metadata": public_source_metadata(row.get("metadata"))},
                "view": view, "text": text[offset:end], "offset": offset, "next_offset": end if end < len(text) else None,
                "total_chars": len(text), "truncated": end < len(text), "cursor": _cursor(reader, self.store)}

    def inbox(self, *, cursor=None, limit=100):
        _limit(limit)
        after = ""
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded.get("query") != "global-inbox":
                raise ValueError("Inbox cursor belongs to another collection")
            after = decoded["after"]
        with self.store.transaction() as reader:
            total = int(reader.query("SELECT count(*) AS n FROM ingestion_inbox")[0]["n"])
            rows = reader.query("SELECT id,path,source_offset,sha256,client,cwd,reason,created_at "
                "FROM ingestion_inbox WHERE id>:offset_id ORDER BY id LIMIT :limit",
                {"offset_id": after, "limit": limit + 1})
            return {"items": [{**_clean(row), "project_id": None, "entity_type": "inbox"} for row in rows[:limit]],
                "total": total, "next_cursor": _encode_cursor({"query": "global-inbox", "after": rows[limit - 1]["id"]})
                if len(rows) > limit else None, "cursor": _cursor(reader, self.store)}

    def inbox_source(self, identifier, *, view="raw", offset=0, limit=65536):
        if view not in {"raw", "metadata"}:
            raise ValueError("Inbox view must be raw or metadata")
        _limit(limit, 262144)
        if type(offset) is not int or offset < 0:
            raise ValueError("Source offset must be nonnegative")
        with self.store.transaction() as reader:
            row = _one(reader, "SELECT FROM ingestion_inbox WHERE id=:id", {"id": identifier}, False)
            if not row:
                raise InspectorNotFound("Unknown unassigned source")
            metadata = {key: value for key, value in row.items() if key not in {"raw", "id"}}
            text = str(row.get("raw", "")) if view == "raw" else _json(metadata)
            end = min(len(text), offset + limit)
            return {"source": {"id": identifier, "project_id": None, "kind": "unassigned",
                "uri": row.get("path"), "sha256": row.get("sha256"), "created_at": row.get("created_at"),
                "metadata": {key: row.get(key) for key in ("client", "cwd", "reason", "source_offset")}},
                "view": view, "text": text[offset:end], "offset": offset,
                "next_offset": end if end < len(text) else None, "total_chars": len(text),
                "truncated": end < len(text), "cursor": _cursor(reader, self.store)}

    def summary(self, project=None):
        with self.store.transaction() as reader:
            projects = [self._project(project)] if project else self.store.list_projects()
            items, totals = [], Counter()
            for row in projects:
                counts = {}
                for table in ("sources", "records", "record_revisions", "plans", "plan_revisions", "nodes", "edges", "jobs", "check_runs"):
                    counts[table] = int(reader.query(f"SELECT count(*) AS n FROM {table} WHERE project_id=:project",
                                                    {"project": row["id"]})[0]["n"])
                items.append({"project": row, "counts": counts})
                totals.update(counts)
            if project is None:
                totals["inbox"] = int(reader.query("SELECT count(*) AS n FROM ingestion_inbox")[0]["n"])
            return {"projects": items, "totals": dict(totals), "cursor": _cursor(reader, self.store)}

    def review(self, project, record_id, action, expected_version, *, expected_plan_version=None, reason=None):
        if action not in {"confirm", "dispute"}:
            raise ValueError("Review action must be confirm or dispute")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Review requires the exact record version")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 4000):
            raise ValueError("Review reason must be at most 4000 characters")
        if action == "dispute" and (not reason or not reason.strip()):
            raise ValueError("Disputing a record requires a reason")
        pid = self._project(project)["id"]
        status = "confirmed" if action == "confirm" else "disputed"
        with self.store.transaction() as reader:
            old = _one(reader, "SELECT FROM records WHERE id=:id AND project_id=:project", {"id": record_id, "project": pid}, False)
            if not old:
                raise InspectorNotFound("Unknown record in requested project")
            if old["version"] != expected_version or old["status"] in {"superseded", status}:
                raise ValueError("Review version conflict; read the current record")
            plan_id = old.get("metadata", {}).get("plan_ref") if old["kind"] == "task" else None
            if plan_id:
                current = _one(reader, "SELECT version FROM plans WHERE id=:id AND project_id=:project", {"id": plan_id, "project": pid})
                if type(expected_plan_version) is not int or current["version"] != expected_plan_version:
                    raise ValueError("Review plan version conflict; read the current plan")
            audit = {"actor": "local_user", "action": action, "record_id": record_id, "previous_version": expected_version,
                "previous_status": old["status"], "status": status, "reason": (reason or "").strip(), "at": _now(),
                "content_sha256": hashlib.sha256(old["content"].encode()).hexdigest()}
            source = self.store.snapshot(pid, "review://" + str(uuid4()), _json(audit), "manual",
                                         {"origin": "local_user_review", "role": "user", "record_id": record_id})
            update = {"id": record_id, "expected_version": expected_version, "status": status}
            if plan_id:
                update["expected_plan_version"] = expected_plan_version
            saved = self.store.remember(pid, update, [*old["source_ids"], source["id"]])
            return {"record": saved, "audit_source_id": source["id"], "cursor": _cursor(reader, self.store)}

    def _graph_rows(self, project, worktree=None):
        scope = self._scope(project, worktree)
        pid = scope[0]["id"]
        result = {}
        fields = {
            "sources": "id,kind,uri,is_current,created_at,metadata.root AS root,metadata.worktree AS worktree,metadata.path AS path,metadata.role AS role,metadata.client AS client,metadata.session_id AS session_id,metadata.source_timestamp AS source_timestamp,metadata.byte_offset AS byte_offset",
            "records": "id,kind,status,version,metadata.root AS root,metadata.worktree AS worktree,metadata.path AS path",
            "plans": "id,title,version",
            "record_revisions": "id,record_id,version,snapshot.metadata.root AS root,snapshot.metadata.worktree AS worktree",
            "plan_revisions": "id,plan_id,version",
            "nodes": "id,kind,label,source_id,is_current,metadata.root AS root,metadata.worktree AS worktree,metadata.path AS path",
            "edges": "id,from_id,to_id,kind,source_id,is_current,status",
        }
        for table, selected in fields.items():
            rows = self.store.query(f"SELECT {selected} FROM {table} WHERE project_id=:project ORDER BY id", {"project": pid})
            result[table] = [row for row in rows if self._matches(scope, {"kind": row.get("kind"),
                "metadata": {key: row.get(key) for key in ("root", "worktree", "path")}}, source=table == "sources")]
        if scope[1] is not None:
            source_ids = {row["id"] for row in result["sources"]}
            record_ids = {row["id"] for row in result["records"]}
            result["nodes"] = [row for row in result["nodes"] if not row.get("source_id") or row["source_id"] in source_ids]
            result["edges"] = [row for row in result["edges"] if not row.get("source_id") or row["source_id"] in source_ids]
            result["record_revisions"] = [row for row in result["record_revisions"] if row["record_id"] in record_ids]
        return scope, result

    def graph(self, project=None, *, worktree=None, group=None, limit=NODE_LIMIT, page_cursor=None, _full=False):
        _limit(limit, NODE_LIMIT)
        with self.store.transaction() as reader:
            watermark = _cursor(reader, self.store)
            if project is None:
                summary = self.summary()
                nodes = [{"id": "project:" + row["project"]["id"], "kind": "project", "label": row["project"]["name"],
                          "project_id": row["project"]["id"], "count": row["counts"]["sources"] + row["counts"]["records"],
                          "expandable": True, "counts": row["counts"]} for row in summary["projects"]]
                inbox_group = {"id": "global:inbox", "kind": "group", "entity_type": "inbox_group",
                    "group": "inbox", "label": "Nieprzypisane", "project_id": None,
                    "count": summary["totals"].get("inbox", 0), "expandable": True}
                if group in {"inbox", "global:inbox"}:
                    page = self.inbox(cursor=page_cursor, limit=min(500, max(1, limit - 1)))
                    children = [{"id": "inbox:" + row["id"], "kind": "unassigned", "entity_type": "inbox",
                        "inbox_id": row["id"], "project_id": None, "label": str(row.get("path", row["id"]))[:200],
                        "client": row.get("client"), "reason": row.get("reason")} for row in page["items"]]
                    included_group = [inbox_group] if limit > 1 else []
                    edges = [{"id": "contains:" + row["id"], "from_id": "global:inbox", "to_id": row["id"],
                              "kind": "contains", "structural": True, "project_id": None} for row in children] if included_group else []
                    return {"nodes": [*included_group, *children], "edges": edges, "groups": [inbox_group],
                        "counts": {"inbox": page["total"]}, "truncated": bool(page["next_cursor"]),
                        "next_cursor": page["next_cursor"], "cursor": watermark, "node_budget": limit, "edge_budget": EDGE_LIMIT}
                if inbox_group["count"]:
                    nodes.append(inbox_group)
                return {"nodes": nodes[:limit], "edges": [], "groups": nodes, "counts": summary["totals"],
                        "truncated": len(nodes) > limit, "cursor": watermark, "node_budget": limit, "edge_budget": EDGE_LIMIT}
            scope, rows = self._graph_rows(project, worktree)
            pid = scope[0]["id"]
            nodes, edges, buckets = {}, {}, {}

            def node(identifier, kind, label, bucket, **extra):
                nodes[identifier] = {"id": identifier, "project_id": pid, "kind": kind, "label": str(label)[:200], **extra}
                buckets[identifier] = bucket

            def edge(identifier, first, second, kind, **extra):
                edges[identifier] = {"id": identifier, "project_id": pid, "from_id": first, "to_id": second,
                                     "kind": kind, **extra}

            for row in rows["nodes"]:
                node(row["id"], row["kind"], row.get("label", row["id"]), "native:" + row["kind"],
                     source_id=row.get("source_id"), historical=not row.get("is_current", True), entity_type="node")
            for row in rows["sources"]:
                message = row["kind"] in {"transcript", "transcript_unknown"}
                role = row.get("role", "unknown")
                bucket = ("session:" + str(row.get("client") or "unknown") + ":" + str(row.get("session_id") or "unknown")) if message else "sources:" + row["kind"]
                node("source:" + row["id"], "message" if message and role in {"user", "assistant", "tool"} else "technical" if message else "source",
                     f"{role}: {row['uri']}" if message else row["uri"], bucket, source_id=row["id"],
                     role=role, client=row.get("client"), session_id=row.get("session_id"), historical=not row.get("is_current", True), entity_type="source")
                nodes["source:" + row["id"]].update(timestamp=row.get("source_timestamp") or row.get("created_at"),
                                                      byte_offset=row.get("byte_offset"))
            for row in rows["records"]:
                node("record:" + row["id"], row["kind"], f"{row['kind']} · v{row['version']}", "records:" + row["kind"],
                     record_id=row["id"], version=row["version"], status=row["status"], historical=row["status"] == "superseded", entity_type="record")
            for row in rows["plans"]:
                node("plan:" + row["id"], "plan", row["title"], "plans", plan_id=row["id"], version=row["version"], entity_type="plan")
            for table, owner, prefix in (("record_revisions", "record_id", "record"), ("plan_revisions", "plan_id", "plan")):
                for row in rows[table]:
                    identifier = f"revision:{prefix}:{row['id']}"
                    node(identifier, "revision", f"{prefix} v{row['version']}", "history:" + prefix,
                         entity_type="revision", owner_type=prefix, owner_id=row[owner], revision_id=row["id"], version=row["version"], historical=True)
                    edge("history:" + identifier, identifier, prefix + ":" + row[owner], "version_of", structural=True)
            for row in rows["edges"]:
                edge(row["id"], row["from_id"], row["to_id"], row["kind"], source_id=row.get("source_id"),
                     status=row.get("status"), historical=not row.get("is_current", True))
            edges = {identifier: item for identifier, item in edges.items() if item["from_id"] in nodes and item["to_id"] in nodes}
            counts = {table: len(items) for table, items in rows.items()}
            counts.update(display_nodes=len(nodes), display_edges=len(edges))
            bucket_counts = Counter(buckets.values())
            groups = [{"id": "group:" + key, "group": key, "kind": "group", "label": key,
                       "project_id": pid, "count": count, "expandable": True} for key, count in sorted(bucket_counts.items())]
            structural = [{"id": "contains:" + identifier, "project_id": pid, "from_id": "group:" + buckets[identifier],
                           "to_id": identifier, "kind": "contains", "structural": True} for identifier in nodes]
            if _full:
                return {"nodes": [*groups, *nodes.values()], "edges": [*edges.values(), *structural],
                        "groups": groups, "counts": counts, "cursor": watermark}
            selected = sorted(identifier for identifier in nodes if group is None or buckets[identifier] == group.removeprefix("group:"))
            query_key = hashlib.sha256(_json([pid, str(scope[1]), group]).encode()).hexdigest()
            if page_cursor:
                decoded = _decode_cursor(page_cursor)
                if decoded.get("query") != query_key:
                    raise ValueError("Graph page cursor belongs to another scope")
                selected = [identifier for identifier in selected if identifier > decoded["after"]]
            remaining = len(selected)
            visible_groups = [] if group else groups[:max(1, min(500, limit // 4))]
            available = max(0, limit - len(visible_groups))
            selected = selected[:available]
            selected_ids = set(selected)
            visible_edges = [item for item in edges.values() if item["from_id"] in selected_ids and item["to_id"] in selected_ids]
            if not group:
                visible_edges += [item for item in structural if item["to_id"] in selected_ids]
            return {"nodes": [*visible_groups[:limit], *[nodes[identifier] for identifier in selected]],
                "edges": [item for item in visible_edges if not item.get("structural") or
                          item["from_id"] in {row["id"] for row in visible_groups}][:EDGE_LIMIT],
                "groups": groups[:500], "groups_total": len(groups), "counts": counts,
                "truncated": len(selected_ids) < remaining
                             or len(visible_edges) > EDGE_LIMIT or len(visible_groups) > limit,
                "next_cursor": _encode_cursor({"query": query_key, "after": selected[-1]}) if selected and remaining > len(selected) else None,
                "cursor": watermark, "node_budget": limit, "edge_budget": EDGE_LIMIT}

    def groups(self, project, *, worktree=None, cursor=None, limit=100):
        _limit(limit)
        projection = self.graph(project, worktree=worktree, _full=True)
        rows = projection["groups"]
        query_key = hashlib.sha256(_json([project, str(worktree), "groups"]).encode()).hexdigest()
        total = len(rows)
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded.get("query") != query_key:
                raise ValueError("Group cursor belongs to another scope")
            rows = [row for row in rows if row["id"] > decoded["after"]]
        return {"items": rows[:limit], "total": total, "next_cursor": _encode_cursor({"query": query_key,
                "after": rows[limit - 1]["id"]}) if len(rows) > limit else None, "cursor": projection["cursor"]}

    def neighbors(self, project, node_id, *, worktree=None, limit=100, cursor=None):
        _limit(limit, 500)
        projection = self.graph(project, worktree=worktree, _full=True)
        nodes = {row["id"]: row for row in projection["nodes"]}
        if node_id not in nodes:
            raise InspectorNotFound("Unknown graph node in requested scope")
        query_key = hashlib.sha256(_json([project, str(worktree), node_id]).encode()).hexdigest()
        rows = sorted((row for row in projection["edges"] if node_id in (row["from_id"], row["to_id"])), key=lambda row: row["id"])
        total = len(rows)
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded.get("query") != query_key:
                raise ValueError("Neighbor cursor belongs to another node")
            rows = [row for row in rows if row["id"] > decoded["after"]]
        selected = rows[:limit]
        ids = {node_id, *[row[key] for row in selected for key in ("from_id", "to_id")]}
        return {"nodes": [nodes[identifier] for identifier in sorted(ids)], "edges": selected, "total": total,
            "truncated": len(rows) > limit, "next_cursor": _encode_cursor({"query": query_key, "after": selected[-1]["id"]})
            if len(rows) > limit else None, "cursor": projection["cursor"]}
