# ArcadeDB migration contract — source of truth

Backend contracts below remain in force. DevBrain 0.3 adds the workflow contracts in
[V03_CONTRACTS.md](V03_CONTRACTS.md): schema 2, portable payload 3 and unchanged
checksummed backup envelope 1.

Runtime SQLite is removed. ArcadeDB 26.11.1 (Apache2.0), native Java21 server.
Settings fields now exist: arcade_url (loopback http default2480), arcade_user,
arcade_password (secret in0600 config), arcade_database (derived per-data-dir hash),
arcade_version26.11.1. Test fixture root sets CAIRNMIND_ARCADE_URL/PASSWORD with an
actual shared test engine; each temporary data-dir derives its own isolated DB.

Access rules (#72, `database_credentials.py`, checked by `ArcadeClient.__init__`): http only on
127.0.0.1, localhost and ::1; https on any host, always with certificate and host name verification
(the PEM file `arcade_ca_file`, or else OpenSSL's default authorities, which SSL_CERT_FILE and
SSL_CERT_DIR replace when set; there is no switch to turn it off). On a host
that is not loopback the user root and an empty password are refused. Only plain http on loopback is
started by Cairnmind (engine.is_managed_url): for any other address ensure_engine reports why the
server cannot be reached and never writes a password under data_dir/arcade. The release pin applies
to a server on another host too: Store refuses a server that is not 26.11.1. The password comes from
CAIRNMIND_ARCADE_PASSWORD (the first spelling that is not empty), then the file `arcade_password_file` (absolute path, regular file of the
user, mode 0600, not a link; or a mounted Kubernetes secret, followed only in the kubelet's layout
NAME -> ..data/NAME, ..data -> ..<time>, to a regular file of the user that group and others cannot
read, #211), then config.json for loopback only. Settings.save never writes a value
load took from the environment or a file. `cairnmind db create-user --admin-password-file F` signs in
once as root, creates the database and the user `arcade_user` in the group admin of that database only.
The managed local engine keeps root and its config.json password.

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
Only a current source has chunks: the transaction that replaces or deactivates a source
deletes its chunks and vectors, and keeps the source row with its content and sha256.
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

One declared writer per store (#70, `store_writer.py`). The file locks (database-writer.lock,
service.lock, the ingestion and configuration locks) serialise writers on one machine only, so a
database names the one installation that writes it; no database leases are kept.
- Installation: `data_dir/installation.json` (0600) holds a random `installation_id` and `bound_to`,
  a SHA-256 of the machine identity (/etc/machine-id, else the host name), the data directory's
  absolute path and its inode. A copy of the directory (another machine, another path, a restore
  onto a new filesystem) does not match: it gets a new id at once and keeps the old one as
  `copied_from`. The host name is a label only.
- Writer: row `devbrain_meta` `name='writer'` {installation_id, host, since, history (last 20:
  installation_id, previous, host, at, how first|takeover, note)}; the same id in the journal head
  `journal_state` `id='current'` field `writer`. The first installation that opens a database without
  the row claims it (also a store of an earlier release), so a local store changes nothing for anyone.
- Every other installation's `Store(path)` opens read-only (`not_writer` says why, `read_only` is
  true): reads work, writes raise `StoreNotWriter` (a ValueError naming the writer and the command),
  no schema statement or upgrade runs, `claim_job` refuses, and the service does not start.
- Fence: JournalTransaction.flush already reads and updates the head in every committing change;
  it refuses the commit when the head names another writer (`StoreWriterLost`; after that every write
  of the store reads the writer row first and is refused while it names another installation, so a
  takeover back on this installation lets the same store, and a running service, write again without
  a restart). A transaction that read the head before a takeover fails on commit
  with the engine's concurrent-change error. Writes outside the journal (Store.command DDL, restore)
  read the writer row first. Restore and a journal reset keep the writer in the head.
- `cairnmind store takeover --yes [--note]` (a person, never a coding session) rewrites both, appends
  the change to the history and returns the jobs the previous writer was running to pending. It also
  works when this release cannot read the store as a reader (schema of another release, no journal
  head): the writer row moves first, then the store is opened as its writer; a reader's other commands
  on such a store name the writer and the takeover command.
  `cairnmind store writer`, `doctor` (`store_writer`) and `status` (`writer`) report the role:
  writer, reader, lost, none or unknown.
- Not fenced: a job claimed before a takeover runs on in the previous writer until it tries to store
  its result (bounded by its lease and model timeout); files that process writes outside the
  database; installations of a release before #70, which do not check the writer.

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
