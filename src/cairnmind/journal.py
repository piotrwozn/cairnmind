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
    """A Store transaction proxy; SQL and the final outbox share one commit."""

    def __init__(self, native):
        self.native = native
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

    def _record(self, table, before, after):
        first = {row["id"]: _clean(row) for row in before if isinstance(row.get("id"), str)}
        last = {row["id"]: _clean(row) for row in after if isinstance(row.get("id"), str)}
        for identifier in first.keys() | last.keys():
            key = table, identifier
            if key not in self.changes:
                self.changes[key] = [deepcopy(first.get(identifier)), None]
            self.changes[key][1] = deepcopy(last.get(identifier))

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
        if self.reset_reason is not None or not state:
            epoch = str(uuid4())
            sequence = 0
            self.native.command("DELETE FROM journal_events")
            self.native.command("DELETE FROM journal_state")
            self.native.command("INSERT INTO journal_state CONTENT :row", {"row": {
                "id": "current", "epoch": epoch, "seq": 0, "first_seq": 1}})
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
