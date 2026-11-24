"""A page of the inspector's graph is what the whole read gave: field by field, in order, on every page (#450).

WholeRead is the inspector's graph, groups and neighbors as they were before #450 (they read every row of the project
and cut the page out of it), kept here as the reference the paged reads are compared with.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json

import pytest

from cairnmind.code_graph import CODE_GRAPH_NOT_BUILT, code_graph_pending
from cairnmind.inspector import (EDGE_LIMIT, NODE_LIMIT, Inspector, InspectorNotFound, _cursor, _decode_cursor,
                                 _encode_cursor, _limit)
from cairnmind.plans import PlanManager
from cairnmind.sql import json_text


class WholeRead(Inspector):
    def _graph_rows(self, project, worktree=None):
        scope = self._scope(project, worktree)
        pid = scope[0]["id"]
        result = {}
        fields = {
            "sources": "id,kind,uri,is_current,created_at,metadata.root AS root,metadata.worktree AS worktree,metadata.path AS path,metadata.role AS role,metadata.client AS client,metadata.session_id AS session_id,metadata.origin_client AS origin_client,metadata.origin_evidence AS origin_evidence,metadata.source_timestamp AS source_timestamp,metadata.byte_offset AS byte_offset",
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
            warnings = [CODE_GRAPH_NOT_BUILT] if code_graph_pending(scope[0]) else []  # a graph being written is partial (#377)
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
                        "groups": groups, "counts": counts, "warnings": warnings, "cursor": watermark}
            selected = sorted(identifier for identifier in nodes if group is None or buckets[identifier] == group.removeprefix("group:"))
            query_key = hashlib.sha256(json_text([pid, str(scope[1]), group]).encode()).hexdigest()
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
                "groups": groups[:500], "groups_total": len(groups), "counts": counts, "warnings": warnings,
                "truncated": len(selected_ids) < remaining
                             or len(visible_edges) > EDGE_LIMIT or len(visible_groups) > limit,
                "next_cursor": _encode_cursor({"query": query_key, "after": selected[-1]}) if selected and remaining > len(selected) else None,
                "cursor": watermark, "node_budget": limit, "edge_budget": EDGE_LIMIT}

    def groups(self, project, *, worktree=None, cursor=None, limit=100):
        _limit(limit)
        projection = self.graph(project, worktree=worktree, _full=True)
        rows = projection["groups"]
        query_key = hashlib.sha256(json_text([project, str(worktree), "groups"]).encode()).hexdigest()
        total = len(rows)
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded.get("query") != query_key:
                raise ValueError("Group cursor belongs to another scope")
            rows = [row for row in rows if row["id"] > decoded["after"]]
        return {"items": rows[:limit], "total": total, "next_cursor": _encode_cursor({"query": query_key,
                "after": rows[limit - 1]["id"]}) if len(rows) > limit else None, "warnings": projection["warnings"],
                "cursor": projection["cursor"]}

    def neighbors(self, project, node_id, *, worktree=None, limit=100, cursor=None):
        _limit(limit, 500)
        projection = self.graph(project, worktree=worktree, _full=True)
        nodes = {row["id"]: row for row in projection["nodes"]}
        if node_id not in nodes:
            raise InspectorNotFound("Unknown graph node in requested scope")
        query_key = hashlib.sha256(json_text([project, str(worktree), node_id]).encode()).hexdigest()
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
            if len(rows) > limit else None, "warnings": projection["warnings"], "cursor": projection["cursor"]}


# ---- the fixtures ----

def populate(store, project, root):
    """Every kind of row the view has: notes, transcripts of several sessions, code, records with revisions, a plan, vertices and edges."""
    pid = project["id"]
    note = store.snapshot(pid, "note://a", "Use durable local memory.", "manual")
    decision = store.remember(pid, {"kind": "decision", "content": "Use durable local memory."}, [note["id"]])
    store.remember(pid, {"id": decision["id"], "expected_version": 1, "content": "Updated."}, [note["id"]])
    store.remember(pid, {"id": decision["id"], "expected_version": 2, "content": "Updated twice."}, [note["id"]])
    store.remember(pid, {"kind": "fact", "content": "A fact."}, [note["id"]])
    saved = PlanManager(store).save(pid, {"title": "Plan", "goal": "Goal"},
        [{"key": "a", "content": "Step a"}, {"key": "b", "content": "Step b"}], "reference", worktree=root, activate=True)
    PlanManager(store).save(pid, {"id": saved["plan"]["id"], "title": "Plan", "goal": "Goal again"},
        [{"key": "a", "content": "Step a"}, {"key": "b", "content": "Step b changed"}], "reference again",
        expected_version=saved["plan"]["version"], task_versions={row["id"]: row["version"] for row in saved["tasks"]}, worktree=root)
    for index, (kind, client, session, role) in enumerate([
            ("transcript", "claude", "s1", "user"), ("transcript", "claude", "s1", "assistant"), ("transcript", "codex", "s2", "tool"),
            ("transcript", "codex", "s2", "metadata"), ("transcript", None, None, "user"), ("transcript", "", "", "assistant"),
            ("transcript_unknown", "claude", "s3", "user"), ("transcript", "claude", "s1", "assistant")]):
        metadata = {"role": role, **({"client": client} if client is not None else {}), **({"session_id": session} if session is not None else {})}
        store.snapshot(pid, f"transcript://{index}", f"Message {index}", kind, metadata)
    code = {}
    for name in ("a.py", "b.py", "c.py"):
        path = root / name
        code[name] = store.snapshot(pid, str(path), f"def {name[0]}(): pass", "code", {"path": str(path), "root": str(root)})
    store.snapshot(pid, str(root / "a.py"), "def a(): return 1", "code", {"path": str(root / "a.py"), "root": str(root)})  # the first a.py is history now
    store.snapshot(pid, str(root / "README.md"), "# readme", "document", {"path": str(root / "README.md"), "root": str(root)})
    nodes = [{"id": f"file:{name}", "project_id": pid, "kind": "file", "label": name, "source_id": source["id"],
              "metadata": {"path": str(root / name), "root": str(root)}} for name, source in code.items()]
    nodes += [{"id": f"symbol:{name}:{n}", "project_id": pid, "kind": "function", "label": f"{name}::f{n}", "source_id": source["id"],
               "metadata": {"path": str(root / name), "root": str(root)}} for name, source in code.items() for n in range(3)]
    nodes.append({"id": "concept:loose", "project_id": pid, "kind": "concept", "label": "No source, no path", "metadata": {}})
    edges = [{"id": f"edge:contains:{node['id']}", "project_id": pid, "from_id": f"file:{node['label'].split('::')[0]}", "to_id": node["id"],
              "kind": "contains", "source_id": node["source_id"]} for node in nodes if node["kind"] == "function"]
    edges += [{"id": "edge:calls:1", "project_id": pid, "from_id": "symbol:a.py:0", "to_id": "symbol:b.py:1", "kind": "calls",
               "source_id": code["a.py"]["id"]},
              {"id": "edge:self", "project_id": pid, "from_id": "concept:loose", "to_id": "concept:loose", "kind": "mentions"},
              {"id": "edge:note", "project_id": pid, "from_id": "concept:loose", "to_id": f"record:{decision['id']}", "kind": "about"}]
    with store.transaction() as tx:
        store.write_graph(tx, pid, nodes, edges)
    return decision


@pytest.fixture
def stores(tmp_path):
    from cairnmind.store import Store
    root, other = tmp_path / "project", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    store = Store(tmp_path / "database")
    project = store.add_project("graph-reference", root)
    populate(store, project, root)
    return store, project, root, other


def same(new, old, label):
    assert json.dumps(new, sort_keys=False) == json.dumps(old, sort_keys=False), label


def every_page(read, **params):
    """The answers of a route, page by page, following next_cursor (or cursor for the routes that name it so)."""
    answers, cursor = [], None
    while True:
        answers.append(read(cursor))
        cursor = answers[-1].get("next_cursor")
        if not cursor:
            return answers


def compare_graph(store, pid, worktree, limits=(1, 3, 2000)):
    new, old = Inspector(store), WholeRead(store)
    whole = old.graph(pid, worktree=worktree, limit=NODE_LIMIT)
    groups = [None, "group:no-such-group", *[row["id"] for row in whole["groups"]], *[row["group"] for row in whole["groups"][:2]]]
    checked = 0
    for limit in limits:
        for group in groups:
            def read(reader):
                return lambda cursor: reader.graph(pid, worktree=worktree, group=group, limit=limit, page_cursor=cursor)
            pages_new = every_page(read(new))
            pages_old = every_page(read(old))
            assert len(pages_new) == len(pages_old), (limit, group)
            for number, (a, b) in enumerate(zip(pages_new, pages_old)):
                same(a, b, (limit, group, number))
                checked += 1
    return checked


def test_the_fixture_is_read_by_pages_when_nothing_is_held_back(stores):
    store, project, root, other = stores
    inspector = Inspector(store)
    from cairnmind import inspector_graph
    assert inspector_graph.keeps_everything(inspector, inspector._scope(project["id"], None))
    assert inspector_graph.keeps_everything(inspector, inspector._scope(project["id"], root))


@pytest.mark.parametrize("scoped", [False, True])
def test_every_page_of_the_graph_route_is_what_the_whole_read_gave(stores, scoped):
    store, project, root, other = stores
    assert compare_graph(store, project["id"], root if scoped else None) > 40


def test_a_second_worktrees_rows_keep_the_whole_read_and_the_same_answers(stores, tmp_path):
    from cairnmind import inspector_graph
    store, project, root, other = stores
    pid = project["id"]
    store.add_project_root(pid, other)
    foreign = store.snapshot(pid, str(other / "x.py"), "other = 1", "code", {"path": str(other / "x.py"), "root": str(other)})
    store.upsert_node({"id": "file:x.py", "project_id": pid, "kind": "file", "label": "x.py", "source_id": foreign["id"],
                       "metadata": {"path": str(other / "x.py"), "root": str(other)}})
    store.upsert_edge({"id": "edge:cross", "project_id": pid, "from_id": "file:x.py", "to_id": "file:a.py", "kind": "calls",
                       "source_id": foreign["id"]})
    inspector = Inspector(store)
    assert inspector_graph.keeps_everything(inspector, inspector._scope(pid, None))
    assert not inspector_graph.keeps_everything(inspector, inspector._scope(pid, root))
    assert not inspector_graph.keeps_everything(inspector, inspector._scope(pid, other))
    for worktree in (None, root, other):
        assert compare_graph(store, pid, worktree, limits=(3, 2000)) > 10
        compare_groups_and_neighbors(store, pid, worktree)


def compare_groups_and_neighbors(store, pid, worktree):
    new, old = Inspector(store), WholeRead(store)
    for limit in (2, 100):
        pages_new = every_page(lambda cursor: new.groups(pid, worktree=worktree, cursor=cursor, limit=limit))
        pages_old = every_page(lambda cursor: old.groups(pid, worktree=worktree, cursor=cursor, limit=limit))
        assert len(pages_new) == len(pages_old)
        for number, (a, b) in enumerate(zip(pages_new, pages_old)):
            same(a, b, ("groups", limit, number))
    projection = old.graph(pid, worktree=worktree, _full=True)
    identifiers = [row["id"] for row in projection["nodes"]]
    assert any(i.startswith("group:") for i in identifiers) and any(i.startswith("revision:") for i in identifiers)
    for identifier in identifiers:
        for limit in (1, 2, 100):
            pages_new = every_page(lambda cursor: new.neighbors(pid, identifier, worktree=worktree, cursor=cursor, limit=limit))
            pages_old = every_page(lambda cursor: old.neighbors(pid, identifier, worktree=worktree, cursor=cursor, limit=limit))
            assert len(pages_new) == len(pages_old), (identifier, limit)
            for number, (a, b) in enumerate(zip(pages_new, pages_old)):
                same(a, b, ("neighbors", identifier, limit, number))
    for read in (new, old):
        with pytest.raises(InspectorNotFound):
            read.neighbors(pid, "no-such-node", worktree=worktree)
        with pytest.raises(ValueError, match="another node"):
            read.neighbors(pid, identifiers[0], worktree=worktree, cursor=_encode_cursor({"query": "x", "after": "x"}))


@pytest.mark.parametrize("scoped", [False, True])
def test_every_page_of_groups_and_neighbors_is_what_the_whole_read_gave(stores, scoped):
    store, project, root, other = stores
    compare_groups_and_neighbors(store, project["id"], root if scoped else None)


def test_the_guards_of_a_request_hold_on_the_paged_read(stores, tmp_path):
    store, project, root, other = stores
    inspector = Inspector(store)
    for call in (lambda: inspector.graph("no-such-project"), lambda: inspector.groups("no-such-project"),
                 lambda: inspector.neighbors("no-such-project", "file:a.py")):
        with pytest.raises(InspectorNotFound):
            call()
    stray = tmp_path / "not-a-root"
    stray.mkdir()
    for call in (lambda: inspector.graph(project["id"], worktree=stray), lambda: inspector.groups(project["id"], worktree=stray),
                 lambda: inspector.neighbors(project["id"], "file:a.py", worktree=stray)):
        with pytest.raises(ValueError, match="registered project root"):
            call()
    with pytest.raises(ValueError, match="another scope"):
        inspector.graph(project["id"], page_cursor=_encode_cursor({"query": "other", "after": "a"}))


# ---- the rows one request reads ----

def test_a_request_reads_the_rows_of_its_page_not_the_project(tmp_path):
    from cairnmind.store import Store
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "database")
    project = store.add_project("large-graph", root)
    pid = project["id"]
    source = store.snapshot(pid, str(root / "big.py"), "x = 1", "code", {"path": str(root / "big.py"), "root": str(root)})
    nodes = [{"id": f"symbol:{n:05d}", "project_id": pid, "kind": "function", "label": f"f{n}", "source_id": source["id"],
              "metadata": {"path": str(root / "big.py"), "root": str(root)}} for n in range(900)]
    edges = [{"id": f"edge:{n:05d}", "project_id": pid, "from_id": f"symbol:{n:05d}", "to_id": f"symbol:{(n + 1) % 900:05d}", "kind": "calls",
              "source_id": source["id"]} for n in range(900)]
    with store.transaction() as tx:
        store.write_graph(tx, pid, nodes, edges)
    inspector = Inspector(store)
    seen = []
    real = store.query

    def counting(sql, params=None):
        rows = real(sql, params)
        seen.append((sql, len(rows)))
        return rows

    store.query = counting
    limit = 20
    for call in (lambda: inspector.graph(pid, limit=limit), lambda: inspector.graph(pid, worktree=root, limit=limit),
                 lambda: inspector.groups(pid, limit=limit), lambda: inspector.neighbors(pid, "symbol:00450", limit=limit)):
        seen.clear()
        call()
        assert sum(count for _, count in seen) < 6 * limit, [(sql[:60], count) for sql, count in seen if count > 5]
        assert len(seen) < 40
        assert all(count <= limit + 1 or "GROUP BY" in sql for sql, count in seen), [(sql[:80], count) for sql, count in seen if count > limit + 1]
