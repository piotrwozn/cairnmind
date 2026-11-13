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
# syntax-v2: Python module constants and class fields are symbols too.
PARSER_VERSION = "syntax-v2"


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


def _python_assignments(root: Any) -> list[dict[str, Any]]:
    """Module constants and class fields: assignments to one identifier, annotated or not.

    Only statements directly in the module or directly in a class body count; an assignment
    inside a function or an if/try/with/for block, an augmented assignment, a tuple, attribute
    or subscript target and a dunder name do not. Each assignment is its own symbol, so a name
    assigned twice in one scope yields two, each with the lines of its whole statement.

    A field is named ``Owner.name``, with ``owner`` and ``member`` kept apart. Calls are resolved
    by bare name within a file, and a field is never called by its bare name; the qualified name
    is also how a field is written in prose (``Settings.cockpit_port``).
    """
    found: list[dict[str, Any]] = []

    def scan(block: Any, owner: str | None) -> None:
        for statement in block.named_children:
            if statement.type != "expression_statement" or len(statement.named_children) != 1:
                continue
            assignment = statement.named_children[0]
            target = assignment.child_by_field_name("left") if assignment.type == "assignment" else None
            if target is None or target.type != "identifier":
                continue
            member = _text(target)
            if not member or (member.startswith("__") and member.endswith("__")):
                continue
            item: dict[str, Any] = {"name": member, "kind": "constant",
                                    "line_start": statement.start_point.row + 1,
                                    "line_end": statement.end_point.row + 1}
            if owner:
                item.update(name=f"{owner}.{member}", kind="field", owner=owner, member=member)
            found.append(item)

    scan(root, None)
    for node in _walk(root):
        if node.type == "class_definition":
            owner, body = _text(node.child_by_field_name("name")), node.child_by_field_name("body")
            if owner and body is not None:
                scan(body, owner)
    return found


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
    if name == "python":
        result["symbols"].extend(_python_assignments(tree.root_node))
    if tree.root_node.has_error and not result["errors"]:
        # Missing punctuation can be an unnamed node, outside the fact walk.
        result["errors"].append("Syntax tree contains missing or invalid tokens")
    return result
