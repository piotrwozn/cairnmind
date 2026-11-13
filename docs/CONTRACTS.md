# Cairnmind implementation contracts

The active backend is **ArcadeDB 26.11.1**, using native document types, graph
vertices/edges, FULL_TEXT/BM25 and LSM_VECTOR indexes. See
[ARCADE_CONTRACTS.md](ARCADE_CONTRACTS.md) for component ownership and the HTTP DAL.
The current 0.3 workflow and interface additions are specified in
[V03_CONTRACTS.md](V03_CONTRACTS.md); they supersede earlier task and launcher rules.

Public memory tools remain `ask(project, question)`, `remember(project, record,
sources)` and `checkpoint(project, state)`. Source and record IDs are stable strings.
Additional tools are `save_plan`, `next_task`, `resume_context`, `ack_resume` and
read-only `check_status`. Task progress (`metadata.state`) is separate from knowledge confirmation status.
Updates require `id` and `expected_version`; task completion checks its declared
acceptance/test/review requirements. New test-gated completion requires fresh local
runner evidence; reported text and legacy booleans alone cannot satisfy it.
The same tools serve an external MCP client (`cairnmind mcp --client external`) in an
evidence-degraded session: its packet says `history: unarchived` and `evidence: degraded`,
and its writes carry a server-set `origin_*` mark and are never verified (V03_CONTRACTS.md).

`Store` owns native transactions, project isolation, source snapshots, record
revisions, task validation, durable job leases, native graph identity, export and
checksummed source-complete `.cairnmind.json.gz` backups (`.devbrain.json.gz` up to 0.9.0; both are restored). Callers use named-parameter
ArcadeSQL through `query`, `command` and `transaction`, never another storage engine.

The local service owns the priority inference queue. End hooks write durable
filesystem events without connecting to the database; scanning imports them later.
No model answer is promoted into independent evidence. All answer/source/context
payloads have explicit budgets, and unavailable or incomplete evidence is reported.
