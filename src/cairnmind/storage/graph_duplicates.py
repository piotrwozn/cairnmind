"""Two vertices or two edges of one id (#456): noticed after a graph write, removed with their edges put back, and said.

The engine's unique indexes of ``nodes.id`` and ``edges.id`` are what keep an id once, and what the graph write asks to
know whether a row exists (``Store._checked_node``). On the pinned image in production mode a pass stored a
``source:`` vertex twice, with no refusal; the index that lags behind (#377, #433) is the suspected cause (replaying the
recorded requests showed it, no reduced reproduction was made) and can let a second row of an id through as well as
refuse one. So the unique index is not trusted for it: after a pass that wrote rows, the project's rows and
its distinct ids are counted by the project's own index, which does not lag with it, and a difference is repaired.

Repair keeps the first stored copy of an id (the lowest record id) and deletes the others (the engine takes the edges of
a vertex with it). Every write of it is made by the record id it already holds, never by an id lookup, which is what
missed: in the same transactions of the graph write (``GraphTransactions``) it writes on the vertex that stays what this
pass computed for the id (the copy that stays may be the older one, which the pass's write missed) and the edges the
deleted copies held that the vertex does not hold. It then counts again: a store that still holds one fails the pass by
name, with the graph marked as not built.

All of it is ArcadeDB's: the embedded SQLite backend makes ``id`` the primary key of every collection, so it cannot hold
an id twice, runs no raw SQL, and every function here answers "none" for it without asking.
"""
from __future__ import annotations

import re

from cairnmind.storage.sql import quote_name

# The count is named, not inflected: "1 vertices" and "1 wierzchołków" are wrong where one was seen every time.
REPAIRED = ("Graph vertices of project {project} that the database stored twice under one id: {count}. The extra ones "
            "were removed, the edges they held were written again, and the graph was counted again.")
EDGES_REPAIRED = ("Graph edges of project {project} that the database stored twice under one id: {count}. The extra ones "
                  "were removed, and the graph was counted again.")
IN_PROJECT = ("Project {project} holds graph vertices stored twice under one id ({vertices}) or graph edges ({edges}). "
              "`cairnmind index {project}` removes the extra ones, with no file changed.")
MESSAGES = {
    REPAIRED: ("Wierzchołki grafu projektu {project}, które baza zapisała dwa razy pod jednym id: {count}. Nadmiarowe "
               "usunięto, krawędzie, które trzymały, zapisano ponownie, a graf policzono jeszcze raz."),
    EDGES_REPAIRED: ("Krawędzie grafu projektu {project}, które baza zapisała dwa razy pod jednym id: {count}. Nadmiarowe "
                     "usunięto, a graf policzono jeszcze raz."),
    IN_PROJECT: ("Projekt {project} ma wierzchołki grafu zapisane dwa razy pod jednym id ({vertices}) lub krawędzie ({edges}). "
                 "`cairnmind index {project}` usuwa nadmiarowe, bez zmiany żadnego pliku."),
}


def _keyed_by_id(reader) -> bool:
    return getattr(getattr(reader, "backend", None), "backend_kind", None) == "sqlite"


def surplus(reader, project_id: str | None = None, table: str = "nodes") -> int:
    """How many rows of the project (of the whole store without one) share an id with another: rows minus ids.

    One request that counts the rows and the distinct ids, by the project's index of ``project_id`` or, without
    one, by a scan of the type: neither is the unique index of ``id``, which may have let the second one in."""
    if _keyed_by_id(reader):
        return 0
    where, params = ("WHERE project_id=:project", {"project": project_id}) if project_id else ("", None)
    row = reader.query(f"SELECT count(*) AS n, count(DISTINCT id) AS d FROM {table} {where}".strip(), params)[0]
    return int(row.get("n") or 0) - int(row.get("d") or 0)


def stored_twice(reader, project_id: str) -> bool:
    return bool(surplus(reader, project_id) or surplus(reader, project_id, "edges"))


def by_project(reader) -> list[dict]:
    """The projects that hold a vertex or an edge twice, with how many rows each: doctor's note names the command for each."""
    found: dict[str, dict] = {}
    if _keyed_by_id(reader):
        return []
    for table, key in (("nodes", "vertices"), ("edges", "edges")):
        for row in reader.query(f"SELECT project_id, count(*) AS n, count(DISTINCT id) AS d FROM {table} GROUP BY project_id"):
            if isinstance(row.get("project_id"), str) and int(row.get("n") or 0) > int(row.get("d") or 0):
                found.setdefault(row["project_id"], {"vertices": 0, "edges": 0})[key] = int(row["n"]) - int(row["d"])
    names = {row["id"]: row["name"] for row in reader.query("SELECT id,name FROM projects")}
    return [{"project": names.get(identifier, identifier), **counts} for identifier, counts in sorted(found.items())]


def _position(rid: str) -> tuple[int, int]:
    bucket, _, position = str(rid).lstrip("#").partition(":")
    return int(bucket), int(position)


