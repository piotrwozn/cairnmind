"""Cross-OS normalised AST symbol hashing and tagged-comment decision extraction (#411, #412).

A symbol anchor stores a SHA-256 of the canonicalised token stream of a function, class
or method parsed by tree-sitter:
- comments inside the symbol are ignored;
- whitespace, indentation width, blank lines, line wrapping and trailing commas are ignored;
- CRLF/CR line endings are normalised to LF and file paths to POSIX form;
- Python block boundaries are preserved as explicit structural markers so changing the
  indentation scope of a statement always changes the hash;
- simple string quote styles ('a' vs "a") and optional JS/TS statement semicolons are
  canonicalised so running a formatter (ruff format / prettier) produces the identical hash
  on Windows, macOS and Linux.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

NORM_VERSION = 1
SYMBOL_ANCHOR_KINDS = frozenset({"function", "class", "method"})

COMMENT_TAGS: dict[str, str] = {
    "DECISION": "decision",
    "INVARIANT": "constraint",
    "WHY": "decision",
}

_TAG_LINE_RE = re.compile(r"^(DECISION|INVARIANT|WHY)\s*:\s*(.*)$")
_COMMENT_NODE_TYPES = frozenset({"comment", "line_comment", "block_comment"})
_BLOCK_NODE_TYPES = frozenset({"block", "statement_block", "class_body"})
_CLOSING_DELIMITERS = frozenset({")", "]", "}"})
_JS_SEMI_PARENTS = frozenset({
    "expression_statement",
    "return_statement",
    "lexical_declaration",
    "variable_declaration",
    "import_statement",
    "export_statement",
    "throw_statement",
    "break_statement",
    "continue_statement",
    "field_definition",
    "public_field_definition",
    "property_signature",
    "type_alias_declaration",
    "debugger_statement",
})


def posix_path(path: str | Path) -> str:
    """Normalise a relative path into POSIX form regardless of host OS."""
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return str(PurePosixPath(text)) if text else ""


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _node_text(node: Any) -> str:
    raw = node.text
    if isinstance(raw, bytes):
        return _lf(raw.decode("utf-8", errors="replace"))
    return _lf(str(raw or ""))


def _canonical_python_string(node: Any) -> str | None:
    """Canonicalise a simple Python string literal without interpolations ('a' == "a")."""
    if any(child.type == "interpolation" for child in node.children):
        return None
    text = _node_text(node)
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError, TypeError):
        return None
    if isinstance(value, str):
        return "STR:" + json.dumps(_lf(value), ensure_ascii=False)
    if isinstance(value, bytes):
        return "BYTES:" + value.hex()
    return None


def _canonical_js_string(node: Any) -> str | None:
    """Canonicalise a JS/TS single- or double-quoted string literal ('a' == "a")."""
    text = _node_text(node)
    if len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]:
        inner = text[1:-1].replace("\\'", "'").replace('\\"', '"')
        return "STR:" + json.dumps(_lf(inner), ensure_ascii=False)
    return None


def _next_significant_child(children: list[Any], index: int) -> Any | None:
    for pos in range(index + 1, len(children)):
        if children[pos].type not in _COMMENT_NODE_TYPES:
            return children[pos]
    return None


def normalize_symbol_tokens(node: Any, language: str) -> list[str]:
    """Return the canonical token list for a tree-sitter symbol AST subtree."""
    tokens: list[str] = []

    def emit(current: Any, parent_type: str | None) -> None:
        ntype = current.type
        if ntype in _COMMENT_NODE_TYPES:
            return
        # Line continuation token in Python (`\`) is pure formatting.
        if ntype == "line_continuation":
            return
        # Canonicalise simple string literals across quote styles.
        if language == "python" and ntype == "string":
            canonical = _canonical_python_string(current)
            if canonical is not None:
                tokens.append(canonical)
                return
        elif language != "python" and ntype == "string":
            canonical = _canonical_js_string(current)
            if canonical is not None:
                tokens.append(canonical)
                return

        children = list(current.children)
        if not children:
            text = _node_text(current).strip()
            if not text:
                return
            if language != "python" and text == ";" and parent_type in _JS_SEMI_PARENTS:
                return
            tokens.append(text)
            return

        wrap_block = ntype in _BLOCK_NODE_TYPES
        if wrap_block:
            tokens.append("\x01B")
        for idx, child in enumerate(children):
            ctype = child.type
            if ctype in _COMMENT_NODE_TYPES or ctype == "line_continuation":
                continue
            # Strip trailing commas immediately preceding a closing delimiter.
            if not child.children and _node_text(child).strip() == ",":
                nxt = _next_significant_child(children, idx)
                if nxt is not None and not nxt.children and _node_text(nxt).strip() in _CLOSING_DELIMITERS:
                    continue
            emit(child, ntype)
        if wrap_block:
            tokens.append("\x02B")

    emit(node, None)
    return tokens


def symbol_body_hash(node: Any, language: str) -> str:
    """Compute a deterministic cross-OS SHA-256 over a symbol's normalised AST body."""
    tokens = normalize_symbol_tokens(node, language)
    payload = f"v{NORM_VERSION}\x1f{language}\x1f" + "\x1f".join(tokens)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enclosing_chain(node: Any) -> list[tuple[str, str]]:
    """Return outer [(kind, name), ...] enclosing `node`, outermost first."""
    chain: list[tuple[str, str]] = []
    cur = node.parent
    while cur is not None:
        ctype = cur.type
        if ctype in {"class_definition", "class_declaration", "abstract_class_declaration"}:
            name = _node_text(cur.child_by_field_name("name")).strip() if cur.child_by_field_name("name") else ""
            if name:
                chain.append(("class", name))
        elif ctype in {
            "function_definition",
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
        }:
            name = _node_text(cur.child_by_field_name("name")).strip() if cur.child_by_field_name("name") else ""
            if name:
                chain.append(("function", name))
        cur = cur.parent
    chain.reverse()
    return chain


