# Portable project memory version 1

Cairnmind writes one project's archived knowledge under `.memory/cairnmind/`, with the format name `cairnmind-portable-memory`. Versions from DevBrain 0.4.0 to Cairnmind 0.9.0 wrote it under `.memory/devbrain/` with the format name `devbrain-portable-memory`. Both directories are reserved and both format names are read; a manifest is never rewritten, because the format name is part of what its identifier is computed from. When both directories hold a package, the one in `.memory/cairnmind/` is read, and `memory status` says under `earlier_package` whether the other one is part of its history (`superseded`) or not (`diverged`).
The package is data, not executable configuration. An explicit import is separate
from the administrative whole-database `restore` command.

## Identity and completeness

The manifest identifies the stable project UUID, the selected origin worktree,
snapshot and parent hashes, counts, and processing state at capture. Canonical
JSON objects are stored by SHA-256. Sources retain exact UTF-8 content and content
hashes, including archived conversation events and historical evidence. Records,
record revisions, plans, plan revisions, and logical graph relationships retain
their identities. Foreign code graphs and file snapshots do not establish the
state of the current local checkout.

Only committed, project-assigned rows are captured. Pending import, extraction,
or embedding work is reported rather than represented as finished. The package
does not contain environment configuration, credential files, embeddings,
sessions, resume packets, ACKs, cursors, or active job leases. Raw archived text
can itself contain private information; a full package is not automatically
sanitized for publication.

## Publication and local scheduling

Publication locks the reserved destination, captures a consistent database view,
then writes immutable objects before atomically replacing the manifest. Five
available successful manifests are retained, with shared objects deduplicated.
Unchanged knowledge produces no new snapshot. Git history is not erased by this
retention policy. Cairnmind does not stage, commit, or push files.

The per-project scheduler is opt-in and defaults to 15 minutes. One registered
worktree is its configured destination. The service must be running; it coalesces
missed intervals instead of replaying every missed tick. The first eligible tick
can create the initial snapshot. Conflicts preserve existing package contents and
are reported in status. An unknown or externally modified manifest requires
explicit inspection and import before further publication.

Only the reserved subtree is excluded from indexing and code file manifests.
Other `.memory/` content remains covered. A Git commit still changes HEAD and can
therefore invalidate checks and resume acknowledgements.

## Import and authority

Inspection validates hashes, declared identity, evidence closure, and package
structure without opening a database. Import binds the stable project identity
to an explicitly chosen existing directory; names and Git URLs do not establish
identity. A dry run compares the destination through an already running service
without initializing configuration or changing database rows.

The importer transaction preserves unrelated projects and historical completion.
Repeated import of the same snapshot is a no-op. Fast-forward updates require a
compatible baseline and unchanged local knowledge. Divergent heads, contradictory
immutable IDs, missing history, or ambiguous path bindings are rejected. No
automatic two-way merge or deletion of a project is performed.

Imported check runs have `origin=portable_import` and cannot satisfy local
verification. Existing genuine local runs are preserved when memory returns to
its original computer. New sessions require the normal packet read and exact
ACK. Imported task state `done` remains historical progress; it does not imply
that tests are fresh on this machine. Named checks use the local checkout and
must be run explicitly. No imported command or check profile is executed.

Text chunks are rebuilt from archived source views during import; semantic
embeddings and current-code analysis rebuild locally. Original paths remain
provenance, with local scope bindings kept separately.

## Validation budgets and quality evaluation

Version 1 limits an individual JSON object to 16 MiB, a manifest to 32 MiB, and a
complete package to 512 MiB and one million object descriptors. A package exceeding
these limits fails explicitly before replacing the current manifest; data is
never silently truncated. Source text is not sent to a model by export or import.

`cairnmind eval memory` uses two disposable databases and local Ollama to exercise
positive extraction, transfer, fresh-agent cited recall, and project isolation.
The three explicit fixture facts must all survive extraction; an empty extraction
does not pass this fixture. This is separate from the existing pre-seeded recall
evaluation. It launches no Codex or Claude processes. Exit codes are 0 for passed,
1 for an attempted failure, and 3 when required models are unavailable before the
test begins. Controlled-adapter regression results and real inference results are
reported separately.
