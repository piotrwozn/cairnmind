"""Incremental, source-preserving ingestion of project files and CLI histories.

The archive is independent of inference. Graph relationships here come from
syntax, never from filename resemblance or generated answers.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pathspec

from devbrain.parsing import analyze_code, parser_identity
from devbrain.arcade import ArcadeError
from devbrain.memory_paths import bound_metadata, imported_source_ids, is_memory_path, memory_bindings

MAX_FILE_BYTES = 2 * 1024 * 1024
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt", "dist", "build",
    "target", "vendor", ".idea", ".devbrain", ".terraform", ".ssh", ".aws", ".azure", ".gnupg",
    ".codex", ".claude",
}
SECRET_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".sqlite3", ".db"}
GRAPH_EDGE_KINDS = ("imports", "defines", "calls", "references", "test_imports", "snapshot_of")
GRAPH_NODE_KINDS = ("file", "symbol", "external_module", "unresolved_reference")
TRANSCRIPT_PARSER_REVISION = 2
UNKNOWN_TRANSCRIPT_WARNING = "Unknown transcript schema; raw record retained"
CODEX_TECHNICAL_RECORDS = {"world_state", "token_usage_record"}
_CLAUDE_TECHNICAL_FIELDS = {
    "mode": {"mode": str},
    "permission-mode": {"permissionMode": str},
    "session-owner": {"accountUuid": str, "organizationUuid": str},
    "atis-latch": {"atis": str},
    "attachment": {"parentUuid": (type(None), str), "isSidechain": bool, "attachment": dict,
                   "uuid": str, "timestamp": str, "userType": str, "entrypoint": str,
                   "cwd": str, "version": str, "gitBranch": str},
    "api-request-shape": {"shapeHash": str, "timestamp": str, "version": str, "shape": dict},
    "api-request": {"id": str, "timestamp": str, "version": str, "querySource": str,
                    "shapeHash": str, "params": dict, "keep": int, "tail": list,
                    "cacheControl": list, "requestDigest": str},
    "last-prompt": {"lastPrompt": str, "leafUuid": str},
    "cost-state": {"totalCostUSD": (int, float), "totalAPIDuration": int,
                   "totalAPIDurationWithoutRetries": int, "totalToolDuration": int,
                   "totalLinesAdded": int, "totalLinesRemoved": int, "totalDuration": int,
                   "startTime": int, "modelUsage": dict, "hasUnknownModelCost": bool},
    "ai-title": {"aiTitle": str},
    "tools-used": {"tools": list},
}
CLAUDE_TECHNICAL_RECORDS = set(_CLAUDE_TECHNICAL_FIELDS)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _metadata(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _identifier(*parts: Any) -> str:
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()


@contextmanager
def _ingestion_lock(store, category: str, identity: str, *, deadline: float | None = None):
    """Serialize whole ingest operations across workers and CLI processes.

    Native database transactions protect writes, but cannot prevent an older
    filesystem scan from publishing its source snapshot after a newer scan.
    These advisory locks cover that read/parse/write operation as one unit.
    """
    folder = store.data_dir / "locks"
    folder.mkdir(exist_ok=True, mode=0o700)
    filename = folder / f"{category}-{_identifier(identity)}.lock"
    descriptor = os.open(filename, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if deadline is None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Transcript reparse deadline exceeded")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=15,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=False,
        )
        return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _is_secret(path: Path) -> bool:
    lower = path.name.lower()
    return (
        lower in SECRET_NAMES or lower.startswith(".env.") or lower.endswith(".credentials.json")
        or path.suffix.lower() in SECRET_SUFFIXES
    )


def _roots(store, project: dict) -> list[Path]:
    rows = store.query("SELECT path FROM project_roots WHERE project_id=:project", {"project": project["id"]})
    return list(dict.fromkeys([Path(project["root"]).resolve(), *[Path(r["path"]).resolve() for r in rows]]))


def _files(root: Path, excluded: tuple[Path, ...] = ()):
    """Walk with nested gitignore rules. Symlinks are deliberately not followed."""
    rules: dict[Path, pathspec.GitIgnoreSpec] = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        folder = Path(directory)
        if is_memory_path(folder, root) or any(folder.is_relative_to(base) for base in excluded):
            dirs[:] = []
            continue
        ignore = folder / ".gitignore"
        if ignore.is_file() and not ignore.is_symlink():
            try:
                rules[folder] = pathspec.GitIgnoreSpec.from_lines(ignore.read_text(errors="replace").splitlines())
            except OSError:
                pass

        def ignored(item: Path, is_dir: bool = False) -> bool:
            answer = False
            for base, spec in sorted(rules.items(), key=lambda row: len(row[0].parts)):
                if item.is_relative_to(base):
                    relative = item.relative_to(base).as_posix() + ("/" if is_dir else "")
                    checked = spec.check_file(relative)
                    if checked.include is not None:
                        answer = checked.include
            return answer

        dirs[:] = sorted(
            name for name in dirs
            if name not in EXCLUDED_DIRS and not (folder / name).is_symlink()
            and not is_memory_path(folder / name, root)
            and not any((folder / name).is_relative_to(base) for base in excluded)
            and not ignored(folder / name, True)
        )
        for name in sorted(names):
            path = folder / name
            if path.is_symlink() or _is_secret(path) or ignored(path):
                continue
            yield path


def _chunks(text: str):
    lines = text.splitlines()
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines) and end - start < 80:
            length = len(lines[end]) + 1
            if end > start and size + length > 6000:
                break
            size += length
            end += 1
        if end == start:
            end += 1
        content = "\n".join(lines[start:end])
        # A minified line still remains fully archived and searchable in pieces.
        if len(content) > 6000:
            for offset in range(0, len(content), 6000):
                yield content[offset:offset + 6000], start + 1, end
        else:
            yield content, start + 1, end
        start = max(start + 1, end - 12) if end < len(lines) else end


def _write_chunks(store, project_id: str, source_id: str, text: str, metadata: dict | None = None) -> int:
    with _ingestion_lock(store, "chunks", source_id):
        with store.transaction() as tx:
            sources = tx.query("SELECT project_id,is_current FROM sources WHERE id=:source", {"source": source_id})
            if not sources or sources[0]["project_id"] != project_id:
                raise ValueError("Chunk source does not belong to the requested project")
            existing = tx.query("SELECT id FROM chunks WHERE source_id=:source", {"source": source_id})
            ids = []
            if not existing:
                for number, (content, first, last) in enumerate(_chunks(text)):
                    chunk_id = _identifier(project_id, source_id, number, first, last, content)
                    tx.command(
                        "INSERT INTO chunks SET id=:id,project_id=:project,source_id=:source,text=:text,"
                        "line_start=:first,line_end=:last,metadata=:metadata,is_current=:current",
                        {"id": chunk_id, "project": project_id, "source": source_id, "text": content,
                         "first": first, "last": last, "metadata": metadata or {}, "current": sources[0]["is_current"]},
                    )
                    ids.append(chunk_id)
            else:
                ids = [row["id"] for row in existing]
    for chunk_id in ids:
        # Replaying this is intentional: a crash after chunk commit but before
        # enqueue must not leave permanently unindexed evidence.
        store.enqueue("embed", project_id, {"chunk_id": chunk_id}, f"embed:{chunk_id}")
    return 0 if existing else len(ids)


def _source_before(store, project_id: str, uri: str):
    rows = store.query(
        "SELECT FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
        {"project": project_id, "uri": uri},
    )
    return rows[0] if rows else None


def _deactivate_source(store, source_id: str) -> None:
    with store.transaction() as tx:
        tx.command("UPDATE sources SET is_current=false,current_key=null WHERE id=:source", {"source": source_id})
        tx.command("UPDATE chunks SET is_current=false WHERE source_id=:source", {"source": source_id})
        tx.command(
            "UPDATE nodes SET is_current=false WHERE source_id=:source "
            "AND kind NOT IN ['source','decision','task','fact','constraint','question','preference','checkpoint']",
            {"source": source_id},
        )
        tx.command(
            "UPDATE edges SET is_current=false WHERE source_id=:source "
            "AND kind NOT IN ['supported_by','depends_on','supersedes']",
            {"source": source_id},
        )
        store._delete_old_vectors(tx, [source_id])


def index_pending_sources(store, limit: int = 100) -> dict:
    """Index archived memory/evidence sources created outside file importers.

    CLI notes, verification evidence and checkpoints are already literal source
    snapshots; their content must participate in semantic retrieval too. This
    does not read their provenance paths or extract more facts from summaries.
    Unknown custom source kinds are eligible only when they explicitly provide
    a nonempty normalized text view and are not files/transcripts/generated
    answers. Unassigned inbox data never enters a project's search index.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("Pending source batch limit must be between 1 and 1000")
    known_kinds = ("manual", "checkpoint", "verification", "test_result", "plan")
    excluded_kinds = ("code", "file", "transcript", "transcript_unknown", "agent_answer", "generated_answer")
    rows = store.query(
        "SELECT FROM sources WHERE is_current=true AND project_id IS NOT NULL "
        "AND kind NOT IN :excluded AND (kind IN :known OR metadata.path IS NULL) "
        "AND id NOT IN (SELECT source_id FROM chunks) ORDER BY created_at,id",
        {"excluded": list(excluded_kinds), "known": list(known_kinds)},
    )
    counts = {"sources": 0, "chunks": 0, "errors": []}
    for row in rows:
        metadata = _metadata(row["metadata"])
        normalized = metadata.get("normalized_text")
        text = normalized if isinstance(normalized, str) and normalized.strip() else row["content"]
        if row["kind"] not in known_kinds and (
            not isinstance(normalized, str) or not normalized.strip() or metadata.get("derived")
        ):
            continue
        if not text.strip():
            continue
        try:
            added = _write_chunks(store, row["project_id"], row["id"], text, {
                "source_kind": row["kind"], "role": metadata.get("role", "unknown"),
                "derived": row["kind"] == "checkpoint" or bool(metadata.get("derived")),
                "index_origin": "pending_sources",
            })
            counts["sources"] += 1
            counts["chunks"] += added
        except (OSError, ValueError, ArcadeError) as exc:
            counts["errors"].append(f"{row['id']}: {exc}")
        if counts["sources"] >= limit:
            break
    return counts