def _copies(tx, table: str, project_id: str) -> dict[str, list[str]]:
    """The record ids of each id of the project that is stored more than once, the first stored first. Read by the project's index."""
    copies: dict[str, list[str]] = {}
    for row in tx.query(f"SELECT @rid AS rid,id FROM {table} WHERE project_id=:project", {"project": project_id}):
        copies.setdefault(row["id"], []).append(str(row["rid"]))
    return {identifier: sorted(rids, key=_position) for identifier, rids in copies.items() if len(rids) > 1}


def _edges_of(tx, rids: list[str]) -> list[dict]:
    """The edges of the vertices at `rids`, with their ends, read from the vertices themselves and not by an index."""
    edges = []
    for offset in range(0, len(rids), 500):
        edges += tx.query("SELECT *, @out AS out_rid, @in AS in_rid FROM (SELECT expand(bothE()) FROM nodes WHERE @rid IN :rids)",
                         {"rids": rids[offset:offset + 500]})
    return edges


def write_node_at(store, tx, rid, node) -> None:
    """Store.write_node_at: the vertex, after the checks of its upsert, written on the stored one at `rid`."""
    node, _ = store._checked_node(tx, node)
    sets = ",".join(f"{quote_name(key)}=:u_{key}" for key in node)
    tx.command(f"UPDATE nodes SET {sets} WHERE @rid=:rid",
               {**{f"u_{key}": value for key, value in node.items()}, "rid": rid, "project_id": node["project_id"]})


def write_edge_between(tx, out, into, edge) -> None:
    """Store.write_edge_between: an edge created between the vertices at two record ids."""
    if not all(isinstance(rid, str) and re.fullmatch(r"#\d+:\d+", rid) for rid in (out, into)):
        raise ValueError("An edge is written between two record ids")
    tx.command(f"CREATE EDGE edges FROM {out} TO {into} CONTENT :edge", {"edge": edge, "project_id": edge["project_id"]})


def repair(store, project_id: str, written=()) -> tuple[int, int]:
    """Remove the extra vertices and the extra edges of an id in the project; return how many of each were removed.

    `written` are the vertex rows this pass computed and wrote: those of the duplicated ids are written again on the
    copy that stays, so the graph is what a full write gives. A repair that does not end whole leaves the code graph
    marked as not built."""
    from cairnmind.storage.graph_transactions import GraphTransactions
    if not stored_twice(store, project_id):
        return 0, 0
    marked = store.query("SELECT graph_fingerprint FROM projects WHERE id=:project", {"project": project_id})[0].get("graph_fingerprint")
    batches = GraphTransactions(store, project_id)
    try:
        # Not built until the last commit: a repair of several transactions must not read as built part-way through.
        batches.tx.command("UPDATE projects SET graph_fingerprint=null WHERE id=:project", {"project": project_id})
        twice = _copies(batches.tx, "nodes", project_id)
        survivor = {rid: rids[0] for rids in twice.values() for rid in rids}  # each copy: the one that stays
        extra = [rid for rids in twice.values() for rid in rids[1:]]
        lost = {edge["id"]: edge for edge in _edges_of(batches.tx, extra)}  # the edges the engine takes with the extra vertices
        for offset in range(0, len(extra), 500):
            part = extra[offset:offset + 500]
            batches.tx.command("DELETE FROM nodes WHERE project_id=:project AND @rid IN :rids", {"project": project_id, "rids": part})
            batches.wrote(len(part))
        for row in written:
            if row["id"] in twice:
                store.write_node_at(batches.tx, twice[row["id"]][0], row)
                batches.wrote(1)
        held = {edge["id"] for edge in _edges_of(batches.tx, sorted(set(survivor.values())))}
        for identifier, edge in lost.items():
            if identifier not in held:
                ends = [survivor.get(str(edge[side]), str(edge[side])) for side in ("out_rid", "in_rid")]
                row = {key: value for key, value in edge.items() if not key.startswith("@") and key not in {"in", "out", "in_rid", "out_rid"}}
                store.write_edge_between(batches.tx, ends[0], ends[1], row)
                batches.wrote(1)
        extra_edges = [rid for rids in _copies(batches.tx, "edges", project_id).values() for rid in rids[1:]]
        for offset in range(0, len(extra_edges), 500):
            part = extra_edges[offset:offset + 500]
            batches.tx.command("DELETE FROM edges WHERE project_id=:project AND @rid IN :rids", {"project": project_id, "rids": part})
            batches.wrote(len(part))
        batches.commit()
        if left := surplus(store, project_id) + surplus(store, project_id, "edges"):
            raise RuntimeError(f"{left} graph rows of the project are still stored twice after they were removed")
        with store.transaction() as tx:  # whole again: the mark of the pass that wrote the graph is put back
            tx.command("UPDATE projects SET graph_fingerprint=:fingerprint WHERE id=:project", {"project": project_id, "fingerprint": marked})
    except BaseException:
        batches.abandon()
        with store.transaction() as tx:
            tx.command("UPDATE projects SET graph_fingerprint=null WHERE id=:project", {"project": project_id})
        raise
    return len(extra), len(extra_edges)
