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

Connect-only mode (#68, `database_mode.py`). An installation is connect-only when `arcade_connect_url`
(CAIRNMIND_ARCADE_CONNECT_URL) is set, which then replaces `arcade_url` as the address at load and is
never written over the stored `arcade_url`, or when `arcade_url` is not plain http on loopback.
Otherwise it is managed, as before. Two different addresses in CAIRNMIND_ARCADE_URL and
`arcade_connect_url` are a settings problem that `ArcadeClient` refuses.
- `engine.install_engine`, `start_engine` and `stop_engine` raise `ConnectOnly` (an ArcadeError) and do
  nothing; `stop_engine` still stops a process recorded in `arcade/process.json` of this data directory.
  `ensure_engine` sends one request (`GET /api/v1/server`) and never starts anything. `engine_status`
  has `mode` (`managed`|`connect`); in connect mode it has `host`, `user`, `database`, `transport`,
  `supported_versions` and, for an unsupported server, `problem`, and no `installed`, `java` or
  `managed_java`.
- `database_mode.supported_versions(settings)` is the pinned `arcade_version` alone. A server of another
  version is refused by `ensure_engine` and `store_readonly.check_ready` before any read or write.
- `ArcadeClient.ensure_database` only checks that the database exists; a missing one is an ArcadeError
  naming `cairnmind db create-user`, the one path that creates it (the administrator client).
- `Settings.load(create=True)` generates no database password. On loopback a connect-only client without
  a password is refused before a request; for another host the rules of #72 apply unchanged.
- A transport failure is reported as `ArcadeDB at URL is unavailable ... starts no engine of its own`,
  and HTTP 401 adds that the user may not exist yet. A write is never sent twice.
- No file lock of the data directory guards the database between machines; the declared writer below
  does. `tests/test_connect_only_network.py` cuts the connection during indexing, a restore, a schema
  upgrade and the creation of a store: only whole transactions remain, and running the command again
  completes it.

One declared writer per store (#70, `store_writer.py`). The file locks (database-writer.lock,
service.lock, the ingestion and configuration locks) serialise writers on one machine only, so a
database names the one installation that writes it; no database leases are kept.
- Installation: `data_dir/installation.json` (0600) holds a random `installation_id` and `bound_to`,
  a SHA-256 of the machine identity (/etc/machine-id, else the host name), the data directory's
  absolute path and its inode. A copy of the directory (another machine, another path, a restore
  onto a new filesystem) does not match: it gets a new id at once and keeps the old one as
  `copied_from`. The host name is a label only.
- Writer: row `devbrain_meta` `name='writer'` {installation_id, host, since, history (last 20:
  installation_id, previous, host, at, how first|takeover|repair, note)}; the same id in the journal head
  `journal_state` `id='current'` field `writer` (with `writer_change`, see below). The first installation that opens a database without
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
  read the writer row and the head first and are refused when either names another installation.
  Row and head are read outside a transaction, the row once more after the head (a transaction is not
  one view of two records: a record it reads for the first time shows the latest commit), so a
  takeover between the reads is not taken for a broken store; `store writer`, `doctor` and `status`
  read them the same way. Restore and a journal reset keep the writer in the head.
- `cairnmind store takeover --yes [--note]` (a person, never a coding session) rewrites both, appends
  the change to the history and returns the jobs the previous writer was running to pending. It also
  works when this release cannot read the store as a reader (schema of another release, no journal
  head): the writer row moves first, then the store is opened as its writer; a reader's other commands
  on such a store name the writer and the takeover command.
  `cairnmind store writer`, `doctor --json` (`store_writer`) and `status` (`writer`) report the role:
  writer, reader, lost, none or unknown.
- Two claims or takeovers at once: each is one transaction that reads the head, then the writer row,
  writes the row and names itself in the head. ArcadeDB 26.11.1 finds a conflict per record, at
  commit, and only for a record the transaction really changed: an UPDATE that sets the values the
  record already has in the transaction's view is dropped (it reports count 1, writes nothing and
  never conflicts), and a WHERE clause is evaluated on that view, so a compare-and-set does not
  help. Every stamp of the head therefore also writes `writer_change`, a new random value, so the
  head always changes: of two overlapping transactions the second commit fails whole (the command
  says so and is run again), or it started after the first and commits on top with both records its
  own. Row and head never name different installations. The head also changes with every journaled
  write of the writer (`seq`), so a takeover that overlaps one is refused in the same way, with
  nothing changed; the refusal names both causes, and against a writer that keeps writing it can
  come more than once.