def index_project(store, project_id: str) -> dict:
    """Snapshot changed files, update chunks, and rebuild the derived code graph."""
    project_id = store.resolve_project(project_id)["id"]
    with _ingestion_lock(store, "project", project_id):
        return _index_project_locked(store, project_id)


def _index_project_locked(store, project_id: str) -> dict:
    project = store.resolve_project(project_id)
    project_id = project["id"]
    counts: dict[str, Any] = {"files": 0, "changed": 0, "unchanged": 0, "deleted": 0, "chunks": 0, "errors": []}
    indexed: dict[Path, dict] = {}
    registered_roots = _roots(store, project)
    for root in registered_roots:
        if not root.is_dir():
            counts["errors"].append(f"Project root unavailable: {root}")
            continue
        commit = _git(root, "rev-parse", "HEAD") or None
        for path in _files(root, excluded=(store.data_dir,)):
            try:
                if path.stat().st_size > MAX_FILE_BYTES or not path.is_file():
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw:
                    continue
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            except OSError as exc:
                counts["errors"].append(f"{path}: {exc}")
                continue
            counts["files"] += 1
            digest = hashlib.sha256(content.encode()).hexdigest()
            previous = _source_before(store, project_id, str(path))
            if previous and previous["sha256"] == digest:
                source = previous
                metadata = _metadata(source["metadata"])
                analysis = metadata.get("analysis") if metadata.get("analysis_version") == parser_identity() else None
                if not analysis:
                    analysis = analyze_code(path, content)
                    store.command(
                        "UPDATE sources SET metadata.analysis=:analysis,metadata.analysis_version=:version WHERE id=:source",
                        {"analysis": analysis, "version": parser_identity(), "source": source["id"]},
                    )
                counts["unchanged"] += 1
            else:
                analysis = analyze_code(path, content)
                metadata = {
                    "path": str(path), "root": str(root), "relative_path": path.relative_to(root).as_posix(),
                    "git_commit": commit, "content_hash": digest, "analysis": analysis,
                    "analysis_version": parser_identity(), "language": analysis["language"], "snapshot": "working_tree",
                }
                source = store.snapshot(
                    project_id, str(path), content,
                    kind="code" if analysis["language"] != "text" else "document", metadata=metadata,
                )
                counts["changed"] += 1
            counts["chunks"] += _write_chunks(store, project_id, source["id"], content, {"language": analysis["language"]})
            indexed[path.resolve()] = {"source": source, "analysis": analysis, "root": root}
            # Idempotent enqueue also repairs a crash between source write and
            # queue write. Code facts themselves are already represented by syntax.
            if path.suffix.lower() in {".md", ".mdx", ".rst", ".txt"}:
                store.enqueue("extract", project_id, {"source_id": source["id"]}, f"extract:{source['id']}")

    seen = {str(p) for p in indexed}
    rows = store.query(
        "SELECT id,uri,metadata FROM sources WHERE project_id=:project AND is_current=true AND kind IN ['code','document']",
        {"project": project_id},
    )
    for row in rows:
        metadata = _metadata(row["metadata"])
        path = metadata.get("path")
        if path and path not in seen:
            # Do not call a temporarily unavailable root a project-wide deletion.
            root = Path(metadata.get("root", project["root"]))
            if root.is_dir():
                _deactivate_source(store, row["id"])
                counts["deleted"] += 1
    counts.update(_build_graph(store, project_id, indexed))
    return counts


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
    return next((p.resolve() for p in candidates if p.resolve() in indexed), None)


