"""A page of the inspector's graph, and its counts, asked of the database (#450).

The graph the inspector shows is a view: every native vertex and edge, plus a node for each source, record, plan and
revision, which stands in for the native vertex of the same identifier (`record:ID`, `source:ID`, `plan:ID`). A request
used to read every row of the project, build that whole view and cut one page out of it. Here the database sums the
counts and the groups, and returns only the rows of the page: the first identifiers in order, then their rows and the
edges between them. The answers are the same as the whole read gave (tests/test_inspector_graph.py compares them).
They rest on what the writers guarantee: a vertex names a source of its project, an edge joins two vertices of its
project, and a native vertex with a prefixed identifier has the source, record or plan of that identifier.
"""
from __future__ import annotations

from collections import Counter

TRANSCRIPT = ("transcript", "transcript_unknown")
FILE_KINDS = ("code", "document", "file")
FIELDS = {
    "sources": "id,kind,uri,is_current,created_at,metadata.root AS root,metadata.worktree AS worktree,metadata.path AS path,metadata.role AS role,metadata.client AS client,metadata.session_id AS session_id,metadata.origin_client AS origin_client,metadata.origin_evidence AS origin_evidence,metadata.source_timestamp AS source_timestamp,metadata.byte_offset AS byte_offset",
    "records": "id,kind,status,version,metadata.root AS root,metadata.worktree AS worktree,metadata.path AS path",
    "plans": "id,title,version",
    "record_revisions": "id,record_id,version,snapshot.metadata.root AS root,snapshot.metadata.worktree AS worktree",
    "plan_revisions": "id,plan_id,version",
    "nodes": "id,kind,label,source_id,is_current,metadata.root AS root,metadata.worktree AS worktree,metadata.path AS path",
    "edges": "id,from_id,to_id,kind,source_id,is_current,status",
}
PREFIXES = {"sources": "source:", "records": "record:", "plans": "plan:",
            "record_revisions": "revision:record:", "plan_revisions": "revision:plan:"}
STANDING_IN = "NOT (id LIKE 'source:%' OR id LIKE 'record:%' OR id LIKE 'plan:%')"  # the native vertices no node replaces


def build(pid, rows):
    """The nodes, edges and groups of the rows given, in the order the whole view has them."""
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
        message = row["kind"] in TRANSCRIPT
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
    return nodes, {identifier: item for identifier, item in edges.items() if item["from_id"] in nodes and item["to_id"] in nodes}, buckets


def group_rows(pid, buckets):
    return [{"id": "group:" + key, "group": key, "kind": "group", "label": key,
             "project_id": pid, "count": count, "expandable": True} for key, count in sorted(buckets.items())]


def keeps_everything(inspector, scope):
    """True when the request's worktree holds back no row of the project, so the whole project is the answer.

    Every distinct root and worktree the project's rows name, and every path of its file sources, goes through the
    match the whole read applies to each row. A project that holds another worktree's rows is read whole instead.
    """
    store, root = inspector.store, scope[1]
    if root is None:
        return True
    params = {"project": scope[0]["id"]}
    for table, field in (("sources", "metadata"), ("records", "metadata"), ("nodes", "metadata"), ("record_revisions", "snapshot.metadata")):
        for name in ("root", "worktree"):
            for row in store.query(f"SELECT {field}.{name} AS value FROM {table} WHERE project_id=:project "
                                   f"AND {field}.{name} IS NOT NULL GROUP BY {field}.{name}", params):
                if not inspector._matches(scope, {"metadata": {name: row["value"]}}):
                    return False
    for row in store.query("SELECT metadata.path AS value FROM sources WHERE project_id=:project AND kind IN :kinds "
                           "AND metadata.path IS NOT NULL GROUP BY metadata.path", {**params, "kinds": list(FILE_KINDS)}):
        if not inspector._matches(scope, {"kind": "code", "metadata": {"path": row["value"]}}, source=True):
            return False
    return True


