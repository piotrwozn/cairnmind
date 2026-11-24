"""Two vertices of one id (#456): noticed after a graph write, removed with their edges put back, and said.

The engine's unique index of ``nodes.id`` is what keeps an id once, and what the graph write asks to know whether a
vertex exists (``Store._checked_node``). On one long-used database in production mode a pass stored a ``source:``
vertex twice, with no refusal: the index that lags behind (#377, #433) can let a second vertex of an id through as
well as refuse one. So the unique index is not trusted for it: after a pass that wrote vertices, the project's rows and
its distinct ids are counted by the project's own index, which does not lag with it, and a difference is repaired.

Repair keeps the first stored copy of an id (the lowest record id), deletes the others (the engine takes their edges
with them), and in the same transactions of the graph write (``GraphTransactions``) writes on the copy that stays what
this pass computed for the id (the copy that stays may be the older one, which the pass's write missed) and the edges
the deleted copies held. It then counts again: a store that still holds one fails the pass by name, with the graph
marked as not built.
"""
from __future__ import annotations

# The count is named, not inflected: "1 vertices" and "1 wierzchołków" are wrong where one was seen every time.
REPAIRED = ("Graph vertices of project {project} that the database stored twice under one id: {count}. The extra ones "
            "were removed, the edges they held were written again, and the graph was counted again.")
IN_STORE = ("Vertices of the store that share an id with another one: {count}. Indexing a project whose code graph is "
            "written again removes the extra ones of that project.")
MESSAGES = {
    REPAIRED: ("Wierzchołki grafu projektu {project}, które baza zapisała dwa razy pod jednym id: {count}. Nadmiarowe "
               "usunięto, krawędzie, które trzymały, zapisano ponownie, a graf policzono jeszcze raz."),
    IN_STORE: ("Wierzchołki magazynu, które mają to samo id co inny: {count}. Indeksowanie projektu, którego graf kodu "
               "jest zapisywany ponownie, usuwa nadmiarowe wierzchołki tego projektu."),
}


def surplus(reader, project_id: str | None = None) -> int:
    """How many vertices of the project (of the whole store without one) share an id with another: rows minus ids.

    One request that counts the rows and the distinct ids, by the project's index of ``project_id`` or, without
    one, by a scan of the type: neither is the unique index of ``id``, which may have let the second one in."""
    where, params = ("WHERE project_id=:project", {"project": project_id}) if project_id else ("", None)
    row = reader.query(f"SELECT count(*) AS n, count(DISTINCT id) AS d FROM nodes {where}".strip(), params)[0]
    return int(row.get("n") or 0) - int(row.get("d") or 0)


def _position(rid: str) -> tuple[int, int]:
    bucket, _, position = str(rid).lstrip("#").partition(":")
    return int(bucket), int(position)


def repair(store, project_id: str, written=()) -> int:
    """Remove the extra vertices of an id in the project; return how many were removed (0: there were none).

    `written` are the vertex rows this pass computed and wrote: those of the duplicated ids are written again on the
    copy that stays, so the graph is what a full write gives. A repair that does not end whole leaves the code graph
    marked as not built."""
    from .graph_transactions import GraphTransactions
    if not surplus(store, project_id):
        return 0
    batches = GraphTransactions(store, project_id)
    try:
        # Read inside the transaction that deletes, and the edges from the extra vertices themselves, not by an index.
        copies: dict[str, list[str]] = {}
        for row in batches.tx.query("SELECT @rid AS rid,id FROM nodes WHERE project_id=:project", {"project": project_id}):
            copies.setdefault(row["id"], []).append(str(row["rid"]))
        extra = [rid for rids in copies.values() if len(rids) > 1 for rid in sorted(rids, key=_position)[1:]]
        named = {identifier for identifier, rids in copies.items() if len(rids) > 1}
        lost: dict[str, dict] = {}  # the edges the engine takes with the extra vertices
        for offset in range(0, len(extra), 500):
            for edge in batches.tx.query("SELECT expand(bothE()) FROM nodes WHERE @rid IN :rids", {"rids": extra[offset:offset + 500]}):
                lost[edge["id"]] = {key: value for key, value in edge.items() if not key.startswith("@") and key not in {"in", "out"}}
        for offset in range(0, len(extra), 500):
            part = extra[offset:offset + 500]
            batches.tx.command("DELETE FROM nodes WHERE project_id=:project AND @rid IN :rids", {"project": project_id, "rids": part})
            batches.wrote(len(part))
        again = [row for row in written if row["id"] in named]
        edges = list(lost.values())
        for offset in range(0, max(len(again), len(edges)), 500):
            nodes, part = again[offset:offset + 500], edges[offset:offset + 500]
            store.prefetch_graph(batches.tx, nodes, part)
            store.write_graph(batches.tx, project_id, nodes, part)
            batches.wrote(len(nodes) + len(part))
        batches.commit()
        if left := surplus(store, project_id):
            raise RuntimeError(f"{left} graph vertices of the project are still stored twice after they were removed")
    except BaseException:
        batches.abandon()
        with store.transaction() as tx:
            tx.command("UPDATE projects SET graph_fingerprint=null WHERE id=:project", {"project": project_id})
        raise
    return len(extra)
