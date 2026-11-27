"""Syntax-derived code facts for the knowledge graph.

No relationships are inferred from prose here: every returned fact has a source
line. Unsupported languages remain searchable text, rather than pretending to
have a complete dependency graph.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import tomllib
from typing import Any


# Bump when syntax-to-fact semantics change, independently of grammar upgrades.
# syntax-v2: Python module constants and class fields are symbols too.
# syntax-v3: keys of TOML files and Python parameter defaults are symbols too.
# syntax-v4: normalised AST symbol body hashes, qualified names, and tagged decision comments (#411, #412).
PARSER_VERSION = "syntax-v4"
MAX_CONFIG_KEYS = 1000  # symbols kept from one configuration file
_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


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


def _python_parameter_defaults(root: Any) -> list[dict[str, Any]]:
    """Parameters of named functions that have a default: ``function.parameter``, kind parameter.

    A value written only as a default (``def connect(host, port=7311)``) has no other definition.
    ``owner`` is the function's bare name and ``member`` the parameter's, as for a class field; the
    lines are those of the parameter, and ``column_start``/``column_end`` (UTF-8 byte offsets in
    its first and last line) mark its own text on a line it shares with others. A default of
    None, a lambda's parameter and an unpacked parameter are left out.
    """
    found: list[dict[str, Any]] = []
    for node in _walk(root):
        if node.type != "function_definition":
            continue
        owner, parameters = _text(node.child_by_field_name("name")), node.child_by_field_name("parameters")
        if not owner or parameters is None:
            continue
        for parameter in parameters.named_children:
            if parameter.type not in {"default_parameter", "typed_default_parameter"}:
                continue
            name, value = parameter.child_by_field_name("name"), parameter.child_by_field_name("value")
            if name is None or name.type != "identifier" or value is None or value.type == "none":
                continue
            member = _text(name)
            found.append({"name": f"{owner}.{member}", "kind": "parameter", "owner": owner, "member": member,
                          "line_start": parameter.start_point.row + 1, "line_end": parameter.end_point.row + 1,
                          "column_start": parameter.start_point.column, "column_end": parameter.end_point.column})
    return found


def _toml_key(line: str, pos: int) -> tuple[list[str], int] | None:
    """(parts, position after the key and its spaces) of the dotted TOML key at ``pos``, or None."""
    parts: list[str] = []
    while True:
        while pos < len(line) and line[pos] in " \t":
            pos += 1
        if pos < len(line) and line[pos] in "\"'":
            quote, end = line[pos], pos + 1
            while end < len(line) and line[end] != quote:
                end += 2 if quote == '"' and line[end] == "\\" else 1
            if end >= len(line):
                return None
            parts.append(line[pos + 1:end])  # escapes are kept as written
            pos = end + 1
        else:
            match = _TOML_BARE_KEY.match(line, pos)
            if match is None:
                return None
            parts.append(match.group())
            pos = match.end()
        while pos < len(line) and line[pos] in " \t":
            pos += 1
        if pos < len(line) and line[pos] == ".":
            pos += 1
            continue
        return parts, pos


def _toml_value_end(lines: list[str], row: int, pos: int) -> int:
    """Index of the last line of the TOML value that starts at ``lines[row][pos]``.

    A value goes on while an array or inline table is open or a multi-line string is; brackets
    and "#" inside strings, and anything after a comment, do not count.
    """
    depth, multi = 0, None
    while row < len(lines):
        line = lines[row]
        while pos < len(line):
            if multi:
                if multi == '"""' and line[pos] == "\\":
                    pos += 2
                elif line.startswith(multi, pos):
                    pos, multi = pos + 3, None
                else:
                    pos += 1
                continue
            char = line[pos]
            if line.startswith(('"""', "'''"), pos):
                multi, pos = line[pos:pos + 3], pos + 3
                continue
            if char in "\"'":
                end = pos + 1
                while end < len(line) and line[end] != char:
                    end += 2 if char == '"' and line[end] == "\\" else 1
                pos = end + 1
                continue
            if char == "#":
                break
            depth += (char in "[{") - (char in "]}")
            pos += 1
        if not multi and depth <= 0:
            return row
        row, pos = row + 1, 0
    return len(lines) - 1


