"""Durable metadata-only invalidations committed with their domain mutations.

The cursor is a database epoch plus a monotonic sequence. Events are hints to
refetch authoritative rows; they never contain document, model or PTY content.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from uuid import UUID, uuid4

from .sql import json_text, quote_name


JOURNAL_TYPES = ("journal_state", "journal_events")
MAX_EVENTS = 50_000
MAX_TRANSACTION_EVENTS = 500
EVENTS_PER_STATEMENT = 250
TABLE_TYPES = {
    "nodes": "node", "edges": "edge",
    "projects": "project", "project_roots": "scope", "sources": "source", "chunks": "chunk",
    "records": "record", "record_revisions": "record", "plans": "plan", "plan_revisions": "plan",
    "plan_requests": "plan_request", "worktree_states": "scope", "jobs": "job", "embeddings": "embedding",
    "vector_indexes": "embedding_index", "import_cursors": "import", "ingestion_inbox": "inbox",
    "sessions": "session", "resume_packets": "session", "resume_acks": "session", "session_events": "session",
    "check_runs": "check", "memory_settings": "memory", "memory_snapshots": "memory",
    "memory_imports": "memory", "memory_bindings": "memory",
}
_MUTATION = re.compile(r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+VERTEX|CREATE\s+EDGE)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\b", re.I)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _clean(row):
    return {key: value for key, value in row.items() if not key.startswith("@") and key not in {"in", "out"}}


def mutation(sql):
    match = _MUTATION.match(sql)
    if not match:
        return None
    operation, table = " ".join(match[1].upper().split()), match[2]
    if table not in TABLE_TYPES and not table.startswith("vector_"):
        return None
    return operation, table


def _where(sql):
    quote, depth, index = None, 0, 0
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}" :
            depth -= 1
        elif depth == 0 and (index == 0 or not sql[index - 1].isalnum()) and sql[index:index + 5].upper() == "WHERE":
            end = index + 5
            if end == len(sql) or not sql[end].isalnum():
                return sql[end:].strip()
        index += 1
    return ""


def _inserted(sql, params, result):
    # These forms cover both the helper DAL and the direct importer statements.
    content = re.search(r"\bCONTENT\s+(.*)$", sql, re.I | re.S)
    if content:
        value = content[1].strip()
        if value.startswith(":"):
            row = params.get(value[1:])
        else:
            try:
                row = json.loads(value)
            except (ValueError, TypeError):
                row = None
        if isinstance(row, dict):
            return [_clean(row)]
    rows = [_clean(row) for row in result if isinstance(row, dict) and isinstance(row.get("id"), str)]
    if rows:
        return rows
    assignments = re.findall(r"(?:\bSET\s+|,)\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*=\s*:([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)
    row = {column: deepcopy(params[key]) for column, key in assignments if key in params}
    return [row] if isinstance(row.get("id"), str) else []


def _event_key(table, row):
    entity_type = TABLE_TYPES.get(table, "embedding")
    identifier = row.get("id")
    if table == "projects":
        project = identifier
    else:
        project = row.get("project_id")
    if table == "record_revisions":
        identifier = row.get("record_id")
    elif table == "plan_revisions":
        identifier = row.get("plan_id")
    elif table in {"resume_packets", "resume_acks", "session_events"}:
        identifier = row.get("session_id")
    elif entity_type == "memory":
        identifier = None
    if not isinstance(project, str):
        project = None
    if not isinstance(identifier, str) or len(identifier) > 512:
        identifier = None
    return project, entity_type, identifier


class JournalTransaction:
    """A Store transaction proxy; structured mutations and the final outbox share one commit.

    Structured methods (``insert_document``, ``insert_documents``, ``update_document``,
    ``upsert_document``, ``delete_documents``, ``create_vertex``, ``create_edge``) record
    journal diffs directly from explicit collection/row metadata without regex SQL parsing.
    ``command(sql, params)`` remains supported for backward compatibility.
    """

    def __init__(self, native, *, writer=None, check=None):
        self.native = native
        # The writing installation (#70): carried into a recreated journal head, and ``check(head, native)``
        # refuses the commit when the head read in flush names another writer (store_writer.check_head).
        self.writer = writer
        self.check = check
        self.changes = {}
        self.explicit = {}
        self.graph_resets = set()
        self.reset_reason = None
        # Facts read earlier in this transaction (Store.prefetch_graph). None means nothing is remembered.
        # Any write to these tables that is not made by the code that keeps them current forgets them all.
        self.known = None
        self.keeps_known = False

    def __getattr__(self, name):
        return getattr(self.native, name)

    def query(self, sql, params=None):
        return self.native.query(sql, params)

    def server_time_ms(self) -> int:
        if hasattr(self.native, "server_time_ms"):
            return int(self.native.server_time_ms())
        rows = self.native.query("SELECT sysdate().asLong() AS milliseconds")
        milliseconds = rows[0]["milliseconds"] if rows else None
        if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
            raise ValueError("The database server gave no time; the writer row was not changed.")
        return milliseconds

    @staticmethod
    def _is_tracked(table: str) -> bool:
        return table in TABLE_TYPES or table.startswith("vector_")

    def _invalidate_known(self, table: str) -> None:
        if self.known is not None and not self.keeps_known and table in {"projects", "sources", "nodes", "edges"}:
            self.known = None

    def _skip_graph_row_tracking(self, table: str, project) -> bool:
        return table in {"nodes", "edges"} and project in self.graph_resets

    def _record(self, table, before, after):
        first = {row["id"]: _clean(row) for row in before if isinstance(row.get("id"), str)}
        last = {row["id"]: _clean(row) for row in after if isinstance(row.get("id"), str)}
        for identifier in first.keys() | last.keys():
            key = table, identifier
            if key not in self.changes:
                self.changes[key] = [deepcopy(first.get(identifier)), None]
            self.changes[key][1] = deepcopy(last.get(identifier))

    def record_mutation(self, table: str, before: list[dict], after: list[dict], *, project=None) -> None:
        """Explicitly record a structured mutation without regex SQL parsing."""
        self._invalidate_known(table)
        if not self._is_tracked(table) or self.reset_reason is not None:
            return
        if self._skip_graph_row_tracking(table, project):
            return
        self._record(table, before, after)

    def insert_document(self, collection: str, data: dict) -> dict:
        self._invalidate_known(collection)
        if hasattr(self.native, "insert_document"):
            self.native.insert_document(collection, data)
        else:
            self.native.command(f"INSERT INTO {quote_name(collection)} CONTENT {json_text(data)}")
        if self._is_tracked(collection) and self.reset_reason is None:
            project = data.get("id") if collection == "projects" else data.get("project_id")
            if not self._skip_graph_row_tracking(collection, project):
                if isinstance(data.get("id"), str):
                    self._record(collection, [], [_clean(data)])
                else:
                    self.touch(project, TABLE_TYPES.get(collection, "embedding"), op="reset")
        return data

    def insert_documents(self, collection: str, rows: list[dict]) -> list[dict]:
        self._invalidate_known(collection)
        if hasattr(self.native, "insert_documents"):
            stored = self.native.insert_documents(collection, rows)
            if not isinstance(stored, list) or len(stored) != len(rows):
                raise RuntimeError(f"The database did not store every row of a group in {collection}")
            if self._is_tracked(collection) and self.reset_reason is None and rows:
                cleaned = [_clean(row) for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]
                if cleaned:
                    first_project = cleaned[0].get("id") if collection == "projects" else cleaned[0].get("project_id")
                    if not self._skip_graph_row_tracking(collection, first_project):
                        self._record(collection, [], cleaned)
            return stored
        stored = self.command(f"INSERT INTO {quote_name(collection)} CONTENT :rows", {"rows": rows})
        if not isinstance(stored, list) or len(stored) != len(rows):
            raise RuntimeError(f"The database did not store every row of a group in {collection}")
        return stored

    def update_document(self, collection: str, data: dict, where: str, params: dict) -> list[dict]:
        self._invalidate_known(collection)
        params = params or {}
        project = params.get("project_id", params.get("project"))
        tracked = (
            self._is_tracked(collection)
            and self.reset_reason is None
            and not self._skip_graph_row_tracking(collection, project)
        )
        selection = f"SELECT FROM {quote_name(collection)}" + (f" WHERE {where}" if where else "")
        before = self.native.query(selection, params) if tracked else []
        if hasattr(self.native, "update_document"):
            result = self.native.update_document(collection, data, where, params)
        else:
            assignments = ",".join(f"{quote_name(key)}=:value_{key}" for key in data)
            result = self.native.command(
                f"UPDATE {quote_name(collection)} SET {assignments} WHERE {where}",
                {**params, **{f"value_{key}": value for key, value in data.items()}},
            )
        if tracked:
            identifiers = {row["id"] for row in before if isinstance(row.get("id"), str)}
            identifiers.update(row["id"] for row in result if isinstance(row, dict) and isinstance(row.get("id"), str))
            if isinstance(data.get("id"), str):
                identifiers.add(data["id"])
            after = (
                self.native.query(
                    f"SELECT FROM {quote_name(collection)} WHERE id IN :journal_ids",
                    {"journal_ids": list(identifiers)},
                )
                if identifiers
                else self.native.query(selection, params)
            )
            self._record(collection, before, after)
        return result

    def upsert_document(self, collection: str, data: dict, key_field: str = "id") -> dict:
        self._invalidate_known(collection)
        key_val = data[key_field]
        project = data.get("id") if collection == "projects" else data.get("project_id")
        tracked = (
            self._is_tracked(collection)
            and self.reset_reason is None
            and not self._skip_graph_row_tracking(collection, project)
        )
        before = (
            self.native.query(
                f"SELECT FROM {quote_name(collection)} WHERE {quote_name(key_field)}=:upsert_key",
                {"upsert_key": key_val},
            )
            if tracked
            else []
        )
        if hasattr(self.native, "upsert_document"):
            self.native.upsert_document(collection, data, key_field=key_field)
        else:
            setters = ",".join(f"{quote_name(key)}=:value_{key}" for key in data)
            params = {f"value_{key}": value for key, value in data.items()}
            params["upsert_key"] = key_val
            self.native.command(
                f"UPDATE {quote_name(collection)} SET {setters} UPSERT WHERE {quote_name(key_field)}=:upsert_key",
                params,
            )
        if tracked:
            after = self.native.query(
                f"SELECT FROM {quote_name(collection)} WHERE {quote_name(key_field)}=:upsert_key",
                {"upsert_key": key_val},
            )
            self._record(collection, before, after or [_clean(data)])
        return data

    def delete_documents(self, collection: str, where: str = "", params: dict | None = None) -> list[dict]:
        self._invalidate_known(collection)
        params = params or {}
        project = params.get("project_id", params.get("project"))
        tracked = (
            self._is_tracked(collection)
            and self.reset_reason is None
            and not self._skip_graph_row_tracking(collection, project)
        )
        selection = f"SELECT FROM {quote_name(collection)}" + (f" WHERE {where}" if where else "")
        before = self.native.query(selection, params) if tracked else []
        if hasattr(self.native, "delete_documents"):
            result = self.native.delete_documents(collection, where, params)
        else:
            clause = f" WHERE {where}" if where else ""
            result = self.native.command(f"DELETE FROM {quote_name(collection)}{clause}", params)
        if tracked:
            if collection == "nodes":
                for row in before:
                    self.touch(row.get("project_id"), "graph", op="reset")
            self._record(collection, before, [])
        return result

    def create_vertex(self, collection: str, node: dict) -> dict:
        self._invalidate_known(collection)
        if hasattr(self.native, "create_vertex"):
            self.native.create_vertex(collection, node)
        else:
            self.native.command(f"CREATE VERTEX {quote_name(collection)} CONTENT {json_text(node)}")
        if self._is_tracked(collection) and self.reset_reason is None:
            project = node.get("project_id")
            if not self._skip_graph_row_tracking(collection, project) and isinstance(node.get("id"), str):
                self._record(collection, [], [_clean(node)])
        return node

    def create_edge(self, collection: str, edge: dict) -> dict:
        self._invalidate_known(collection)
        if hasattr(self.native, "create_edge"):
            self.native.create_edge(collection, edge)
        else:
            self.native.command(
                f"CREATE EDGE {quote_name(collection)} FROM (SELECT FROM nodes WHERE id=:from_id) "
                f"TO (SELECT FROM nodes WHERE id=:to_id) CONTENT {json_text(edge)}",
                {"from_id": edge["from_id"], "to_id": edge["to_id"]},
            )
        if self._is_tracked(collection) and self.reset_reason is None:
            project = edge.get("project_id")
            if not self._skip_graph_row_tracking(collection, project) and isinstance(edge.get("id"), str):
                self._record(collection, [], [_clean(edge)])
        return edge

    def command(self, sql, params=None):
        params = params or {}
        parsed = mutation(sql)
        if self.known is not None and not self.keeps_known and parsed is not None \
                and parsed[1] in {"projects", "sources", "nodes", "edges"}:
            self.known = None
        if parsed is None or self.reset_reason is not None:
            return self.native.command(sql, params)
        operation, table = parsed
        project = params.get("project_id", params.get("project"))
        if table in {"nodes", "edges"} and project in self.graph_resets:
            return self.native.command(sql, params)
        where = _where(sql)
        selection = f"SELECT FROM `{table}`" + (f" WHERE {where}" if where else "")
        before = self.native.query(selection, params) if operation in {"UPDATE", "DELETE FROM"} else []
        result = self.native.command(sql, params)
        if operation == "DELETE FROM":
            after = []
        elif operation == "UPDATE":
            identifiers = {row["id"] for row in before if isinstance(row.get("id"), str)}
            identifiers.update(row["id"] for row in result if isinstance(row, dict) and isinstance(row.get("id"), str))
            # CAS predicates commonly stop matching after a successful update.
            # Read the affected IDs, never infer deletion from the old predicate.
            prefix = sql[:len(sql) - len(where)] if where else sql
            assignment = re.search(r"(?:\bSET\s+|,)\s*`?id`?\s*=\s*:([A-Za-z_][A-Za-z0-9_]*)", prefix, re.I)
            if assignment and isinstance(params.get(assignment[1]), str):
                identifiers.add(params[assignment[1]])
            after = (self.native.query(f"SELECT FROM `{table}` WHERE id IN :journal_ids", {"journal_ids": list(identifiers)})
                     if identifiers else self.native.query(selection, params))
        else:
            after = _inserted(sql, params, result)
        if table == "nodes" and operation == "DELETE FROM":
            for row in before:
                self.touch(row.get("project_id"), "graph", op="reset")
        if not before and not after and operation != "DELETE FROM" and operation != "UPDATE":
            self.touch(project, TABLE_TYPES.get(table, "embedding"), op="reset")
        self._record(table, before, after)
        return result

    def script(self, statements, params, project_id):
        """Graph statements of one project sent in one request, the last of which may be a SELECT that checks them.

        Only in a transaction that reports that project's graph as reset: the journal then names no single
        node or edge of it, so it needs none of the rows these statements write (see ``command``).
        """
        if self.reset_reason is None and project_id not in self.graph_resets:
            raise ValueError("Grouped graph statements need a transaction that reports the project's graph as reset")
        if not isinstance(statements, list) or not statements:
            raise ValueError("Grouped graph statements require a list of statements")
        for index, sql in enumerate(statements):
            parsed = mutation(sql) if isinstance(sql, str) else None
            checking = isinstance(sql, str) and index == len(statements) - 1 and sql.lstrip().upper().startswith("SELECT ")
            if (not checking and (parsed is None or parsed[1] not in {"nodes", "edges"})) or ";" in sql:
                raise ValueError("Grouped graph statements may only write nodes and edges")
        if self.known is not None and not self.keeps_known:
            self.known = None
        return self.native.script(";\n".join(statements), params)

    def touch(self, project_id, entity_type, entity_id=None, op="upsert"):
        if (not isinstance(entity_type, str) or not _IDENTIFIER.fullmatch(entity_type)
                or op not in {"upsert", "delete", "reset"}
                or (project_id is not None and not isinstance(project_id, str))
                or (entity_id is not None and not isinstance(entity_id, str))):
            raise ValueError("Invalid journal invalidation")
        self.explicit[(project_id, entity_type, entity_id)] = op
        if entity_type == "graph" and op == "reset":
            self.graph_resets.add(project_id)

    def reset(self, reason="reset"):
        if not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason):
            raise ValueError("Invalid journal reset reason")
        self.reset_reason = reason
        self.changes.clear()
        self.explicit.clear()

    def pending(self):
        """The events this transaction would write now, before a fold into resets; empty after a reset."""
        events = {}
        if self.reset_reason is None:
            for (table, _), (before, after) in self.changes.items():
                if before == after:
                    continue
                if table in {"nodes", "edges"} and any(
                        row is not None and row.get("project_id") in self.graph_resets for row in (before, after)):
                    continue
                if before is not None and (after is None or _event_key(table, before) != _event_key(table, after)):
                    events[_event_key(table, before)] = "delete"
                if after is not None:
                    events[_event_key(table, after)] = "upsert"
            events.update(self.explicit)
        return events

    def flush(self):
        events = self.pending()
        if not events and self.reset_reason is None:
            return
        state = self.native.query("SELECT FROM journal_state WHERE id='current'")
        if self.check is not None:
            # The head is updated below in this same commit: a takeover committed after this read makes
            # the commit fail with a conflict, one committed before it is seen here.
            self.check(state[0] if state else None, self.native)
        if self.reset_reason is not None or not state:
            epoch = str(uuid4())
            sequence = 0
            self.native.command("DELETE FROM journal_events")
            self.native.command("DELETE FROM journal_state")
            head = {"id": "current", "epoch": epoch, "seq": 0, "first_seq": 1}
            if self.writer is not None:
                head["writer"] = self.writer
            self.native.command("INSERT INTO journal_state CONTENT :row", {"row": head})
            events = {(None, "reset", None): "reset"}
        else:
            epoch, sequence = state[0]["epoch"], state[0]["seq"]
        if len(events) > MAX_TRANSACTION_EVENTS:
            events = {(project, entity_type, None): "reset" for project, entity_type, _ in events}
        at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        rows = []
        for (project, entity_type, entity_id), operation in sorted(events.items(), key=lambda item: _json(item[0])):
            sequence += 1
            rows.append({"id": f"{epoch}:{sequence}", "epoch": epoch, "seq": sequence, "project_id": project,
                         "entity_type": entity_type, "entity_id": entity_id, "op": operation, "at": at})
        # One statement stores a list of events. The engine returns each row it stored; fewer than were
        # sent would be events lost without a word, so the transaction fails instead.
        for offset in range(0, len(rows), EVENTS_PER_STATEMENT):
            part = rows[offset:offset + EVENTS_PER_STATEMENT]
            stored = self.native.command("INSERT INTO journal_events CONTENT :rows", {"rows": part})
            if not isinstance(stored, list) or sorted(row.get("seq") for row in stored if isinstance(row, dict)) != \
                    [row["seq"] for row in part]:
                raise RuntimeError("The journal did not store every event of the transaction")
        first = max(1, sequence - MAX_EVENTS + 1)
        self.native.command("UPDATE journal_state SET seq=:seq,first_seq=:first WHERE id='current'",
                            {"seq": sequence, "first": first})
        if first > 1:
            self.native.command("DELETE FROM journal_events WHERE epoch=:epoch AND seq<:first",
                                {"epoch": epoch, "first": first})


class Journal:
    def __init__(self, store):
        self.store = store

    def cursor(self, reader=None):
        rows = (reader or self.store).query("SELECT epoch,seq FROM journal_state WHERE id='current'")
        if not rows:
            raise ValueError("Journal state is unavailable")
        return {"epoch": rows[0]["epoch"], "seq": rows[0]["seq"]}

    @staticmethod
    def touch(tx, project_id, entity_type, entity_id=None, op="upsert"):
        if not isinstance(tx, JournalTransaction):
            raise ValueError("Journal changes require a Store transaction")
        tx.touch(project_id, entity_type, entity_id, op)

    @staticmethod
    def reset(tx, project_id=None, reason="reset"):
        if not isinstance(tx, JournalTransaction):
            raise ValueError("Journal reset requires a Store transaction")
        tx.reset(reason)

    def read(self, after=None, project_id=None, limit=200):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Journal limit must be between 1 and 1000")
        if project_id is not None and not isinstance(project_id, str):
            raise ValueError("Invalid journal project")
        if isinstance(after, str):
            try:
                epoch, sequence = after.rsplit(":", 1)
                after = {"epoch": epoch, "seq": int(sequence)}
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid journal cursor") from exc
        if after is not None:
            if (not isinstance(after, dict) or set(after) != {"epoch", "seq"}
                    or type(after.get("seq")) is not int or after["seq"] < 0):
                raise ValueError("Invalid journal cursor")
            try:
                if str(UUID(after["epoch"])) != after["epoch"]:
                    raise ValueError("Noncanonical journal epoch")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Invalid journal epoch") from exc
        with self.store.transaction() as tx:
            state = tx.query("SELECT FROM journal_state WHERE id='current'")[0]
            head = {"epoch": state["epoch"], "seq": state["seq"]}
            if (after is None or after["epoch"] != head["epoch"] or after["seq"] > head["seq"]
                    or after["seq"] < state.get("first_seq", 1) - 1):
                return {"epoch": head["epoch"], "cursor": head, "events": [], "reset_required": True}
            rows = tx.query("SELECT FROM journal_events WHERE epoch=:epoch AND seq>:seq "
                + ("AND (project_id=:project OR project_id IS NULL) " if project_id is not None else "")
                + f"ORDER BY seq LIMIT {limit + 1}", {"epoch": head["epoch"], "seq": after["seq"], "project": project_id})
            more = len(rows) > limit
            events = [{key: row.get(key) for key in ("epoch", "seq", "project_id", "entity_type", "entity_id", "op", "at")}
                      for row in rows[:limit]]
            cursor = {"epoch": head["epoch"], "seq": events[-1]["seq"] if more else head["seq"]}
            return {"epoch": head["epoch"], "cursor": cursor, "events": events, "reset_required": False}