def _build_graph(store, project_id: str, indexed: dict[Path, dict]) -> dict:
    fingerprint = _identifier(parser_identity(), *[
        f"{path}:{indexed[path]['source']['id']}" for path in sorted(indexed)
    ])
    markers = store.query(
        "SELECT graph_fingerprint,graph_nodes,graph_edges FROM projects WHERE id=:project",
        {"project": project_id},
    )
    if markers and markers[0].get("graph_fingerprint") == fingerprint:
        return {"nodes": int(markers[0].get("graph_nodes") or 0),
                "edges": int(markers[0].get("graph_edges") or 0), "graph_unchanged": True}
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    symbols: dict[Path, dict[str, list[dict]]] = {}

    def node(kind: str, label: str, source_id: str | None, metadata: dict, identity: str | None = None) -> str:
        identifier = _identifier(project_id, kind, identity or label)
        nodes[identifier] = {"id": identifier, "project_id": project_id, "kind": kind,
                             "label": label, "source_id": source_id, "metadata": metadata}
        return identifier

    def edge(first: str, second: str, kind: str, source_id: str, line: int = 0):
        identifier = _identifier(project_id, first, second, kind, source_id, line)
        edges[identifier] = {"id": identifier, "project_id": project_id, "from_id": first, "to_id": second,
                             "kind": kind, "source_id": source_id, "status": "observed"}

    file_nodes = {}
    for path, entry in indexed.items():
        source = entry["source"]
        file_nodes[path] = node("file", str(path), source["id"], {"path": str(path)}, str(path))
        # Store.remember uses exactly this evidence-node identity. Bridging the
        # immutable snapshot to the file node connects decision/task provenance
        # to real code dependencies without guessing a semantic relationship.
        source_node = f"source:{source['id']}"
        nodes[source_node] = {
            "id": source_node, "project_id": project_id, "kind": "source", "label": source["uri"],
            "source_id": source["id"], "metadata": {"source_id": source["id"], "source_kind": source["kind"]},
        }
        edge(source_node, file_nodes[path], "snapshot_of", source["id"])
        symbols[path] = {}
        for item in entry["analysis"]["symbols"]:
            identifier = node(
                "symbol", item["name"], source["id"], {**item, "path": str(path)},
                f"{path}:{item['name']}:{item['line_start']}",
            )
            symbols[path].setdefault(item["name"], []).append({**item, "id": identifier})
            edge(file_nodes[path], identifier, "defines", source["id"], item["line_start"])

    for path, entry in indexed.items():
        source_id = entry["source"]["id"]
        imported: dict[str, tuple[Path, str]] = {}
        namespace: dict[str, Path] = {}
        is_test = path.name.startswith("test_") or bool(re.search(r"[._](test|spec)\.", path.name))
        for item in entry["analysis"]["imports"]:
            module = item["module"]
            target = _resolve_import(path, entry["root"], module, indexed)
            if target is None:
                target_id = node("external_module", module, None, {"unresolved": True})
            else:
                target_id = file_nodes[target]
                aliases = item.get("aliases", {})
                for name in item.get("names", []):
                    local = aliases.get(name, name)
                    if name in {module, "*"}:
                        namespace[local] = target
                    else:
                        imported[local] = (target, name)
            edge(file_nodes[path], target_id, "imports", source_id, item["line"])
            if is_test and target is not None:
                edge(file_nodes[path], target_id, "test_imports", source_id, item["line"])
        for item in entry["analysis"]["calls"]:
            label = item["name"]
            candidates = symbols[path].get(label, [])
            if label in imported:
                target, original = imported[label]
                candidates = symbols[target].get(original, [])
            if "." in label:
                first, last = label.rsplit(".", 1)
                if first in namespace:
                    candidates = symbols[namespace[first]].get(last, [])
            owner_candidates = [
                symbol for group in symbols[path].values() for symbol in group
                if symbol["line_start"] <= item["line"] <= symbol["line_end"]
            ]
            owner = min(owner_candidates, key=lambda s: s["line_end"] - s["line_start"])["id"] if owner_candidates else file_nodes[path]
            if len(candidates) == 1:
                edge(owner, candidates[0]["id"], "calls", source_id, item["line"])
            else:
                target_id = node(
                    "unresolved_reference", label, source_id,
                    {"path": str(path), "line": item["line"], "unresolved": True}, f"{path}:{label}:{item['line']}",
                )
                edge(owner, target_id, "references", source_id, item["line"])

    with store.transaction() as tx:
        if not tx.query("SELECT id FROM projects WHERE id=:project", {"project": project_id}):
            raise ValueError("Project no longer exists; graph was not rebuilt")
        from .journal import Journal
        Journal.touch(tx, project_id, "graph", op="reset")
        tx.command(
            "DELETE FROM edges WHERE project_id=:project AND kind IN :kinds",
            {"project": project_id, "kinds": list(GRAPH_EDGE_KINDS)},
        )
        for item in nodes.values():
            store.upsert_node(item, tx=tx)
        for item in edges.values():
            store.upsert_edge(item, tx=tx)
        previous_nodes = tx.query(
            "SELECT id FROM nodes WHERE project_id=:project AND kind IN :kinds",
            {"project": project_id, "kinds": list(GRAPH_NODE_KINDS)},
        )
        for row in previous_nodes:
            if row["id"] not in nodes:
                # Knowledge-record relationships referencing a removed code node must
                # not silently become dangling. Retain the archived node in that case.
                linked = tx.query("SELECT id FROM edges WHERE from_id=:id OR to_id=:id LIMIT 1", {"id": row["id"]})
                if not linked:
                    tx.command("DELETE FROM nodes WHERE id=:id", {"id": row["id"]})
        # This marker commits atomically with the native vertex/edge replacement.
        # An interrupted/failed graph transaction can never claim completion.
        tx.command(
            "UPDATE projects SET graph_fingerprint=:fingerprint,graph_nodes=:nodes,graph_edges=:edges WHERE id=:project",
            {"fingerprint": fingerprint, "nodes": len(nodes), "edges": len(edges), "project": project_id},
        )
    return {"nodes": len(nodes), "edges": len(edges), "graph_unchanged": False}


