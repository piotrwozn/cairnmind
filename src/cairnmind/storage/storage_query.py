"""Backend-neutral structured query, filter, and mutation primitives for StorageBackend.

Replaces raw ArcadeDB SQL string assembly across business modules with a small,
explicit, injection-proof AST compiled natively by both ``ArcadeBackend`` and
``SQLiteBackend`` (without regex SQL dialect transpilation).
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from cairnmind.storage.sql import IDENTIFIER, _arcade_ident, json_text, quote_name

FIELD_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
VALID_OPS = frozenset({"=", "!=", "<", "<=", ">", ">=", "in", "not_in", "is_null", "is_not_null", "like"})
# SQLite builds often cap SQLITE_MAX_VARIABLE_NUMBER at 999 or 32766; switch to TEMP TABLE well below both.
SQLITE_TEMP_TABLE_THRESHOLD = 400


@dataclass(frozen=True)
class FieldRef:
    """Reference to another field on the same document (e.g., ``compacted_at < updated_at``)."""

    field: str

    def __post_init__(self) -> None:
        validate_field_path(self.field)


@dataclass(frozen=True)
class FieldCondition:
    field: str
    op: str
    value: Any = None

    def __post_init__(self) -> None:
        validate_field_path(self.field)
        if self.op not in VALID_OPS:
            raise ValueError(f"Unsupported filter operator: {self.op!r}")


@dataclass(frozen=True)
class OrGroup:
    conditions: tuple[FilterNode, ...]


@dataclass(frozen=True)
class AndGroup:
    conditions: tuple[FilterNode, ...]


FilterNode = FieldCondition | OrGroup | AndGroup
WhereInput = None | dict[str, Any] | FilterNode | Sequence[FilterNode]


@dataclass(frozen=True)
class OrderSpec:
    field: str
    desc: bool = False

    def __post_init__(self) -> None:
        validate_field_path(self.field)


@dataclass(frozen=True)
class ColumnSpec:
    field: str
    alias: str | None = None

    def __post_init__(self) -> None:
        validate_field_path(self.field)
        if self.alias is not None and not IDENTIFIER.fullmatch(self.alias):
            raise ValueError(f"Invalid column alias: {self.alias!r}")


def validate_field_path(field: str) -> str:
    if not isinstance(field, str) or not FIELD_PATH_RE.fullmatch(field):
        raise ValueError(f"Invalid database field path: {field!r}")
    return field


def eq(field: str, value: Any) -> FieldCondition:
    return FieldCondition(field, "=", value)


def ne(field: str, value: Any) -> FieldCondition:
    return FieldCondition(field, "!=", value)


def lt(field: str, value: Any) -> FieldCondition:
    return FieldCondition(field, "<", value)


def lte(field: str, value: Any) -> FieldCondition:
    return FieldCondition(field, "<=", value)


def gt(field: str, value: Any) -> FieldCondition:
    return FieldCondition(field, ">", value)


def gte(field: str, value: Any) -> FieldCondition:
    return FieldCondition(field, ">=", value)


def is_in(field: str, values: Iterable[Any]) -> FieldCondition:
    return FieldCondition(field, "in", tuple(values))


def not_in(field: str, values: Iterable[Any]) -> FieldCondition:
    return FieldCondition(field, "not_in", tuple(values))


def is_null(field: str) -> FieldCondition:
    return FieldCondition(field, "is_null", None)


def is_not_null(field: str) -> FieldCondition:
    return FieldCondition(field, "is_not_null", None)


def like(field: str, pattern: str) -> FieldCondition:
    return FieldCondition(field, "like", pattern)


def any_of(*conditions: FilterNode) -> OrGroup:
    return OrGroup(tuple(conditions))


def all_of(*conditions: FilterNode) -> AndGroup:
    return AndGroup(tuple(conditions))


def asc(field: str) -> OrderSpec:
    return OrderSpec(field, desc=False)


def desc(field: str) -> OrderSpec:
    return OrderSpec(field, desc=True)


def col(field: str, alias: str | None = None) -> ColumnSpec:
    return ColumnSpec(field, alias=alias)


def field_ref(field: str) -> FieldRef:
    return FieldRef(field)


def normalize_where(where: WhereInput) -> tuple[FilterNode, ...]:
    if where is None:
        return ()
    if isinstance(where, dict):
        return tuple(eq(k, v) for k, v in where.items())
    if isinstance(where, (FieldCondition, OrGroup, AndGroup)):
        return (where,)
    return tuple(where)


def normalize_columns(columns: Sequence[str | tuple[str, str] | ColumnSpec] | None) -> tuple[ColumnSpec, ...] | None:
    if columns is None:
        return None
    out: list[ColumnSpec] = []
    for item in columns:
        if isinstance(item, ColumnSpec):
            out.append(item)
        elif isinstance(item, tuple):
            out.append(ColumnSpec(item[0], alias=item[1]))
        else:
            out.append(ColumnSpec(item))
    return tuple(out)


def normalize_order_by(order_by: Sequence[str | tuple[str, str] | OrderSpec] | None) -> tuple[OrderSpec, ...]:
    if not order_by:
        return ()
    out: list[OrderSpec] = []
    for item in order_by:
        if isinstance(item, OrderSpec):
            out.append(item)
        elif isinstance(item, tuple):
            field, direction = item
            out.append(OrderSpec(field, desc=direction.strip().upper() == "DESC"))
        else:
            cleaned = item.strip()
            if cleaned.startswith("-"):
                out.append(OrderSpec(cleaned[1:], desc=True))
            else:
                out.append(OrderSpec(cleaned, desc=False))
    return tuple(out)


def quote_arcade_field(field: str) -> str:
    validate_field_path(field)
    parts = field.split(".")
    return ".".join(_arcade_ident(part) for part in parts)


def compile_arcade_where(
    where: WhereInput,
    *,
    param_prefix: str = "w",
) -> tuple[str, dict[str, Any]]:
    """Compile a backend-neutral filter tree into parameterized ArcadeDB SQL."""
    nodes = normalize_where(where)
    if not nodes:
        return "", {}
    params: dict[str, Any] = {}
    counter = [0]

    def _next_param(value: Any) -> str:
        name = f"{param_prefix}{counter[0]}"
        counter[0] += 1
        params[name] = value
        return f":{name}"

    def _compile(node: FilterNode) -> str:
        if isinstance(node, AndGroup):
            if not node.conditions:
                return "1=1"
            return "(" + " AND ".join(_compile(c) for c in node.conditions) + ")"
        if isinstance(node, OrGroup):
            if not node.conditions:
                return "1=0"
            return "(" + " OR ".join(_compile(c) for c in node.conditions) + ")"
        expr = quote_arcade_field(node.field)
        op = node.op
        if op == "is_null":
            return f"{expr} IS NULL"
        if op == "is_not_null":
            return f"{expr} IS NOT NULL"
        if op in {"in", "not_in"}:
            items = list(node.value or ())
            if not items:
                return "1=1" if op == "not_in" else "1=0"
            pname = _next_param(items)
            kw = "NOT IN" if op == "not_in" else "IN"
            return f"{expr} {kw} {pname}"
        if op == "like":
            return f"{expr} LIKE {_next_param(node.value)}"
        if isinstance(node.value, FieldRef):
            rhs = quote_arcade_field(node.value.field)
            return f"{expr}<>{rhs}" if op == "!=" else f"{expr}{op}{rhs}"
        if op == "!=":
            return f"{expr}<>{_next_param(node.value)}"
        return f"{expr}{op}{_next_param(node.value)}"

    return " AND ".join(_compile(n) for n in nodes), params


def sqlite_field_expr(table_cols: set[str], field: str, *, table_alias: str = "") -> str:
    validate_field_path(field)
    prefix = f"{table_alias}." if table_alias else ""
    if "." not in field and field in table_cols:
        return f'{prefix}"{field}"'
    return f"json_extract({prefix}doc, '$.{field}')"


def _sqlite_param_val(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list)):
        return json_text(value)
    return value


def populate_sqlite_temp_ids(
    conn: sqlite3.Connection,
    values: Sequence[Any],
    *,
    slot: int,
) -> str:
    """Populate a connection-local TEMP TABLE for large IN/NOT IN filters (> 400 items)."""
    temp_name = f"_cm_in_{slot}"
    conn.execute(f'CREATE TEMP TABLE IF NOT EXISTS "{temp_name}" (val TEXT PRIMARY KEY)')
    conn.execute(f'DELETE FROM "{temp_name}"')
    conn.executemany(
        f'INSERT OR IGNORE INTO "{temp_name}"(val) VALUES (?)',
        [(_sqlite_param_val(v),) for v in values],
    )
    return temp_name


def sqlite_id_filter_sql(
    conn: sqlite3.Connection,
    expr: str,
    values: Sequence[Any],
    *,
    negated: bool = False,
    slot: int = 0,
) -> tuple[str, list[Any]]:
    """Build an IN / NOT IN SQLite predicate safe for arbitrarily large value lists."""
    items = list(values)
    if not items:
        return ("1=1" if negated else "0=1"), []
    kw = "NOT IN" if negated else "IN"
    if len(items) <= SQLITE_TEMP_TABLE_THRESHOLD:
        placeholders = ", ".join("?" for _ in items)
        return f"{expr} {kw} ({placeholders})", [_sqlite_param_val(v) for v in items]
    temp_name = populate_sqlite_temp_ids(conn, items, slot=slot)
    return f'{expr} {kw} (SELECT val FROM "{temp_name}")', []


def compile_sqlite_where(
    conn: sqlite3.Connection,
    table_cols: set[str],
    where: WhereInput,
    *,
    table_alias: str = "",
) -> tuple[str, list[Any]]:
    """Compile a backend-neutral filter tree into native SQLite SQL with TEMP TABLE safety."""
    nodes = normalize_where(where)
    if not nodes:
        return "", []
    params: list[Any] = []
    temp_slot = [0]

    def _compile(node: FilterNode) -> str:
        if isinstance(node, AndGroup):
            if not node.conditions:
                return "1=1"
            return "(" + " AND ".join(_compile(c) for c in node.conditions) + ")"
        if isinstance(node, OrGroup):
            if not node.conditions:
                return "0=1"
            return "(" + " OR ".join(_compile(c) for c in node.conditions) + ")"
        expr = sqlite_field_expr(table_cols, node.field, table_alias=table_alias)
        op = node.op
        if op == "is_null":
            return f"{expr} IS NULL"
        if op == "is_not_null":
            return f"{expr} IS NOT NULL"
        if op in {"in", "not_in"}:
            slot = temp_slot[0]
            temp_slot[0] += 1
            clause, extra_params = sqlite_id_filter_sql(
                conn, expr, list(node.value or ()), negated=(op == "not_in"), slot=slot
            )
            params.extend(extra_params)
            return clause
        if op == "like":
            params.append(_sqlite_param_val(node.value))
            return f"{expr} LIKE ?"
        if isinstance(node.value, FieldRef):
            rhs = sqlite_field_expr(table_cols, node.value.field, table_alias=table_alias)
            return f"{expr} {op} {rhs}"
        if op == "!=":
            params.append(_sqlite_param_val(node.value))
            return f"{expr} != ?"
        params.append(_sqlite_param_val(node.value))
        return f"{expr} {op} ?"

    return " AND ".join(_compile(n) for n in nodes), params


def apply_nested_updates(
    doc: dict[str, Any],
    set_fields: dict[str, Any] | None = None,
    remove_fields: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a copy of `doc` with dotted `set_fields` written and `remove_fields` popped."""
    updated = deepcopy(doc)
    for field_path, val in (set_fields or {}).items():
        validate_field_path(field_path)
        parts = field_path.split(".")
        if len(parts) == 1:
            updated[parts[0]] = val
        else:
            cursor = updated
            for segment in parts[:-1]:
                existing = cursor.get(segment)
                if not isinstance(existing, dict):
                    existing = {}
                    cursor[segment] = existing
                cursor = existing
            cursor[parts[-1]] = val
    for field_path in remove_fields:
        validate_field_path(field_path)
        parts = field_path.split(".")
        if len(parts) == 1:
            updated.pop(parts[0], None)
        else:
            cursor = updated
            for segment in parts[:-1]:
                existing = cursor.get(segment)
                if not isinstance(existing, dict):
                    cursor = None
                    break
                cursor = existing
            if isinstance(cursor, dict):
                cursor.pop(parts[-1], None)
    return updated


