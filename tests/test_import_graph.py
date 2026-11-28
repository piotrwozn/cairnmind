"""Permanent import-graph and layer-boundary guards (Phase 4 / P1.2).

Enforces:
1. Zero top-level circular imports (Tarjan SCC size == 1 for every module under src/cairnmind/).
2. Zero reciprocal imports (even lazy function-body imports) on all broken cycle pairs
   (brief/scope, scope/verification, impact/impact_verdicts, sql/storage_query, cli/cli_cmds/*,
   service/mcp.service_*, cockpit/cockpit_handlers, store/store_graph/store_jobs).
3. Clean bottom-layer isolation: `cairnmind.pal.*` never imports any upper domain package.
4. Lazy mock isolation: importing `cairnmind.ui.server` or `cairnmind.ui.ui_server` never
   loads `cairnmind.ui.mock_state` at top level.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "cairnmind"

UPPER_ROOTS = frozenset({
    "storage", "memory", "agents", "scip", "mcp", "terminal", "ui", "cli_cmds",
    "cli", "store", "service",
})


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC_DIR)
    if rel == Path("__init__.py"):
        return "cairnmind"
    if path.name == "__init__.py":
        return "cairnmind." + ".".join(rel.parent.parts)
    return "cairnmind." + ".".join(rel.with_suffix("").parts)


def _resolve_import(current_mod: str, is_pkg: bool, node: ast.AST, known: set[str]) -> set[str]:
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            while name:
                if name in known:
                    targets.add(name)
                    break
                if "." not in name:
                    break
                name = name.rsplit(".", 1)[0]
    elif isinstance(node, ast.ImportFrom):
        if node.level > 0:
            pkg_parts = current_mod.split(".") if is_pkg else current_mod.split(".")[:-1]
            up = node.level - 1
            base_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
            base = ".".join(base_parts)
            full_mod = f"{base}.{node.module}" if node.module else base
        else:
            full_mod = node.module or ""
        if full_mod in known:
            targets.add(full_mod)
        for alias in node.names:
            cand = f"{full_mod}.{alias.name}" if full_mod else alias.name
            if cand in known:
                targets.add(cand)
    return targets


def _build_graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    files = sorted(SRC_DIR.rglob("*.py"))
    known = {_module_name(p) for p in files}
    top_graph: dict[str, set[str]] = {m: set() for m in known}
    all_graph: dict[str, set[str]] = {m: set() for m in known}

    for path in files:
        mod = _module_name(path)
        is_pkg = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for tgt in _resolve_import(mod, is_pkg, stmt, known):
                    if tgt != mod:
                        top_graph[mod].add(tgt)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for tgt in _resolve_import(mod, is_pkg, node, known):
                    if tgt != mod:
                        all_graph[mod].add(tgt)
    return top_graph, all_graph


def _tarjan_sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    index_counter = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index_counter
        indices[v] = lowlink[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(sorted(comp))

    for v in sorted(graph):
        if v not in indices:
            strongconnect(v)
    return sccs


def test_zero_top_level_import_cycles():
    """Top-level imports across all src/cairnmind modules must form a strict DAG."""
    top_graph, _ = _build_graph()
    cycles = [comp for comp in _tarjan_sccs(top_graph) if len(comp) > 1]
    assert not cycles, f"Top-level circular imports detected: {cycles}"


def test_broken_direct_cycles_stay_broken():
    """Explicitly broken reciprocal import pairs must never re-introduce cycles (even lazy)."""
    _, all_graph = _build_graph()
    forbidden_pairs = [
        ("cairnmind.storage.sql", "cairnmind.storage.storage_query"),
        ("cairnmind.memory.brief", "cairnmind.memory.scope"),
        ("cairnmind.memory.scope", "cairnmind.memory.verification"),
        ("cairnmind.memory.impact", "cairnmind.memory.impact_verdicts"),
        ("cairnmind.cli", "cairnmind.cli_cmds.cli_importers"),
        ("cairnmind.cli", "cairnmind.cli_cmds.cli_published"),
        ("cairnmind.cli", "cairnmind.cli_cmds.cli_record_kinds"),
        ("cairnmind.cli", "cairnmind.cli_cmds.service_version"),
        ("cairnmind.cli", "cairnmind.cli_cmds.io"),
        ("cairnmind.cli", "cairnmind.cli_cmds.diagnostics"),
        ("cairnmind.cli", "cairnmind.cli_cmds.engine_cmds"),
        ("cairnmind.cli", "cairnmind.cli_cmds.projects"),
        ("cairnmind.cli", "cairnmind.cli_cmds.plans"),
        ("cairnmind.cli", "cairnmind.cli_cmds.sessions"),
        ("cairnmind.cli", "cairnmind.cli_cmds.eval_cmds"),
        ("cairnmind.cli", "cairnmind.cli_cmds.memory_cmds"),
        ("cairnmind.cli", "cairnmind.cli_cmds.models_cmds"),
        ("cairnmind.cli", "cairnmind.cli_cmds.scip_cmds"),
        ("cairnmind.service", "cairnmind.mcp.service_models"),
        ("cairnmind.service", "cairnmind.mcp.service_workers"),
        ("cairnmind.service", "cairnmind.mcp.service_routes"),
        ("cairnmind.terminal.cockpit", "cairnmind.terminal.cockpit_handlers"),
        ("cairnmind.store", "cairnmind.storage.store_graph"),
        ("cairnmind.store", "cairnmind.storage.store_jobs"),
    ]
    violations = [
        (a, b) for a, b in forbidden_pairs
        if b in all_graph.get(a, ()) and a in all_graph.get(b, ())
    ]
    assert not violations, f"Reciprocal import cycles re-introduced: {violations}"


def test_pal_layer_never_imports_upper_domains():
    """Platform abstraction layer (`cairnmind.pal.*`) must remain strictly below all domain layers."""
    _, all_graph = _build_graph()
    violations: list[str] = []
    for mod, deps in sorted(all_graph.items()):
        if not mod.startswith("cairnmind.pal."):
            continue
        for dep in sorted(deps):
            parts = dep.split(".")
            if len(parts) >= 2 and parts[1] in UPPER_ROOTS:
                violations.append(f"{mod} -> {dep}")
    assert not violations, "PAL modules must not import upper layers:\n" + "\n".join(violations)


def test_ui_live_modules_never_import_mock_state_at_top_level():
    """Production UI server and live adapter must not eagerly import mock fixtures."""
    top_graph, all_graph = _build_graph()
    mock_mod = "cairnmind.ui.mock_state"
    for mod in ("cairnmind.ui.server", "cairnmind.ui.ui_server", "cairnmind.ui.ui_cockpit"):
        if mod in top_graph:
            assert mock_mod not in top_graph[mod], f"{mod} eagerly imports {mock_mod} at top level"
    if "cairnmind.ui.live_adapter" in all_graph:
        assert mock_mod not in all_graph["cairnmind.ui.live_adapter"], "live_adapter must never import mock_state"


def test_zero_broad_suppress_exception_in_src():
    """Never use bare `contextlib.suppress(Exception)` in production modules; narrow to concrete types."""
    hits: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_suppress = (
                (isinstance(fn, ast.Name) and fn.id == "suppress")
                or (isinstance(fn, ast.Attribute) and fn.attr == "suppress")
            )
            if is_suppress and any(isinstance(arg, ast.Name) and arg.id == "Exception" for arg in node.args):
                hits.append(f"{path.relative_to(SRC_DIR).as_posix()}:{node.lineno}")
    assert not hits, "Found bare suppress(Exception) calls:\n" + "\n".join(hits)


def test_zero_production_classes_over_1000_loc():
    """Production classes (outside lazy mock fixtures) must remain decomposed below 1,000 LOC."""
    oversized: list[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        rel = path.relative_to(SRC_DIR).as_posix()
        if rel == "ui/mock_state.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                span = (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1
                if span > 1000:
                    oversized.append(f"{rel}:{node.name} ({span} LOC)")
    assert not oversized, "Production classes exceed 1,000 LOC:\n" + "\n".join(oversized)


def test_pyright_standard_config_and_ci_wired():
    """Pyright `standard` mode must cover pal/, storage/, and mcp/ and be enforced in CI."""
    import json

    repo = SRC_DIR.parents[1]
    cfg_path = repo / "pyrightconfig.standard.json"
    assert cfg_path.is_file(), "Missing pyrightconfig.standard.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("typeCheckingMode") == "standard"
    includes = set(cfg.get("include") or ())
    assert {"src/cairnmind/pal", "src/cairnmind/storage", "src/cairnmind/mcp"} <= includes
    ci_text = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "uv run pyright -p pyrightconfig.standard.json" in ci_text

