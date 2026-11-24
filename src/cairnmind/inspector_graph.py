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


SCOPED = {"sources": "metadata", "records": "metadata", "nodes": "metadata", "record_revisions": "snapshot.metadata"}
HELD_LIMIT = 50000  # rows a scope may hold back before its page is cut out of the whole read instead


class Held:
    """What a request's worktree holds back, as the whole read decides it, row by row.

    `bad` maps (table, field) to the distinct values of root, worktree or path that name another worktree (a path only
    for the file sources); `sources` and `records` are the identifiers of the rows that carry one, `own` those of the
    native vertices that carry one of their own, and `kept` those of the native vertices of a held source that stand
    for a record or plan in scope. A vertex whose source is held back is held back with it. Nothing held: `empty`, and
    every condition below is empty too.
    """

    def __init__(self):
        self.bad, self.sources, self.records, self.own, self.kept = {}, [], [], [], []

    @property
    def empty(self):
        return not self.bad


def _checks(held, table):
    """The conditions that a row of `table` names another worktree by its own fields, and that it does not: (out, keep, params).

    A field a row does not have names nothing (`NULL` is not in a list, and not out of it either), so each `keep` says so.
    """
    out, keep, params = [], [], {}
    for name in ("root", "worktree"):
        if (table, name) in held.bad:
            field = f"{SCOPED[table]}.{name}"
            out.append(f"{field} IN :bad_{name}")
            keep.append(f"({field} IS NULL OR {field} NOT IN :bad_{name})")
            params[f"bad_{name}"] = held.bad[(table, name)]
    if (table, "path") in held.bad:
        out.append("(kind IN :files AND metadata.path IN :bad_path)")
        keep.append("(kind NOT IN :files OR metadata.path IS NULL OR metadata.path NOT IN :bad_path)")
        params.update(files=list(FILE_KINDS), bad_path=held.bad[(table, "path")])
    return out, keep, params


def inside(held, table):
    """` AND ...` and its parameters: the rows of `table` the scope keeps; nothing when it holds back none of them."""
    _, parts, params = _checks(held, table) if table in SCOPED else ([], [], {})
    for belongs, ids, name, field in ((table in ("nodes", "edges"), held.sources, "held_sources", "source_id"),
                                      (table == "record_revisions", held.records, "held_records", "record_id")):
        if belongs and ids:
            parts.append(f"({field} IS NULL OR {field} NOT IN :{name})")
            params[name] = ids
    return "".join(" AND " + part for part in parts), params


def held(inspector, scope):
    """What the scope holds back, or None when the database cannot cut the page (the whole read answers then).

    Every distinct root and worktree the project's rows name, and every path of its file sources, goes through the
    match the whole read applies to each row (one row each, not one per vertex); the rows that carry a value that does
    not match are what is held back. None for a value that is not text, or when more than HELD_LIMIT rows are held.
    """
    store, root = inspector.store, scope[1]
    result = Held()
    if root is None:
        return result
    params = {"project": scope[0]["id"]}
    for table, field in SCOPED.items():
        for name in ("root", "worktree"):
            values = [row["value"] for row in store.query(f"SELECT {field}.{name} AS value FROM {table} WHERE project_id=:project "
                                                          f"AND {field}.{name} IS NOT NULL GROUP BY {field}.{name}", params)]
            out = [value for value in values if not inspector._matches(scope, {"metadata": {name: value}})]
            if out:
                result.bad[(table, name)] = out
    paths = [row["value"] for row in store.query(
        "SELECT metadata.path AS value FROM sources WHERE project_id=:project AND kind IN :kinds "
        "AND metadata.path IS NOT NULL GROUP BY metadata.path", {**params, "kinds": list(FILE_KINDS)})]
    out = [value for value in paths if not inspector._matches(scope, {"kind": "code", "metadata": {"path": value}}, source=True)]
    if out:
        result.bad[("sources", "path")] = out
    if any(not isinstance(value, str) for values in result.bad.values() for value in values):
        return None
    for table in ("sources", "records"):
        out, _, extra = _checks(result, table)
        if out:
            setattr(result, table, [row["id"] for row in store.query(
                f"SELECT id FROM {table} WHERE project_id=:project AND ({' OR '.join(out)})", {**params, **extra})])
    out, _, extra = _checks(result, "nodes")
    if out:  # vertices held by their own root or worktree (a record's); those held by their source are not listed
        result.own = [row["id"] for row in store.query(
            f"SELECT id FROM nodes WHERE project_id=:project AND ({' OR '.join(out)})", {**params, **extra})]
    if result.sources:  # a held vertex of a record or plan that is in scope still stands for it in the view
        result.kept = [row["id"] for row in store.query(
            "SELECT id FROM nodes WHERE project_id=:project AND source_id IN :held_sources AND (id LIKE 'record:%' OR id LIKE 'plan:%')",
            {**params, "held_sources": result.sources}) if not row["id"].startswith("record:") or row["id"][7:] not in set(result.records)]
    if len(result.sources) + len(result.records) + len(result.own) > HELD_LIMIT:
        return None
    return result