def _save_cursor(store, path: Path, project_id: str | None, client: str, offset: int) -> None:
    with store.transaction() as tx:
        if project_id and not tx.query("SELECT id FROM projects WHERE id=:project", {"project": project_id}):
            raise ValueError("Project no longer exists; history cursor was not recreated")
        tx.command(
            "UPDATE import_cursors SET id=:id,path=:path,project_id=:project,client=:client,`offset`=:offset,"
            "fingerprint=:fingerprint,updated_at=:updated UPSERT WHERE path=:path",
            {"id": _identifier(path), "path": str(path), "project": project_id, "client": client,
             "offset": offset, "fingerprint": _fingerprint(path, offset), "updated": _now()},
        )


def _event_cwd(event: dict) -> str | None:
    value = event.get("cwd")
    if not value and isinstance(event.get("payload"), dict):
        value = event["payload"].get("cwd")
    return value if isinstance(value, str) else None


def _codex_technical_metadata(event: dict) -> bool:
    """Validate the two Codex 0.166 technical rollout payloads we understand.

    These payloads carry accounting/state metadata, never user decisions. Keep
    the original JSONL record rather than turning arbitrary state into text.
    Unknown fields follow serde's forward-compatible handling; required fields
    and their actual wire types must still match the official schema.
    """
    kind, payload = event.get("type"), event.get("payload")
    if not isinstance(payload, dict):
        return False
    if kind == "world_state":
        return type(payload.get("full")) is bool and isinstance(payload.get("state"), dict)
    if kind != "token_usage_record":
        return False
    for key in ("thread_id", "session_id"):
        if not isinstance(payload.get(key), str):
            return False
        try:
            UUID(payload[key])
        except ValueError:
            return False
    if not all(isinstance(payload.get(key), str) for key in ("turn_id", "root_turn_id", "response_id")):
        return False
    required = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    for name in ("usage", "turn_token_usage", "thread_token_usage"):
        usage = payload.get(name)
        if not isinstance(usage, dict):
            return False
        for key in (*required, *(("cache_write_input_tokens",) if "cache_write_input_tokens" in usage else ())):
            value = usage.get(key)
            if type(value) is not int or not -(2**63) <= value < 2**63:
                return False
        budget = usage.get("codex_rollout_budget_units")
        if budget is not None and (type(budget) not in {int, float}
                                   or (isinstance(budget, float) and not math.isfinite(budget))):
            return False
    return True


