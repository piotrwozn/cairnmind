# Cairnmind

[![CI](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml/badge.svg)](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fpiotrwozn%2Fcairnmind%2Fbadges%2Fcoverage.json)](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)

**Evidence-grounded project memory for coding agents, in your terminal.**

*A cairn is the pile of stones a walker leaves to mark the trail for the next one. Cairnmind is the memory one coding agent leaves for the next. Formerly DevBrain.*

Cairnmind is a local workspace for Codex CLI, Claude Code, GitHub Copilot and API-based coding agents (OpenAI, Anthropic, OpenAI-compatible endpoints and local Ollama). It keeps a durable, versioned memory of each project outside any single conversation, so the next session, or a different assistant, can pick up exactly where the last one stopped. It is not a plugin for one assistant and not a notes store that a model writes into: it is a separate local service that assistants connect to, and it does not treat a model's statement as evidence.

What sets it apart from a `CLAUDE.md` file or a generic memory layer is that Cairnmind treats memory as evidence, not as notes:

- **Every answer cites its sources.** The local knowledge agent must back an `ask` answer with verbatim quotes. The service checks that each quote occurs literally in the source it read and that the source is still current, and then asks the local model once more whether the quotes state the answer; otherwise the answer is not presented as grounded. When the project does not contain the answer, the agent says `unknown`.
- **Resuming is verified, not assumed.** A bound session can write to memory only after the model acknowledges the exact plan and task versions it read. The check happens in the service, not in a prompt.
- **"Done" requires fresh proof.** For tasks that require tests, completion needs a passing run of each named test profile, bound to the task definition and to a fingerprint of the current code. A later code change marks that evidence stale.
- **Memory travels with the repository.** A checksummed snapshot in `.memory/cairnmind/` moves one project's knowledge, plans and history to another machine without moving credentials.

## How it compares

| | Instruction file (`CLAUDE.md`, `AGENTS.md`) | Memory built into an assistant | Memory plugin or layer | Cairnmind |
|---|---|---|---|---|
| Who writes it | You, by hand | The model | The model | The model and you, each record tied to a source |
| Checked against the source | No | Usually no | Usually no | An answer's quotes must occur literally in a current source |
| Works across assistants | Each assistant reads its own file | No | Depends on the plugin | Codex CLI, Claude Code, Copilot, API agents; other MCP clients, marked unverified |
| "Done" needs proof | No | No | Usually no | For tasks that require tests: a passing check bound to the current code |
| Setup | None | None | Usually one command | Java, tmux, Ollama and local models |

The columns are kinds of tools, not products; a particular tool may do more than its column says. The last row is the price: Cairnmind is the heaviest of the four to install.

