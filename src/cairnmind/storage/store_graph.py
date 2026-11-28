"""Graph vertex, edge, prefetch, batching, and record-graph operations for Store."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

from cairnmind.storage import graph_transactions
from cairnmind.storage.domain_types import GraphEdgeRecord, GraphNodeRecord, MemoryRecord, SourceRecord
from cairnmind.storage.sql import clean_row
from cairnmind.storage.storage_query import any_of, db_delete_where, db_find, db_find_one, eq, is_in

PROVENANCE_NODE_KINDS = frozenset({
    "decision",
    "task",
    "fact",
    "constraint",
    "question",
    "preference",
    "checkpoint",
    "source",
})


@contextmanager
def keeping_known(tx: Any) -> Iterator[dict[str, Any] | None]:
    """Graph writes that keep what the transaction remembers current, instead of forgetting it."""
    if getattr(tx, "known", None) is None:
        yield None
        return
    tx.keeps_known = True
    try:
        yield tx.known
    finally:
        tx.keeps_known = False


def prefetch_graph(
    store: Any,
    tx: Any,
    nodes: Iterable[GraphNodeRecord | Mapping[str, Any]],
    edges: Iterable[GraphEdgeRecord | Mapping[str, Any]],
) -> None:
    """Read, in a few requests, what upsert_node and upsert_edge would look up row by row.

    Only inside the transaction that then writes these rows. The checks stay the same checks:
    a row that was not read here is read when it is needed, and one known to be absent fails as before.
    """
    _ = store
    if not hasattr(tx, "known"):
        return
    node_list, edge_list = list(nodes), list(edges)
    wanted = {
        "sources": {
            source_id
            for item in (*node_list, *edge_list)
            if isinstance((source_id := item.get("source_id")), str) and source_id
        },
        "nodes": {item["id"] for item in node_list if isinstance(item.get("id"), str)}
        | {item[key] for item in edge_list for key in ("from_id", "to_id") if isinstance(item.get(key), str)},
        "edges": {item["id"] for item in edge_list if isinstance(item.get("id"), str)},
    }
    fields = {
        "sources": ("id", "project_id", "is_current"),
        "nodes": ("id", "project_id"),
        "edges": ("id", "project_id", "from_id", "to_id"),
    }
    known: dict[str, Any] = {"projects": set(), "sources": {}, "nodes": {}, "edges": {}}
    for table, id_set in wanted.items():
        identifiers = sorted(id_set)
        for offset in range(0, len(identifiers), 500):
            part = identifiers[offset:offset + 500]
            found = {row["id"]: row for row in db_find(tx, table, is_in("id", part), columns=fields[table])}
            known[table].update({identifier: found.get(identifier) for identifier in part})
    tx.known = known


def upsert_node(
    store: Any,
    node: GraphNodeRecord | Mapping[str, Any],
    tx: Any = None,
) -> GraphNodeRecord:
    if tx is None:
        with store.transaction() as native:
            return store.upsert_node(node, native)
    checked, existing = store._checked_node(tx, node)
    with store._keeping(tx) as known:
        store.backend.upsert_vertex(tx, checked, existing=bool(existing))
        if known is not None:
            known["nodes"][checked["id"]] = {"id": checked["id"], "project_id": checked["project_id"]}
    return checked


def checked_node(
    store: Any,
    tx: Any,
    node: GraphNodeRecord | Mapping[str, Any],
) -> tuple[GraphNodeRecord, dict[str, Any] | None]:
    """The vertex upsert_node writes, after its checks, and the stored row with its ID if there is one."""
    out: dict[str, Any] = {"metadata": {}, **node}
    for key in ("id", "project_id", "kind", "label"):
        if not isinstance(out.get(key), str):
            raise ValueError(f"Graph vertex requires {key}")
    store._require_project(tx, out["project_id"])
    source = None
    if out.get("source_id"):
        source = store._remembered(
            tx,
            "sources",
            out["source_id"],
            lambda: db_find_one(tx, "sources", {"id": out["source_id"]}, columns=("project_id", "is_current")),
        )
        if not source or source.get("project_id") != out["project_id"]:
            raise ValueError("Graph vertex source must belong to the same project")
    # Evidence/decision vertices remain traversable as provenance even if a code file changed.
    out.setdefault(
        "is_current",
        bool(source["is_current"]) if source and out["kind"] not in PROVENANCE_NODE_KINDS else True,
    )
    existing = store._remembered(
        tx, "nodes", out["id"], lambda: db_find_one(tx, "nodes", {"id": out["id"]})
    )
    if existing and existing.get("project_id") != out["project_id"]:
        raise ValueError("Graph vertex belongs to another project")
    return cast(GraphNodeRecord, out), existing


def upsert_edge(
    store: Any,
    edge: GraphEdgeRecord | Mapping[str, Any],
    tx: Any = None,
) -> GraphEdgeRecord:
    if tx is None:
        with store.transaction() as native:
            return store.upsert_edge(edge, native)
    checked, existing = store._checked_edge(tx, edge)
    with store._keeping(tx) as known:
        if known is not None:
            known["edges"][checked["id"]] = {key: checked[key] for key in ("id", "project_id", "from_id", "to_id")}
        store.backend.upsert_edge(tx, checked, existing=existing)
    return checked


def checked_edge(
    store: Any,
    tx: Any,
    edge: GraphEdgeRecord | Mapping[str, Any],
) -> tuple[GraphEdgeRecord, dict[str, Any] | None]:
    """The edge upsert_edge writes, after its checks, and the stored row with its ID if there is one."""
    out: dict[str, Any] = {"status": "observed", **edge}
    for key in ("id", "project_id", "from_id", "to_id", "kind"):
        if not isinstance(out.get(key), str):
            raise ValueError(f"Graph edge requires {key}")
    for endpoint in (out["from_id"], out["to_id"]):
        vertex = store._remembered(
            tx,
            "nodes",
            endpoint,
            lambda endpoint=endpoint: db_find_one(tx, "nodes", {"id": endpoint}, columns=("project_id",)),
        )
        if not vertex or vertex.get("project_id") != out["project_id"]:
            raise ValueError("Graph edge endpoints must exist in the same project")
    source = None
    if out.get("source_id"):
        source = store._remembered(
            tx,
            "sources",
            out["source_id"],
            lambda: db_find_one(tx, "sources", {"id": out["source_id"]}, columns=("project_id", "is_current")),
        )
        if not source or source.get("project_id") != out["project_id"]:
            raise ValueError("Graph edge source must belong to the same project")
    out.setdefault(
        "is_current",
        bool(source["is_current"])
        if source and out["kind"] not in {"supported_by", "depends_on", "supersedes"}
        else True,
    )
    existing = store._remembered(
        tx, "edges", out["id"], lambda: db_find_one(tx, "edges", {"id": out["id"]})
    )
    if existing and existing.get("project_id") != out["project_id"]:
        raise ValueError("Graph edge belongs to another project")
    return cast(GraphEdgeRecord, out), existing


def write_graph(
    store: Any,
    tx: Any,
    project_id: str,
    nodes: Iterable[GraphNodeRecord | Mapping[str, Any]],
    edges: Iterable[GraphEdgeRecord | Mapping[str, Any]],
) -> None:
    """Write graph rows as upsert_node, then upsert_edge, would for each, with one request per group of rows.

    Every row passes the checks of its upsert before its group is written. Grouped only in a transaction
    that reports the project's graph as reset, whose journal needs no single row; otherwise row by row.
    A group the database did not write in full fails the transaction, which then leaves the graph as it was.
    """
    from cairnmind.storage.journal import JournalTransaction
    node_list, edge_list = list(nodes), list(edges)
    if not isinstance(tx, JournalTransaction) or (tx.reset_reason is None and project_id not in tx.graph_resets):
        for item in node_list:
            store.upsert_node(item, tx)
        for item in edge_list:
            store.upsert_edge(item, tx)
        return
    for rows, kind in ((node_list, "node"), (edge_list, "edge")):
        if any(not isinstance(item, dict) or item.get("project_id") != project_id for item in rows):
            raise ValueError(f"Every graph {kind} of a grouped write belongs to its project")
        if len({item.get("id") for item in rows}) != len(rows):
            raise ValueError(f"A grouped graph write names a {kind} twice")
    created_nodes: list[GraphNodeRecord] = []
    updated_nodes: list[GraphNodeRecord] = []
    for item in node_list:
        node, existing = store._checked_node(tx, item)
        (updated_nodes if existing else created_nodes).append(node)
        if tx.known is not None:
            tx.known["nodes"][node["id"]] = {"id": node["id"], "project_id": node["project_id"]}
    with store._keeping(tx):
        store.backend.write_graph_batch(tx, project_id, created_nodes, updated_nodes, [])
    created_edges: list[GraphEdgeRecord] = []
    existing_edges: list[GraphEdgeRecord | Mapping[str, Any]] = []
    for item in edge_list:
        edge, existing = store._checked_edge(tx, item)
        if existing:
            existing_edges.append(item)
        else:
            created_edges.append(edge)
            if tx.known is not None:
                tx.known["edges"][edge["id"]] = {key: edge[key] for key in ("id", "project_id", "from_id", "to_id")}
    with store._keeping(tx):
        store.backend.write_graph_batch(tx, project_id, [], [], created_edges)
    for item in existing_edges:  # rare here: an edge that is kept is not written again
        store.upsert_edge(item, tx)


def remove_graph_nodes(
    store: Any,
    tx: Any,
    project_id: str,
    identifiers: Iterable[Any],
) -> list[str]:
    """Delete the project's vertices among `identifiers` that no edge names, one group per request.

    The same rows go as when each one was checked and deleted on its own: a vertex that an edge
    still names (a knowledge record's relationship, say) is kept, so that edge never dangles.
    A group the database did not delete in full fails the transaction, which then leaves the graph as it was.
    The engine counts the vertices a DELETE removes, not the edges that go with them (measured, #402).
    """
    id_list = sorted({identifier for identifier in identifiers if isinstance(identifier, str)})
    # A transaction's rows in one request: the engine scans the project's index once per DELETE, so groups of 250
    # made it 80 times more scans for a replaced graph (#402). The count is vertices only.
    group = graph_transactions.ROWS_PER_TRANSACTION
    kept: list[str] = []  # returned: the caller counts the vertices the store still holds (#426)
    tx.extra_copies = 0  # read by the caller: the copies deleted beyond the ids asked (#456)
    for offset in range(0, len(id_list), group):
        part = id_list[offset:offset + group]
        named: set[Any] = set()
        for row in db_find(
            tx,
            "edges",
            any_of(is_in("from_id", part), is_in("to_id", part)),
            columns=("from_id", "to_id"),
        ):
            named.update((row.get("from_id"), row.get("to_id")))
        kept.extend(identifier for identifier in part if identifier in named)
        unlinked = [identifier for identifier in part if identifier not in named]
        if not unlinked:
            continue
        # The project names the graph a reset transaction reports, so the journal reads nothing for this statement.
        deleted = store.backend.delete_graph_nodes(tx, project_id, unlinked)
        count = deleted[0].get("count") if isinstance(deleted, list) and deleted and isinstance(deleted[0], dict) else None
        # More than asked: an id stored twice (#456) goes with every copy, and the pass says how many extra went.
        if type(count) is not int or count < len(unlinked):
            raise RuntimeError("The database did not delete every graph vertex of a group")
        tx.extra_copies += count - len(unlinked)
    return kept


def load_checked_sources(tx: Any, project_id: str, sources: Any) -> dict[str, SourceRecord]:
    if not isinstance(sources, list) or not sources or not all(isinstance(item, str) for item in sources):
        raise ValueError("A memory record requires at least one source ID: cite a source you read, "
                         "or the source_ids a checkpoint returns")
    unique_ids = list(dict.fromkeys(sources))
    loaded: dict[str, SourceRecord] = {}
    for start in range(0, len(unique_ids), 500):
        for row in db_find(tx, "sources", is_in("id", unique_ids[start:start + 500])):
            cleaned = clean_row(row)
            loaded[cleaned["id"]] = cast(SourceRecord, cleaned)
    if len(loaded) != len(unique_ids) or any(row.get("project_id") != project_id for row in loaded.values()):
        raise ValueError("Sources must exist and belong to the same project")
    return loaded


def record_graph(
    store: Any,
    tx: Any,
    record: MemoryRecord | Mapping[str, Any],
    *,
    source_rows: Mapping[str, SourceRecord | Mapping[str, Any]] | None = None,
) -> None:
    resolved_sources: dict[str, dict[str, Any]] = {}
    missing_source_ids: list[str] = []
    for source_id in dict.fromkeys(record["source_ids"]):
        if source_rows is not None and source_id in source_rows:
            resolved_sources[source_id] = dict(source_rows[source_id])
        else:
            missing_source_ids.append(source_id)
    for start in range(0, len(missing_source_ids), 500):
        for row in db_find(tx, "sources", is_in("id", missing_source_ids[start:start + 500])):
            cleaned = clean_row(row)
            resolved_sources[cleaned["id"]] = cleaned
    for source_id in missing_source_ids:
        if source_id not in resolved_sources:
            found_src = db_find_one(tx, "sources", {"id": source_id}, required=True)
            if found_src is None:
                raise ValueError(f"Unknown source: {source_id}")
            resolved_sources[source_id] = found_src

    node_id = f"record:{record['id']}"
    record_meta = record.get("metadata") or {}
    dep_ids = [f"record:{dependency}" for dependency in (record_meta.get("depends_on") or [])]
    supersedes_val = record.get("supersedes") if isinstance(record, Mapping) else None
    sup_ids = [f"record:{supersedes_val}"] if supersedes_val else []
    source_node_ids = [f"source:{source_id}" for source_id in resolved_sources]
    candidate_ids = list(dict.fromkeys([node_id, *source_node_ids, *dep_ids, *sup_ids]))
    existing_nodes: dict[str, dict[str, Any]] = {}
    for start in range(0, len(candidate_ids), 500):
        for row in db_find(tx, "nodes", is_in("id", candidate_ids[start:start + 500])):
            cleaned = clean_row(row)
            existing_nodes[cleaned["id"]] = cleaned

    has_known = hasattr(tx, "known")
    prior_known = tx.known if has_known else None
    try:
        if has_known:
            if tx.known is None:
                tx.known = {"projects": set(), "sources": {}, "nodes": {}, "edges": {}}
            tx.known["projects"].add(record["project_id"])
            for sid, src in resolved_sources.items():
                tx.known["sources"][sid] = {
                    "id": sid,
                    "project_id": src.get("project_id"),
                    "is_current": src.get("is_current"),
                }
            for nid in candidate_ids:
                tx.known["nodes"][nid] = existing_nodes.get(nid)

        had_record_node = node_id in existing_nodes
        store.upsert_node(
            {
                "id": node_id,
                "project_id": record["project_id"],
                "kind": record["kind"],
                "label": record["content"][:1000],
                "source_id": record["source_ids"][0],
                "is_current": record["status"] != "superseded",
                "metadata": {**record_meta, "record_id": record["id"], "status": record["status"]},
            },
            tx,
        )
        if had_record_node:
            with store._keeping(tx):
                db_delete_where(
                    tx,
                    "edges",
                    [eq("from_id", node_id), is_in("kind", ("supported_by", "depends_on", "supersedes"))],
                )

        def edge(target: str, kind: str, source_id: str) -> None:
            eid = hashlib.sha256(f"{node_id}\0{kind}\0{target}".encode()).hexdigest()
            if has_known and tx.known is not None:
                tx.known["edges"][eid] = None
            store.upsert_edge(
                {
                    "id": eid,
                    "project_id": record["project_id"],
                    "from_id": node_id,
                    "to_id": target,
                    "kind": kind,
                    "source_id": source_id,
                    "is_current": record["status"] != "superseded",
                },
                tx,
            )

        for source_id in record["source_ids"]:
            source = resolved_sources[source_id]
            target = f"source:{source_id}"
            desired: dict[str, Any] = {
                "id": target,
                "project_id": record["project_id"],
                "kind": "source",
                "label": source["uri"],
                "source_id": source_id,
                "is_current": True,
                "metadata": {"source_id": source_id, "source_kind": source["kind"]},
            }
            existing_src = existing_nodes.get(target)
            if not (
                existing_src is not None
                and existing_src.get("project_id") == desired["project_id"]
                and existing_src.get("kind") == "source"
                and existing_src.get("label") == desired["label"]
                and existing_src.get("source_id") == source_id
                and existing_src.get("is_current") is True
                and existing_src.get("metadata") == desired["metadata"]
            ):
                store.upsert_node(desired, tx)
                existing_nodes[target] = desired
            edge(target, "supported_by", source_id)
        for dependency in record_meta.get("depends_on") or []:
            target = f"record:{dependency}"
            if target in existing_nodes:
                edge(target, "depends_on", record["source_ids"][0])
        # As for a dependency: a replacement written before the record it replaces (an import in ID order)
        # gets its edge when its graph is written again after that record's (#239).
        if supersedes_val:
            sup_target = f"record:{supersedes_val}"
            if sup_target in existing_nodes:
                edge(sup_target, "supersedes", record["source_ids"][0])
    finally:
        if has_known and prior_known is None:
            tx.known = None