def _claude_technical_metadata(event: dict) -> bool:
    """Validate the observed Claude bookkeeping envelopes, retaining opaque raw JSON.

    These records are searchable source evidence, not independently authored
    user decisions. Required wire fields and their types are checked; extra
    fields and nested attachment/request/accounting payloads remain untouched.
    """
    kind = event.get("type")
    fields = _CLAUDE_TECHNICAL_FIELDS.get(kind) if isinstance(kind, str) else None
    if fields is None or not isinstance(event.get("sessionId"), str):
        return False
    for key, expected in fields.items():
        allowed = expected if isinstance(expected, tuple) else (expected,)
        if key not in event or type(event[key]) not in allowed:
            return False
        if type(event[key]) is float and not math.isfinite(event[key]):
            return False
    return True


def _technical_metadata_client(event: dict) -> str | None:
    if _codex_technical_metadata(event):
        return "codex"
    if _claude_technical_metadata(event):
        return "claude"
    return None


def _event_text(event: dict, client: str) -> tuple[str, str, str, bool]:
    """Return normalized text, role, detected client, and schema recognition."""
    kind = event.get("type", "")
    if not isinstance(kind, str):
        return "", "unknown", client, False
    payload = event.get("payload")
    if kind in CODEX_TECHNICAL_RECORDS:
        return ("", "metadata", "codex", True) if _codex_technical_metadata(event) else ("", "unknown", client, False)
    if kind in CLAUDE_TECHNICAL_RECORDS:
        return ("", "metadata", "claude", True) if _claude_technical_metadata(event) else ("", "unknown", client, False)
    if isinstance(payload, dict) and kind in {"session_meta", "turn_context"}:
        return "", "metadata", "codex", True
    if kind == "event_msg" and isinstance(payload, dict):
        subkind = payload.get("type")
        if subkind in {"user_message", "agent_message"}:
            return str(payload.get("message", "")), "user" if subkind == "user_message" else "assistant", "codex", True
        return "", "metadata", "codex", True
    message = event.get("message")
    if kind == "response_item" and isinstance(payload, dict):
        client = "codex"
        message = payload
    elif isinstance(message, dict) and (kind in {"user", "assistant"} or "sessionId" in event):
        client = "claude"
    elif "role" in event and "content" in event:
        message = event
    elif kind in {"progress", "system", "summary", "file-history-snapshot", "queue-operation"}:
        return str(event.get("summary", "")), "metadata", "claude" if client == "auto" else client, True
    if isinstance(message, dict):
        role = message.get("role", "tool" if message.get("type") in {"function_call", "function_call_output"} else "unknown")
        content = message.get("content", message.get("output", ""))
        if message.get("type") == "function_call":
            content = f"Tool: {message.get('name', '')}\n{message.get('arguments', '')}"
        if isinstance(content, list):
            blocks = []
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                # Claude represents tool responses inside role=user messages.
                # They are not decisions or instructions authored by the user.
                role = "tool"
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    blocks.append(block["text"])
                elif block.get("type") == "tool_result":
                    result = block.get("content", "")
                    blocks.append(result if isinstance(result, str) else _json(result))
                elif block.get("type") == "tool_use":
                    blocks.append(f"Tool: {block.get('name', '')}\n{_json(block.get('input', {}))}")
            content = "\n".join(blocks)
        return str(content) if content is not None else "", str(role), client, True
    return "", "unknown", client, False


def _fingerprint(path: Path, offset: int = 0) -> str:
    stat = path.stat()
    # A prefix and the last committed bytes detect in-place rewrites as well as
    # replacement/truncation, while remaining stable under ordinary appends.
    with path.open("rb") as handle:
        head = handle.readline(4096)
        handle.seek(max(0, offset - 256))
        tail = handle.read(min(256, offset))
    return f"{stat.st_dev}:{stat.st_ino}:{hashlib.sha256(head).hexdigest()}:{hashlib.sha256(tail).hexdigest()}"


def _source_project(store, requested: str | None, cwd: str | None) -> tuple[str | None, str | None]:
    inferred = None
    if cwd:
        try:
            inferred = store.resolve_project(cwd)["id"]
        except ValueError:
            pass
    if requested and cwd and requested != inferred:
        return None, (
            "Declared working directory belongs to another project" if inferred
            else "Declared working directory is outside the requested project's registered roots"
        )
    return requested or inferred, None