def dropped(store, pid, held):
    """The edges of the scope's sources that join a vertex the scope holds back: the whole read leaves them out.

    A vertex is held back by its own root or worktree (`own`) or by its source, unless it stands in the view for a record
    or plan that is in scope (`kept`). The edges that join one are asked of the database by the source of their ends.
    """
    if held.empty:
        return 0
    where, params = inside(held, "edges")
    found, kept, sources = set(), set(held.kept), set(held.sources)
    if sources:
        for row in store.query("SELECT id,from_id,to_id,out.source_id AS first,in.source_id AS second FROM edges WHERE project_id=:project "
                               f"AND (out.source_id IN :held_sources OR in.source_id IN :held_sources){where}", {"project": pid, **params}):
            if (row["first"] in sources and row["from_id"] not in kept) or (row["second"] in sources and row["to_id"] not in kept):
                found.add(row["id"])
    for start in range(0, len(held.own), 2000):
        for side in ("from_id", "to_id"):
            found.update(row["id"] for row in store.query(
                f"SELECT id FROM edges WHERE project_id=:project AND {side} IN :ids{where}",
                {"project": pid, "ids": held.own[start:start + 2000], **params}))
    return len(found)


def totals(store, pid, counted=True, held=None):
    """The counts of the whole graph and the sizes of its groups, summed by the database; the session groups' sources."""
    held = held or Held()

    def count(table):
        where, params = inside(held, table)
        return store.query(f"SELECT count(*) AS n FROM {table} WHERE project_id=:project{where}", {"project": pid, **params})[0]["n"]

    def grouped(table, fields, by, where=""):
        scope, params = inside(held, table)
        return store.query(f"SELECT {fields}, count(*) AS n FROM {table} WHERE project_id=:project{where}{scope} GROUP BY {by}",
                           {"project": pid, **params})

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
    counts.update(display_nodes=sum(buckets.values()),
                  display_edges=counts["edges"] - dropped(store, pid, held) + sum(revisions.values()))
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


def rows_for(store, pid, identifiers, held=None):
    """The rows of the nodes with these identifiers that the scope keeps, as the whole read would hold them."""
    held, rows = held or Held(), {table: [] for table in FIELDS}
    if not identifiers:
        return rows
    for table, prefix in PREFIXES.items():
        found = [identifier[len(prefix):] for identifier in identifiers if identifier.startswith(prefix)]
        if found:
            scope, params = inside(held, table)
            rows[table] = store.query(f"SELECT {FIELDS[table]} FROM {table} WHERE project_id=:project AND id IN :ids{scope} ORDER BY id",
                                      {"project": pid, "ids": found, **params})
    # The native vertices of the same identifiers too: a node that stands in for one keeps the place it has in the view.
    scope, params = inside(held, "nodes")
    rows["nodes"] = store.query(f"SELECT {FIELDS['nodes']} FROM nodes WHERE project_id=:project AND id IN :ids{scope} ORDER BY id",
                                {"project": pid, "ids": list(identifiers), **params})
    return rows


def select(store, pid, group, after, size, sessions, held=None):
    """`size` node identifiers of the group in order after `after`, and whether more follow."""
    held, found = held or Held(), []
    for table, prefix in (("nodes", ""), *PREFIXES.items()):
        where, after_where = _where(table, group, sessions), _after(prefix, after)
        if where is None or after_where is None:
            continue
        scope, params = inside(held, table)
        found += [prefix + row["id"] for row in store.query(
            f"SELECT id FROM {table} WHERE project_id=:project {where[0]} {after_where[0]}{scope}"
            f"{' AND ' + STANDING_IN if table == 'nodes' else ''} ORDER BY id LIMIT {size + 1}",
            {"project": pid, **where[1], **after_where[1], **params})]
    found.sort()
    return found[:size], len(found) > size


def page_rows(store, pid, selected, held=None):
    """The rows of the selected nodes and of the edges between them."""
    rows, (scope, params) = rows_for(store, pid, selected, held), inside(held or Held(), "edges")
    rows["edges"] = store.query(f"SELECT {FIELDS['edges']} FROM edges WHERE project_id=:project AND from_id IN :ids AND to_id IN :ids"
                                f"{scope} ORDER BY id", {"project": pid, "ids": selected, **params}) if selected else []
    return rows


