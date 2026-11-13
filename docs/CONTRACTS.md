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

## Extension surfaces

Cairnmind does not load third-party code into any of its own processes: no plugins, entry points or hooks that run someone else's Python, or a command, inside the service, the workspace broker, the workers or the MCP bridge. This holds for 1.0.0 as well. The service token and the database password are stored together in `config.json` in the data directory (`config.py`), so code running in a Cairnmind process could read both. Nothing could then enforce the resume acknowledgement, the rule that reviews, conflict decisions and impact verdicts are a person's, or the evidence rules against it.

What can be added is added outside those processes, through the surfaces below. Each one is described as it exists today.

### Supported today

**Another check.** Declare a named profile in `.cairnmind/checks.toml` of the project (the name `.devbrain/checks.toml` used up to 0.9.0 is still read; declaring both is an error). A profile has `argv`, a non-empty list of non-empty strings, and optionally `cwd` (relative, inside the worktree), `timeout_seconds` (above 0, at most 86400, default 600), `output_limit_bytes` (1 to 10485760, default 10485760) and `extra_inputs`; any other key makes the profile invalid (`verification.py`, `load_profiles`). `cairnmind check run PROJECT PROFILE --task TASK_ID` starts `argv` as a subprocess with an argument list, without a shell, without standard input and in its own process group, with the environment of the `cairnmind` command that runs it (`_execute`). A timeout or an exceeded output limit ends the process and the run fails. The run is stored as a `check_runs` record with origin `local_runner`, the only origin there is. It passes only when the process completed with exit code 0 and the code, the profile and the task did not change during the run; its standard output and error are stored as `check_output` sources (`_finish_run`, `_output_source`). Nothing in the output is parsed: the exit code is the result. A check is never run through MCP or by the service. The file is tracked in the repository, so whoever can edit the repository can declare a command that runs with the permissions of the person who runs it. See [WORKFLOW.md](WORKFLOW.md#named-check-profiles).

**A reaction to events.** `GET /events` of the memory service (`inspector_api.py`, `create_inspector_router`; the service listens on `127.0.0.1:8765` by default) streams the change journal as server-sent events. It needs the header `Authorization: Bearer TOKEN` with the token from `config.json`, as every route except `/health` does. The query parameters are `after` (a cursor `EPOCH:SEQ`), `project` and `once`; a `Last-Event-ID` header is read as the cursor too. Each `event: change` has the id `EPOCH:SEQ` and a JSON `data` with only `epoch`, `seq`, `project_id`, `entity_type`, `entity_id`, `op` and `at` (`journal.py`, `Journal.read`). It says that a source, record, plan, check run, session and so on changed, never what it contains; a client that wants the content asks the service for that id. A cursor from another epoch, or older than the journal keeps, gets an `event: reset` with the current cursor, and a comment line is sent every 15 seconds. The workspace reads the same feed through its own proxy. The route exists and is tested (`tests/test_inspector.py`), but it is not documented elsewhere and is not a promised stable contract: its fields and entity types may change in a release. Cairnmind runs no command in answer to an event; whatever listens is a process of the person's own.

**Another MCP client.** `cairnmind mcp --client external --project PATH` serves the memory tools to Cursor, Cline, Zed or any other client that speaks MCP over stdio. Its session is evidence-degraded: its resume packet says `history: unarchived` and `evidence: degraded`, what it writes carries an `origin_*` mark set by the service (`sessions.py`, `origin_mark`) and is never anchored or verified, and the acknowledgement is required as for any client. The command refuses to start where the launcher set `CAIRNMIND_SESSION_ID` or `CAIRNMIND_PACKET_ID` (`mcp_server.py`). The README describes it in [Cursor, Cline, Zed and other MCP clients](../README.md#cursor-cline-zed-and-other-mcp-clients).

**Another model for the built-in agent.** A connection profile of the API agent names a provider from a fixed list (`openai`, `anthropic`, `openai_compatible` for any server that speaks Chat Completions at a base URL of its own, and `ollama`) and a model id (`api_profiles.py`, `PROVIDERS`, `normalize_profile`). It is created in the workspace under **Connections** ([API.md](API.md)). A server that speaks one of those four protocols can be used; a fifth protocol needs a change to Cairnmind, and nothing loads one from outside.

**Tools from MCP servers for the built-in agent.** A person adds up to four stdio MCP servers per project under **More → MCP servers** in the workspace; they are stored in the owner-local cockpit preferences (`project_mcp_servers`, `mcp_servers.py`) and copied into the configuration of the next API agent run of that project (`api_runtime.py`). Each server runs as a separate child process of the agent worker, never inside a Cairnmind process: with a minimal environment built from scratch and no Cairnmind variable or credential, a private working directory, no stderr and its own process group, stopped when the run ends (`mcp_host.py`). Its tools are offered only after the resume acknowledgement, every call needs the person's one-use approval, and what it returns is archived as degraded third-party text that is never chunked or anchored; after a third-party result, nothing the agent says or does in that session counts as evidence (`mcp_agent.py`, `agent_memory.py`). A server is a program the person chose to run under their own account, so it can read what that account can, including `config.json`; Cairnmind is not a sandbox for it. See [API.md](API.md#third-party-mcp-tools).

### Planned, not yet there

- History of a client whose transcripts Cairnmind does not read, through a converter command that a person registers and runs from the command line outside any coding session: issue #127, later slices. Converted history would be marked as never verified. None of it exists yet; history import reads Codex and Claude Code only.

No date is promised for it.

### Not supported

- Views in the workspace or the cockpit. They are part of Cairnmind; a tool that wants the data reads it through the CLI output or the routes above.
- Plugins that run inside a Cairnmind process, in any language.
- Anything that writes past the resume gate: no extension can write to a project as a session without the acknowledgement, and none can give a review, a conflict decision or an impact verdict as a person.