def reparse_history_sources(store, path: Path, project_id: str, client: str = "auto", limit: int = 1000,
                            *, deadline: float | None = None) -> dict:
    """Repair only previously unknown, now understood technical client records.

    The stored raw snapshot is the authority, including when its original file
    has gone away. This does not replay cursors, replace evidence, extract
    decisions, or change project attribution. Normal archive proof still has to
    independently establish the captured transcript horizon.
    """
    path = Path(path).expanduser().resolve()
    project_id = store.resolve_project(project_id)["id"]
    _validate_reparse_options(client, limit, deadline)
    with _ingestion_lock(store, "history", str(path), deadline=deadline):
        return _reparse_history_sources_locked(store, path, project_id, client, limit, deadline=deadline)


def _validate_reparse_options(client: str, limit: int, deadline: float | None) -> None:
    if client not in {"auto", "codex", "claude"}:
        raise ValueError("client must be auto, codex, or claude")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Transcript reparse limit must be between 1 and 1000")
    if deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)):
        raise ValueError("Transcript reparse deadline must be a finite monotonic timestamp")


def _reparse_history_sources_locked(store, path: Path, project_id: str | None, client: str = "auto",
                                    limit: int = 1000, *, deadline: float | None = None) -> dict:
    _validate_reparse_options(client, limit, deadline)
    counts = {"repaired": 0, "unchanged": 0, "errors": []}
    sources = store.query(
        "SELECT FROM sources WHERE is_current=true AND kind='transcript_unknown' "
        "AND metadata.import_path=:path AND metadata.schema_warning=:warning "
        + ("AND project_id=:project " if project_id else "")
        + "ORDER BY metadata.byte_offset,id LIMIT :limit",
        {"path": str(path), "project": project_id, "warning": UNKNOWN_TRANSCRIPT_WARNING, "limit": limit},
    )
    for source in sources:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Transcript reparse deadline exceeded")
        counts["unchanged"] += 1
        metadata = _metadata(source.get("metadata"))
        content = source.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            counts["errors"].append(f"Transcript source {source['id']}: raw record exceeds the reparse budget")
            continue
        try:
            event = json.loads(content)
        except ValueError:
            continue
        detected = _technical_metadata_client(event) if isinstance(event, dict) else None
        if detected is None:
            continue
        offset, session_id, original_client = (metadata.get("byte_offset"), metadata.get("session_id"),
                                                metadata.get("client"))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (source.get("sha256") != digest or not content.endswith("\n")
                or type(offset) is not int or offset < 0 or not isinstance(session_id, str) or not session_id
                or original_client not in {"auto", detected}
                or client not in {"auto", detected}
                or (detected == "claude" and event.get("sessionId") != session_id)
                or source.get("uri") != f"transcript://{original_client}/{_identifier(path, session_id)}/{offset}/{digest}"
                or metadata.get("role") != "unknown" or metadata.get("normalized_text") != ""):
            counts["errors"].append(f"Transcript source {source['id']}: original provenance or raw hash does not match")
            continue
        cwd, event_cwd = metadata.get("cwd"), _event_cwd(event)
        if ((cwd is not None and not isinstance(cwd, str)) or (event_cwd and event_cwd != cwd)
                or _source_project(store, source["project_id"], cwd)[0] != source["project_id"]):
            counts["errors"].append(f"Transcript source {source['id']}: working directory conflicts with its project")
            continue
        # Old unknown records were indexed as their raw JSON. Understood
        # metadata schemas have the same raw text view, so retain every chunk
        # identity and embedding. Only their derived role changes.
        expected = {
            _identifier(source["project_id"], source["id"], number, first, last, text): (text, first, last)
            for number, (text, first, last) in enumerate(_chunks(content))
        }
        with store.transaction() as tx:
            store._require_project(tx, source["project_id"])
            current = tx.query("SELECT FROM sources WHERE id=:id AND project_id=:project AND is_current=true",
                               {"id": source["id"], "project": source["project_id"]})
            if (not current or current[0].get("kind") != "transcript_unknown"
                    or current[0].get("sha256") != digest or current[0].get("content") != content
                    or _metadata(current[0].get("metadata")) != metadata):
                counts["errors"].append(f"Transcript source {source['id']}: source changed during reparse")
                continue
            chunks = tx.query("SELECT FROM chunks WHERE source_id=:source", {"source": source["id"]})
            if (set(expected) != {chunk["id"] for chunk in chunks}
                    or any(chunk.get("project_id") != source["project_id"]
                           or not chunk.get("is_current")
                           or (chunk.get("text"), chunk.get("line_start"), chunk.get("line_end")) != expected[chunk["id"]]
                           for chunk in chunks)):
                counts["errors"].append(f"Transcript source {source['id']}: raw chunk projection does not match")
                continue
            updated = {**metadata, "role": "metadata", "normalized_text": "", "schema_warning": None,
                       "transcript_parser_revision": TRANSCRIPT_PARSER_REVISION,
                       "transcript_reparse": {"previous_kind": "transcript_unknown",
                                              "previous_schema_warning": metadata["schema_warning"],
                                              "previous_parser_revision": metadata.get("transcript_parser_revision")}}
            tx.command("UPDATE sources SET kind='transcript',metadata=:metadata WHERE id=:source",
                       {"metadata": updated, "source": source["id"]})
            for chunk in chunks:
                tx.command("UPDATE chunks SET metadata=:metadata WHERE id=:id",
                           {"metadata": {**_metadata(chunk.get("metadata")), "role": "metadata"}, "id": chunk["id"]})
        counts["repaired"] += 1
        counts["unchanged"] -= 1
    return counts


