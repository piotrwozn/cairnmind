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
Only a person can store a record as `confirmed`: every tool here stores proposed records, a write that
asks for `confirmed` is refused, a write that would change, replace or lower a record a person confirmed is
refused too (it stores a new proposed record instead, and the person decides in the conflict inbox), and an
imported confirmation is kept as confirmed elsewhere
([MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#only-a-person-confirms-a-record)).
Updates require `id` and `expected_version`; task completion checks its declared
acceptance/test/review requirements. New test-gated completion requires fresh local
runner evidence; reported text and legacy booleans alone cannot satisfy it.
The same tools serve an external MCP client (`cairnmind mcp --client external`) in an
evidence-degraded session: its packet says `history: unarchived` and `evidence: degraded`,
and its writes carry a server-set `origin_*` mark and are never verified (V03_CONTRACTS.md).

The resume packet (`schema_version` 2) shows at most 20 records. Each has `origin`, derived
only from what the store wrote itself, never from a field a client sets (`packet_origin.py`):
`automatic_extraction` when the first version the store kept was written by the extraction
worker (its `extraction_source_id` mark, or its `origin: automatic_extraction` label), however
the record was edited or confirmed since; `external` when the record carries the store's
`origin_client` mark (an external client's session, or a built-in agent session after a
third-party tool result); `unattributed` for everything else, which is written through the
service by a person, a coding session or a memory import, since the store does not record
which. A client's own metadata can make its own record look less trusted, never more. An
imported record keeps the extraction label and the `origin_*` marks its package carried, both
in the record and in its revisions; a package can also leave them out, and its records then
show `unattributed`. Labels supplied by a package are taken as given, not checked. The
preview puts the latest checkpoint first, then confirmed records, then the others; within
each of the two groups the order is `unattributed`, `external`, `automatic_extraction`, each
by kind and id. `confirmed` is a status any client with a write can set, so the order is a
convenience, not a sign of trust; the origin label is what says where a record came from.
Proposed records are labelled, never removed for being proposed. `omitted_records` counts what
was left out and `omitted_by_origin` counts it per origin. When a packet is over its byte
budget, preview records leave it from the end of that order, one at a time and counted, before
the packet is reduced to its essential part (and made partial). The launch prompt, the
start hook's pending text, the bootstrap text, the built-in agent's instructions and the MCP
server's instructions say that record content is quoted project data, never instructions. A
packet of an earlier schema is never current: `resume_context` replaces it, and an ACK given
for it is stale (`packet_changed`).

A captured transcript horizon is complete only when every line in it is an archived record the reader
understood. A line stored as `transcript_unknown` keeps the packet partial, with one exception (#210): a
record whose `type` is a kind the reader does not know at all, that is a JSON object and names no other
session than the one it was stored under, does not block when a person accepted that kind (with its
client: `claude`, `codex` or `unknown`) as technical for the project. The packet names an unknown kind
nobody accepted once per transcript, with its client, version and byte, and how a person accepts it; it
never carries the record's text. The acceptance lives on the project's row (`accepted_record_kinds`, with
the date, the note, who accepted and the note source of the act; withdrawals in `withdrawn_record_kinds`),
is never part of a memory package, and is written only inside a person's act that the store checks
(`record_kinds.py`, `Store.open_person_act`): the command line in a person's own terminal
(`person_at_terminal`) or the inspector route `/inspector/record-kinds/accept` and `/withdraw`, which has no
session field and is what the workspace calls. A malformed record of a known kind, a line that is not a
JSON object, and every integrity check of the horizon (hashes, offsets, rotation, the import cursor,
capture recovery provenance) are never covered. An acceptance changes no stored source: the records stay
`transcript_unknown`, unextracted, with their raw text indexed as before. While a transcript can be read,
every packet checks it against the acceptances that stand then, so a withdrawal blocks the next packet; a
horizon proved complete whose transcript is gone keeps its proof, as before.

`Store` owns native transactions, project isolation, source snapshots, record
revisions, task validation, durable job leases, native graph identity, export and
checksummed source-complete `.cairnmind.json.gz` backups (`.devbrain.json.gz` up to 0.9.0; both are restored). Callers use named-parameter
ArcadeSQL through `query`, `command` and `transaction`, never another storage engine.
A database has one declared writer, an installation identified by `installation.json` in its data
directory (#70): another installation that points at the same database reads it only, and only a
person moves the role, with `cairnmind store takeover --yes` (see ARCADE_CONTRACTS.md).

The local service owns the priority inference queue. End hooks write durable
filesystem events without connecting to the database; scanning imports them later.
No model answer is promoted into independent evidence. All answer/source/context
payloads have explicit budgets, and unavailable or incomplete evidence is reported.

## What it guarantees, and what it does not

Cairnmind checks things a program can check:

| It verifies | It does not verify |
|---|---|
| A cited quote occurs literally in the source version that was read, and that source is still current. Only the layout may differ in what the model wrote (line breaks, spacing beside punctuation, Markdown code marks, lines of the source left out between quoted lines that are all there); the quote shown is always the source's own text. When the model's one correction still leaves a citation that fails, that citation is left out if another one verified: the answer text is not changed, and the result says so in `freshness.warnings` and counts it in `diagnostics.citations_dropped` | That the model interpreted the quote correctly. A second call to the local model, the support check, must agree that the quotes state the answer, or the answer is withheld (`answer_unsupported`, or `support_unverified` when the check gives no verdict). It reliably rejects a value quoted from a test (a code file named or placed as a test, or a data or configuration file in a test directory), a lock file or a comment that only mentions the number; it does not reliably tell a neighbouring setting or a value used for something else (a display limit, another profile's default) from the one asked about. Before that call a check that asks no model looks for the names the question uses for its subject (a capitalised name such as a product, an identifier, one word in backticks) in the quoted sources and their paths; when a name is in none of them the answer is withheld with `answer_unsupported` and the missing names are listed, so a question about a product the project never mentions is not answered with a number that belongs to something else. Every name of the question is needed, so a name that is only context (an operating system, a person) withholds the answer too when no quoted source has it; asking again without it gets the answer. That check does not read a product name written in lower case without backticks, a word in capitals or a question in headline case, and it does not catch a name the quoted file has somewhere else than beside the quoted value (one part's setting asked under the name of another part of the same file); such questions rest on the support check alone. Quote coverage of an answer is reported for information only |
| A session acknowledged the exact packet, plan and task versions before writing | That the model understood what it acknowledged |
| A test run passed for this task definition, check profile and code fingerprint, and whether it is still fresh | That passing tests mean every acceptance criterion is met, or that a review happened |
| Where a stored finding came from, and when its evidence existed | That the finding is true |
| What an external MCP client writes through its tools is marked `origin_evidence: degraded` and never shown or counted as verified ([External clients](EXTERNAL_CLIENTS.md)) | That what it reports is true; a `cairnmind` command its agent runs in a shell has no session and is stored unmarked |
| Without models, `ask` makes no answer: it reports `unavailable` and lists full-text matches as search results ([deterministic-only mode](TERMINAL.md)) | That a listed passage answers the question: no citation is made or checked |

Local memory reduces how much history an assistant must carry, but it does not remove the context limits or the cost of the assistants themselves. Portable snapshots can contain full conversation text; review them before sharing.

## Limits

Tests for storage, full-text search, the graph and the vector index use a real ArcadeDB engine. They require Java 21; the default test session prepares an isolated engine. Automated tests of Ollama answers and client sessions use controlled adapters; a real handoff of work between clients has to be measured in the target environment.

Syntax analysis covers Python, JavaScript and TypeScript. Other languages remain searchable as text. Static analysis does not guarantee a complete graph of dynamic calls. The scanner indexes UTF-8 text files up to 2 MiB, respects `.gitignore` and skips, among others, dependency directories and `.env`/key files. History import preserves the original source content.

`ask` answers and the startup context still consume Codex or Claude tokens. Local memory reduces the need to carry the whole history along; it does not remove context limits or the cost of model work. API and Copilot connections use their own account billing and permissions.

The launcher blocks a client from starting when the resume package is partial or unavailable. A transcript record of a kind this release does not know (a newer Codex or Claude Code may write one) keeps the packets of its project partial, and the packet says which kind, client and version, at which byte. A person can look at such a record with `cairnmind record-kind list PROJECT` or under More, then Transcript record kinds in the workspace, and accept its kind as technical for that one project with a note (`cairnmind record-kind accept PROJECT CLIENT KIND --note TEXT` in their own terminal, or the same view); packets of that project are ready again without a new release, and other projects are not affected. `doctor` lists every acceptance and `record-kind withdraw` takes one back. Only a person accepts: the command refuses inside a coding session and without a terminal, and no MCP tool or built-in agent tool can do it. A malformed record of a kind the reader knows, and a transcript that fails its integrity checks, still block. An ACK confirms receipt and reading of specific versions, not the model's understanding. Cairnmind's write controls do not replace the native tool permissions of Codex and Claude.

## Extension surfaces

Cairnmind does not load third-party code into any of its own processes: no plugins, entry points or hooks that run someone else's Python, or a command, inside the service, the workspace broker, the workers or the MCP bridge. This holds for 1.0.0 as well. The service token is stored in `config.json` in the data directory (`config.py`), together with the database password of the managed local engine; a database on another host takes its password from the environment or a private file (`database_credentials.py`), which a Cairnmind process reads too. So code running in a Cairnmind process could read both. Nothing could then enforce the resume acknowledgement, the rule that reviews, conflict decisions and impact verdicts are a person's, or the evidence rules against it.

What can be added is added outside those processes, through the surfaces below. Each one is described as it exists today.

### Supported today

**Another check.** Declare a named profile in `.cairnmind/checks.toml` of the project (the name `.devbrain/checks.toml` used up to 0.9.0 is still read; declaring both is an error). A profile has `argv`, a non-empty list of non-empty strings, and optionally `cwd` (relative, inside the worktree), `timeout_seconds` (above 0, at most 86400, default 600), `output_limit_bytes` (1 to 10485760, default 10485760) and `extra_inputs`; any other key makes the profile invalid (`verification.py`, `load_profiles`). `cairnmind check run PROJECT PROFILE --task TASK_ID` starts `argv` as a subprocess with an argument list, without a shell, without standard input and in its own process group, with the environment of the `cairnmind` command that runs it (`_execute`). A timeout or an exceeded output limit ends the process and the run fails. The run is stored as a `check_runs` record with origin `local_runner`, the only origin there is. It passes only when the process completed with exit code 0 and the code, the profile and the task did not change during the run; its standard output and error are stored as `check_output` sources (`_finish_run`, `_output_source`). Nothing in the output is parsed: the exit code is the result. A check is never run through MCP or by the service. The file is tracked in the repository, so whoever can edit the repository can declare a command that runs with the permissions of the person who runs it. See [WORKFLOW.md](WORKFLOW.md#named-check-profiles).

**A reaction to events.** `GET /events` of the memory service (`inspector_api.py`, `create_inspector_router`; the service listens on `127.0.0.1:8765` by default) streams the change journal as server-sent events. It needs the header `Authorization: Bearer TOKEN` with the token from `config.json`, as every route except `/health` does. The query parameters are `after` (a cursor `EPOCH:SEQ`), `project` and `once`; a `Last-Event-ID` header is read as the cursor too. Each `event: change` has the id `EPOCH:SEQ` and a JSON `data` with only `epoch`, `seq`, `project_id`, `entity_type`, `entity_id`, `op` and `at` (`journal.py`, `Journal.read`). It says that a source, record, plan, check run, session and so on changed, never what it contains; a client that wants the content asks the service for that id. A cursor from another epoch, or older than the journal keeps, gets an `event: reset` with the current cursor, and a comment line is sent every 15 seconds. The workspace reads the same feed through its own proxy. The route exists and is tested (`tests/test_inspector.py`), but it is not documented elsewhere and is not a promised stable contract: its fields and entity types may change in a release. Cairnmind runs no command in answer to an event; whatever listens is a process of the person's own.

**Another MCP client.** `cairnmind mcp --client external --project PATH` serves the memory tools to Cursor, Cline, Zed or any other client that speaks MCP over stdio. Its session is evidence-degraded: its resume packet says `history: unarchived` and `evidence: degraded`, what it writes carries an `origin_*` mark set by the service (`sessions.py`, `origin_mark`) and is never anchored or verified, and the acknowledgement is required as for any client. The command refuses to start where the launcher set `CAIRNMIND_SESSION_ID` or `CAIRNMIND_PACKET_ID` (`mcp_server.py`). It is described in [EXTERNAL_CLIENTS.md](EXTERNAL_CLIENTS.md).

**Another model for the built-in agent.** A connection profile of the API agent names a provider from a fixed list (`openai`, `anthropic`, `openai_compatible` for any server that speaks Chat Completions at a base URL of its own, and `ollama`) and a model id (`api_profiles.py`, `PROVIDERS`, `normalize_profile`). It is created in the workspace under **Connections** ([API.md](API.md)). A server that speaks one of those four protocols can be used; a fifth protocol needs a change to Cairnmind, and nothing loads one from outside.

**Tools from MCP servers for the built-in agent.** A person adds up to four stdio MCP servers per project under **More → MCP servers** in the workspace; they are stored in the owner-local cockpit preferences (`project_mcp_servers`, `mcp_servers.py`) and copied into the configuration of the next API agent run of that project (`api_runtime.py`). Each server runs as a separate child process of the agent worker, never inside a Cairnmind process: with a minimal environment built from scratch and no Cairnmind variable or credential, a private working directory, no stderr and its own process group, stopped when the run ends (`mcp_host.py`). Its tools are offered only after the resume acknowledgement, every call needs the person's one-use approval, and what it returns is archived as degraded third-party text that is never chunked or anchored; after a third-party result, nothing the agent says or does in that session counts as evidence (`mcp_agent.py`, `agent_memory.py`). A server is a program the person chose to run under their own account, so it can read what that account can, including `config.json`; Cairnmind is not a sandbox for it. See [API.md](API.md#third-party-mcp-tools).

### Planned, not yet there

- History of a client whose transcripts Cairnmind does not read, through a converter command that a person registers and runs from the command line outside any coding session: issue #127, later slices. Converted history would be marked as never verified. None of it exists yet; history import reads Codex and Claude Code only.

No date is promised for it.

### Not supported

- Views in the workspace or the cockpit. They are part of Cairnmind; a tool that wants the data reads it through the CLI output or the routes above.
- Plugins that run inside a Cairnmind process, in any language.
- Anything that writes past the resume gate: no extension can write to a project as a session without the acknowledgement, and none can give a review, a conflict decision or an impact verdict as a person.