def neighbors(store, pid, node_id, after, limit, groups, sessions, held=None):
    """What the neighbors route answers for one node: (nodes, edges, total, more), or None when the view has no such node.

    The edges of a node are its native edges, the version edges between a record or plan and its revisions, and the
    one `contains` edge from its group; a group's edges are the `contains` edges to its nodes. All in the order of their
    identifiers, which is how a page is cut. When the scope holds rows back, an edge to a vertex held back is not one of
    them: the edges of the node are then read all (as many as its degree, not the graph), each end checked by a request
    for the ends, and the total counts what is left.
    """
    held = held or Held()
    group = {row["id"]: row for row in groups}.get(node_id)
    if group is not None:
        contained, more = [], False
        if after is None or after < "contains:":
            contained, more = select(store, pid, node_id, None, limit, sessions, held)
        elif after.startswith("contains:"):
            contained, more = select(store, pid, node_id, after.removeprefix("contains:"), limit, sessions, held)
        edges = [{"id": "contains:" + identifier, "project_id": pid, "from_id": node_id, "to_id": identifier,
                  "kind": "contains", "structural": True} for identifier in contained]
        nodes = build(pid, rows_for(store, pid, contained, held))[0]
        nodes[node_id] = group
        return [nodes[identifier] for identifier in sorted({node_id, *contained})], edges, group["count"], more
    found, _, buckets = build(pid, {**rows_for(store, pid, [node_id], held), "edges": []})
    if node_id not in found:
        return None
    # The identifiers of the node's edges after `after`, at most one more than a page; the native ones with their rows.
    edge_scope, scope_params = inside(held, "edges")
    since = after if held.empty else None  # a scope that holds rows back counts the edges before the cursor too
    params, candidates = {"project": pid, "node": node_id, **scope_params, **({"beyond": since} if since is not None else {})}, {}
    where, cap = "" if since is None else " AND id>:beyond", f" LIMIT {limit + 1}" if held.empty else ""
    total = 0
    for side in ("from", "to"):
        candidates.update({row["id"]: row for row in store.query(
            f"SELECT {FIELDS['edges']} FROM edges WHERE project_id=:project AND {side}_id=:node{where}{edge_scope} ORDER BY id{cap}", params)})
        total += store.query(f"SELECT count(*) AS n FROM edges WHERE project_id=:project AND {side}_id=:node", params)[0]["n"]
    total -= store.query("SELECT count(*) AS n FROM edges WHERE project_id=:project AND from_id=:node AND to_id=:node", params)[0]["n"]
    for table, prefix, owner in (("record_revisions", "record:", "record_id"), ("plan_revisions", "plan:", "plan_id")):
        if node_id.startswith(prefix):  # the revisions of a record or plan, each with its version edge
            own, revision_scope = {"project": pid, "owner": node_id.removeprefix(prefix)}, inside(held, table)
            after_where = _after("history:revision:" + prefix, since)
            if after_where is not None:
                candidates.update({"history:revision:" + prefix + row["id"]: None for row in store.query(
                    f"SELECT id FROM {table} WHERE project_id=:project AND {owner}=:owner {after_where[0]}{revision_scope[0]} ORDER BY id{cap}",
                    {**own, **after_where[1], **revision_scope[1]})})
            total += store.query(f"SELECT count(*) AS n FROM {table} WHERE project_id=:project AND {owner}=:owner{revision_scope[0]}",
                                 {**own, **revision_scope[1]})[0]["n"]
    own_edges = ["contains:" + node_id, *(["history:" + node_id] if node_id.startswith("revision:") else [])]
    candidates.update({identifier: None for identifier in own_edges})
    total += len(own_edges)
    if not held.empty:  # the second request: which ends of its native edges the scope keeps
        ends = sorted({row[key] for row in candidates.values() if row is not None for key in ("from_id", "to_id")} - {node_id})
        kept = {*build(pid, {**rows_for(store, pid, ends, held), "edges": []})[0], node_id}
        candidates = {identifier: row for identifier, row in candidates.items()
                      if row is None or (row["from_id"] in kept and row["to_id"] in kept)}
        total = len(candidates)
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
    nodes, edges, _ = build(pid, {**rows_for(store, pid, sorted(ids), held), "edges": [row for row in candidates.values() if row is not None and row["id"] in selected]})
    group = "group:" + buckets[node_id]
    chosen = [{"id": identifier, "project_id": pid, "from_id": group, "to_id": node_id, "kind": "contains", "structural": True}
              if identifier.startswith("contains:") else edges.get(identifier) for identifier in selected]
    chosen = [edge for edge in chosen if edge is not None]
    nodes[group] = {row["id"]: row for row in groups}[group]
    return [nodes[identifier] for identifier in sorted({node_id, *[edge[key] for edge in chosen for key in ("from_id", "to_id")]})], \
        chosen, total, len(ordered) > limit