def import_history(store, path: Path, project_id: str | None, client: str = "auto") -> dict:
    """Durably import complete JSONL records; unfinished tails are retried later."""
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        # Directories delegate to per-file locks below, allowing unrelated
        # transcripts to be imported concurrently without nested lock cycles.
        return _import_history_locked(store, path, project_id, client)
    with _ingestion_lock(store, "history", str(path)):
        return _import_history_locked(store, path, project_id, client)


def _import_history_locked(store, path: Path, project_id: str | None, client: str = "auto") -> dict:
    path = Path(path).expanduser().resolve()
    if project_id:
        project_id = store.resolve_project(project_id)["id"]
    if client not in {"auto", "codex", "claude"}:
        raise ValueError("client must be auto, codex, or claude")
    counts: dict[str, Any] = {"files": 0, "archived": 0, "chunks": 0, "inbox": 0, "unknown": 0, "errors": []}
    if path.is_dir():
        for candidate in sorted(path.rglob("*.jsonl")):
            if candidate.is_symlink():
                continue
            result = import_history(store, candidate, project_id, client)
            for key in counts:
                if key == "errors":
                    counts[key].extend(result[key])
                else:
                    counts[key] += result[key]
        return counts
    if not path.is_file():
        raise ValueError(f"History does not exist: {path}")
    # The cursor may already be at EOF, and snapshot() intentionally returns
    # the same immutable source for identical raw bytes. Repair the narrow
    # known metadata compatibility gap independently of append ingestion.
    reparsed = _reparse_history_sources_locked(store, path, project_id, client)
    counts["errors"].extend(reparsed["errors"])
    cursors = store.query("SELECT FROM import_cursors WHERE path=:path", {"path": str(path)})
    row = cursors[0] if cursors else None
    offset = int(row["offset"]) if row else 0
    if row and row["fingerprint"] != _fingerprint(path, offset):
        offset = 0
    # Explicit reassignment replays sources from the inbox without losing originals.
    if row and project_id and row["project_id"] != project_id:
        offset = 0
    if path.stat().st_size < offset:
        offset = 0
    cwd = None
    session_id = path.stem
    # Discover session metadata even when resuming beyond its first record.
    with path.open("rb") as probe:
        for _ in range(30):
            line = probe.readline(MAX_FILE_BYTES)
            if not line:
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                cwd = _event_cwd(event) or cwd
                session_id = str(event.get("sessionId") or (event.get("payload") or {}).get("id", session_id)) if isinstance(event.get("payload", {}), dict) else session_id
                if cwd:
                    break
    counts["files"] = 1
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            event_offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break
            end = handle.tell()
            text = raw.decode("utf-8", "replace")
            reason = None
            event: dict = {}
            try:
                decoded = json.loads(text)
                if not isinstance(decoded, dict):
                    reason = "JSONL event is not an object"
                else:
                    event = decoded
            except ValueError:
                reason = "Invalid complete JSONL record"
            cwd = _event_cwd(event) or cwd
            if isinstance(event.get("sessionId"), str):
                session_id = event["sessionId"]
            if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                session_id = str(event["payload"].get("id", session_id))
            resolved, conflict = _source_project(store, project_id, cwd)
            reason = reason or conflict
            normalized, role, detected, recognized = _event_text(event, client)
            if not recognized:
                counts["unknown"] += 1
                reason = reason or UNKNOWN_TRANSCRIPT_WARNING
            if not resolved:
                store.command(
                    "UPDATE ingestion_inbox SET id=:id,path=:path,source_offset=:offset,sha256=:sha256,"
                    "raw=:raw,client=:client,cwd=:cwd,reason=:reason,created_at=:created "
                    "UPSERT WHERE id=:id",
                    {"id": _identifier(path, event_offset, text), "path": str(path), "offset": event_offset,
                     "sha256": hashlib.sha256(raw).hexdigest(), "raw": text, "client": detected, "cwd": cwd,
                     "reason": reason or "No registered project matches working directory", "created": _now()},
                )
                counts["inbox"] += 1
            else:
                # Every complete source event is archived, including unrecognized records.
                # Immutable event URIs keep old evidence valid when a client
                # rotates/replaces a file and reuses its byte offsets.
                event_hash = hashlib.sha256(raw).hexdigest()
                source = store.snapshot(
                    resolved, f"transcript://{detected}/{_identifier(path, session_id)}/{event_offset}/{event_hash}", text,
                    kind="transcript" if recognized else "transcript_unknown",
                    metadata={"client": detected, "role": role, "session_id": session_id, "cwd": cwd,
                              "import_path": str(path), "byte_offset": event_offset, "normalized_text": normalized,
                              "schema_warning": reason, "source_timestamp": event.get("timestamp"),
                              "transcript_parser_revision": TRANSCRIPT_PARSER_REVISION},
                )
                counts["archived"] += 1
                counts["chunks"] += _write_chunks(store, resolved, source["id"], normalized or text, {"role": role})
                if normalized and role in {"user", "assistant"}:
                    store.enqueue("extract", resolved, {"source_id": source["id"]}, f"extract:{source['id']}")
                # Assignment moves this exact event into the project's archive.
                # Only remove its inbox copy after source/chunks/jobs are durable;
                # a crash before this point safely replays without losing bytes.
                store.command(
                    "DELETE FROM ingestion_inbox WHERE path=:path AND source_offset=:offset AND sha256=:sha256",
                    {"path": str(path), "offset": event_offset, "sha256": event_hash},
                )
            if reason:
                counts["errors"].append(f"{path}:{event_offset}: {reason}")
            # A crash before cursor commit is safe: sources and jobs are idempotent.
            _save_cursor(store, path, project_id, detected, end)
            offset = end
    # Register even empty or currently partial files so polling notices appends.
    if not store.query("SELECT path FROM import_cursors WHERE path=:path", {"path": str(path)}):
        _save_cursor(store, path, project_id, client, offset)
    return counts