def symbol_ast_target(node: Any) -> Any:
    """Include Python decorators wrapping a function/class definition in its body hash."""
    parent = getattr(node, "parent", None)
    if parent is not None and parent.type == "decorated_definition":
        return parent
    return node


def _strip_comment_syntax(raw: str, language: str) -> list[str]:
    """Strip `#`, `//` or `/* ... */` markers and return trimmed lines."""
    text = _lf(raw).strip()
    if language == "python":
        if text.startswith("#"):
            return [text[1:].strip()]
        return [text]
    if text.startswith("//"):
        return [text[2:].strip()]
    if text.startswith("/*"):
        body = text[2:-2] if text.endswith("*/") else text[2:]
        lines = []
        for part in body.split("\n"):
            cleaned = part.strip()
            if cleaned.startswith("*"):
                cleaned = cleaned[1:].strip()
            lines.append(cleaned)
        return lines
    return [text]


def _normalize_comment_text(text: str) -> str:
    return " ".join(_lf(text).split()).strip()


def _bind_comment_to_symbol(
    line_start: int,
    line_end: int,
    anchor_symbols: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the symbol this comment documents: an immediately following nested/top-level symbol,
    or otherwise the innermost enclosing function/class/method."""
    enclosing = [
        sym for sym in anchor_symbols
        if sym["line_start"] <= line_start and sym["line_end"] >= line_end
    ]
    innermost = (
        min(enclosing, key=lambda sym: (sym["line_end"] - sym["line_start"], sym["line_start"]))
        if enclosing
        else None
    )
    # Leading comment attached directly above a symbol or its decorators (at most 1 line gap).
    # When already inside an enclosing symbol (e.g. a class body), only bind to a following
    # symbol that lives inside that same enclosing scope (e.g. a method of that class).
    following = [
        sym for sym in anchor_symbols
        if 0 < (sym.get("decorator_line_start", sym["line_start"]) - line_end) <= 1
        and (
            innermost is None
            or (
                sym is not innermost
                and sym["line_start"] >= innermost["line_start"]
                and sym["line_end"] <= innermost["line_end"]
            )
        )
    ]
    if following:
        return min(following, key=lambda sym: (sym["line_start"], sym["line_end"] - sym["line_start"]))
    return innermost


def extract_tagged_comments(
    root: Any,
    language: str,
    symbols: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract `DECISION:`, `INVARIANT:`, and `WHY:` comments bound to their enclosing symbol."""
    anchor_symbols = [
        sym for sym in symbols
        if sym.get("kind") in SYMBOL_ANCHOR_KINDS and isinstance(sym.get("body_hash"), str)
    ]
    # Collect comment AST nodes in source order.
    raw_comments: list[Any] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _COMMENT_NODE_TYPES:
            raw_comments.append(node)
        else:
            stack.extend(reversed(node.children))
    raw_comments.sort(key=lambda n: (n.start_point.row, n.start_point.column))

    results: list[dict[str, Any]] = []
    index = 0
    while index < len(raw_comments):
        node = raw_comments[index]
        start_row = node.start_point.row + 1
        end_row = node.end_point.row + 1
        start_col = node.start_point.column
        raw_text = _node_text(node)
        lines = _strip_comment_syntax(raw_text, language)
        if not lines:
            index += 1
            continue

        # Block comments can contain one or more tagged paragraphs; single-line comments merge continuations.
        if raw_text.lstrip().startswith("/*"):
            cur_tag: str | None = None
            cur_parts: list[str] = []

            def flush_block(
                block_start: int = start_row,
                block_end: int = end_row,
                block_raw: str = raw_text,
            ) -> None:
                nonlocal cur_tag, cur_parts
                if cur_tag is None:
                    return
                body = _normalize_comment_text(" ".join(cur_parts))
                if body:
                    bound = _bind_comment_to_symbol(block_start, block_end, anchor_symbols)
                    results.append(_make_comment_entry(cur_tag, body, block_start, block_end, bound, block_raw))
                cur_tag, cur_parts = None, []

            for line in lines:
                match = _TAG_LINE_RE.match(line)
                if match:
                    flush_block()
                    cur_tag = match.group(1)
                    cur_parts = [match.group(2)] if match.group(2) else []
                elif cur_tag is not None:
                    if line:
                        cur_parts.append(line)
                    else:
                        flush_block()
            flush_block()
            index += 1
            continue

        first_line = lines[0]
        match = _TAG_LINE_RE.match(first_line)
        if not match:
            index += 1
            continue

        tag = match.group(1)
        parts = [match.group(2)] if match.group(2) else []
        raw_parts = [raw_text]
        last_row = end_row
        next_idx = index + 1
        while next_idx < len(raw_comments):
            nxt = raw_comments[next_idx]
            nxt_raw = _node_text(nxt)
            if nxt_raw.lstrip().startswith("/*"):
                break
            nxt_row = nxt.start_point.row + 1
            if nxt_row != last_row + 1 or nxt.start_point.column != start_col:
                break
            nxt_lines = _strip_comment_syntax(nxt_raw, language)
            if not nxt_lines or not nxt_lines[0] or _TAG_LINE_RE.match(nxt_lines[0]):
                break
            parts.append(nxt_lines[0])
            raw_parts.append(nxt_raw)
            last_row = nxt.end_point.row + 1
            next_idx += 1

        body = _normalize_comment_text(" ".join(parts))
        if body:
            bound = _bind_comment_to_symbol(start_row, last_row, anchor_symbols)
            results.append(_make_comment_entry(tag, body, start_row, last_row, bound, raw_parts[0]))
        index = next_idx

    return results


def _make_comment_entry(
    tag: str,
    body: str,
    line_start: int,
    line_end: int,
    bound: dict[str, Any] | None,
    raw: str | None = None,
) -> dict[str, Any]:
    return {
        "tag": tag,
        "kind": COMMENT_TAGS[tag],
        "text": body,
        "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "raw": raw or f"{tag}: {body}",
        "line_start": line_start,
        "line_end": line_end,
        "symbol": (bound.get("qualified_name") or bound.get("name")) if bound else None,
        "symbol_kind": bound.get("kind") if bound else None,
        "symbol_body_hash": bound.get("body_hash") if bound else None,
    }


def make_symbol_anchor(path: str | Path, symbol: str, kind: str, body_hash: str) -> dict[str, Any]:
    return {
        "path": posix_path(path),
        "symbol": symbol,
        "kind": kind,
        "body_hash": body_hash,
        "norm_version": NORM_VERSION,
    }


def validate_symbol_anchor(value: Any) -> dict[str, Any] | None:
    """Return a clean copy of a well-formed symbol_anchor dict, or None if invalid/absent."""
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    symbol = value.get("symbol")
    kind = value.get("kind")
    body_hash = value.get("body_hash")
    if (
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(symbol, str)
        or not symbol.strip()
        or kind not in SYMBOL_ANCHOR_KINDS
        or not isinstance(body_hash, str)
        or len(body_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in body_hash)
    ):
        return None
    return make_symbol_anchor(path, symbol.strip(), kind, body_hash)


def resolve_symbol_anchor(
    relative_path: str | Path,
    source_text: str,
    analysis: dict[str, Any] | None,
    *,
    quote: str | None = None,
    symbol_name: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a canonical `symbol_anchor` from a file analysis plus an explicit symbol or quote."""
    if not isinstance(analysis, dict):
        return None
    candidates = [
        sym for sym in (analysis.get("symbols") or [])
        if isinstance(sym, dict)
        and sym.get("kind") in SYMBOL_ANCHOR_KINDS
        and isinstance(sym.get("body_hash"), str)
        and len(sym["body_hash"]) == 64
    ]
    if not candidates:
        return None

    if isinstance(symbol_name, str) and symbol_name.strip():
        target = symbol_name.strip()
        exact = [sym for sym in candidates if (sym.get("qualified_name") or sym.get("name")) == target]
        if len(exact) == 1:
            chosen = exact[0]
            return make_symbol_anchor(
                relative_path,
                chosen.get("qualified_name") or chosen["name"],
                chosen["kind"],
                chosen["body_hash"],
            )
        bare = [sym for sym in candidates if sym.get("name") == target]
        if len(bare) == 1:
            chosen = bare[0]
            return make_symbol_anchor(
                relative_path,
                chosen.get("qualified_name") or chosen["name"],
                chosen["kind"],
                chosen["body_hash"],
            )
        return None

    if isinstance(quote, str) and quote.strip():
        norm_src = _lf(source_text)
        norm_quote = _lf(quote)
        pos = norm_src.find(norm_quote)
        if pos < 0:
            norm_quote = norm_quote.strip()
            pos = norm_src.find(norm_quote)
        if pos < 0:
            return None
        line_start = norm_src[:pos].count("\n") + 1
        line_end = line_start + norm_quote.count("\n")
        enclosing = [
            sym for sym in candidates
            if sym.get("decorator_line_start", sym["line_start"]) <= line_start
            and sym["line_end"] >= line_end
        ]
        if enclosing:
            chosen = min(enclosing, key=lambda sym: (sym["line_end"] - sym["line_start"], sym["line_start"]))
            return make_symbol_anchor(
                relative_path,
                chosen.get("qualified_name") or chosen["name"],
                chosen["kind"],
                chosen["body_hash"],
            )
    return None


def find_symbol_in_analysis(
    analysis: dict[str, Any] | None,
    symbol_name: str,
    kind: str | None = None,
) -> dict[str, Any] | None:
    """Look up a symbol in an analysis dict by qualified_name (or unique bare name fallback)."""
    if not isinstance(analysis, dict):
        return None
    symbols = [
        sym for sym in (analysis.get("symbols") or [])
        if isinstance(sym, dict)
        and sym.get("kind") in SYMBOL_ANCHOR_KINDS
        and isinstance(sym.get("body_hash"), str)
    ]
    exact = [
        sym for sym in symbols
        if (sym.get("qualified_name") or sym.get("name")) == symbol_name
        and (kind is None or sym.get("kind") == kind)
    ]
    if exact:
        return exact[0]
    exact_any_kind = [sym for sym in symbols if (sym.get("qualified_name") or sym.get("name")) == symbol_name]
    if exact_any_kind:
        return exact_any_kind[0]
    bare = [sym for sym in symbols if sym.get("name") == symbol_name and (kind is None or sym.get("kind") == kind)]
    if len(bare) == 1:
        return bare[0]
    return None
