"""Syntax-derived code facts for the knowledge graph.

No relationships are inferred from prose here: every returned fact has a source
line. Unsupported languages remain searchable text, rather than pretending to
have a complete dependency graph.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


# Bump when syntax-to-fact semantics change, independently of grammar upgrades.
PARSER_VERSION = "syntax-v1"


@lru_cache(maxsize=1)
def parser_identity() -> str:
    packages = ("tree-sitter", "tree-sitter-python", "tree-sitter-javascript", "tree-sitter-typescript")
    versions = []
    for package in packages:
        try:
            versions.append(f"{package}={version(package)}")
        except PackageNotFoundError:
            versions.append(f"{package}=unavailable")
    return PARSER_VERSION + ":" + ":".join(versions)


def _text(node: Any | None) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


@lru_cache(maxsize=4)
def _language(name: str) -> Any:
    from tree_sitter import Language

    if name == "python":
        import tree_sitter_python

        return Language(tree_sitter_python.language())
    if name == "javascript":
        import tree_sitter_javascript

        return Language(tree_sitter_javascript.language())
    import tree_sitter_typescript

    return Language(
        tree_sitter_typescript.language_tsx()
        if name == "tsx"
        else tree_sitter_typescript.language_typescript()
    )


def _walk(root: Any):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.named_children))


def _field_children(node: Any, field: str) -> list[Any]:
    return [
        child
        for index, child in enumerate(node.children)
        if node.field_name_for_child(index) == field
    ]


def _import_part(node: Any) -> tuple[str, str]:
    if node.type == "aliased_import":
        name = _text(node.child_by_field_name("name"))
        alias = _text(node.child_by_field_name("alias"))
        return name, alias or name
    return _text(node), _text(node)


def _string_value(node: Any | None) -> str:
    if node is None:
        return ""
    fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
    if fragment is not None:
        return _text(fragment)
    return _text(node).strip("\"'`")


def analyze_code(path: Path, content: str) -> dict[str, Any]:
    """Return declarations, imports and calls from Python/JS/TS syntax trees.

    Import ``names`` are exported/original names, with optional ``aliases``
    recording their local spelling. Source coordinates are one-based and refer
    to the exact supplied content, including when it is an uncommitted snapshot.
    Syntax errors are reported; valid subtrees still yield useful evidence.
    """
    suffix = Path(path).suffix.lower()
    name = {
        ".py": "python", ".pyi": "python",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    }.get(suffix, "text")
    result: dict[str, Any] = {
        "language": name, "symbols": [], "imports": [], "calls": [], "errors": []
    }
    if name == "text":
        return result
    try:
        from tree_sitter import Parser

        tree = Parser(_language(name)).parse(content.encode("utf-8"))
    except (ImportError, TypeError, ValueError, RuntimeError) as exc:
        result["errors"].append(f"Parser unavailable: {exc}")
        return result

    symbol_kinds = {
        "function_definition": "function", "class_definition": "class",
        "function_declaration": "function", "generator_function_declaration": "function",
        "class_declaration": "class", "abstract_class_declaration": "class",
        "method_definition": "method", "interface_declaration": "interface",
        "type_alias_declaration": "type", "enum_declaration": "enum",
    }
    for node in _walk(tree.root_node):
        if node.type == "ERROR" or node.is_missing:
            if len(result["errors"]) < 20:
                result["errors"].append(f"Syntax error at line {node.start_point.row + 1}")

        if node.type in symbol_kinds:
            label = _text(node.child_by_field_name("name"))
            if label:
                result["symbols"].append({
                    "name": label, "kind": symbol_kinds[node.type],
                    "line_start": node.start_point.row + 1,
                    "line_end": node.end_point.row + 1,
                })
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            label_node = node.child_by_field_name("name")
            if value is not None and value.type in {"arrow_function", "function_expression", "generator_function"}:
                if label_node is not None and label_node.type == "identifier":
                    result["symbols"].append({
                        "name": _text(label_node), "kind": "function",
                        "line_start": node.start_point.row + 1,
                        "line_end": node.end_point.row + 1,
                    })

        if name == "python" and node.type in {"import_statement", "import_from_statement"}:
            parts = [_import_part(child) for child in _field_children(node, "name")]
            if node.type == "import_statement":
                for module, alias in parts:
                    result["imports"].append({
                        "module": module, "names": [module],
                        "aliases": {module: alias}, "line": node.start_point.row + 1,
                    })
            else:
                module = _text(node.child_by_field_name("module_name"))
                if any(child.type == "wildcard_import" for child in node.named_children):
                    parts.append(("*", "*"))
                result["imports"].append({
                    "module": module, "names": [p[0] for p in parts],
                    "aliases": dict(parts), "line": node.start_point.row + 1,
                })
        elif name != "python" and node.type in {"import_statement", "export_statement"}:
            source = node.child_by_field_name("source")
            if source is not None:
                names: list[str] = []
                aliases: dict[str, str] = {}
                for child in _walk(node):
                    if child.type in {"import_specifier", "export_specifier"}:
                        original = _text(child.child_by_field_name("name"))
                        alias = _text(child.child_by_field_name("alias")) or original
                        if original:
                            names.append(original)
                            aliases[original] = alias
                    elif child.type == "import_clause":
                        for part in child.named_children:
                            if part.type == "identifier":
                                names.append("default")
                                aliases["default"] = _text(part)
                    elif child.type == "namespace_import":
                        local = next((c for c in child.named_children if c.type == "identifier"), None)
                        names.append("*")
                        aliases["*"] = _text(local)
                result["imports"].append({
                    "module": _string_value(source), "names": names, "aliases": aliases,
                    "line": node.start_point.row + 1,
                })

        if node.type in {"call", "call_expression"}:
            function = node.child_by_field_name("function")
            if function is not None:
                function_name = _text(function)
                # A computed expression isn't a statically resolvable callee.
                if function.type in {"identifier", "attribute", "member_expression"}:
                    result["calls"].append({"name": function_name, "line": node.start_point.row + 1})
                if name != "python" and function_name in {"require", "import"}:
                    arguments = node.child_by_field_name("arguments")
                    first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
                    if first is not None and first.type == "string":
                        result["imports"].append({
                            "module": _string_value(first), "names": [], "aliases": {},
                            "line": node.start_point.row + 1,
                        })
    if tree.root_node.has_error and not result["errors"]:
        # Missing punctuation can be an unnamed node, outside the fact walk.
        result["errors"].append("Syntax tree contains missing or invalid tokens")
    return result
