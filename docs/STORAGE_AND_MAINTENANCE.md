# Storage and maintenance

## Data and backups

The default data directory is `~/.local/share/cairnmind` for a new installation. An installation that already has data in `~/.local/share/devbrain` keeps using that directory where it is; nothing is moved or copied. Choose another one with `CAIRNMIND_HOME` or the global `--data-dir` option. Variables are spelled `CAIRNMIND_*`; the earlier `DEVBRAIN_*` spelling is still read. For a configuration variable the new spelling wins when both are set; `cairnmind doctor` reports a pair that disagrees. ArcadeDB data lives in `arcade/databases`. The knowledge service and the engine run in the background for as long as WSL is running. `cairnmind stop` stops the knowledge service; once clients are closed, `cairnmind engine stop` stops the database without deleting data. A [connect-only installation](DEPLOYMENT.md#connect-only-installations) (`CAIRNMIND_ARCADE_CONNECT_URL`) has no `arcade/` folder and no engine: its database runs elsewhere, the `engine install`, `start` and `stop` commands say so and do nothing, and `engine status` reports the server it connects to.

Commands that only read the store never start the engine, create the data directory, configuration or database, or upgrade the schema: `project list`, `records`, `source`, `task list`, `plan list`, `plan show`, `plan next`, `plan contract` without `--write`, `check profiles`, `memory orphans`, `memory extraction-stats`, `impact`, `usage`, `proposed`, `answers list`, `answers show` and `store size`. When the engine is not running, the store was never created or its schema needs an upgrade, they stop at once with one line that says what to run, `cairnmind engine start` or `cairnmind init`. `brief` still starts a stopped engine, as do the commands that write and `check list`, `export` and `backup`. On a connect-only installation no command starts an engine: when the server does not answer, each of them says where it looked.

```bash
cairnmind export ~/memory.json --project my-app
cairnmind export ~/memory.md --project my-app --markdown
cairnmind backup ~/cairnmind-backup.cairnmind.json.gz
```

The ArcadeDB schema stays at version 4, the profile format at 2, and the administrative backup at 5, with support for the earlier formats 2–4. Old plan groups can be adopted explicitly with `plan adopt`. To move a single project, use `memory import`; `restore` replaces the entire store.

Archiving does not wait for the GPU. Status shows archiving, indexing, model processing and the queue separately. The portable backup contains a consistent export of sources and database state; it does not contain credentials, model weights or vectors (#187: the `embeddings` rows and the `vector_*` types are written empty, in the same format 5, and `cairnmind export` leaves them out too). A restore sets the completed embedding jobs back to pending, in its one transaction, says how many, and the service rebuilds the vectors from the text; until then search uses the text only and says so, and `cairnmind status` shows the jobs left. A file written by 0.12.0 or earlier still restores with its vectors. How much a backup now saves is measured by `scripts/store_growth.py` ([evaluation/RESULTS.md](evaluation/RESULTS.md#store-growth-185)). Automatic backup runs while the service is running and keeps the seven most recent copies.

A store has one writer: the installation that first opened it, named in the database. An installation is its data directory, identified by `installation.json` there; a copy of that directory on another machine or under another path gets an identity of its own and does not write as the original. A second installation pointed at the same database (for example a server on another host, #72) reads it only: commands that read work, a command that writes stops with one line naming the writer, and its service does not start. `cairnmind store writer` shows which installation writes and what this one is; `doctor` and `status` show the same. To move writing to this installation, a person runs `cairnmind store takeover --yes [--note TEXT]`; the change is kept in the store, the jobs the previous writer was running go back to the queue, and the previous writer stops at its next write. For a store used from one machine nothing changes.

## Store size

`store size [PROJECT] [--json]` reports how large the store is. Like the other commands that only read, it never starts the engine or the service and changes nothing; it prints counts and sizes, never the text of a record or a source. It lists:

- every collection (the tables of the schema, `nodes`, `edges` and each `vector_*` type) with its rows and approximate bytes;
- sources by kind, current against replaced (an earlier version of the same address), and their chunks grouped by the kind of their source in the same way;
- vector rows per embedding namespace, and jobs per state;
- disk use: the whole data directory, `arcade/databases` and this store's database in it, `arcade/engine.log` and its rotated copies `engine.log.1`, `engine.log.2` and so on, the engine's `arcade/log`, the backups in `backups/` (scheduled ones, `before-schema-*`, `before-restore-*` and others, each a count and a size) and the agent journals in `runtime/agents` (sessions and size).

With `PROJECT` the rows are that project's only; `journal_state` and `ingestion_inbox` belong to no project and are shown as store-wide (JSON: `rows` and `bytes` null, `"store_wide": true`), and the disk use is always the whole data directory. Bytes are the length of each row's JSON form, measured in the engine (equal to UTF-8 bytes for ASCII text), so they are close to what the row takes in a backup before compression, not what it takes on disk (except the rows of `embeddings` and of the `vector_*` types, which a backup does not carry, #187). A collection of at most 1,000 rows is summed exactly, and so are one project's rows. Above that, for the whole store, the bytes are estimated from 256 rows, 32 windows of 8 rows at random positions spread over the whole collection, and marked `~` (JSON: `"sampled": true`); the row counts are always exact. The estimate is close when rows of different sizes are mixed through the collection (within about 2% in the tests and on a synthetic store of uneven rows); a collection where a few rows are far larger than the rest can be further off. To group chunks by the kind of their source the command reads one short row per source (id, kind and chunk count, no text), so its time grows with the number of sources: on a test store of 20,000 sources and 80,000 chunks it took about one second, and about one and a half seconds for one project of that store. The figures are not one snapshot: each is its own query, so a row the service writes meanwhile can be counted in one figure and not in another, and chunks whose source was not found are listed as `(source not found)` (JSON: `"source_found": false`). The text form looks like this (from a store filled by the test fixture in `tests/test_store_size.py`; collections with no rows are left out here):

```text
Store size (all projects)
Bytes are approximate (length of each row's JSON form in the engine; equal to UTF-8 bytes for ASCII text); ~ marks a figure estimated from 256 sampled rows.

Collections
  projects                                  2         422 B
  sources                                   4       3.9 KiB
  chunks                                    7       4.5 KiB
  records                                   1         692 B
  jobs                                      7       4.4 KiB
  embeddings                                3       1.2 KiB
  journal_events                           34      11.5 KiB
  vector_994f28d5f148c8082083cef6           3       1.3 KiB

Sources by kind: current / replaced
  document                     1     1.5 KiB  /         1     1.6 KiB
  manual                       2       899 B  /         0         0 B

Chunks by source kind: current / replaced
  document                     2     1.7 KiB  /         3     2.2 KiB
  manual                       2       617 B  /         0         0 B

Vectors by namespace
  embedder@digest: 1 type(s), 3 rows, 1.3 KiB

Jobs by state
  pending: 7

Disk
  data directory        6.9 KiB  /tmp/.../memory
  databases               700 B
  this database           700 B
  engine.log              300 B
  old engine.log         absent
  engine logs            absent
  backups scheduled          1 file(s)      3.3 KiB
  backups before-schema      1 file(s)        200 B
  backups before-restore     1 file(s)        300 B
  backups other              0 file(s)          0 B
  safety backups (newest first; delete old ones with `cairnmind store prune-backups --keep K`)
    2026-11-14T09:12:40+00:00       300 B  before-restore-abc.cairnmind.json.gz
    2026-11-02T18:03:11+00:00       200 B  before-schema-3-to-4-abc.cairnmind.json.gz
  agent journals      2 session(s)      1.5 KiB
Measured in 0.197 s.
```

`--json` prints the same data with the keys `project` (null, or `id` and `name`), `method` (`exact_rows`, `sample_rows`, `bytes`), `collections` (each `name`, `rows`, `bytes`, `sampled`, `store_wide`), `sources` and `chunks` (each `sampled` and `kinds`: `kind` (for chunks also `source_found`), then `current` and `replaced`, each `rows` and `bytes`), `vectors` (`types`: `name`, `namespace`, `dimension`, `project_id`, `rows`, `bytes`, `sampled`; `namespaces`: `namespace`, `types`, `rows`, `bytes`, `sampled`), `jobs` (state to count), `disk` (`data_dir`, `databases`, `database`, `engine_log`, `old_engine_logs` (the rotated `engine.log.<n>` copies), `engine_logs`, each `path`, `exists`, `files`, `bytes`; `backups` with `path` and `scheduled`, `before_schema`, `before_restore`, `other`, each `count` and `bytes`, and `safety`: each safety backup with `name`, `kind`, `bytes` and `modified`, newest first; `agent_journals` with `path`, `exists`, `sessions`, `files`, `bytes`), `elapsed_seconds` and `text`. A size is `null` (text: `unknown`) when it could not be estimated.

## Replaced sources and what else is kept

When a file is edited or deleted, its earlier version stays as a replaced source with its full text and hash, so evidence that cites it can still be read and checked; its chunks and vectors are deleted with the replacement, since search never reads them. A store written by an earlier release still holds such chunks (`Chunks by source kind`, column `replaced`). `store drop-replaced-chunks [--batch N] [--max-batches N]` deletes them, for every project in the store (it takes no project), and prints how many rows went (`removed`: `chunks`, `embeddings`, `vector_rows`; `sources`, `transactions`, `complete`). It deletes at most `--batch` rows (default 200), chunks or left-over vectors, per transaction, so other writes are not held up for long; `transactions` counts every one of them. It can be stopped and run again, and `complete` is false when `--max-batches` stopped it with rows still to delete. No source, record or other evidence is touched. Restoring a backup made before the clean-up brings those chunks back; run it again afterwards.

What else is kept without end is bounded as follows:

- Safety backups. The store copies itself to `backups/before-schema-*` before it upgrades its schema and to `backups/before-restore-*` before a restore. Nothing deletes these on its own, and the daily rotation (which keeps the last seven scheduled backups) never counts them. `store prune-backups --keep K` deletes all but the newest K of each kind: it prints the files it would delete and those it keeps, and asks before deleting (`--yes` skips the question). Only a person in their own terminal can run it; inside a coding session, or without a terminal, it deletes nothing. A file written again after the list was shown is left in place. Scheduled backups and backups you named yourself are never touched.
- Job history. Every job row stays, because later work reads it: a job's key stops the same extraction or embedding from being queued again, `status` and the job summaries count jobs by state, import coverage reads the import job, and a held or all-rejected extraction can be queued again. What goes is the attempt history of completed jobs that last changed more than `job_history_days` ago (`config.json`, default 30, 0 keeps it): once a day, right after the daily backup, the writer installation empties it and keeps how many events there were (`jobs show` still counts them). Failed, pending and running jobs keep their history.
- `arcade/engine.log`. When the engine starts and the log has reached `engine_log_bytes` (default 10 MiB), it is moved to `engine.log.1`, older copies shift up, and `engine_log_copies` of them are kept (default 3). The engine therefore always starts on a log under the limit; one long run can still write past it until the next start. If the filesystem refuses the rotation, the engine starts anyway, appends to the old log and writes a line there saying why.