def poll_imports(store) -> dict:
    rows = store.query("SELECT path,project_id,client FROM import_cursors")
    result = {"files": 0, "archived": 0, "chunks": 0, "inbox": 0, "unknown": 0, "errors": []}
    for row in rows:
        try:
            item = import_history(store, Path(row["path"]), row["project_id"], row["client"])
            for key, value in item.items():
                if key == "errors":
                    result[key].extend(value)
                else:
                    result[key] += value
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{row['path']}: {exc}")
    return result


def register_default_histories(store, project_id: str, root: Path) -> dict:
    project = store.resolve_project(project_id)
    roots = _roots(store, project)
    root = Path(root).resolve()
    if root not in roots:
        raise ValueError("History discovery root must be a registered project root")
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    locations = [(codex_home / "sessions", "codex"), (codex_home / "archived_sessions", "codex"), (claude_home / "projects", "claude")]
    result = {"discovered": 0, "registered": 0, "archived": 0, "errors": []}
    for base, client in locations:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.jsonl")):
            if path.is_symlink():
                continue
            result["discovered"] += 1
            declared = None
            try:
                with path.open("rb") as handle:
                    for _ in range(30):
                        line = handle.readline(MAX_FILE_BYTES)
                        if not line:
                            break
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            declared = _event_cwd(event)
                            if declared:
                                break
                if not declared:
                    continue
                try:
                    belongs = store.resolve_project(declared)["id"] == project["id"]
                except ValueError:
                    belongs = False
                if belongs:
                    counts = import_history(store, path, project["id"], client)
                    result["registered"] += 1
                    result["archived"] += counts["archived"]
                    result["errors"].extend(counts["errors"])
            except OSError as exc:
                result["errors"].append(f"{path}: {exc}")
    return result


def current_source(store, source_id: str) -> dict:
    """Refresh code/document evidence against disk; never read outside registered roots."""
    source = store.get_source(source_id)
    metadata = _metadata(source.get("metadata"))
    if source["kind"] not in {"code", "document"} or not metadata.get("path"):
        return source
    project = store.resolve_project(source["project_id"])
    roots = _roots(store, project)
    path = Path(os.path.normpath(metadata["path"]))
    bindings = memory_bindings(store, project["id"])
    local = bound_metadata(store, project["id"], metadata, bindings=bindings)
    mapped_path = Path(local["path"])
    imported = source_id in imported_source_ids(store, project["id"])
    if imported:
        # A saved foreign URI is provenance, never a request to read that host
        # path. Only the actual local index can establish a current file source.
        latest = _source_before(store, project["id"], str(mapped_path)) if mapped_path != path or any(
            path.is_relative_to(origin) for origin, _ in bindings) else None
        if latest and latest["id"] != source_id:
            return current_source(store, latest["id"])
        return {**source, "is_current": False,
                "freshness_warning": "Imported file snapshot is historical until the local checkout is indexed"}
    allowed_roots = [root for root in roots if path.is_absolute() and path.is_relative_to(root)
                     and path.resolve().is_relative_to(root)]
    excluded_directory = any(part in EXCLUDED_DIRS for root in allowed_roots for part in path.relative_to(root).parts[:-1])
    if (path.is_symlink() or not allowed_roots or _is_secret(path) or excluded_directory
            or any(is_memory_path(path, root) for root in roots)
            or path.resolve().is_relative_to(store.data_dir)):
        raise ValueError("Source path is outside registered roots or excluded")
    if not path.is_file():
        if not any(root.is_dir() for root in allowed_roots):
            # As in index_project: an unavailable root is not a deletion of its files.
            return {**source, "freshness_warning": "Project root is unavailable; the current file cannot be checked"}
        active = store.query(
            "SELECT id FROM sources WHERE project_id=:project AND uri=:uri AND is_current=true",
            {"project": project["id"], "uri": source["uri"]},
        )
        for row in active:
            _deactivate_source(store, row["id"])
        return {**source, "is_current": False, "deleted": True}
    if path.stat().st_size > MAX_FILE_BYTES:
        return {**source, "freshness_warning": "Current file exceeds indexing size limit"}
    try:
        # Preserve BOM and CRLF exactly as archived by index_project.
        content = path.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return {**source, "freshness_warning": str(exc)}
    digest = hashlib.sha256(content.encode()).hexdigest()
    latest = _source_before(store, project["id"], source["uri"])
    if latest and latest["sha256"] == digest:
        return latest
    index_project(store, project["id"])
    return _source_before(store, project["id"], source["uri"]) or {**source, "is_current": False, "freshness_warning": "Source no longer indexed"}