def totals(store, pid, counted=True):
    """The counts of the whole graph and the sizes of its groups, summed by the database; the session groups' sources."""
    params = {"project": pid}

    def count(table):
        return store.query(f"SELECT count(*) AS n FROM {table} WHERE project_id=:project", params)[0]["n"]

    def grouped(table, fields, by, where=""):
        return store.query(f"SELECT {fields}, count(*) AS n FROM {table} WHERE project_id=:project{where} GROUP BY {by}", params)

    buckets, sessions, counts = Counter(), {}, {}
    counts["sources"] = counts["records"] = 0
    for row in grouped("sources", "kind, metadata.client AS client, metadata.session_id AS session_id",
                       "kind, metadata.client, metadata.session_id"):
        if row["kind"] in TRANSCRIPT:
            key = "session:" + str(row.get("client") or "unknown") + ":" + str(row.get("session_id") or "unknown")
            sessions.setdefault(key, []).append(row)
        else:
            key = "sources:" + row["kind"]
        buckets[key] += row["n"]
        counts["sources"] += row["n"]
    for row in grouped("records", "kind", "kind"):
        buckets["records:" + row["kind"]] += row["n"]
        counts["records"] += row["n"]
    for row in grouped("nodes", "kind", "kind", " AND " + STANDING_IN):
        buckets["native:" + row["kind"]] += row["n"]
    plans, revisions = count("plans"), {"record_revisions": count("record_revisions"), "plan_revisions": count("plan_revisions")}
    for key, number in (("plans", plans), ("history:record", revisions["record_revisions"]), ("history:plan", revisions["plan_revisions"])):
        if number:
            buckets[key] = number
    if not counted:  # the groups and neighbors routes show no counts
        return None, buckets, sessions
    counts.update(plans=plans, **revisions, nodes=count("nodes"), edges=count("edges"))
    counts.update(display_nodes=sum(buckets.values()), display_edges=counts["edges"] + sum(revisions.values()))
    return counts, buckets, sessions


def _where(table, group, sessions):
    """The condition that keeps the rows of one group, or None when none of this table's rows is in it."""
    if group is None:
        return "", {}
    key = group.removeprefix("group:")
    if table == "nodes":
        return ("AND kind=:kind", {"kind": key.removeprefix("native:")}) if key.startswith("native:") else None
    if table == "records":
        return ("AND kind=:kind", {"kind": key.removeprefix("records:")}) if key.startswith("records:") else None
    if table in ("plans", "record_revisions", "plan_revisions"):
        return ("", {}) if key == {"plans": "plans", "record_revisions": "history:record", "plan_revisions": "history:plan"}[table] else None
    if key.startswith("sources:"):
        return "AND kind=:kind AND kind NOT IN :transcripts", {"kind": key.removeprefix("sources:"), "transcripts": list(TRANSCRIPT)}
    parts, params = [], {}
    for number, row in enumerate(sessions.get(key, [])):
        clause, params[f"kind{number}"] = [f"kind=:kind{number}"], row["kind"]
        for field, name in (("metadata.client", "client"), ("metadata.session_id", "session_id")):
            if row.get(name) is None:
                clause.append(f"{field} IS NULL")
            else:
                clause.append(f"{field}=:{name}{number}")
                params[f"{name}{number}"] = row[name]
        parts.append("(" + " AND ".join(clause) + ")")
    return ("AND (" + " OR ".join(parts) + ")", params) if parts else None


def _after(prefix, after):
    """The condition for the rows whose prefixed identifier sorts after `after`, or None when none does."""
    if after is None:
        return "", {}
    if after.startswith(prefix):
        return "AND id>:beyond", {"beyond": after[len(prefix):]}
    return ("", {}) if after < prefix else None


def rows_for(store, pid, identifiers):
    """The rows of the nodes with these identifiers, as the whole read would hold them."""
    rows = {table: [] for table in FIELDS}
    if not identifiers:
        return rows
    for table, prefix in PREFIXES.items():
        found = [identifier[len(prefix):] for identifier in identifiers if identifier.startswith(prefix)]
        if found:
            rows[table] = store.query(f"SELECT {FIELDS[table]} FROM {table} WHERE project_id=:project AND id IN :ids ORDER BY id",
                                      {"project": pid, "ids": found})
    # The native vertices of the same identifiers too: a node that stands in for one keeps the place it has in the view.
    rows["nodes"] = store.query(f"SELECT {FIELDS['nodes']} FROM nodes WHERE project_id=:project AND id IN :ids ORDER BY id",
                                {"project": pid, "ids": list(identifiers)})
    return rows


def select(store, pid, group, after, size, sessions):
    """`size` node identifiers of the group in order after `after`, and whether more follow."""
    found = []
    for table, prefix in (("nodes", ""), *PREFIXES.items()):
        where, after_where = _where(table, group, sessions), _after(prefix, after)
        if where is None or after_where is None:
            continue
        found += [prefix + row["id"] for row in store.query(
            f"SELECT id FROM {table} WHERE project_id=:project {where[0]} {after_where[0]}"
            f"{' AND ' + STANDING_IN if table == 'nodes' else ''} ORDER BY id LIMIT {size + 1}",
            {"project": pid, **where[1], **after_where[1]})]
    found.sort()
    return found[:size], len(found) > size


