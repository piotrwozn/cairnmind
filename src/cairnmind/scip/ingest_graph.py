"""Import resolution, graph part construction, verification, and transactional graph persistence."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from cairnmind.scip.code_graph import code_graph_pending
from cairnmind.scip.ingest_walk import _identifier, _json, _metadata
from cairnmind.scip.parsing import parser_identity
from cairnmind.storage import graph_transactions
from cairnmind.storage.graph_transactions import GraphTransactions, await_vertices
from cairnmind.storage.storage_query import (
    col,
    db_count,
    db_find,
    db_find_one,
    db_update_where,
    eq,
    is_in,
)

GRAPH_EDGE_KINDS = ("imports", "defines", "calls", "references", "test_imports", "snapshot_of")
GRAPH_NODE_KINDS = ("file", "symbol", "external_module", "unresolved_reference")


def _stored_analyses(store, source_ids: list[str], *, parser_identity_fn=None) -> dict[str, dict | None]:
    """The analysis stored with each source, or None where it is of another parser version or missing."""
    get_parser_id = parser_identity if parser_identity_fn is None else parser_identity_fn
    found: dict[str, dict | None] = {identifier: None for identifier in source_ids}
    for offset in range(0, len(source_ids), 500):
        for row in db_find(
            store,
            "sources",
            [is_in("id", source_ids[offset:offset + 500])],
            columns=("id", col("metadata.analysis", "analysis"), col("metadata.analysis_version", "version")),
        ):
            version = row.get("version")
            if ((version == get_parser_id() or (isinstance(version, str) and version.startswith("scip-v1:")))
                    and isinstance(row.get("analysis"), dict)):
                found[row["id"]] = row["analysis"]
    return found


def _graph_markers(store, project_id: str) -> dict:
    """What the graph markers of the project say, for a run that left the graph as it was."""
    marker = db_find_one(store, "projects", {"id": project_id}, columns=("graph_nodes", "graph_edges"))
    return {"nodes": int((marker.get("graph_nodes") if marker else 0) or 0),
            "edges": int((marker.get("graph_edges") if marker else 0) or 0), "graph_unchanged": True}


def _resolve_import(path: Path, root: Path, module: str, indexed: dict[Path, dict]) -> Path | None:
    if not module:
        return None
    if path.suffix in {".py", ".pyi"}:
        dots = len(module) - len(module.lstrip("."))
        if dots:
            base = path.parent
            for _ in range(dots - 1):
                base = base.parent
            target = base / module[dots:].replace(".", "/")
        else:
            target = root / module.replace(".", "/")
        candidates = [target.with_suffix(".py"), target / "__init__.py"]
    else:
        if not module.startswith("."):
            return None  # Package aliases need configuration, not a guess.
        target = path.parent / module
        candidates = [target]
        for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            candidates.extend([Path(str(target) + suffix), target / f"index{suffix}"])
        # TypeScript often names emitted .js while the source is .ts.
        if target.suffix == ".js":
            candidates.extend([target.with_suffix(".ts"), target.with_suffix(".tsx")])

    def reached(candidate: Path) -> Path | None:
        try:
            return candidate.resolve()
        except (RuntimeError, OSError):  # a loop of symlinks leads nowhere: the import stays unresolved (#451)
            return None

    return next((found for found in map(reached, candidates) if found in indexed), None)


def _node_digest(item: dict) -> bytes:
    return hashlib.blake2b(_json(item).encode(), digest_size=16).digest()


def _graph_as_remembered(tx, project_id: str, remembered: dict | None) -> bool:
    """Whether the graph in the database is still the one this process wrote last."""
    if not remembered:
        return False
    marker = db_find_one(
        tx,
        "projects",
        {"id": project_id},
        columns=("graph_fingerprint", "graph_nodes", "graph_edges"),
    )
    if not marker or marker.get("graph_fingerprint") != remembered["fingerprint"]:
        return False
    if (int(marker.get("graph_nodes") or 0), int(marker.get("graph_edges") or 0)) != remembered["counts"]:
        return False
    if db_count(tx, "edges", [eq("project_id", project_id), is_in("kind", GRAPH_EDGE_KINDS)]) != remembered["counts"][1]:
        return False
    # The nodes that are not evidence nodes: counted, not read one by one, to know that none was added or taken (#426).
    return (
        db_count(tx, "nodes", [eq("project_id", project_id), is_in("kind", GRAPH_NODE_KINDS)])
        == remembered["code_nodes"]
    )


def _code_nodes(digests: dict) -> int:
    """The nodes of a graph that `_build_graph` may delete: every node but the evidence nodes of the sources."""
    return sum(1 for identifier in digests if not identifier.startswith("source:"))


def _union(parts: dict) -> tuple[dict[str, bytes], set[str]]:
    """The digest of every node and the id of every edge of a graph, from its parts."""
    nodes: dict[str, bytes] = {}
    edges: set[str] = set()
    for part in parts.values():
        for digests, ids in (part["first"], part["second"]):
            nodes.update(digests)
            edges |= ids
    return nodes, edges


def _graph_parts(
    project_id: str,
    indexed: dict[Path, dict],
    reuse: dict,
    *,
    node_digest_fn=None,
) -> tuple[dict, dict, dict]:
    """The graph that follows from `indexed`: its parts, and the rows (nodes, edges) that were computed for them.

    A part is what one file adds: `first` is its own rows (the file, its source and its symbols), `second` the
    rows of its imports and calls, which look at the symbols of the files it imports; each is the digests of the
    nodes and the ids of the edges. `reuse` are the parts of the last graph of this process, for the very same
    files: then only a file whose source changed is computed again, and the files that import a file whose
    symbols changed (a name, or a line, is in the id of a symbol). Resolving an import looks only at which files
    exist, and these are the same. The parts, and so the graph, are those of a computation of every file.
    """
    digest_node = _node_digest if node_digest_fn is None else node_digest_fn
    tables: dict[Path, dict[str, list[dict]]] = {}
    firsts: dict[Path, tuple[dict, dict]] = {}

    def emitters(nodes: dict, edges: dict):
        def node(kind: str, label: str, source_id: str | None, metadata: dict, identity: str | None = None) -> str:
            identifier = _identifier(project_id, kind, identity or label)
            nodes[identifier] = {"id": identifier, "project_id": project_id, "kind": kind,
                                 "label": label, "source_id": source_id, "metadata": metadata}
            return identifier

        def edge(first: str, second: str, kind: str, source_id: str, line: int = 0):
            identifier = _identifier(project_id, first, second, kind, source_id, line)
            edges[identifier] = {"id": identifier, "project_id": project_id, "from_id": first, "to_id": second,
                                 "kind": kind, "source_id": source_id, "status": "observed"}
        return node, edge

    def file_node(path: Path) -> str:
        return _identifier(project_id, "file", str(path))

    def first(path: Path) -> tuple[dict, dict]:
        if path not in firsts:
            entry = indexed[path]
            source = entry["source"]
            nodes, edges = {}, {}
            node, edge = emitters(nodes, edges)
            identifier = node("file", str(path), source["id"], {"path": str(path)}, str(path))
            # Store.remember uses exactly this evidence-node identity. Bridging the
            # immutable snapshot to the file node connects decision/task provenance
            # to real code dependencies without guessing a semantic relationship.
            source_node = f"source:{source['id']}"
            nodes[source_node] = {
                "id": source_node, "project_id": project_id, "kind": "source", "label": source["uri"],
                "source_id": source["id"], "metadata": {"source_id": source["id"], "source_kind": source["kind"]},
            }
            edge(source_node, identifier, "snapshot_of", source["id"])
            tables[path] = {}
            for item in entry["analysis"]["symbols"]:
                symbol = node("symbol", item["name"], source["id"], {**item, "path": str(path)},
                              f"{path}:{item['name']}:{item['line_start']}")
                tables[path].setdefault(item["name"], []).append({**item, "id": symbol})
                edge(identifier, symbol, "defines", source["id"], item["line_start"])
            firsts[path] = nodes, edges
        return firsts[path]

    resolved: dict[tuple, Path | None] = {}

    def lead(path: Path, root: Path, module: str) -> Path | None:
        # What an import leads to depends on the kind of file, the folder it starts from and the module only; a
        # standard module is looked for by many files.
        key = path.suffix in {".py", ".pyi"}, path.parent if module.startswith(".") else root, module
        if key not in resolved:
            resolved[key] = _resolve_import(path, root, module, indexed)
        return resolved[key]

    def leads_to(path: Path) -> tuple:
        entry = indexed[path]
        if entry["analysis"].get("source") == "scip":
            return tuple(
                Path(item["target_path"]) for item in entry["analysis"].get("resolved_imports", ())
                if Path(item["target_path"]) in indexed
            )
        return tuple(lead(path, entry["root"], item["module"]) for item in entry["analysis"]["imports"])

    def _scip_symbol_id(target_path: Path, name: str, line_start: int) -> str | None:
        if target_path not in indexed:
            return None
        first(target_path)
        for cand in tables[target_path].get(name, ()):
            if cand.get("line_start") == line_start:
                return cand["id"]
        return None

    def second(path: Path) -> tuple[dict, dict, set[Path]]:
        entry = indexed[path]
        source_id = entry["source"]["id"]
        nodes, edges, targets = {}, {}, set()
        node, edge = emitters(nodes, edges)
        first(path)
        symbols = tables[path]
        imported: dict[str, tuple[Path, str]] = {}
        namespace: dict[str, Path] = {}
        is_test = (
            path.name.startswith("test_")
            or bool(re.search(r"[._](test|spec)\.", path.name))
            or bool(re.search(r"(Tests?|Spec)\.(java|kt|kts|cs|php)$", path.name))
        )
        if entry["analysis"].get("source") == "scip":
            for item in entry["analysis"].get("resolved_imports", ()):
                target = Path(item["target_path"])
                if target not in indexed:
                    continue
                targets.add(target)
                target_id = file_node(target)
                edge(file_node(path), target_id, "imports", source_id, item["line"])
                if is_test:
                    edge(file_node(path), target_id, "test_imports", source_id, item["line"])
            for item in entry["analysis"].get("external_imports", ()):
                target_id = node("external_module", item["module"], None, {"unresolved": False, "source": "scip"})
                edge(file_node(path), target_id, "imports", source_id, item["line"])
            for edge_kind, key_name in (("calls", "resolved_calls"), ("references", "resolved_references")):
                for item in entry["analysis"].get(key_name, ()):
                    target_path = Path(item["target_path"])
                    target_sym = _scip_symbol_id(target_path, item["target_name"], item["target_line"])
                    if target_sym is None:
                        continue
                    if target_path != path:
                        targets.add(target_path)
                    owner_id = file_node(path)
                    if item.get("owner_name") and item.get("owner_line"):
                        resolved_owner = _scip_symbol_id(path, item["owner_name"], item["owner_line"])
                        if resolved_owner is not None:
                            owner_id = resolved_owner
                    edge(owner_id, target_sym, edge_kind, source_id, item["line"])
            return nodes, edges, targets
        for item in entry["analysis"]["imports"]:
            module = item["module"]
            target = lead(path, entry["root"], module)
            if target is None:
                target_id = node("external_module", module, None, {"unresolved": True})
            else:
                targets.add(target)
                target_id = file_node(target)
                aliases = item.get("aliases", {})
                for name in item.get("names", []):
                    local = aliases.get(name, name)
                    if name in {module, "*"}:
                        namespace[local] = target
                    else:
                        imported[local] = (target, name)
            edge(file_node(path), target_id, "imports", source_id, item["line"])
            if is_test and target is not None:
                edge(file_node(path), target_id, "test_imports", source_id, item["line"])
        for item in entry["analysis"]["calls"]:
            label = item["name"]
            candidates = symbols.get(label, [])
            if label in imported:
                target, original = imported[label]
                first(target)
                candidates = tables[target].get(original, [])
            if "." in label:
                head, last = label.rsplit(".", 1)
                if head in namespace:
                    first(namespace[head])
                    candidates = tables[namespace[head]].get(last, [])
            # A call in a parameter's default belongs to the function, not to the parameter.
            owner_candidates = [
                symbol for group in symbols.values() for symbol in group
                if symbol["line_start"] <= item["line"] <= symbol["line_end"] and symbol.get("kind") != "parameter"
            ]
            owner = min(owner_candidates, key=lambda s: s["line_end"] - s["line_start"])["id"] if owner_candidates else file_node(path)
            if len(candidates) == 1:
                edge(owner, candidates[0]["id"], "calls", source_id, item["line"])
            else:
                target_id = node(
                    "unresolved_reference", label, source_id,
                    {"path": str(path), "line": item["line"], "unresolved": True}, f"{path}:{label}:{item['line']}",
                )
                edge(owner, target_id, "references", source_id, item["line"])
        return nodes, edges, targets

    def part(rows: tuple[dict, dict]) -> tuple[dict, set]:
        return {identifier: digest_node(item) for identifier, item in rows[0].items()}, set(rows[1])

    parts: dict[Path, dict] = dict(reuse)
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    keys = {
        path: (
            entry["source"]["id"],
            entry["source"]["uri"],
            entry["source"]["kind"],
            str(entry["root"]),
            entry["analysis"].get("scip_version", ""),
        )
        for path, entry in indexed.items()
    }
    changed = [path for path in indexed if path not in reuse or reuse[path]["key"] != keys[path]]
    moved: set[Path] = set()
    for path in changed:
        rows = first(path)
        # With multiplicity: two symbols of one id make a call ambiguous, and one does not.
        symbols = tuple(sorted(item["id"] for group in tables[path].values() for item in group))
        if not reuse or reuse[path]["symbols"] != symbols:
            moved.add(path)
        parts[path] = {"key": keys[path], "symbols": symbols, "first": part(rows)}
        nodes.update(rows[0])
        edges.update(rows[1])
    # Resolving an import follows a symlink, which the walk leaves out: where every import leads is asked again, and
    # a file whose imports lead elsewhere is computed again though no file changed.
    for path in set(changed) | {path for path, old in reuse.items() if old["deps"] & moved or old["leads"] != leads_to(path)}:
        second_nodes, second_edges, targets = second(path)
        parts[path] = {
            **parts[path],
            "deps": frozenset(targets),
            "leads": leads_to(path),
            "second": part((second_nodes, second_edges)),
        }
        nodes.update(second_nodes)
        edges.update(second_edges)
    return parts, nodes, edges


def _stored_graph_is(store, project_id: str, nodes: dict[str, dict], edges: dict[str, dict]) -> bool:
    """Whether the database holds these graph rows as a full replace would write them, read row by row.

    The fingerprint does not cover everything the graph follows from (the root a file was
    reached through steers import resolution), so a computed graph is remembered as the
    stored one only after it was compared with what is stored.
    """
    stored_edges = db_find(
        store,
        "edges",
        [eq("project_id", project_id), is_in("kind", GRAPH_EDGE_KINDS)],
        columns=("id", "from_id", "to_id", "kind", "source_id", "status", "is_current"),
    )
    # Every computed row belongs to a current source or to none, so a full replace writes it as current.
    if len(stored_edges) != len(edges) or any(
            row["id"] not in edges or row.get("is_current") is not True
            or any(row.get(key) != edges[row["id"]][key] for key in ("from_id", "to_id", "kind", "source_id", "status"))
            for row in stored_edges):
        return False
    stored_nodes = {
        row["id"]: row
        for row in db_find(
            store,
            "nodes",
            [eq("project_id", project_id), is_in("kind", (*GRAPH_NODE_KINDS, "source"))],
            columns=("id", "kind", "label", "source_id", "metadata", "is_current"),
        )
    }
    return all(
        identifier in stored_nodes and stored_nodes[identifier].get("is_current") is True
        and stored_nodes[identifier].get("kind") == item["kind"] and stored_nodes[identifier].get("label") == item["label"]
        and stored_nodes[identifier].get("source_id") == item["source_id"]
        and _metadata(stored_nodes[identifier].get("metadata")) == item["metadata"]
        for identifier, item in nodes.items())


def _build_graph(
    store,
    project_id: str,
    indexed: dict[Path, dict],
    remembered: dict | None = None,
    state: dict | None = None,
    retried: bool = False,
    links: tuple = (),
    *,
    parser_identity_fn=None,
    await_vertices_fn=None,
    graph_parts_fn=None,
    stored_graph_is_fn=None,
    build_graph_fn=None,
    time_mod=None,
) -> dict:
    """Write the graph that follows from `indexed`.

    With `remembered`, the graph this process wrote last, only rows that differ are written,
    provided the database still holds exactly that graph. Otherwise every row is replaced.
    `state` receives what was written, for the next call. `links` are the symlinks the walk saw and where they
    lead: resolving an import follows them, and the fingerprint, which holds only files, does not (#451).
    """
    get_parser_id = parser_identity if parser_identity_fn is None else parser_identity_fn
    wait_vertices = await_vertices if await_vertices_fn is None else await_vertices_fn
    compute_parts = _graph_parts if graph_parts_fn is None else graph_parts_fn
    check_stored = _stored_graph_is if stored_graph_is_fn is None else stored_graph_is_fn
    clock = time if time_mod is None else time_mod

    fingerprint = _identifier(get_parser_id(), *[
        f"{path}:{indexed[path]['source']['id']}"
        + (f":{indexed[path]['analysis']['scip_version']}" if indexed[path]["analysis"].get("scip_version") else "")
        for path in sorted(indexed)
    ])
    marker = db_find_one(
        store,
        "projects",
        {"id": project_id},
        columns=("graph_fingerprint", "graph_nodes", "graph_edges"),
    )
    unchanged = marker is not None and marker.get("graph_fingerprint") == fingerprint
    ours = unchanged and remembered is not None and remembered["fingerprint"] == fingerprint  # the stored graph is this process's
    if unchanged and marker is not None and (state is None or (ours and remembered is not None and remembered.get("links", ()) == links)):
        if state is not None and remembered is not None:
            state.update(remembered)
        return {"nodes": int(marker.get("graph_nodes") or 0),
                "edges": int(marker.get("graph_edges") or 0), "graph_unchanged": True}
    # The last graph of this process is the start only for the very same files, and when the stored fingerprint
    # is not already ours (a graph that another writer made is compared row by row below).
    last = remembered or {}
    reuse = last.get("parts") or {}
    if (unchanged and not ours) or last.get("parser") != get_parser_id() or reuse.keys() != indexed.keys():
        reuse = {}
    parts, nodes, edges = compute_parts(project_id, indexed, reuse)
    digests, edge_ids = _union(parts)
    written = {"fingerprint": fingerprint, "parser": get_parser_id(), "parts": parts, "links": links,
               "counts": (len(digests), len(edge_ids)), "code_nodes": _code_nodes(digests)}
    # Not for a graph that is ours and only a link moved: the write below checks it against the remembered one
    # by counts, and writes the rows that differ, as after a changed file.
    if unchanged and marker is not None and not ours and check_stored(store, project_id, nodes, edges):
        # The fingerprint said that the stored graph follows from these sources, and this
        # process has now seen that it does, row for row. Otherwise it is replaced in full below.
        if state is not None:
            state.update(written)
        return {"nodes": int(marker.get("graph_nodes") or 0),
                "edges": int(marker.get("graph_edges") or 0), "graph_unchanged": True}

    # One transaction for a graph that fits in one; else bounded ones, the fingerprint cleared first (graph_transactions.py).
    batches = GraphTransactions(store, project_id)
    try:
        tx = batches.tx
        if not db_find_one(tx, "projects", {"id": project_id}, columns=("id",)):
            raise ValueError("Project no longer exists; graph was not rebuilt")
        as_remembered = _graph_as_remembered(tx, project_id, remembered)  # reads the fingerprint: before it is cleared
        if reuse and not as_remembered:
            # Only the rows of the files that changed were computed, and the stored graph is not the remembered
            # one: the rows of every file are needed to compare with it.
            parts, nodes, edges = compute_parts(project_id, indexed, {})
            digests, edge_ids = _union(parts)
            written.update(parts=parts, counts=(len(digests), len(edge_ids)), code_nodes=_code_nodes(digests))
        db_update_where(tx, "projects", set_fields={"graph_fingerprint": None}, where={"id": project_id})
        if as_remembered and remembered is not None:
            before, kept = _union(remembered["parts"])
            removed = sorted(kept - edge_ids)
            changed_nodes = [item for identifier, item in nodes.items() if before.get(identifier) != digests[identifier]]
            # The stored nodes are those the process wrote (counted above): the ones that go are known, not read.
            doomed = sorted(identifier for identifier in before if identifier not in digests and not identifier.startswith("source:"))
            carried = remembered.get("kept", frozenset()) - digests.keys()  # kept before, still not computed
        else:
            doomed = None
            carried = frozenset()
            # An edge's id names its ends, kind, source and line: a stored edge with a computed id and the marks a
            # full replace gives it is that edge, and it stays. Deleting every edge and writing the same ids again
            # left the engine's unique index holding entries of the deleted ones from about 40,000 edges on, and
            # the write failed (#385). Only the stored edges that are not in the computed graph are deleted.
            stored = db_find(
                tx,
                "edges",
                [eq("project_id", project_id), is_in("kind", GRAPH_EDGE_KINDS)],
                columns=("id", "from_id", "to_id", "kind", "source_id", "status", "is_current"),
            )
            kept = {row["id"] for row in stored
                    if row["id"] in edges and row.get("is_current") is True
                    and all(row.get(key) == edges[row["id"]][key] for key in ("from_id", "to_id", "kind", "source_id", "status"))}
            removed = sorted(row["id"] for row in stored if row["id"] not in kept)
            changed_nodes = list(nodes.values())
        # A whole transaction's rows in one request: with the project in the condition the engine scans the project's
        # index once per request, so groups of 500 made the scans, and the removal's cost, 80 times more (#385). By id
        # alone it finds each edge in the index of its id (25 ms for 1,000 edges of 110,000, not 230, #426); the id
        # holds the project, and the count below fails the write when a group is not deleted in full.
        group = graph_transactions.ROWS_PER_TRANSACTION
        for offset in range(0, len(removed), group):
            part = removed[offset:offset + group]
            deleted = store.backend.delete_graph_edges(batches.tx, project_id, part)
            count = deleted[0].get("count") if isinstance(deleted, list) and deleted and isinstance(deleted[0], dict) else None
            if type(count) is int and count < len(part):
                # The engine's index of ids missed rows (569 of 20,000 after two commits of the same write, the way
                # it misses vertices committed a moment ago, graph_transactions.py): the project's index finds the rest.
                rest = store.backend.delete_graph_edges(batches.tx, project_id, part, kinds=GRAPH_EDGE_KINDS)
                more = rest[0].get("count") if isinstance(rest, list) and rest and isinstance(rest[0], dict) else None
                count = count + more if type(more) is int else None
            if type(count) is not int or count != len(part):
                raise RuntimeError("The database did not delete every graph edge of a group")
            batches.wrote(len(part))
        new_edges = [item for identifier, item in edges.items() if identifier not in kept]
        # The transaction reports the graph as reset, so its rows are written in groups (issue #167).
        # What the upserts look up row by row is read in a few requests per group; the checks stay theirs.
        step = min(1000, graph_transactions.ROWS_PER_TRANSACTION)
        for offset in range(0, len(changed_nodes), step):
            part = changed_nodes[offset:offset + step]
            store.prefetch_graph(batches.tx, part, [])
            store.write_graph(batches.tx, project_id, part, [])
            batches.wrote(len(part))
        if batches.commits and new_edges:  # the edges look their vertices up by the index of nodes.id
            batches.commit()
            wait_vertices(store, [item["id"] for item in changed_nodes])
            batches.open()
        for offset in range(0, len(new_edges), step):
            part = new_edges[offset:offset + step]
            store.prefetch_graph(batches.tx, [], part)
            store.write_graph(batches.tx, project_id, [], part)
            batches.wrote(len(part))
        tx = batches.tx
        if hasattr(tx, "known"):
            tx.known = None
        if doomed is None:
            previous_nodes = db_find(
                tx,
                "nodes",
                [eq("project_id", project_id), is_in("kind", GRAPH_NODE_KINDS)],
                columns=("id",),
            )
            doomed = [row["id"] for row in previous_nodes if row["id"] not in digests]
        # Knowledge-record relationships referencing a removed code node must not silently
        # become dangling: such a node is retained. The rest go in groups (issue #167).
        kept_nodes = set(carried)
        for offset in range(0, len(doomed), graph_transactions.ROWS_PER_TRANSACTION):
            kept_nodes.update(store.remove_graph_nodes(batches.tx, project_id, doomed[offset:offset + graph_transactions.ROWS_PER_TRANSACTION]) or ())
            batches.wrote(len(doomed[offset:offset + graph_transactions.ROWS_PER_TRANSACTION]))
        # What the store holds is what was computed and what an edge keeps: the next pass counts exactly that.
        written.update(kept=frozenset(kept_nodes), code_nodes=_code_nodes(digests) + len(kept_nodes))
        # This marker commits with the last of the vertex/edge replacement, and until it does the graph is not built.
        # An interrupted/failed graph write can never claim completion.
        db_update_where(
            batches.tx,
            "projects",
            set_fields={"graph_fingerprint": fingerprint, "graph_nodes": len(digests), "graph_edges": len(edge_ids)},
            where={"id": project_id},
        )
        batches.commit()
    except BaseException as exc:
        batches.abandon()
        if not graph_transactions.refused_by_id_index(exc):
            raise
        # The engine's index still holds the id of a vertex that an earlier pass deleted (a directory renamed and
        # renamed back, #433) and the index cannot be asked about it: the commit was refused, or an edge found no
        # endpoint, and the transaction was rolled back. The graph is written once more after a wait (the fingerprint
        # is cleared if an earlier transaction committed, so it is the whole write); a second refusal fails by name.
        if retried:
            row = db_find_one(store, "projects", {"id": project_id}, columns=("graph_fingerprint",))
            # Only a write of several transactions has committed anything: one that fits in one leaves the previous graph.
            left = ("the code graph is marked not built" if not row or code_graph_pending(row) else
                    "the previous code graph is kept, though it no longer matches the files")
            raise RuntimeError(f"The database's index of vertex ids still refused a graph vertex whose id was deleted "
                               f"shortly before, {graph_transactions.DUPLICATE_WAIT:g} s after the first refusal ({str(exc)[:300]}); "
                               f"{left} and the next indexing writes the graph again") from exc
        clock.sleep(graph_transactions.DUPLICATE_WAIT)
        if build_graph_fn is not None:
            retry_result = build_graph_fn(store, project_id, indexed, remembered, state, True, links)
        else:
            retry_result = _build_graph(
                store, project_id, indexed, remembered, state, True, links,
                parser_identity_fn=parser_identity_fn,
                await_vertices_fn=await_vertices_fn,
                graph_parts_fn=graph_parts_fn,
                stored_graph_is_fn=stored_graph_is_fn,
                time_mod=time_mod,
            )
        return {**retry_result, "graph_written_again": True}
    if state is not None:
        state.update(written)
    return {"nodes": len(digests), "edges": len(edge_ids), "graph_unchanged": False,
            "graph_written": {"nodes": len(changed_nodes), "edges": len(new_edges)}}