def _toml_keys(content: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Keys of a TOML file as symbols, and the file's syntax error if it has one.

    Every ``key = value`` is a symbol with the lines of its whole value. A key in a table is a
    field named ``table.key`` (``project.requires-python``) with ``owner`` (the table, dotted)
    and ``member`` (the last part of the key) kept apart, as a class field is; a key above the
    first table is a constant. Inline tables and arrays are one value. At most MAX_CONFIG_KEYS.
    Validity is the standard library's verdict; keys on the lines that can be read are kept, and
    a key under a table header that cannot be read is skipped.
    """
    errors: list[str] = []
    try:
        tomllib.loads(content)
    except ValueError as exc:  # tomllib.TOMLDecodeError is one
        errors.append(f"Syntax error: {exc}")
    except RecursionError:
        # The standard parser recurses into arrays and inline tables; a value nested a few hundred
        # levels deep in an untrusted file must not stop indexing. The line scan below is iterative.
        errors.append("Syntax error: the file is nested too deeply to be read")
    lines = content.split("\n")
    symbols: list[dict[str, Any]] = []
    table: list[str] | None = []
    row = 0
    while row < len(lines) and len(symbols) < MAX_CONFIG_KEYS:
        line = lines[row]
        stripped = line.strip()
        start = len(line) - len(line.lstrip())
        if not stripped or stripped.startswith("#"):
            row += 1
            continue
        if stripped.startswith("["):
            closing = "]]" if stripped.startswith("[[") else "]"
            parsed = _toml_key(line, start + len(closing))
            table = parsed[0] if parsed and line.startswith(closing, parsed[1]) else None
            row += 1
            continue
        parsed = _toml_key(line, start)
        if parsed is None or not line.startswith("=", parsed[1]):
            row += 1
            continue
        last = _toml_value_end(lines, row, parsed[1] + 1)
        if table is not None:
            path = [*table, *parsed[0]]
            item: dict[str, Any] = {"name": ".".join(path), "kind": "constant",
                                    "line_start": row + 1, "line_end": last + 1}
            if len(path) > 1:
                item.update(kind="field", owner=".".join(path[:-1]), member=path[-1])
            symbols.append(item)
        row = last + 1
    return symbols, errors


def analyze_code(path: Path, content: str) -> dict[str, Any]:
    """Return declarations, imports and calls from Python/JS/TS syntax trees.

    Import ``names`` are exported/original names, with optional ``aliases``
    recording their local spelling. Source coordinates are one-based and refer
    to the exact supplied content, including when it is an uncommitted snapshot.
    Syntax errors are reported; valid subtrees still yield useful evidence. A TOML file
    (``*.toml``, not a lock file) yields its keys as symbols (see ``_toml_keys``).
    """
    suffix = Path(path).suffix.lower()
    name = {
        ".py": "python", ".pyi": "python",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
        ".toml": "toml",
    }.get(suffix, "text")
    result: dict[str, Any] = {
        "language": name, "symbols": [], "imports": [], "calls": [], "errors": [], "tagged_comments": [],
    }
    if name == "toml":
        result["symbols"], result["errors"] = _toml_keys(content)
    if name in {"text", "toml"}:
        return result
    try:
        from tree_sitter import Parser

        tree = Parser(_language(name)).parse(content.encode("utf-8"))
    except (ImportError, TypeError, ValueError, RuntimeError) as exc:
        result["errors"].append(f"Parser unavailable: {exc}")
        return result

    from cairnmind.memory.symbol_anchor import (
        SYMBOL_ANCHOR_KINDS,
        enclosing_chain,
        extract_tagged_comments,
        symbol_ast_target,
        symbol_body_hash,
    )

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
                kind = symbol_kinds[node.type]
                chain = enclosing_chain(node)
                if name == "python" and node.type == "function_definition" and chain and chain[-1][0] == "class":
                    kind = "method"
                qualified = ".".join([*(part for _, part in chain), label]) if chain else label
                entry: dict[str, Any] = {
                    "name": label,
                    "kind": kind,
                    "line_start": node.start_point.row + 1,
                    "line_end": node.end_point.row + 1,
                }
                if kind in SYMBOL_ANCHOR_KINDS:
                    target = symbol_ast_target(node)
                    entry["qualified_name"] = qualified
                    entry["body_hash"] = symbol_body_hash(target, name)
                    if target is not node:
                        entry["decorator_line_start"] = target.start_point.row + 1
                    if chain and chain[-1][0] == "class":
                        entry["owner"] = ".".join(part for _, part in chain)
                        entry["member"] = label
                result["symbols"].append(entry)
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            label_node = node.child_by_field_name("name")
            if value is not None and value.type in {"arrow_function", "function_expression", "generator_function"}:
                if label_node is not None and label_node.type == "identifier":
                    label = _text(label_node)
                    chain = enclosing_chain(node)
                    qualified = ".".join([*(part for _, part in chain), label]) if chain else label
                    result["symbols"].append({
                        "name": label,
                        "kind": "function",
                        "qualified_name": qualified,
                        "body_hash": symbol_body_hash(node, name),
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
        result["symbols"].extend(_python_parameter_defaults(tree.root_node))
    result["tagged_comments"] = extract_tagged_comments(tree.root_node, name, result["symbols"])
    if tree.root_node.has_error and not result["errors"]:
        # Missing punctuation can be an unnamed node, outside the fact walk.
        result["errors"].append("Syntax tree contains missing or invalid tokens")
    return result
