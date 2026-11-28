"""Translate binary SCIP indexes into Cairnmind code-graph analyses and persist them in ArcadeDB.

Zero ArcadeDB schema migration is needed: SCIP analyses are stored in
``sources.metadata.analysis`` with ``analysis_version = "scip-v1:<indexer>@<version>:<digest>"``
and ``source = "scip"``. ``ingest._graph_parts`` consumes these pre-resolved symbols,
imports, external modules, calls, and references to emit standard Cairnmind graph nodes
and edges (``defines``, ``imports``, ``test_imports``, ``calls``, ``references``).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from cairnmind.config import Settings
from cairnmind.scip.scip_manager import ScipError, cleanup_temp_index, ensure_indexer, run_indexer
from cairnmind.scip.scip_proto import (
    CALLABLE_KINDS,
    ROLE_DEFINITION,
    ROLE_FORWARD_DEFINITION,
    ROLE_IMPORT,
    ScipIndex,
    ScipSymbolInfo,
    is_local_symbol,
    parse_scip_file,
    parse_scip_symbol,
)
from cairnmind.scip.scip_registry import (
    EXTENSION_TO_LANGUAGE,
    INDEXERS,
    LANGUAGE_TO_INDEXER,
    detect_project_languages,
    indexers_for_languages,
)
from cairnmind.storage.storage_query import col, db_find, db_update_where

SCIP_ANALYSIS_PREFIX = "scip-v1:"


def is_scip_analysis_version(version: Any) -> bool:
    """True when ``version`` marks a SCIP-produced source analysis."""
    return isinstance(version, str) and version.startswith(SCIP_ANALYSIS_PREFIX)


def _normalize_language(doc_lang: str, path: Path) -> str:
    raw = (doc_lang or "").strip().lower()
    aliases = {
        "golang": "go",
        "c++": "cpp",
        "c#": "csharp",
        "cs": "csharp",
        "kt": "kotlin",
        "rs": "rust",
        "rb": "ruby",
    }
    if raw in aliases:
        return aliases[raw]
    if raw in LANGUAGE_TO_INDEXER:
        return raw
    return EXTENSION_TO_LANGUAGE.get(path.suffix.lower(), raw or "code")


def _enclosing_symbol(line: int, symbols: list[dict[str, Any]]) -> tuple[str, int] | None:
    """Find the innermost non-parameter symbol covering 1-based ``line``."""
    best: dict[str, Any] | None = None
    best_span = 1 << 60
    for sym in symbols:
        if sym.get("kind") == "parameter":
            continue
        start = sym["line_start"]
        end = sym["line_end"]
        if start <= line <= end:
            span = end - start
            if span < best_span:
                best = sym
                best_span = span
    if best is None:
        return None
    return best["name"], best["line_start"]


def scip_to_file_analyses(
    index: ScipIndex,
    root: Path,
    *,
    indexer: str = "",
    version: str = "",
) -> dict[Path, dict[str, Any]]:
    """Convert a decoded ``ScipIndex`` into Cairnmind per-file analysis dicts keyed by resolved Path."""
    root_resolved = Path(root).resolve()
    tool_name = indexer or index.metadata.tool_info.name or "scip"
    tool_version = version or index.metadata.tool_info.version or "unknown"

    # Build lookup of SymbolInformation across documents and external_symbols.
    sym_info: dict[str, ScipSymbolInfo] = {}
    for info in index.external_symbols:
        if info.symbol:
            sym_info[info.symbol] = info

    doc_paths: list[tuple[Path, Any]] = []
    for doc in index.documents:
        for info in doc.symbols:
            if info.symbol:
                sym_info[info.symbol] = info
        if not doc.relative_path:
            continue
        candidate = (root_resolved / PurePosixPath(doc.relative_path)).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            continue  # Never allow a SCIP document to escape the registered project root.
        doc_paths.append((candidate, doc))

    # Pass 1: collect canonical definitions for every non-local SCIP symbol.
    definitions: dict[str, dict[str, Any]] = {}
    file_symbols: dict[Path, list[dict[str, Any]]] = {}
    file_symbol_dedupe: dict[Path, set[tuple[str, int]]] = {}

    for abs_path, doc in doc_paths:
        syms = file_symbols.setdefault(abs_path, [])
        seen_keys = file_symbol_dedupe.setdefault(abs_path, set())
        for occ in doc.occurrences:
            if not occ.symbol or is_local_symbol(occ.symbol):
                continue
            is_def = bool(occ.symbol_roles & ROLE_DEFINITION)
            is_fwd = bool(occ.symbol_roles & ROLE_FORWARD_DEFINITION)
            if not is_def and not is_fwd:
                continue
            if is_fwd and occ.symbol in definitions:
                continue

            line_start = max(1, occ.start_line + 1)
            raw_end = (occ.enclosing_end_line + 1) if occ.enclosing_end_line is not None else (occ.end_line + 1)
            line_end = max(line_start, raw_end)
            parsed = parse_scip_symbol(occ.symbol, sym_info.get(occ.symbol))
            name = parsed["name"]
            if not name:
                continue

            key = (name, line_start)
            if key not in seen_keys:
                seen_keys.add(key)
                sym_entry: dict[str, Any] = {
                    "name": name,
                    "kind": parsed["kind"],
                    "line_start": line_start,
                    "line_end": line_end,
                    "scip_symbol": occ.symbol,
                }
                if parsed.get("owner"):
                    sym_entry["owner"] = parsed["owner"]
                if parsed.get("member"):
                    sym_entry["member"] = parsed["member"]
                syms.append(sym_entry)

            if is_def or occ.symbol not in definitions:
                definitions[occ.symbol] = {
                    "path": str(abs_path),
                    "name": name,
                    "line_start": line_start,
                    "kind": parsed["kind"],
                    "package": parsed.get("package", ""),
                }

    # Pass 2: resolve imports, external modules, calls, and references per document.
    analyses: dict[Path, dict[str, Any]] = {}
    for abs_path, doc in doc_paths:
        syms = sorted(file_symbols.get(abs_path, []), key=lambda s: (s["line_start"], s["name"]))
        lang = _normalize_language(doc.language, abs_path)

        resolved_imports_map: dict[str, int] = {}
        external_imports_map: dict[str, int] = {}
        resolved_calls: list[dict[str, Any]] = []
        resolved_refs: list[dict[str, Any]] = []
        seen_calls: set[tuple[str | None, int | None, str, str, int, int]] = set()
        seen_refs: set[tuple[str | None, int | None, str, str, int, int]] = set()

        for occ in doc.occurrences:
            if not occ.symbol or is_local_symbol(occ.symbol):
                continue
            line = max(1, occ.start_line + 1)
            is_def = bool(occ.symbol_roles & ROLE_DEFINITION)
            is_imp = bool(occ.symbol_roles & ROLE_IMPORT)
            defn = definitions.get(occ.symbol)

            if is_def:
                continue

            if defn is None:
                if is_imp:
                    parsed = parse_scip_symbol(occ.symbol, sym_info.get(occ.symbol))
                    module_label = parsed.get("package") or parsed.get("name")
                    if module_label and module_label not in external_imports_map:
                        external_imports_map[module_label] = line
                continue

            target_path = defn["path"]
            if target_path != str(abs_path):
                prev_line = resolved_imports_map.get(target_path)
                if prev_line is None or line < prev_line:
                    resolved_imports_map[target_path] = line

            if is_imp:
                continue

            owner = _enclosing_symbol(line, syms)
            owner_name, owner_line = owner if owner else (None, None)
            # Avoid noisy self-edges on a symbol's own declaration header.
            if (
                target_path == str(abs_path)
                and owner_name == defn["name"]
                and owner_line == defn["line_start"]
                and line == defn["line_start"]
            ):
                continue

            edge_key = (owner_name, owner_line, target_path, defn["name"], defn["line_start"], line)
            payload = {
                "line": line,
                "owner_name": owner_name,
                "owner_line": owner_line,
                "target_path": target_path,
                "target_name": defn["name"],
                "target_line": defn["line_start"],
            }
            if defn["kind"] in CALLABLE_KINDS:
                if edge_key not in seen_calls:
                    seen_calls.add(edge_key)
                    resolved_calls.append(payload)
            else:
                if edge_key not in seen_refs:
                    seen_refs.add(edge_key)
                    resolved_refs.append(payload)

        # Also incorporate explicit SymbolInformation.relationships (e.g. implements / overrides).
        sym_by_scip = {s["scip_symbol"]: s for s in syms if s.get("scip_symbol")}
        for info in doc.symbols:
            owner_sym = sym_by_scip.get(info.symbol)
            if not owner_sym:
                continue
            for rel in info.relationships:
                if not rel.symbol or is_local_symbol(rel.symbol):
                    continue
                target_def = definitions.get(rel.symbol)
                if not target_def:
                    continue
                if target_def["path"] != str(abs_path) and target_def["path"] not in resolved_imports_map:
                    resolved_imports_map[target_def["path"]] = owner_sym["line_start"]
                edge_key = (
                    owner_sym["name"],
                    owner_sym["line_start"],
                    target_def["path"],
                    target_def["name"],
                    target_def["line_start"],
                    owner_sym["line_start"],
                )
                if edge_key not in seen_refs:
                    seen_refs.add(edge_key)
                    resolved_refs.append(
                        {
                            "line": owner_sym["line_start"],
                            "owner_name": owner_sym["name"],
                            "owner_line": owner_sym["line_start"],
                            "target_path": target_def["path"],
                            "target_name": target_def["name"],
                            "target_line": target_def["line_start"],
                        }
                    )

        resolved_imports = [
            {"target_path": target, "line": line}
            for target, line in sorted(resolved_imports_map.items(), key=lambda item: (item[1], item[0]))
        ]
        external_imports = [
            {"module": mod, "line": line}
            for mod, line in sorted(external_imports_map.items(), key=lambda item: (item[1], item[0]))
        ]

        core_payload = {
            "language": lang,
            "source": "scip",
            "symbols": syms,
            "imports": [],
            "calls": [],
            "resolved_imports": resolved_imports,
            "external_imports": external_imports,
            "resolved_calls": resolved_calls,
            "resolved_references": resolved_refs,
        }
        digest = hashlib.sha256(
            json.dumps(core_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        core_payload["scip_version"] = f"{SCIP_ANALYSIS_PREFIX}{tool_name}@{tool_version}:{digest}"
        analyses[abs_path] = core_payload

    return analyses


def _validate_registered_root(store, project_id: str, root: Path) -> Path:
    resolved = Path(root).expanduser().resolve()
    rows = db_find(
        store,
        "project_roots",
        {"project_id": project_id, "path": str(resolved)},
        columns=("path",),
    )
    if not rows:
        raise ValueError(f"Root {resolved} is not registered for project {project_id}")
    return resolved


def apply_scip_index(
    store,
    project_id: str,
    root: Path,
    scip_path: Path,
    *,
    indexer: str = "",
    version: str = "",
) -> dict[str, Any]:
    """Decode ``scip_path``, persist SCIP analyses on matching project sources, and rebuild the graph."""
    from cairnmind.scip.ingest import forget_index_signature, index_project

    project = store.resolve_project(project_id)
    pid = project["id"]
    resolved_root = _validate_registered_root(store, pid, root)

    decoded = parse_scip_file(scip_path)
    tool_name = indexer or decoded.metadata.tool_info.name or "imported"
    tool_version = version or decoded.metadata.tool_info.version or "external"
    analyses = scip_to_file_analyses(decoded, resolved_root, indexer=tool_name, version=tool_version)

    # Ensure source rows exist before attaching SCIP analyses.
    source_cols = (
        "id",
        "uri",
        "kind",
        col("metadata.path", "path"),
        col("metadata.analysis_version", "analysis_version"),
    )
    existing_rows = db_find(
        store,
        "sources",
        {"project_id": pid, "is_current": True},
        columns=source_cols,
    )
    if not existing_rows:
        index_project(store, pid)
        existing_rows = db_find(
            store,
            "sources",
            {"project_id": pid, "is_current": True},
            columns=source_cols,
        )

    by_resolved_path: dict[Path, dict[str, Any]] = {}
    for row in existing_rows:
        raw_path = row.get("path") or row.get("uri")
        if raw_path:
            try:
                by_resolved_path[Path(raw_path).resolve()] = row
            except OSError:
                continue

    updated = 0
    unchanged = 0
    unmatched = 0
    symbols_total = 0
    calls_total = 0
    refs_total = 0

    for abs_path, analysis in analyses.items():
        row = by_resolved_path.get(abs_path)
        if not row:
            unmatched += 1
            continue
        symbols_total += len(analysis["symbols"])
        calls_total += len(analysis["resolved_calls"])
        refs_total += len(analysis["resolved_references"])
        scip_ver = analysis["scip_version"]
        if row.get("analysis_version") == scip_ver and row.get("kind") == "code":
            unchanged += 1
            continue
        db_update_where(
            store,
            "sources",
            set_fields={
                "kind": "code",
                "metadata.analysis": analysis,
                "metadata.analysis_version": scip_ver,
                "metadata.language": analysis["language"],
            },
            where={"id": row["id"]},
        )
        updated += 1

    forget_index_signature(store, pid)
    graph_result = index_project(store, pid)
    return {
        "project_id": pid,
        "root": str(resolved_root),
        "indexer": tool_name,
        "version": tool_version,
        "documents": len(decoded.documents),
        "files_updated": updated,
        "files_unchanged": unchanged,
        "files_unmatched": unmatched,
        "symbols": symbols_total,
        "calls": calls_total,
        "references": refs_total,
        "graph": {
            "nodes": graph_result.get("nodes", 0),
            "edges": graph_result.get("edges", 0),
            "graph_unchanged": graph_result.get("graph_unchanged", False),
        },
    }


def index_project_scip(
    store,
    settings: Settings,
    project_id: str,
    *,
    languages: list[str] | None = None,
    worktree: Path | str | None = None,
    allow_download: bool = True,
) -> dict[str, Any]:
    """Detect supported SCIP languages in a project, download/run indexers, and ingest into ArcadeDB."""
    project = store.resolve_project(project_id)
    pid = project["id"]
    root = _validate_registered_root(store, pid, Path(worktree) if worktree else Path(project["root"]))

    detected = detect_project_languages(root)
    if languages:
        requested = [lang.strip().lower() for lang in languages if lang.strip()]
        selected_langs = requested
    else:
        selected_langs = [item["language"] for item in detected]
        if settings.scip_languages:
            allowed = {str(lang).strip().lower() for lang in settings.scip_languages}
            selected_langs = [lang for lang in selected_langs if lang in allowed]

    indexer_ids = indexers_for_languages(selected_langs)
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for indexer_id in indexer_ids:
        spec = INDEXERS[indexer_id]
        try:
            installed = ensure_indexer(settings, indexer_id, allow_download=allow_download)
        except Exception as exc:
            errors.append(f"{indexer_id}: {exc}")
            results.append(
                {"indexer": indexer_id, "version": spec.version, "status": "install_failed", "error": str(exc)}
            )
            continue

        if installed.status not in {"ready", "docker"}:
            results.append(
                {
                    "indexer": indexer_id,
                    "version": spec.version,
                    "status": installed.status,
                    "missing_tools": installed.missing_tools,
                    "detail": installed.detail,
                }
            )
            continue

        scip_file: Path | None = None
        try:
            scip_file = run_indexer(settings, indexer_id, root, installed=installed)
            applied = apply_scip_index(
                store, pid, root, scip_file, indexer=indexer_id, version=spec.version
            )
            results.append({"indexer": indexer_id, "version": spec.version, "status": "indexed", **applied})
        except (ScipError, ValueError, OSError) as exc:
            errors.append(f"{indexer_id}: {exc}")
            results.append(
                {"indexer": indexer_id, "version": spec.version, "status": "failed", "error": str(exc)}
            )
        finally:
            if scip_file is not None:
                cleanup_temp_index(scip_file)

    return {
        "project_id": pid,
        "root": str(root),
        "detected_languages": detected,
        "selected_languages": selected_langs,
        "indexers": results,
        "errors": errors,
    }


def enqueue_project_scip(
    store,
    settings: Settings,
    project_id: str,
    root: Path,
    *,
    dedupe_suffix: str = "initial",
) -> dict[str, Any]:
    """Detect SCIP languages and enqueue a non-blocking ``scip_index`` job when ``scip_mode == 'auto'``."""
    detected = detect_project_languages(root)
    langs = [item["language"] for item in detected]
    if settings.scip_languages:
        allowed = {str(lang).strip().lower() for lang in settings.scip_languages}
        langs = [lang for lang in langs if lang in allowed]
    if settings.scip_mode != "auto" or not langs:
        return {
            "mode": settings.scip_mode,
            "detected_languages": langs,
            "queued": False,
            "job_id": None,
        }
    resolved_root = str(Path(root).expanduser().resolve())
    key = f"scip:{project_id}:{resolved_root}:{dedupe_suffix}"
    job_id = store.enqueue(
        "scip_index",
        project_id,
        {"root": resolved_root, "languages": langs},
        key,
    )
    return {
        "mode": settings.scip_mode,
        "detected_languages": langs,
        "queued": True,
        "job_id": job_id,
    }
