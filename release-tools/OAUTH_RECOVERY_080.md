# Private account preservation in 0.8 qualification

These controller helpers are prepared for isolated account tests. Syntax checks
are not proof that their guard tests or real account scenarios passed. Do not run
them outside the qualified, serial Windows/Linux supervision workflow.

`prepare_native_auth(client, source, destination, binary, scope=scope)` returns a **private
in-memory row**. Never put that row, JWT payloads, account principals, credential
digests or credential contents in a manifest or external report.

For Codex, setup takes a per-source lifetime lease and the official `auth.lock`.
It seeds a private copy and a durable transaction before launching a client. The
source and copy directories must already be canonical, owned Linux directories
with mode 0700; credential files must be owned regular 0600 files without links.
The helper does not change permissions on an existing account directory.

The private journal is `source.parent/.devbrain-validation-auth/journal.json`,
under a dedicated 0700 directory. Immutable baseline and candidate snapshots are
0600 files under `destination.parent/.devbrain-validation-auth/`. The journal
binds source, destination, run ID, transaction ID, the verified UUID cgroup and
Linux boot identity, qualified CLI digest and
snapshot digests. Keep these private files after any interrupted or failed run.

The write protocol is `prepared` → `commit_pending` → `committed`. Candidate
contents and the pending journal are fsynced before replacing the original
`auth.json`; that file retains its official schema. The helper never replaces
the official lock. An existing 0644 official lock is accepted inside its private
0700 directory; writable-by-others or linked locks are rejected.

`sync_native_auth(rows)` must run after every completed CLI phase, and again in an
independent `finally` even when the model, database, or test phase failed. It
accepts only a newer refresh for the same nonempty workspace and user. Existing
owner/provenance and non-token fields are preserved. Missing legacy metadata is
not invented. Its return value contains only booleans and fixed error codes.

After actual CLI exit and cleanup, call
`release_native_auth(rows, close_journal=cleanup_complete)`. Include its returned
`passed` flag in the controller verdict. If cleanup is uncertain, this releases
the lease but leaves the journal open, because an orphan might still refresh the
private copy. No success may be inferred from merely releasing the lease.

`recover_native_auth(source)` first requires read-only proof that the old UUID
cgroup is empty or removed, or that its Linux boot has ended. A released lease
alone is insufficient: an orphaned client might still be refreshing the copy.
An inaccessible cgroup hierarchy, changed cgroup identity, missing events file
or populated group fails closed. It can then finish a transaction **using local
files only**. It never launches a client, refreshes over the network, or retries
inference. The next setup also performs this recovery before creating a new seed:

- Original equals the baseline: validate the private refresh and commit it under
  the official lock.
- Original equals the pending candidate: finish durability/commit bookkeeping.
- Original differs from both: report a conflict, preserve both copies and the
  journal, and never overwrite the current account cache.

A closed journal means the previous controller confirmed synchronization and
client cleanup. Newer ordinary official-client changes after that completed run
do not need to be overwritten or rolled back. Unreadable or changed private
recovery artifacts fail closed; do not delete them to bypass a conflict.

For Claude 2.1.319, `native_auth_environment(rows)` pins the known official binary
and sets only `CLAUDE_SECURESTORAGE_CONFIG_DIR` to its original credential root.
`HOME` and `CLAUDE_CONFIG_DIR` remain private, excluding global MCP configuration
and hooks. The official client owns refresh persistence and locking; the helper
does not copy or CAS Claude's opaque tokens. Setup and final synchronization both
require a refreshable OAuth cache. No API-key fallback is enabled.