def extract_field_path(row: dict[str, Any], field: str) -> Any:
    """Read a top-level or dotted field path from a JSON document dict."""
    parts = field.split(".")
    cur: Any = row
    for part in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def split_sql_statements(sql_script: str) -> list[str]:
    """Quote- and comment-aware SQL statement splitter (replaces fragile `.split(';\\n')`)."""
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(sql_script)
    while i < n:
        ch = sql_script[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(sql_script[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and sql_script[i + 1] == quote:
                    buf.append(sql_script[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql_script[i + 1] == "-":
            while i < n and sql_script[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql_script[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql_script[i] == "*" and sql_script[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def missing_row_message(collection: str) -> str:
    from cairnmind.storage.sql import MISSING_KIND
    return f"{MISSING_KIND.get(collection, 'Record')} does not exist"


def _build_arcade_select(
    collection: str,
    where: WhereInput = (),
    *,
    columns: Sequence[str | tuple[str, str] | ColumnSpec] | None = None,
    order_by: Sequence[str | tuple[str, str] | OrderSpec] | None = None,
    limit: int | None = None,
    distinct: bool = False,
) -> tuple[str, dict[str, Any]]:
    col_specs = normalize_columns(columns)
    order_specs = normalize_order_by(order_by)
    where_sql, params = compile_arcade_where(where)
    if col_specs:
        parts = []
        for spec in col_specs:
            expr = quote_arcade_field(spec.field)
            if spec.alias:
                parts.append(f"{expr} AS {_arcade_ident(spec.alias)}")
            else:
                parts.append(expr)
        select_expr = ("DISTINCT " if distinct else "") + ", ".join(parts)
        head = f"SELECT {select_expr} FROM {_arcade_ident(collection)}"
    else:
        head = f"SELECT FROM {_arcade_ident(collection)}"
    clauses = [head]
    if where_sql:
        clauses.append(f"WHERE {where_sql}")
    if order_specs:
        order_parts = [
            f"{quote_arcade_field(spec.field)} {'DESC' if spec.desc else 'ASC'}"
            for spec in order_specs
        ]
        clauses.append("ORDER BY " + ", ".join(order_parts))
    if limit is not None:
        clauses.append(f"LIMIT {max(0, int(limit))}")
    return " ".join(clauses), params


def db_find(
    target: Any,
    collection: str,
    where: WhereInput = (),
    *,
    columns: Sequence[str | tuple[str, str] | ColumnSpec] | None = None,
    order_by: Sequence[str | tuple[str, str] | OrderSpec] | None = None,
    limit: int | None = None,
    distinct: bool = False,
) -> list[dict[str, Any]]:
    """Run a backend-neutral structured document query against a Store, tx, backend, or test fake."""
    if callable(getattr(target, "find", None)):
        return target.find(
            collection,
            where,
            columns=columns,
            order_by=order_by,
            limit=limit,
            distinct=distinct,
        )
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "find"):
        return backend.find(
            collection,
            where,
            columns=columns,
            order_by=order_by,
            limit=limit,
            distinct=distinct,
            reader=target,
        )
    # Fallback strictly for minimal unit-test FakeStore doubles exposing only `.query(sql, params)`.
    sql, params = _build_arcade_select(
        collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct
    )
    return list(target.query(sql, params))


def db_find_one(
    target: Any,
    collection: str,
    where: WhereInput = (),
    *,
    columns: Sequence[str | tuple[str, str] | ColumnSpec] | None = None,
    order_by: Sequence[str | tuple[str, str] | OrderSpec] | None = None,
    required: bool = False,
) -> dict[str, Any] | None:
    """Return the first matching row or None (raising ValueError when `required=True`)."""
    if callable(getattr(target, "find_one", None)):
        return target.find_one(
            collection, where, columns=columns, order_by=order_by, required=required
        )
    rows = db_find(target, collection, where, columns=columns, order_by=order_by, limit=1)
    if not rows:
        if required:
            raise ValueError(missing_row_message(collection))
        return None
    return rows[0]


def db_count(target: Any, collection: str, where: WhereInput = ()) -> int:
    """Count matching rows in `collection` without raw SQL."""
    if callable(getattr(target, "count", None)):
        return int(target.count(collection, where))
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "count"):
        return int(backend.count(collection, where, reader=target))
    where_sql, params = compile_arcade_where(where)
    clause = f" WHERE {where_sql}" if where_sql else ""
    rows = target.query(f"SELECT count(*) AS total FROM {_arcade_ident(collection)}{clause}", params)
    if not rows:
        return 0
    row = rows[0]
    for key in ("total", "n", "c", "count"):
        if key in row and row[key] is not None:
            return int(row[key])
    return int(next(iter(row.values()), 0))


def db_group_counts(
    target: Any,
    collection: str,
    group_by: Sequence[str],
    where: WhereInput = (),
    *,
    count_alias: str = "total",
) -> list[dict[str, Any]]:
    """Aggregate row counts grouped by one or more fields."""
    if callable(getattr(target, "group_counts", None)):
        return target.group_counts(collection, group_by, where, count_alias=count_alias)
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "group_counts"):
        return backend.group_counts(collection, group_by, where, count_alias=count_alias, reader=target)
    fields = [quote_arcade_field(f) for f in group_by]
    where_sql, params = compile_arcade_where(where)
    clause = f" WHERE {where_sql}" if where_sql else ""
    group_expr = ", ".join(fields)
    sql = (
        f"SELECT {group_expr}, count(*) AS {quote_name(count_alias)} "
        f"FROM {quote_name(collection)}{clause} GROUP BY {group_expr}"
    )
    return list(target.query(sql, params))


def db_update_where(
    target: Any,
    collection: str,
    *,
    set_fields: dict[str, Any] | None = None,
    remove_fields: Sequence[str] = (),
    where: WhereInput = (),
) -> list[dict[str, Any]]:
    """Structured update/remove mutation against a Store, tx, or StorageBackend."""
    if callable(getattr(target, "update_where", None)):
        return target.update_where(
            collection, set_fields=set_fields, remove_fields=remove_fields, where=where
        )
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "update_where"):
        return backend.update_where(
            target, collection, set_fields=set_fields, remove_fields=remove_fields, where=where
        )
    # Fallback for unit-test FakeTx doubles
    where_sql, params = compile_arcade_where(where)
    clause = f" WHERE {where_sql}" if where_sql else ""
    out: list[dict[str, Any]] = []
    if set_fields:
        assignments = []
        set_params = dict(params)
        for idx, (field, value) in enumerate(set_fields.items()):
            pname = f"set_{idx}"
            assignments.append(f"{quote_arcade_field(field)}=:{pname}")
            set_params[pname] = value
        out = list(target.command(f"UPDATE {quote_name(collection)} SET {', '.join(assignments)}{clause}", set_params))
    if remove_fields:
        rem = ", ".join(quote_arcade_field(f) for f in remove_fields)
        out = list(target.command(f"UPDATE {quote_name(collection)} REMOVE {rem}{clause}", params))
    return out


def db_delete_where(
    target: Any,
    collection: str,
    where: WhereInput = (),
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Structured delete mutation against a Store, tx, or StorageBackend."""
    if callable(getattr(target, "delete_where", None)):
        return target.delete_where(collection, where, limit=limit)
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "delete_where"):
        return backend.delete_where(target, collection, where, limit=limit)
    where_sql, params = compile_arcade_where(where)
    clause = f" WHERE {where_sql}" if where_sql else ""
    lim = f" LIMIT {max(0, int(limit))}" if limit is not None else ""
    return list(target.command(f"DELETE FROM {_arcade_ident(collection)}{clause}{lim}", params))


def db_insert_document(target: Any, collection: str, data: dict[str, Any]) -> dict[str, Any]:
    """Insert a single document through the structured StorageBackend / transaction API."""
    if callable(getattr(target, "insert_document", None)):
        return target.insert_document(collection, data)
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "insert_document"):
        return backend.insert_document(target, collection, data)
    from cairnmind.storage.sql import insert_document
    insert_document(target, collection, data)
    return data


def db_insert_documents(target: Any, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch-insert documents through the structured StorageBackend / transaction API."""
    if callable(getattr(target, "insert_documents", None)):
        return target.insert_documents(collection, rows)
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "insert_documents"):
        return backend.insert_documents(target, collection, rows)
    stored = target.command(f"INSERT INTO {_arcade_ident(collection)} CONTENT :rows", {"rows": rows})
    return list(stored) if isinstance(stored, list) else []


def db_list_collections(target: Any) -> set[str]:
    """Return existing collection names without raw SQL in callers."""
    if callable(getattr(target, "list_collections", None)):
        return set(target.list_collections())
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "list_collections"):
        return set(backend.list_collections(target))
    return {
        str(row.get("name"))
        for row in target.query("SELECT name FROM schema:types")
        if row.get("name")
    }


def db_server_time_ms(target: Any) -> int:
    """Return the database server clock in epoch milliseconds without raw SQL in callers."""
    if callable(getattr(target, "server_time_ms", None)):
        return int(target.server_time_ms())
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "server_time_ms"):
        return int(backend.server_time_ms(target))
    rows = target.query("SELECT sysdate().asLong() AS milliseconds")
    milliseconds = rows[0]["milliseconds"] if rows else None
    if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
        raise ValueError("The database server gave no time; the writer row was not changed.")
    return milliseconds


def db_upsert_document(
    target: Any,
    collection: str,
    data: dict[str, Any],
    *,
    key_field: str = "id",
) -> dict[str, Any]:
    """Upsert a document by `key_field` through the structured StorageBackend / transaction API."""
    if callable(getattr(target, "upsert_document", None)):
        return target.upsert_document(collection, data, key_field=key_field)
    backend = getattr(target, "backend", None)
    if backend is not None and hasattr(backend, "upsert_document"):
        return backend.upsert_document(target, collection, data, key_field=key_field)
    if callable(getattr(target, "transaction", None)):
        with target.transaction() as tx:
            return db_upsert_document(tx, collection, data, key_field=key_field)
    setters = ",".join(f"{quote_name(key)}=:value_{key}" for key in data)
    params = {f"value_{key}": value for key, value in data.items()}
    params["upsert_key"] = data[key_field]
    target.command(
        f"UPDATE {_arcade_ident(collection)} SET {setters} UPSERT WHERE {_arcade_ident(key_field)}=:upsert_key",
        params,
    )
    return data

