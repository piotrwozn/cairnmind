"""ArcadeDB schema initialization, streaming export, migration, and sizing mixin for ArcadeBackend."""
from __future__ import annotations

import random
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cairnmind.storage.arcade import ArcadeError
from cairnmind.storage.arcade_graph import vector_type_name
from cairnmind.storage.sql import clean_row, json_text, quote_name
from cairnmind.storage.storage_backend import ALL_COLLECTIONS


class _ArcadeMeasureMixin:
    """Mixin providing schema init, streaming export, migration, and sizing for ArcadeBackend."""

    if TYPE_CHECKING:
        _store: Any

        @property
        def _client(self) -> Any: ...

        def list_collections(self, reader: Any = None) -> set[str]: ...

        def ensure_vector_index(
            self,
            project_id: str,
            namespace: str,
            dimension: int,
            model: str,
        ) -> dict[str, Any] | None: ...

    # --- Streaming Export & Counts ---

    def iter_collection(
        self,
        reader: Any,
        collection: str,
        *,
        project_id: str | None = None,
        batch_size: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        target = reader if reader is not None else self._store
        batch_size = max(1, int(batch_size))
        after = None
        while True:
            clauses = []
            params: dict[str, Any] = {}
            if project_id is not None:
                field = "id" if collection == "projects" else "project_id"
                clauses.append(f"{quote_name(field)}=:project")
                params["project"] = project_id
            if after is not None:
                clauses.append("id>:cursor_id")
                params["cursor_id"] = after
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = [
                clean_row(row)
                for row in target.query(
                    f"SELECT FROM {quote_name(collection)}{where} ORDER BY id LIMIT {batch_size}",
                    params,
                )
            ]
            if not rows:
                break
            yield rows
            if len(rows) < batch_size:
                break
            after = rows[-1].get("id")
            if not isinstance(after, str):
                break

    def collection_counts(self, reader: Any = None) -> dict[str, int]:
        target = reader if reader is not None else self._store
        counts: dict[str, int] = {}
        existing = self.list_collections(target)
        for collection in ALL_COLLECTIONS:
            if collection not in existing:
                counts[collection] = 0
                continue
            rows = target.query(f"SELECT count(*) AS total FROM {quote_name(collection)}")
            counts[collection] = int(rows[0]["total"]) if rows else 0
        return counts

    # --- Schema Init, Migration & Sizing Primitives ---

    def init_schema(self, store: Any = None) -> None:
        """Create or upgrade ArcadeDB types, properties, and indexes under writer lock."""
        from cairnmind.config import BACKUP_SUFFIXES
        from cairnmind.storage.sql import insert_document, update_document
        from cairnmind.storage.store_backup import streamed_export
        from cairnmind.store import (
            DOCUMENT_TYPES,
            EXPORT_TYPES,
            LEGACY_EXPORT_TYPES,
            LOOKUP_INDEX_SET,
            SCHEMA_VERSION,
            V03_DOCUMENT_TYPES,
            V03_EXPORT_TYPES,
            V04_DOCUMENT_TYPES,
            V04_EXPORT_TYPES,
            V05_DOCUMENT_TYPES,
        )

        owner = store if store is not None else self._store
        client = owner.client
        client.command("CREATE DOCUMENT TYPE devbrain_meta IF NOT EXISTS")
        meta = {
            row.get("name"): row.get("value")
            for row in client.query(
                "SELECT FROM devbrain_meta WHERE name IN ['schema_version','lookup_indexes']"
            )
        }
        version, indexed = meta.get("schema_version"), meta.get("lookup_indexes")
        if version is not None:
            if version not in {1, 2, 3, SCHEMA_VERSION}:
                raise ValueError("Database schema version is incompatible")
            if version == SCHEMA_VERSION:
                if indexed != LOOKUP_INDEX_SET:
                    self._lookup_indexes(owner, indexed)
                return
            destination = (
                owner.data_dir
                / "backups"
                / f"before-schema-{version}-to-{SCHEMA_VERSION}-{uuid4().hex}{BACKUP_SUFFIXES[0]}"
            )
            with client.transaction() as tx:
                owner._write_backup(
                    destination,
                    streamed_export(
                        tx,
                        LEGACY_EXPORT_TYPES
                        if version == 1
                        else V03_EXPORT_TYPES
                        if version == 2
                        else V04_EXPORT_TYPES,
                        version + 1,
                    ),
                )
        collections = (
            (*V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES)
            if version == 1
            else (*V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES)
            if version == 2
            else V05_DOCUMENT_TYPES
            if version == 3
            else DOCUMENT_TYPES
        )
        exports = collections if version is not None else EXPORT_TYPES
        statements: list[str] = []
        run_ddl = statements.append if version is None else client.command
        for collection in collections:
            run_ddl(f"CREATE DOCUMENT TYPE {collection} IF NOT EXISTS")
        if version is None:
            run_ddl("CREATE VERTEX TYPE nodes IF NOT EXISTS")
            run_ddl("CREATE EDGE TYPE edges IF NOT EXISTS")
        for collection in exports:
            run_ddl(f"CREATE PROPERTY {collection}.id IF NOT EXISTS STRING")
            run_ddl(f"CREATE INDEX IF NOT EXISTS ON {collection}(id) UNIQUE NULL_STRATEGY SKIP")
            if collection != "projects":
                run_ddl(f"CREATE PROPERTY {collection}.project_id IF NOT EXISTS STRING")
                run_ddl(f"CREATE INDEX IF NOT EXISTS ON {collection}(project_id) NOTUNIQUE NULL_STRATEGY SKIP")
        if version is None:
            for collection, field in (
                ("projects", "name"),
                ("projects", "root"),
                ("project_roots", "path"),
                ("sources", "current_key"),
                ("records", "active_key"),
                ("jobs", "job_key"),
                ("import_cursors", "path"),
            ):
                run_ddl(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                run_ddl(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) UNIQUE NULL_STRATEGY SKIP")
            for collection in ("sources", "chunks", "nodes", "edges"):
                run_ddl(f"CREATE PROPERTY {collection}.is_current IF NOT EXISTS BOOLEAN")
            for collection, field in (("chunks", "text"), ("records", "content")):
                run_ddl(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                run_ddl(
                    f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) FULL_TEXT METADATA {{similarity:'BM25'}}"
                )
        run_ddl("CREATE PROPERTY journal_events.epoch IF NOT EXISTS STRING")
        run_ddl("CREATE PROPERTY journal_events.seq IF NOT EXISTS LONG")
        run_ddl("CREATE INDEX IF NOT EXISTS ON journal_events(epoch,seq) UNIQUE")
        if statements:
            client.script(";\n".join(statements))
        with client.transaction() as tx:
            if not tx.query("SELECT id FROM journal_state WHERE id='current'"):
                insert_document(
                    tx, "journal_state", {"id": "current", "epoch": str(uuid4()), "seq": 0, "first_seq": 1}
                )
            if not tx.query("SELECT name FROM devbrain_meta WHERE name='schema_version'"):
                insert_document(tx, "devbrain_meta", {"name": "schema_version", "value": SCHEMA_VERSION})
            else:
                update_document(tx, "devbrain_meta", {"value": SCHEMA_VERSION}, "name='schema_version'", {})
        if indexed != LOOKUP_INDEX_SET:
            self._lookup_indexes(owner, indexed)

    def _lookup_indexes(self, store: Any, recorded: Any) -> None:
        from cairnmind.storage.sql import insert_document, update_document
        from cairnmind.store import LOOKUP_INDEX_SET, LOOKUP_INDEXES

        client = store.client
        try:
            for collection, field in LOOKUP_INDEXES:
                client.command(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                client.command(
                    f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) NOTUNIQUE NULL_STRATEGY SKIP"
                )
        except ArcadeError:
            return
        with client.transaction() as tx:
            if not tx.query("SELECT name FROM devbrain_meta WHERE name='lookup_indexes'"):
                insert_document(tx, "devbrain_meta", {"name": "lookup_indexes", "value": LOOKUP_INDEX_SET})
            else:
                update_document(tx, "devbrain_meta", {"value": LOOKUP_INDEX_SET}, "name='lookup_indexes'", {})

    def ensure_restored_vector_types(self, vector_indexes: list[dict[str, Any]]) -> None:
        for registry in vector_indexes:
            name, dimension = registry.get("type_name"), registry.get("dimension")
            if not isinstance(name, str) or not name.startswith("vector_") or type(dimension) is not int or dimension <= 0:
                raise ValueError("Invalid vector namespace in backup")
            self._store.command(f"CREATE DOCUMENT TYPE {quote_name(name)} IF NOT EXISTS")
            self._store.command(f"CREATE PROPERTY {quote_name(name)}.id IF NOT EXISTS STRING")
            self._store.command(f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(id) UNIQUE")
            self._store.command(f"CREATE PROPERTY {quote_name(name)}.vector IF NOT EXISTS ARRAY_OF_FLOATS")
            self._store.command(
                f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(vector) LSM_VECTOR "
                f"METADATA {{dimensions:{dimension},similarity:'COSINE',idPropertyName:'id'}}"
            )

    def wipe_all_collections(self, collections: tuple[str, ...]) -> None:
        wipe_order = list(reversed(collections))
        existing_types = self.list_collections(self._client)
        with self._client.transaction() as tx:
            for tname in sorted(existing_types):
                if tname.startswith("vector_"):
                    tx.command(f"DELETE FROM {quote_name(tname)}")
            for col in wipe_order:
                if col in existing_types:
                    tx.command(f"DELETE FROM {quote_name(col)}")

    def import_migration_batch(self, collection: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        ids = [r["id"] for r in rows if isinstance(r.get("id"), str)]
        with self._client.transaction() as tx:
            if collection == "journal_state":
                tx.command("DELETE FROM journal_state")
                existing_ids: set[str] = set()
            elif ids:
                found = tx.query(f"SELECT id FROM {quote_name(collection)} WHERE id IN :ids", {"ids": ids})
                existing_ids = {str(r["id"]) for r in found if r.get("id") is not None}
            else:
                existing_ids = set()

            to_insert = [clean_row(r) for r in rows if r.get("id") not in existing_ids]
            if to_insert:
                if collection == "nodes":
                    for offset in range(0, len(to_insert), 250):
                        tx.command("CREATE VERTEX nodes CONTENT :rows", {"rows": to_insert[offset:offset + 250]})
                elif collection == "edges":
                    for edge in to_insert:
                        tx.command(
                            "CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:from_id) "
                            f"TO (SELECT FROM nodes WHERE id=:to_id) CONTENT {json_text(edge)}",
                            {"from_id": edge["from_id"], "to_id": edge["to_id"]},
                        )
                else:
                    for offset in range(0, len(to_insert), 250):
                        tx.command(
                            f"INSERT INTO {quote_name(collection)} CONTENT :rows",
                            {"rows": to_insert[offset:offset + 250]},
                        )

        if collection == "vector_indexes":
            for row in rows:
                pid = row.get("project_id")
                ns = row.get("namespace")
                dim = row.get("dimension")
                model = row.get("model") or ""
                if pid and ns and isinstance(dim, int) and dim > 0:
                    self.ensure_vector_index(pid, ns, dim, model)
        elif collection == "embeddings":
            existing_types = self.list_collections(self._client)
            with self._client.transaction() as tx:
                for row in rows:
                    vec = row.get("vector")
                    pid = row.get("project_id")
                    ns = row.get("namespace")
                    dim = row.get("dimension")
                    if isinstance(vec, list) and vec and pid and ns and isinstance(dim, int):
                        tname = vector_type_name(pid, ns, dim)
                        if tname in existing_types:
                            found = tx.query(f"SELECT id FROM {quote_name(tname)} WHERE id=:id", {"id": row["id"]})
                            if not found:
                                tx.command(
                                    f"INSERT INTO {quote_name(tname)} "
                                    f"CONTENT {json_text({**clean_row(row), 'is_current': True})}"
                                )

    def upsert_meta_row(self, row: dict[str, Any]) -> None:
        cleaned = clean_row(row)
        name = cleaned["name"]
        with self._client.transaction() as tx:
            tx.command("DELETE FROM devbrain_meta WHERE name=:name", {"name": name})
            tx.command(f"INSERT INTO devbrain_meta CONTENT {json_text(cleaned)}")

    def measure_collection_exact_bytes(
        self,
        collection: str,
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
    ) -> int:
        where = f" WHERE {quote_name(project_key)}=:project" if project_id else ""
        params = {"project": project_id} if project_id else {}
        found = self._store.query(
            f"SELECT sum(@this.toJSON().length()) AS b FROM {quote_name(collection)}{where}", params
        )
        return int(found[0].get("b") or 0) if found else 0

    def measure_collection_sample(
        self,
        collection: str,
        rows: int,
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
        fields: str = "",
        sample_points: int = 32,
        sample_window: int = 8,
        seed: int = 184,
    ) -> list[dict[str, Any]]:
        where = f" WHERE {quote_name(project_key)}=:project" if project_id else ""
        params = {"project": project_id} if project_id else {}
        generator, seen, sample = random.Random(seed), set(), []  # noqa: S311 - deterministic non-crypto sampling
        part = rows / sample_points
        for point in range(sample_points):
            offset = int(point * part + generator.random() * max(0.0, part - sample_window))
            for row in self._store.query(
                f"SELECT @rid AS rid, {fields}@this.toJSON().length() AS b "
                f"FROM {quote_name(collection)}{where} SKIP {offset} LIMIT {sample_window}",
                params,
            ):
                if row.get("rid") not in seen:
                    seen.add(row.get("rid"))
                    sample.append(row)
        return sample

    def measure_collection_grouped(
        self,
        collection: str,
        group_fields: tuple[str, ...],
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
        include_bytes: bool = True,
    ) -> list[dict[str, Any]]:
        where = f" WHERE {quote_name(project_key)}=:project" if project_id else ""
        params = {"project": project_id} if project_id else {}
        grp = ", ".join(quote_name(f) for f in group_fields)
        select = f"{grp}, count(*) AS n" + (", sum(@this.toJSON().length()) AS b" if include_bytes else "")
        return list(
            self._store.query(
                f"SELECT {select} FROM {quote_name(collection)}{where} GROUP BY {grp}",
                params,
            )
        )

    def measure_package_table(
        self,
        table: str,
        project_id: str,
        *,
        only: Any = None,
        group_sources: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Measure row count, exact UTF-8 bytes (via width regexes), and max chars for one package table."""
        from cairnmind.storage.storage_query import compile_arcade_where

        length_expr = "@this.toJSON().length()"
        beyond_ascii = "\\u0080-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff"
        widths = (
            beyond_ascii,
            "\\u0800-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff",
            "\\ud800\\udc00-\\udbff\\udfff",
        )
        digits = "0123456789abcdef"

        base_where = "project_id=:project"
        base_params: dict[str, Any] = {"project": project_id}
        if only is not None:
            if isinstance(only, tuple) and len(only) == 2 and isinstance(only[0], str):
                base_where += f" AND {only[0]}"
                base_params.update(only[1])
            else:
                w_sql, w_params = compile_arcade_where(only, param_prefix="only_")
                if w_sql:
                    base_where += f" AND {w_sql}"
                    base_params.update(w_params)

        def run_q(
            select_expr: str,
            extra_where: str = "",
            extra_params: dict[str, Any] | None = None,
            tail: str = "",
        ):
            cond = " AND ".join(p for p in (extra_where, base_where) if p)
            return self._store.query(
                f"SELECT {select_expr} FROM {quote_name(table)} WHERE {cond}{tail}",
                {**base_params, **(extra_params or {})},
            )

        def kept(pattern: str) -> str:
            return f"@this.toJSON().normalize('NFC','[^{pattern}]').length()"

        def parts(low: str | None, high: str | None, prefix: str) -> list[tuple[str | None, str | None]]:
            edges: list[str | None] = [low] + [prefix + d for d in digits[1:]] + [high]
            return list(zip(edges, edges[1:], strict=False))

        def in_range(low: str | None, high: str | None) -> tuple[str, dict[str, Any]]:
            w: list[str] = []
            p: dict[str, Any] = {}
            if low is not None:
                w.append("id >= :low")
                p["low"] = low
            if high is not None:
                w.append("id < :high")
                p["high"] = high
            return " AND ".join(w), p

        group = " GROUP BY kind, is_current" if group_sources else ""
        label = "kind, is_current, " if group_sources else ""
        found = run_q(f"{label}count(*) AS n, sum({length_expr}) AS b, max({length_expr}) AS m", tail=group)

        def sum_of(pattern: str) -> dict[Any, int] | None:
            expr = f"{label}sum({kept(pattern)}) AS w"

            def run_part(low: str | None = None, high: str | None = None):
                ew, ep = in_range(low, high)
                return run_q(expr, extra_where=ew, extra_params=ep, tail=group)

            def add(rows_in: list[dict[str, Any]], into: dict[Any, int]) -> None:
                for r in rows_in:
                    k = (r.get("kind"), r.get("is_current"))
                    into[k] = into.get(k, 0) + int(r.get("w") or 0)

            total: dict[Any, int] = {}
            try:
                add(run_part(), total)
                return total
            except RuntimeError:
                total = {}
            for digit, (first_low, first_high) in zip(digits, parts(None, None, ""), strict=True):
                try:
                    add(run_part(first_low, first_high), total)
                    continue
                except RuntimeError:
                    pass
                for low, high in parts(first_low, first_high, digit):
                    try:
                        add(run_part(low, high), total)
                    except RuntimeError:
                        return None
            return total

        exact = True
        extra: dict[Any, int] = {}
        for pattern in widths:
            wmap = sum_of(pattern)
            if wmap is None:
                exact, extra = False, {}
                break
            for k, val in wmap.items():
                extra[k] = extra.get(k, 0) + val
            if not any(wmap.values()):
                break

        result_rows: list[dict[str, Any]] = []
        for r in found:
            key = (r.get("kind"), r.get("is_current"))
            result_rows.append({
                "kind": r.get("kind"),
                "is_current": r.get("is_current"),
                "n": int(r.get("n") or 0),
                "b": int(r.get("b") or 0) + extra.get(key, 0),
                "m": int(r.get("m") or 0),
            })
        return result_rows, exact

    def measure_package_max_bytes(
        self,
        table: str,
        project_id: str,
        *,
        floor_chars: int,
        only: Any = None,
    ) -> int | None:
        from cairnmind.storage.storage_query import compile_arcade_where

        length_expr = "@this.toJSON().length()"
        beyond_ascii = "\\u0080-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff"
        widths = (
            beyond_ascii,
            "\\u0800-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff",
            "\\ud800\\udc00-\\udbff\\udfff",
        )
        bytes_expr = "(" + length_expr + "".join(
            f" + @this.toJSON().normalize('NFC','[^{pattern}]').length()" for pattern in widths
        ) + ")"
        conds = [f"{length_expr} > :floor", "project_id=:project"]
        params: dict[str, Any] = {"floor": floor_chars, "project": project_id}
        if only is not None:
            if isinstance(only, tuple) and len(only) == 2 and isinstance(only[0], str):
                conds.append(only[0])
                params.update(only[1])
            else:
                w_sql, w_params = compile_arcade_where(only, param_prefix="only_")
                if w_sql:
                    conds.append(w_sql)
                    params.update(w_params)
        found = self._store.query(
            f"SELECT max({bytes_expr}) AS m FROM {quote_name(table)} WHERE {' AND '.join(conds)}",
            params,
        )
        if found and found[0].get("m") is not None:
            return int(found[0]["m"])
        return None