def page_rows(store, pid, selected):
    """The rows of the selected nodes and of the edges between them."""
    rows = rows_for(store, pid, selected)
    rows["edges"] = store.query(f"SELECT {FIELDS['edges']} FROM edges WHERE project_id=:project AND from_id IN :ids AND to_id IN :ids "
                                "ORDER BY id", {"project": pid, "ids": selected}) if selected else []
    return rows


def neighbors(store, pid, node_id, after, limit, groups, sessions):
    """What the neighbors route answers for one node: (nodes, edges, total, more), or None when the view has no such node.

    The edges of a node are its native edges, the version edges between a record or plan and its revisions, and the
    one `contains` edge from its group; a group's edges are the `contains` edges to its nodes. All in the order of their
    identifiers, which is how a page is cut.
    """
    group = {row["id"]: row for row in groups}.get(node_id)
    if group is not None:
        contained, more = [], False
        if after is None or after < "contains:":
            contained, more = select(store, pid, node_id, None, limit, sessions)
        elif after.startswith("contains:"):
            contained, more = select(store, pid, node_id, after.removeprefix("contains:"), limit, sessions)
        edges = [{"id": "contains:" + identifier, "project_id": pid, "from_id": node_id, "to_id": identifier,
                  "kind": "contains", "structural": True} for identifier in contained]
        nodes = build(pid, rows_for(store, pid, contained))[0]
        nodes[node_id] = group
        return [nodes[identifier] for identifier in sorted({node_id, *contained})], edges, group["count"], more
    found, _, buckets = build(pid, {**rows_for(store, pid, [node_id]), "edges": []})
    if node_id not in found:
        return None
    # The identifiers of the node's edges after `after`, at most one more than a page; the native ones with their rows.
    params, candidates = {"project": pid, "node": node_id, **({"beyond": after} if after is not None else {})}, {}
    where = "" if after is None else " AND id>:beyond"
    total = 0
    for side in ("from", "to"):
        candidates.update({row["id"]: row for row in store.query(
            f"SELECT {FIELDS['edges']} FROM edges WHERE project_id=:project AND {side}_id=:node{where} ORDER BY id LIMIT {limit + 1}", params)})
        total += store.query(f"SELECT count(*) AS n FROM edges WHERE project_id=:project AND {side}_id=:node", params)[0]["n"]
    total -= store.query("SELECT count(*) AS n FROM edges WHERE project_id=:project AND from_id=:node AND to_id=:node", params)[0]["n"]
    for table, prefix, owner in (("record_revisions", "record:", "record_id"), ("plan_revisions", "plan:", "plan_id")):
        if node_id.startswith(prefix):  # the revisions of a record or plan, each with its version edge
            own = {"project": pid, "owner": node_id.removeprefix(prefix)}
            after_where = _after("history:revision:" + prefix, after)
            if after_where is not None:
                candidates.update({"history:revision:" + prefix + row["id"]: None for row in store.query(
                    f"SELECT id FROM {table} WHERE project_id=:project AND {owner}=:owner {after_where[0]} ORDER BY id LIMIT {limit + 1}",
                    {**own, **after_where[1]})})
            total += store.query(f"SELECT count(*) AS n FROM {table} WHERE project_id=:project AND {owner}=:owner", own)[0]["n"]
    own_edges = ["contains:" + node_id, *(["history:" + node_id] if node_id.startswith("revision:") else [])]
    candidates.update({identifier: None for identifier in own_edges})
    total += len(own_edges)
    ordered = sorted(identifier for identifier in candidates if after is None or identifier > after)
    selected = ordered[:limit]
    # The nodes they join: the node, the other end of each native edge, each revision and the owner of the revision.
    ids = {node_id}
    for identifier in selected:
        if candidates[identifier] is not None:
            ids.update((candidates[identifier]["from_id"], candidates[identifier]["to_id"]))
        elif identifier.startswith("history:"):
            ids.add(identifier.removeprefix("history:"))
    if node_id.startswith("revision:"):
        ids.add(found[node_id]["owner_type"] + ":" + found[node_id]["owner_id"])
    nodes, edges, _ = build(pid, {**rows_for(store, pid, sorted(ids)), "edges": [row for row in candidates.values() if row is not None and row["id"] in selected]})
    group = "group:" + buckets[node_id]
    chosen = [{"id": identifier, "project_id": pid, "from_id": group, "to_id": node_id, "kind": "contains", "structural": True}
              if identifier.startswith("contains:") else edges.get(identifier) for identifier in selected]
    chosen = [edge for edge in chosen if edge is not None]
    nodes[group] = {row["id"]: row for row in groups}[group]
    return [nodes[identifier] for identifier in sorted({node_id, *[edge[key] for edge in chosen for key in ("from_id", "to_id")]})], \
        chosen, total, len(ordered) > limit