Planned, not there yet: shared project memory for a team and read-only views above a single project. The steps are on the [features board](https://github.com/users/piotrwozn/projects/2).

## Who it is for

Cairnmind suits developers who work with Codex CLI or Claude Code every day, switch between assistants or return to projects after a break, and are comfortable running Linux or WSL2 with a local model through Ollama. It is a single-user, local tool. If you only want a lighter notes file for one assistant, a `CLAUDE.md` is simpler.

## What it guarantees, and what it does not

Cairnmind checks things a program can check:

| It verifies | It does not verify |
|---|---|
| A cited quote occurs literally in the source version that was read, and that source is still current | That the model interpreted the quote correctly. A second call to the local model, the support check, must agree that the quotes state the answer, or the answer is withheld (`answer_unsupported`, or `support_unverified` when the check gives no verdict). It reliably rejects a value quoted from a test (a code file named or placed as a test, or a data or configuration file in a test directory), a lock file or a comment that only mentions the number; it does not reliably tell a neighbouring setting or a value used for something else (a display limit, another profile's default) from the one asked about. Quote coverage of an answer is reported for information only |
| A session acknowledged the exact packet, plan and task versions before writing | That the model understood what it acknowledged |
| A test run passed for this task definition, check profile and code fingerprint, and whether it is still fresh | That passing tests mean every acceptance criterion is met, or that a review happened |
| Where a stored finding came from, and when its evidence existed | That the finding is true |

Local memory reduces how much history an assistant must carry, but it does not remove the context limits or the cost of the assistants themselves. Portable snapshots can contain full conversation text; review them before sharing.

## How it works

```text
Terminal: tmux + Textual ──► Codex CLI / Claude Code / API agent / Copilot SDK
                           │ MCP: resume / ACK / ask / plans
                           ▼
                 Local Cairnmind service
                  │                   │
       knowledge agent (Ollama)    independent archiving
                  │                   │
          Hybrid RAG + GraphRAG ◄─────┘
                  │
       ArcadeDB: sources, vectors, graph, decisions, tasks
```

- **Storage:** a single pinned [ArcadeDB](https://github.com/ArcadeData/arcadedb) engine holds documents, the code dependency graph, full-text and vector indexes.
- **Retrieval:** full-text search, HNSW vectors and graph traversal, fused and re-checked against source content.
- **Knowledge agent:** `qwen3.5:9b` through Ollama with a 16k-token working context, and `qwen3-embedding:0.6b` embeddings. Coding assistants are chosen independently.
- **Code graph:** tree-sitter analysis of Python, JavaScript and TypeScript. Other languages remain searchable as text.
- **Memory:** versioned records and independent source snapshots, including uncommitted code.
- **Queue:** one inference queue shared by all clients; questions take priority over pending extraction and embedding work.
- **Plans:** a whole plan and its dependencies are saved atomically, with stable step identifiers and a deterministic next step per project and worktree.

The way memory and agents are organized is inspired by SynapCores. Cairnmind is an independent implementation of the knowledge layer; it is not a copy of that product or of its database engine.

## Built with

Cairnmind stands on other people's work. In one place:

- [ArcadeDB](https://github.com/ArcadeData/arcadedb): the one engine behind the documents, the code graph, full-text search and the vector index.
- [Ollama](https://ollama.com) with the Qwen models `qwen3.5:9b` and `qwen3-embedding:0.6b`: the local knowledge agent and the embeddings.
- [tree-sitter](https://tree-sitter.github.io/tree-sitter/): the analysis of Python, JavaScript and TypeScript for the code graph.
- [Textual](https://github.com/Textualize/textual) and [tmux](https://github.com/tmux/tmux): the terminal workspace and the panes the assistants run in.
- [Model Context Protocol](https://modelcontextprotocol.io) and its [Python SDK](https://github.com/modelcontextprotocol/python-sdk): how coding assistants reach the memory.
- [FastAPI](https://fastapi.tiangolo.com), [Uvicorn](https://www.uvicorn.org), [HTTPX](https://www.python-httpx.org) and [Typer](https://typer.tiangolo.com): the local service, its clients and the command line.
- [GitHub Copilot SDK](https://pypi.org/project/github-copilot-sdk/): the Copilot connection of the built-in agent.
- [uv](https://github.com/astral-sh/uv), [pytest](https://pytest.org) and [Ruff](https://github.com/astral-sh/ruff): installation, tests and linting.

The coding assistants it works with, Codex CLI and Claude Code among them, are not part of Cairnmind; it starts them and keeps their project memory.

## Measured results

Measured on 9 November 2026 on release 0.8.0 with `qwen3.5:9b` on an RTX 4080 Super, in isolated databases with synthetic fixtures. They were not repeated for 0.9.0, and they are not proof of behaviour on every real project.

| Evaluation | Result |
|---|---|
| Recall: 20 golden questions, pass threshold 0.9 | 1.0 in 6 of 6 runs |
| Hand-off Codex → Claude Code → fresh Codex, real clients | passed once, before the 0.8.0 fixes; not repeated since |
| Portable memory: extraction, transfer, cited recall, project isolation | 3 of 3 runs on 0.8.0 |

The method, the problems the measurement uncovered and the limits of these numbers are described in [docs/EVALUATION.md](docs/EVALUATION.md).

## Look before you install

The full system needs Java, tmux, Ollama and several gigabytes of models. To see what Cairnmind keeps without any of them, read the [sample project memory](examples/sample-project/README.md): a small project with its decisions, a plan and a check run committed next to it, and a read-only command that needs only Python and uv.

## Quick start

Cairnmind runs on Linux, and on Windows inside WSL2 Ubuntu (Ubuntu 24.04 is the tested platform). It needs Java 21+, tmux 3.4+, uv and Ollama. With those in place:

```bash
git clone https://github.com/piotrwozn/cairnmind.git ~/src/cairnmind
cd ~/src/cairnmind
bash scripts/install-wsl.sh     # installs Cairnmind and ArcadeDB, initialises local memory
cairnmind models pull            # downloads the local models (about 7 GB)
cairnmind project add my-app ~/src/my-app
cairnmind install my-app         # connects Codex / Claude Code to this project
cairnmind                        # opens the terminal workspace
```

**[Step-by-step installation guide](docs/INSTALL.md)**: preparing Windows and WSL2, installing from a release, connecting assistants, verifying, upgrading, uninstalling and troubleshooting.

Docker, Node.js and a frontend build are not needed. The reference setup uses an RTX 4080 Super with 16 GB of VRAM; check memory use and latency on your own machine.

## Your first project

`cairnmind` works from any directory and uses a single project registry in the local Cairnmind store. You can also add a project with **Add folder**: type a Linux or Windows path, or pick a directory with the keyboard. The name is optional. `Enter` on the list opens the project for inspection. On the project screen, choose a named assistant and use **Start work**. **Add assistant** adds another connection to the project and keeps existing assignments.

In **Connections**, add a connection, log in to an account or set a key, choose a model and optionally run an explicit test. Copilot uses your GitHub account and needs no API key of its own. Limits and credential references are under **Advanced limits and credentials**. Saving a connection does not generate a response. Opening the environment attaches to existing processes; it does not start every assigned assistant. See [docs/API.md](docs/API.md) for the full API guide.

## Everyday use

### Workspace

The full Cockpit layout needs at least **140 columns × 42 rows**; **180 × 55** is recommended. In a smaller terminal the active pane is enlarged and hidden processes keep running. `Ctrl+B`, then `p`, `1`, `2`, `i` or `g`, selects projects, clients, the inspector or the graph; `z` zooms the pane, `n` opens a form to save a finding to memory and `d` detaches the environment. Pressing `Ctrl+B` twice passes the shortcut through to the native client. Without an interactive terminal, Cairnmind prints instructions instead of opening a browser or initializing services.

```bash
cairnmind workspace status
cairnmind workspace detach
cairnmind workspace takeover
cairnmind workspace stop
cairnmind workspace stop --clients
```

Detaching keeps the environment and its processes. `workspace stop` closes the outer layout; `--clients` also explicitly stops the clients adopted by this workspace. Other launches stay independent. Shutting down WSL ends the processes, but saved memory remains available for an explicit resume.

One terminal controls the workspace. A second terminal opens a read-only view; `cairnmind workspace takeover` explicitly takes control and switches the previous terminal to view mode, without starting new clients.

The launcher and `cairnmind run` refuse to start a client that lacks verified support for MCP, hooks, package handoff and session binding. `cairnmind doctor` shows the detection result. A project with existing code needs a Git repository and a first commit; the exception for an empty project explicitly marks that no code has been verified.

### Without the launcher

```bash
cairnmind run my-app codex
cairnmind run my-app claude
cairnmind ask my-app "Why did we change login, and what depends on it?"
cairnmind context my-app
cairnmind status --project my-app
```

`ask` returns an answer, its sources, knowledge gaps and freshness. When the model is unavailable it reports `unavailable`; it does not pretend that raw search results are an agent answer.

### Cursor, Cline, Zed and other MCP clients

A client the launcher cannot start uses the same memory through an **external session**. Cairnmind cannot read or archive such a client's conversation, so its session is evidence-degraded: it can ask, read the resume packet, acknowledge it and write, but nothing it writes is ever shown or counted as verified. Start the service first (`cairnmind serve`, or the workspace), then give the client this stdio MCP server:

```bash
cairnmind mcp --client external --project /path/to/my-app
```

`--project` takes the project id, its name or a path inside it. A relative path is read from the editor's working directory, not the service's, and a registered name wins over a folder of the same name. Without it the client's working directory is used, which an editor does not always set to the project. `--worktree` picks a registered worktree; by default it is the one containing the project path. Use the absolute path of the `cairnmind` command (`command -v cairnmind`), since an editor may not read your shell's `PATH`. For Cursor (`.cursor/mcp.json`) and Cline (its MCP settings file):

```json
{
  "mcpServers": {
    "cairnmind": {
      "command": "/home/me/.local/bin/cairnmind",
      "args": ["mcp", "--client", "external", "--project", "/path/to/my-app"]
    }
  }
}
```

Zed takes the same command and arguments as a custom context server in its settings; see Zed's documentation for the current form. An editor running on Windows reaches Cairnmind in WSL with `"command": "wsl.exe"` and the arguments `["-d", "Ubuntu", "--", "/home/me/.local/bin/cairnmind", "mcp", ...]`. None of these editors was connected in Cairnmind's own tests, which drive the server with the MCP SDK's client.

What the session does:

- The first `resume_context` creates the session. Its packet has `"client": "external"`, `"history": "unarchived"` and `"evidence": "degraded"`, with a warning saying so. The ACK works as for any client: until `ack_resume` with the exact packet id, hash and readback, `remember`, `checkpoint` and `save_plan` are refused, and a change of plan, task, decisions or code asks for a new packet and ACK.
- `--client external` refuses to start where the launcher set `CAIRNMIND_SESSION_ID` or `CAIRNMIND_PACKET_ID`: a verified native session is never replaced by a degraded one.
- Transcripts of Codex or Claude Code in the same worktree that are not yet archived, or that hold a record Cairnmind cannot parse, keep the packet `partial`, exactly as for a native client.
- Everything the session writes (records, checkpoints, plans and tasks) carries `origin_client: external`, `origin_session_id`, `origin_history: unarchived` and `origin_evidence: degraded`, set by the service and never by the client. A record keeps this mark when anyone edits it later; the one checkpoint record is the exception, since every checkpoint replaces it, so its mark follows whoever saved the latest checkpoint, and a checkpoint by anyone else that replaces an external one keeps none of the external one's fields. It starts from its own state alone, so fields that earlier native checkpoints set and neither of the last two repeats are dropped as well: that is the price of never carrying external text unmarked. A plan save marks the steps whose stored definition it adds or changes (content, dependencies, order, profiles, criteria and their links) and the plan when anything in it changed. Its evidence state is `degraded`, never `anchored`, in resume packets, the inspector, `cairnmind memory orphans` and `cairnmind impact`, and a source it wrote cannot anchor another record; search hits from such a source carry the mark too. An active plan or task it wrote is marked in every later resume packet, also a native client's, whose bootstrap then does not call itself verified. Its test and acceptance claims stay `reported`; only a check run by `cairnmind check run` is machine evidence.
- The mark exists only for writes made through the MCP tools. If the editor's agent runs the `cairnmind` command in its shell, that process has no session: its writes are stored as a person's, unmarked and without the ACK gate, as for any process without a session. Let the editor write through the MCP tools, not through the command line.

### Third-party MCP tools for the built-in agent

The built-in API agent (OpenAI, Anthropic, OpenAI-compatible endpoints, Ollama) can use the tools of up to four stdio MCP servers per project. Only you add or remove them, in the workspace under **More → MCP servers** for the selected project; the list lives in your local cockpit preferences, not in the repository, and applies from the next agent run of that project. Other projects, Copilot runs and the native Codex and Claude Code clients do not get them.

- A server's tools appear as `mcp_<server>_<tool>` and only after the model has acknowledged its resume packet, like every tool with an effect. Every call shows the server, the tool and the exact arguments and needs your one-use approval, also for a tool its server calls read-only. There is no "always allow".
- A server gets no secrets: the form takes a program path, arguments and literal environment values, and refuses names that look like credentials or belong to Cairnmind, and variables such as `PATH`, `HOME`, `LD_PRELOAD`, `PYTHONPATH` or `NODE_OPTIONS` that change which code a program loads. A server whose program, argument or value names a path inside the project folder is not started. A server that needs a token works only through its own sign-in under your home directory.
- What a server returns is untrusted third-party text: bounded, marked as such for the model, kept in the agent's journal, archived with `origin_evidence: degraded` so it never makes a record verified, and never indexed for search. After a third-party tool was used, nothing the agent says or does in that session is searchable or evidence: its later replies, tool requests and results are archived the same way and what it then writes to memory is marked degraded.
- A tool whose input schema is outside the subset every provider accepts is left out; if a provider still refuses the request with them and the same request succeeds without them, the third-party tools are withdrawn for the run and the agent works on without them.
- Only tools are used (no resources, prompts, sampling, elicitation or roots), over the stdio transport only.

Two things are accepted, not prevented. A stdio server is a program you chose to run under your own account: it can read and change what your account can, and Cairnmind is not a sandbox for it. And text from a server can steer the model into editing files in the worktree without an approval, exactly as text read from repository files can today. Details are in [docs/API.md](docs/API.md#third-party-mcp-tools).

## Plans, tasks and evidence

Saving a plan writes the header, the steps and the final dependency graph in a single transaction. Codex and Claude see the same versions. An active plan is bound to a specific registered worktree. The launcher shows plans, the next step, blockers, evidence freshness and session package readiness.

```bash
cairnmind plan save my-app ./plan.json --idempotency-key login-plan-1 --activate
cairnmind plan next my-app
cairnmind plan list my-app
cairnmind task list my-app
```

Progress states are `todo`, `in_progress`, `needs_test`, `needs_review`, `done` and `blocked`. A separate `proposed`/`confirmed` status describes how certain a finding is. A `done` result remains part of the execution history; a later code change can turn its verification to `stale`. The plan format, check profiles and MCP calls are described in [docs/WORKFLOW.md](docs/WORKFLOW.md).

A step's state also names its stage: `implement` (`todo`, `in_progress`), `review` (`needs_review`) or `test` (`needs_test`). In the workspace, **More → Plan stages** maps each stage of a project to one of its assistants (Codex, Claude Code, the built-in API agent or GitHub Copilot), or `test` to a check profile, and **Run next stage** starts the mapped assistant like **Start work** (with its own resume packet to acknowledge) or runs the mapped check with `cairnmind check run`. It never moves a step on and never starts another assistant in place of a missing one or of the one you chose (a stage whose slot now names another profile waits until you save it again), and an API or Copilot profile without its key or runtime is shown as such with nothing started; `cairnmind stage next my-app` shows the same decision without starting anything. See [Plan stages](docs/WORKFLOW.md#plan-stages).

Declare check profiles in `.cairnmind/checks.toml`, for example `[checks.unit]` with `argv = ["uv", "run", "pytest"]`. Running a check is an explicit operation, with the CLI or with **Run next stage** for the profile mapped to the `test` stage:

```bash
cairnmind check run my-app unit --task TASK_ID
cairnmind check list my-app --task TASK_ID
cairnmind eval run
cairnmind eval run --clients
cairnmind eval run --clients --skip-claude
cairnmind benchmark my-app "Where do we handle login errors?"
cairnmind brief my-app
```

A check runs in the current worktree. Exit code 0 confirms that the given command ran successfully; review and acceptance still require a separate check. Legacy test flags are `reported` declarations, never automatically created run results.

`acceptance_passed` records a documented assessment of the task's criteria; the field alone does not prove human acceptance. In the pilot, the same checkpoint may contain the review and the assessment of all criteria. The plan version also increases when task progress changes, so a review saved before the move to `done` may refer to an earlier plan version; test freshness is checked by `check_status`.

The code fingerprint is taken before every write that needs a current acknowledgement, for every question and around every check. It lists the worktree with Git and reads every listed file twice, so its cost grows with the size of the worktree: measured on a local Linux disk, 5,000 files of 4 KB take about 0.2 s and 5,000 files of 100 KB about 0.6 s. The content is always read on purpose. Remembering a file by its size and times would be faster but would miss an edit made through a memory mapping, which changes neither. On a Windows drive mounted in WSL every file operation is slower and the cost has not been measured; keep large generated files out of the worktree or in `.gitignore`.

The fingerprint also covers untracked files that are not ignored. Put generated caches and test output in `.gitignore` so that creating them during a check does not count as a change to the input code.

`eval run` exercises 20 scenarios against a real Ollama in a temporary repository and a separate ArcadeDB database. The optional `--clients` flag starts a fresh Codex, then Claude, then Codex again, using existing logins and consuming their quotas. A missing model, client or login yields `not_run`. The report gives accuracy, state errors, latency, the local model's context size, and token counts only when the provider actually reports them.

`--clients --skip-claude` checks only two fresh Codex sessions and does not start a Claude process. The report labels this as a partial test and does not count it as a full cross-client handoff. Codex tool approvals cover only the four operations of the `cairnmind_eval` server against the temporary database; the sandbox stays `read-only`.

`cairnmind doctor --contract --format text` prints the client compatibility checks as one line per client and check, with the output line that confirmed each one or, for a failed check, what was needed and what was found. A check that needs no output shows its requirement in parentheses. Each installed client ends with a `verified_resume` line; the command exits with 1 only when that line is `failed`. A client that is not installed gets a single `installed  missing` line and does not change the exit code. JSON stays the default.

`brief` prints where work stopped in at most 40 lines: the latest checkpoint and its age, the next task and blockers of the active plan, the check evidence of tasks in progress, commits dated after the checkpoint was saved, and proposed findings waiting for review. It is built only from stored data and Git, asks no model and does not start the service. Below a title and a one-line legend, each line is marked `[machine]` when Cairnmind measured it now, or `[reported]` when it repeats a stored claim that was not verified; a stored title on a `[machine]` line always comes last, after `|`. A checkpoint stores no commit, so commits are selected by date: commits pulled later but dated before the checkpoint are not listed. Tasks are not counted among the proposed findings. The same brief is in the command palette as **Brief**; its text is English in both interface languages. `--json` prints the brief as an object with `project`, `worktree`, `generated_at`, `lines` (each with `basis` and `text`) and `text`.

`usage PROJECT --since 7d` sums the tokens the built-in API agent used, per UTC day, profile and model, from the session journals of that project; `--since` takes `Nd` or `Nh`. `--by week` groups by ISO week in UTC (`2026-W46`, Monday to Sunday; the first days of January can belong to week 52 or 53 of the old year) instead of by day; the row limit, the lower bounds and the totals work the same. Only the last usage report of each turn counts, because a turn reports running totals. A counter nobody reported stays `unknown`. A turn is complete only if every model call it started reported both counts; a row or total that holds a turn which is not complete is marked `incomplete`, its sums are lower bounds and are printed as `at least N` (JSON: `"tokens_are_lower_bound": true`). A cost appears only for a row in which every turn was complete and was priced by the profile's rates when it ran; it is an estimate, not an invoice, and quota left in a subscription is never guessed or turned into money. The report says `PARTIAL REPORT` above the rows (JSON: `"partial": true`, totals not complete, cost unknown) when the run registry cannot be read (`data.registry` is `ok`, `missing` or `unreadable`) or when sessions were skipped because their project cannot be determined. The usage is read from the session journals and nothing is written to them or to the memory; the command asks no model and starts no service. It reads the project, its stored Codex records and the ingestion inbox from the store, which needs the database engine running; unlike `brief`, it never starts the engine (see [Data and backups](#data-and-backups)). Unreadable journal lines are skipped and counted. It covers the built-in API agent (client `api`) and Codex (client `codex`); Claude Code and Copilot sessions are not covered. Codex usage is read from the memory, not from rollout files: history import stores every rollout line as a source `transcript://codex/...` of the project it attributed the event to (by the event's working directory), and the `token_usage_record` lines of Codex 0.166 among them are the data. Attribution is import's own, and a rollout that was moved or deleted after import changes nothing. In such a record `usage` is one response and `thread_token_usage` the running sum of its thread, so the usage of the period is the sum of its responses, each counted once even if the rollout was imported from two paths; a response missing between two stored ones (the sum grew by more than the response says) or an unreadable record makes the rows incomplete. One Codex "turn" in the counts is one model response, and its profile and model are `unknown`. A cost is never computed for Codex (a subscription client): it is `unknown`, so the overall total has no cost when Codex rows exist and each client has its own total row. What the report does not see: rollouts that were never imported, and Codex usage records of the period still waiting in the ingestion inbox (older ones, and lines that only mention the name, do not count; only those of a working directory that belongs to this project or of none known); their number is shown and makes the report `PARTIAL` (`data.codex_waiting`). `--warn-over TOKENS` adds a first line `WARNING: ...` (JSON: a `warning` object with `threshold_tokens`, `observed_tokens` and `tokens_are_lower_bound`) when the input plus output tokens of the period, over all clients, are above `TOKENS`; the line says "at least" when the total is a lower bound. Without the option the threshold is `warn_over_tokens` under `[usage]` in `.cairnmind/checks.toml`, the file that holds the check profiles; that file is tracked in the repository, so adding or changing `[usage]` there makes existing check evidence stale like any other tracked change. If the default cannot be read (two checks files, a parse error, a file nested too deeply, a wrong `[usage]` value) the report is still printed, with the line `Warning threshold not read: <reason>` and the JSON key `warning_unavailable`; the option overrides the file and skips reading it. It is a warning only: nothing is blocked and the exit code does not change. `--json` prints the same data with the keys `project`, `since`, `timezone`, `from`, `to`, `partial`, `groups`, `totals`, `totals_by_client`, `data`, `cost_note`, `by` and `text`. Every row has a `period` (the day or the week) and a `client`; day rows also keep `day`, week rows have no `day`.

`benchmark` times the first answer to a question against the cached repeat. It measures latency only and does not replace a quality evaluation.

## Portable project memory

A full snapshot in `.memory/cairnmind/` carries one project's memory together with the repository: conversations and the exact source content, findings, plans, tasks, checkpoints, revisions and links. Saving and importing do not require Ollama. The memory panel offers preview, save, import check, import and auto-save settings; the same operations are available from the CLI. Merely detecting the files loads nothing.

```bash
cairnmind memory snapshot my-app --preview
cairnmind memory snapshot my-app
cairnmind memory auto my-app --enable --interval-minutes 15
cairnmind memory status my-app
cairnmind memory auto my-app --disable
```

Auto-save is off by default. When enabled, it saves changed memory while the service is running, including after the terminal UI is closed. It keeps the five most recent snapshots and shares identical objects between them. It does not run models, make commits or push anything to GitHub. Full conversations may contain private information: review the export before adding `.memory/cairnmind/` to the repository. Machine configuration, authentication files and configured keys are not exported; the content of saved conversations is not automatically anonymized.

On a second machine, after installing Cairnmind and cloning the repository:

```bash
cairnmind memory inspect ~/src/my-app/.memory/cairnmind
cairnmind memory import ~/src/my-app/.memory/cairnmind --root ~/src/my-app --dry-run
cairnmind memory import ~/src/my-app/.memory/cairnmind --root ~/src/my-app
```

To read a snapshot without importing it, use `memory show`. It needs no Java, engine, model or service, and writes nothing. Inside a Cairnmind coding session the running service first confirms that the package belongs to the session's project, as it does for `memory inspect`:

```bash
cairnmind memory show ~/src/my-app/.memory/cairnmind
cairnmind memory show ~/src/my-app/.memory/cairnmind --kind decision --grep login
cairnmind memory show ~/src/my-app/.memory/cairnmind --kind decision --sources
cairnmind memory show ~/src/my-app/.memory/cairnmind --kind plan
```

It validates the package exactly as `memory inspect` does, then prints the snapshot header and the matching records with their status, version, time of last change and source addresses, and plans with their steps and source addresses. `--kind` takes a record kind or `plan`, `--status` a record status, and `--grep` a case-insensitive text. Check runs stored in the snapshot are printed as historical. A task also lists its acceptance criteria, each with the check it is linked to or "no check", and a record that replaced another, or was replaced, names it (`replaces:`, `replaced by:`). `--sources` also prints the text of each record's sources under it, up to 1,200 characters per source and 60,000 in all with a note of what was left out; a source whose text is empty or binary is named and not printed, and a source that supports several records is printed under each. Source text is untrusted, so control characters and terminal escapes are printed as visible escapes. `--grep` also searches the acceptance criteria and check names it prints for tasks, and with `--sources` the source text shown, and only that.

Each record also has an evidence state, derived whenever it is read and never stored. `anchored` means that at least one supporting source still stands: a file version that is current, or whose identical content is current at the same or, after a move, another path. `evidence_replaced` means that no supporting version stands and at least one supporting file has different content now; when the record carries a quoted passage that occurs in the cited version, Cairnmind also says whether the passage still occurs in the changed file. `orphaned` means that no supporting source has a current version, for example because the only supporting file was deleted. `unverified` means that the record rests on file snapshots imported from another machine and the local files have not been indexed yet. Notes, conversations, plan snapshots and check outputs are statements made once and always stand. The state is as fresh as the last indexing of the project's files. The memory inspector shows it. When a resume packet is prepared, it warns about orphaned and changed records; the warning is advice, so it does not block the resume, and a later change of evidence alone does not invalidate an acknowledged packet. This command lists the records:

```bash
cairnmind memory orphans my-app
```

A plan can say which check proves each acceptance criterion. Write a criterion as text, or as `{"text": ..., "check": "unit"}` to link it to a check profile; add `"argv": [...]` to propose the command of a profile that is not declared yet. Then:

```bash
cairnmind plan contract my-app
cairnmind plan contract my-app --write 3f9a1c0b7d2e
```

The first command lists every criterion of the active plan with how it stands (`machine_fresh` when a passed run of its check matches the current code, `machine_stale`, `machine_missing`, `machine_unknown`, or `manual_pending` and `manual_reported` for a criterion with no check) and shows, as a diff, the profiles it would add to `.cairnmind/checks.toml` from the proposed commands. To add them, repeat the command with `--write CODE`, where the code is printed under the diff and names exactly the proposal you looked at; if the plan or the file changed since, the write is refused. Adding profiles changes the worktree, so check runs made before count as stale. Neither form runs anything: a proposed command is text until you run the check yourself, and a profile that is already declared is never changed. `check_status` and `cairnmind plan next` report the same per-criterion list.

A plan step can also say which files it means to touch: `"scope": ["src/auth/**", "docs/login.md"]`, path patterns as in an ignore file. When the task enters `in_progress`, Cairnmind notes the state of every file of the worktree. From then on `cairnmind plan next`, `cairnmind plan contract`, `cairnmind brief`, `check_status` and `next_task` warn when a file that matches no pattern has changed since, and name it. It is a warning and never a block: a task can still be completed. When the task is done the comparison is made one last time and kept, so what other tasks change later is not counted against it.

What is compared is the content of the worktree, not Git's index: staging or committing a file changes nothing. A file that was already modified when the task started is not reported. Files that Git ignores are not seen, and neither are the files Cairnmind writes itself (`.cairnmind/`, `.mcp.json`, `AGENTS.md`, `CLAUDE.md`, the client configuration it installs). A task that has not started answers `not_started`; a task that started on another machine or in another worktree answers `unknown`, never "in scope", and a task that was finished elsewhere answers `not_recorded`. A task taken up again after it was done starts from a new note. Declaring a scope on a task that is already being worked on notes the tree at that moment. Unchanged files are recognised by size, times and inode without being read, so a change that leaves all of these untouched is not seen; a check run always reads every file. The note stays in the local data directory: it is in no snapshot and no backup.

After a code change, `cairnmind impact` lists the records that are linked to what changed since a commit, with the reason for each link:

```bash
cairnmind impact my-app --since HEAD~3
```

It compares the worktree with that commit, including uncommitted edits and files that Git does not track yet (ignored files and nested repositories are left out; a rename made without `git mv` or `git add` shows as a deleted and an added file). It follows two kinds of stored links: a record whose evidence names a changed file or a function, class, method, module constant or class field whose definition or decorators changed, and a record that cites an earlier snapshot of a changed file as its evidence. A link means "worth checking", not that the record is wrong. The command asks no model, starts no service and writes nothing, so repeating it duplicates nothing and it never changes a task's state. Which record is linked to what comes from the index, so index the project first if files changed since the last indexing. `--json` prints the same list with the changed files and symbols.

You can give each listed record a verdict: still valid, needs update (with a note on what to update) or not related. `cairnmind impact-verdict my-app RECORD_ID --fingerprint F --verdict still_valid --since HEAD~3` records it, with `F` the entry's `fingerprint` from `--json`; the memory service takes the same verdict at `POST /inspector/impact/verdict`. In the workspace, the **Impact** view of the memory inspector (`Ctrl+B i`) shows the list for the open project, compared with `HEAD` or a commit you type, with a filter for entries without a verdict, and **Verdict** saves one without the command line (see [docs/TERMINAL.md](docs/TERMINAL.md)); it reads the list from `GET /inspector/impact`, which takes the parameters of `impact` and answers what `--json` prints. A verdict is a person's note: a coding session cannot give one, and it never changes the record, its status, its revisions or a task's state. It is stored as an audit note (`review://impact/…`) that never counts as evidence and is never searched or quoted in answers. A later `impact` shows `checked: still valid (date)` under the entry instead of asking again, and `--pending` lists only the entries without a verdict. A verdict holds for the entry as it was listed: the record's text, its links to the changed code and the current content of that code. Editing the linked definition (or file) again, or the record's content, makes the entry unreviewed; so does a comparison with another commit that links the record to other changes, because that list asks a different question. Saving the same verdict again stores nothing; a different one replaces it and the earlier one is kept as history. A verdict is saved with the project memory, like a review note: it is a local change of the memory, so the next `memory snapshot` (or auto-save) writes a new package with it, and a newer package from another machine can be imported only after that.

The report also lists documentation pages that mention a changed definition, even when no record points at them: for `refresh_token()` changed, a page `docs/login.md` that says "renewed by refresh_token" is named, with the line. Documentation files are those ending in `.md`, `.mdx`, `.rst` or `.txt` that Git tracks or that are untracked and not ignored; `.memory/`, `.cairnmind/`, `.devbrain/`, `.git/` and the directories the indexer skips (`node_modules/`, `vendor/`, `.claude/`, ...) are never read, and neither are secret-looking files, links, files outside the worktree, files over 1 MB or files that are not UTF-8; the report counts the files it did not read. The names come from the parser, so only a definition the parser knows (in Python, JavaScript and TypeScript files: functions, classes and methods; in Python also module constants and class fields, an assignment to one name directly in a module or a class body; in JavaScript and TypeScript also interfaces, type aliases, enums and variables holding a function) whose text changed or that was removed is looked up; a class field is named `Class.name` and is therefore not looked up in pages: added and moved definitions are not, and a changed file that cannot be compared contributes no names. A name must stand alone as an identifier (`refresh_token_v2` is not a mention of `refresh_token`). To keep the list worth reading, a name must be at least 4 characters long, even when written as code (dunder names such as `__init__`, `constructor` and `#private` members are skipped), and it must either contain an underscore or a capital letter after the first (`refresh_token`, `SessionStore`) or be written as code: after an odd number of backtick runs on the same line (a code span that continues over several lines is not recognised), in a fenced block of a Markdown file (the fence must start a line after at most three spaces; a fence indented further or inside a quote counts as text), or directly followed by `(`. So `login` in a sentence is not listed, while `` `login` `` and `login()` are. At most 50 pages are shown and at most 3 line numbers per page and name, and a run reads at most 2,000 files, stops reading once 20 MB have been read, and looks up at most 500 names; the report says how much was left out. A page that was itself changed in the same range is listed too and marked. As with records, a mention means "worth checking", not that the page is wrong. In the JSON, `documentation` is an object: `documents` holds the pages, and counters (`omitted_documents`, `omitted_lines`, `skipped_documents`, `not_scanned`, `names_not_searched`, `unavailable`) say what was left out or could not be done.

When extraction finds a fact or a decision under a key that is already taken, it stores the new finding beside the old one and marks the pair as a possible conflict. The **Conflicts** view of the memory inspector shows each pending pair with both texts, their evidence times, scope and sources, and says which finding has the newer evidence and whether that order is reliable. You decide: one finding replaces the other and becomes confirmed, both stay because they hold in different scopes, or the alarm is dismissed. A fact and a decision cannot replace each other, so such a pair can only be kept or dismissed. When a marked finding has been replaced in the meantime, the pair is shown against the finding that replaced it. Each decision is saved with an audit note and a new revision, and the scopes you describe are kept on the finding; no source is removed. A model never resolves a pair: the fields that record a decision are ignored when any other writer sends them.

Import keeps the project's identity and does not replace other projects in the database. Re-importing the same state duplicates nothing; diverging changes require conflict resolution. Search indexes are rebuilt locally. Historical `done` states are preserved, but checks carried over from another machine are not local `fresh` evidence. Logins, client configuration and new sessions are set up locally. See [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md) for the format contract, limits and restore rules.

```bash
cairnmind eval memory
```

This is an explicit test against a real Ollama: extraction of a fact, a decision and a task, transfer between two isolated databases, and reconstruction of a sourced answer. It requires all expected findings; an empty extraction result fails the scenario. It does not start Codex or Claude. Exit codes: `0` success, `1` failure, `3` required models missing.

## Data and backups

The default data directory is `~/.local/share/cairnmind` for a new installation. An installation that already has data in `~/.local/share/devbrain` keeps using that directory where it is; nothing is moved or copied. Choose another one with `CAIRNMIND_HOME` or the global `--data-dir` option. Variables are spelled `CAIRNMIND_*`; the earlier `DEVBRAIN_*` spelling is still read. For a configuration variable the new spelling wins when both are set; `cairnmind doctor` reports a pair that disagrees. ArcadeDB data lives in `arcade/databases`. The knowledge service and the engine run in the background for as long as WSL is running. `cairnmind stop` stops the knowledge service; once clients are closed, `cairnmind engine stop` stops the database without deleting data.

Commands that only read the store never start the engine, create the data directory, configuration or database, or upgrade the schema: `project list`, `records`, `source`, `task list`, `plan list`, `plan show`, `plan next`, `plan contract` without `--write`, `check profiles`, `memory orphans`, `impact` and `usage`. When the engine is not running, the store was never created or its schema needs an upgrade, they stop at once with one line that says what to run, `cairnmind engine start` or `cairnmind init`. `brief` still starts a stopped engine, as do the commands that write and `check list`, `export` and `backup`.

```bash
cairnmind export ~/memory.json --project my-app
cairnmind export ~/memory.md --project my-app --markdown
cairnmind backup ~/cairnmind-backup.cairnmind.json.gz
```

The ArcadeDB schema stays at version 4, the profile format at 2, and the administrative backup at 5, with support for the earlier formats 2–4. Old plan groups can be adopted explicitly with `plan adopt`. To move a single project, use `memory import`; `restore` replaces the entire store.

Archiving does not wait for the GPU. Status shows archiving, indexing, model processing and the queue separately. The portable backup contains a consistent export of sources and database state; it does not contain credentials or model weights. Automatic backup runs while the service is running and keeps the seven most recent copies.

## Limits

Tests for storage, full-text search, the graph and the vector index use a real ArcadeDB engine. They require Java 21; the default test session prepares an isolated engine. Automated tests of Ollama answers and client sessions use controlled adapters; a real handoff of work between clients has to be measured in the target environment.

Syntax analysis covers Python, JavaScript and TypeScript. Other languages remain searchable as text. Static analysis does not guarantee a complete graph of dynamic calls. The scanner indexes UTF-8 text files up to 2 MiB, respects `.gitignore` and skips, among others, dependency directories and `.env`/key files. History import preserves the original source content.

`ask` answers and the startup context still consume Codex or Claude tokens. Local memory reduces the need to carry the whole history along; it does not remove context limits or the cost of model work. API and Copilot connections use their own account billing and permissions.

The launcher blocks a client from starting when the resume package is partial or unavailable. An ACK confirms receipt and reading of specific versions, not the model's understanding. Cairnmind's write controls do not replace the native tool permissions of Codex and Claude.

An external MCP session (Cursor, Cline, Zed) has no archived conversation to quote or verify against. Its packet says `history: unarchived` and `evidence: degraded`, and what it writes is never verified. Nothing stops a person from configuring Codex or Claude Code as an external client; such a session is then marked external and degraded like any other. The mark is applied where the service receives a session's write: a `cairnmind` command that the editor's agent runs in its shell carries no session, so what it writes is stored as a person's, unmarked and without the ACK gate. This holds for any process without a session and cannot be told apart; keep an external client's writes to its MCP tools.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md): step-by-step installation, verification, uninstalling and troubleshooting.
- [docs/TERMINAL.md](docs/TERMINAL.md): the terminal workspace, layout, navigation and diagnostics.
- [docs/API.md](docs/API.md): API, Ollama and Copilot connections and permissions.
- [docs/WORKFLOW.md](docs/WORKFLOW.md): plan format, check profiles, evidence states and MCP calls.
- [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md): the portable memory format and import rules.
- [docs/EVALUATION.md](docs/EVALUATION.md): the explicit evaluations and the 0.8.0 results.
- [docs/UPGRADING.md](docs/UPGRADING.md): upgrading an existing installation.
- [docs/CONTRACTS.md](docs/CONTRACTS.md): implementation contracts for the backend and its components, and the [extension surfaces](docs/CONTRACTS.md#extension-surfaces): what can be added (checks, reactions to events, MCP clients, model connections), what is planned, and why there are no plugins.
- [CHANGELOG.md](CHANGELOG.md): release history.

## Repository layout

| Path | Contents |
|---|---|
| [`src/cairnmind/`](src/cairnmind/) | The Python package. `src/devbrain/` is an import alias of the earlier module name, kept for one release so that entries starting `python -m devbrain` keep working until `cairnmind install` rewrites them. |
| [`tests/`](tests/) | Automated tests; see [CONTRIBUTING.md](CONTRIBUTING.md) for the tiers and a code map. |
| [`docs/`](docs/) | Guides and implementation contracts. |
| [`scripts/`](scripts/) | The installer. |
| [`pilot/`](pilot/) | Material for a hand-off pilot between coding assistants. |

## Development

```bash
uv sync --locked --extra dev
uv run ruff check src tests
uv run pytest -m "not arcade and not tmux"   # fast tier, no database or tmux
uv run pytest                                # full suite, needs Java 21 and tmux
uv run pytest -n 4                           # the full suite in 4 parallel processes
```

The automated tests need no Ollama, no models and no signed-in coding clients; those are used only by the explicit evaluations. Test modules that need the real ArcadeDB engine are marked `arcade`, and those that drive a real tmux server are marked `tmux`. CI runs lint, the fast tier and the full suite on every pull request and on every push to `main`.

## Status and roadmap

Version 0.9.0 is the current release and the first under the name Cairnmind; see the [changelog](CHANGELOG.md). New installations use the installer directly. Upgrading from DevBrain 0.8.0 is a backup, the installer and one `cairnmind install` per project; an existing 0.7.2 installation first goes to 0.8.0 through a guarded updater that requires a passed runtime qualification of the host before it touches existing data. Both are in [docs/UPGRADING.md](docs/UPGRADING.md).

Planned work is tracked in [issues](https://github.com/piotrwozn/cairnmind/issues?q=is%3Aissue+label%3Aroadmap).

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first, and report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## License

Cairnmind is licensed under the [Apache License 2.0](LICENSE). ArcadeDB, which Cairnmind downloads and manages, is licensed separately under the same license; see [NOTICE](NOTICE).
