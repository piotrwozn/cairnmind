# ArcadeDB migration contract — source of truth

Backend contracts below remain in force. DevBrain 0.3 adds the workflow contracts in
[V03_CONTRACTS.md](V03_CONTRACTS.md): schema 2, portable payload 3 and unchanged
checksummed backup envelope 1.

Runtime SQLite is removed. ArcadeDB 26.11.1 (Apache2.0), native Java21 server.
Settings fields now exist: arcade_url (loopback http default2480), arcade_user,
arcade_password (secret in0600 config), arcade_database (derived per-data-dir hash),
arcade_version26.11.1. Test fixture root sets CAIRNMIND_ARCADE_URL/PASSWORD with an
actual shared test engine; each temporary data-dir derives its own isolated DB.

## HTTP DAL (root owns arcade.py and engine.py)

`ArcadeError(RuntimeError)` for API/server failures, no secret-bearing URLs/logs.
`ArcadeClient(settings)` is side-effect free. `ensure_database()` creates configured
database if absent. `query(sql, params:dict|None=None)->list[dict]` uses READONLY GET
query endpoint (or command readonly if API requires), returns clean result rows;
`command(sql,params=None)->list[dict]` uses POSTcommand. No positional ? syntax.
`transaction()` context manager yields session client with same query/command and
native /begin -> arcadedb-session-id -> /commit or /rollback; retries not implicit.
Result properties @rid/@type preserved in raw query output, publicStore methods strip
internal properties if desired. API parameter values are JSON native dict/list/bool,
not JSON strings. Clients open short HTTP calls, local trust_env=False; HTTPBasic auth.
`server_info()` authenticated server status. `ensure_engine(settings)` root autostarts
already-installed local managed engine; no automatic downloads. Store constructor calls
ensure_engine then client.ensure_database. Diagnostics never autostart/download.
`Store.open_readonly(settings)` (read-only CLI commands) starts no engine, creates no
database and runs no DDL: it requires config.json, a reachable engine of the pinned
release, the database, schema_version 4 and the journal_state row, else StoreNotReady
names the command to run. Its commands and non-SELECT queries are refused and its
transactions take no writer lock and are rolled back. Such a transaction is not a
snapshot (REPEATABLE_READ is per page), so a read of several queries goes through
`Store.read_consistently(read, kinds)`: journal head before and after; when it moved,
the events in between (at most 500 looked at) repeat the read only if one concerns its
project or no project and a kind it reads (`cli.READ_KINDS`). Up to 3 attempts, then
StoreBusy instead of a mixed result. Jobs, sessions and other projects never repeat it.

## Store (storage agent owns)

Keep public methods from CONTRACTS.md, plus `query`, `command`, `transaction`
delegation. `connect()` REMOVED; no SQLite compatibility layer.

Native document types exact names:
projects,project_roots,sources,chunks,records,record_revisions,jobs,embeddings,
import_cursors,ingestion_inbox. Native graph VERTEX type `nodes`, EDGE type `edges`.
Schema 2 also includes plans,plan_revisions,plan_requests,worktree_states,sessions,
resume_packets,resume_acks,session_events,check_runs. Legacy task groups do not
implicitly become managed plans, and historical reports do not become machine runs.
Public field names same as old schema but JSON properties are real maps/lists;
is_current is BOOLEAN. Chunk IDs deterministic string UUID/hash (not integers).
Native graph records use public id/from_id/to_id props plus native @rid and in/out.

Storage supplies `upsert_node(node:dict,tx=None)` / `upsert_edge(edge:dict,tx=None)`
(actual native edges with native RID endpoints). Do not REPLACE vertex records and
destroy edges; update native vertex existingRID. Idempotent unique id indexes.
Caller node fields id,project_id,kind,label,source_id,metadata; edge fields
id,project_id,from_id,to_id,kind,source_id,status. Source IDs remain strings.

`list_records`,get_source,resolve_project return plain field dictionaries with metadata
already decoded, source_ids list. Remember task gates/version history and leases use
native transactions with a per-datadir process lock for coordinated local writers.
Task source/dependency validation and optimistic conflicts preserved.

Additional root-facing methods needed:
- `cache_stamp(project_id)->str` changes on current sources, records version/status and deletions
- `recent_job_errors(project_id=None,limit=10)->list`
- `retry_jobs()->None`
- `missing_embeddings(namespace,project_id=None)->list[{id:chunk_id,project_id}]`
- `reset_missing_embedding_job(job_id,chunk_id,namespace)->None` reset completed only if vector missing
- `delete_embeddings(project_id)->None`
- export/backup restore: portable JSON/zip artifact includes sources/revisions/cursors/jobs,
  native graph reconstructed via helpers on restore, credentials excluded, transactional
  snapshot export; native engine backup optional extra. Choose `.cairnmind.json.gz` backup (`.devbrain.json.gz` up to 0.9.0)
  default and validate manifest/hash before destructive restore. Backups remain source complete.

## Ingestion (ingestion agent owns)

Use native named-parameter ArcadeSQL (noSQLitecompat). May implement convenience
helpers locally over store.query/command/transaction. Ensure chunks atomic in native
transaction; deterministic public IDs and per-source line metadata; queues aftercommit.
Native graph replace only managed code relation kinds, preserve record provenance.
Inbox/cursors native doc properties. No SQL `json_extract`, no INSERT OR REPLACE,
no rowid/sqlite PRAGMA/blobvectors.

## Retrieval (knowledge agent owns)

FTS native index on chunks.text (name chosen/docs via storageagent). Native vector
LSM_VECTOR/HNSW index: retrieval owns lazy DDL & native query with names isolated by
project+embeddingmodeldigest+dimension. Store `embeddings` can retain canonical vectors
as floatarrays and metadata and separate native index document types as rebuildable
derived data; no scan/cosine in Python or sqlitevector. Exact syntax researcher verifies.
Graph traversal must execute via native graph `both()`/TRAVERSE/MATCH bounded params,
hydrate evidence with sources current/status filters; no loadentiregraph+BFS inPython.
Public tools and output budgets unchanged. Agent record_history scope via Store.query.

## Tests

Root tests/conftest.py starts actual pinned Java server once with isolateddata and a
shared testpassword environment; tmp_path Store data-dir maps isolatedDB. Test mutations
use store.command/query, tests assert actual FTS/vector/nativeedge traversal. CLI clients
and Ollama remain mocked (no vendor model sessions). Root port freeallocated.