- A store left with row and head apart by a build before that rule: `store writer`, `doctor` and
  the role in `status` add `head_writer` and `problem`. Data is committed only by the installation
  the head names, and only from a process that was already running (a new one opens as a reader,
  because the row names the other). The installation the row names is not opened read-only, but
  every write of it is refused with the same text, in a running process and in a new one, and a new
  process runs no schema statement or upgrade. Schema statements are refused on both until the
  repair. Opening a store never repairs it; `store takeover --yes` on either installation does (on
  the one the row names: history entry `how: repair`, result `repaired`).
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
  default and validate manifest/hash before destructive restore. Backups remain source complete: every source
  with its full text and hash. They carry no vectors (#187, owner's decision of 15 November 2026): `embeddings`
  and `vector_indexes` are written as empty arrays and `vector_data` as an empty object, in every kind of backup
  (daily, `cairnmind backup`, `before-schema-*`, `before-restore-*`) and in `Store.export`, with the format
  number 5 kept. That is a legal format 5 file (a store with no embeddings wrote it before), so every earlier
  restore reads it and rebuilds by itself. Restore still loads the vectors of a file that carries them.
  When the restored store holds no `embeddings` row, `Store.restore` sets, in its one transaction, each
  completed `embed` job of a current chunk back to pending and sets `Store.embed_jobs_requeued` to their number
  (None when the backup brought vectors); `restore_claims.checked_restore` adds the number
  (`embed_jobs_requeued`) and one sentence in English and Polish to the output of `cairnmind restore` and
  `cairnmind setup local --backup`. Until the jobs are done, `Retriever.search` with a query vector finds no
  index for the namespace and warns that vectors are not ready, and the text ranking answers.
- whole-database backup (`Store.backup`, the daily backup and the `before-*` safety copies, `store_backup.py`, #196):
  written as a stream inside one transaction under the writer lock. Each type is read in record id order
  (`WHERE @rid > :rid ORDER BY @rid LIMIT n`, the page size adapting to the row size), each row serialized
  with `json_text`, hashed and compressed as it comes; Python memory stays bounded by a page, not by the store.
  The file is unchanged: envelope 1, keys in `json_text` order (`format`, `payload`, `sha256`, `version`),
  the bytes the in-memory writer produced, so every earlier restore reads it. It is written under a temporary
  `.cairnmind-backup-*` name in the destination folder while the transaction is open, then synced and moved
  into place only after the transaction commits, so a failed commit leaves no file (the before-schema copy
  is the exception: its read-only transaction ends after the move, and a failure there stops the migration
  before any change). A partial file left by a killed process has no gzip trailer and is refused; the store
  size view counts it under other backups and the service removes it once it is a day old.
- restore (`Store.restore`, `backup_reader.py`, #196): reads the same envelope 1 in pieces, never whole. A
  first pass walks the document one row at a time with the standard library decoder, recomputes the checksum
  over the rows serialized with `json_text`, and keeps only a bounded index (project ids; for sources, nodes
  and records the id and the fields the cross references use, and whether the source hash was right). A
  second pass checks every cross reference against it. Only then is the before-restore copy made and the
  store changed; the inserts read the file three more times (documents, then vertices, then edges and
  the vectors a file carries, none in a file of 0.13.0) inside the one restore transaction, and, without the person's workspace (#214,
  `restore_claims.py`), rewrite each confirmed record's and revision's confirmation mark into the backup's
  claim and a project's accepted record kinds into withdrawn ones as the rows are inserted, and write the rows in groups (`restore_groups.py`, #219): a
  document or vector group is one `INSERT ... CONTENT :rows` of one type, at most 250 rows or about 1 MiB of
  JSON text; vertices and edges go per project through `prefetch_graph` and `write_graph` (#167), with the
  checks of `upsert_node` and `upsert_edge`, in groups with the same two limits, and at most 1,000 graph rows
  or about 4 MiB of their JSON text held across projects; a second copy of a vertex or edge id starts a new group, so it
  updates the first as row by row did. A group the engine stores only in part fails the transaction. Every
  check, its order and its message are those of
  `Store._validated_backup`, which still loads the rare file that is valid JSON in a layout no writer
  produced (keys out of sorted order or repeated, a document that is not an object). Each later pass compares
  a digest of the bytes it read with the first pass, so a file changed during a restore is refused and the
  transaction rolled back. Some structural checks still run only inside that transaction, after its deletes,
  as in 0.10.0: those of `upsert_node` and `upsert_edge`, a vector entry that is not a list or a vector row
  that is not an object, and repeated ids that hit a unique index; their refusal is undone by the rollback.
  A failure at any point before the commit leaves the store as it was. Python memory
  is bounded by the largest row (or the groups held: one of documents, about 4 MiB of graph rows) plus the
  index; the engine still holds the restore transaction
  itself.

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
