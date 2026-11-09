# DevBrain

DevBrain is a local development environment for **Codex CLI, Claude Code, Ollama, the OpenAI/Anthropic APIs and GitHub Copilot**, with a separate knowledge agent. A single `devbrain` command opens the project list in the current terminal. You pick a project and an assistant, then explicitly start work. Shared project memory answers `ask` questions: what was decided, where the code lives, what it depends on and what needs to happen next. Project state is stored outside model conversations, so it survives across sessions and clients.

## Highlights

- **Focus view by default.** Version 0.8.0 shows one full-width view (**Focus**) and uses English by default. **More → Settings → Language → Polski** switches the interface to Polish. The multi-pane **Cockpit** layout remains available as an option.
- **Command palette.** `Ctrl+B`, then `Space`, opens a searchable command menu; `Ctrl+B`, then `?`, shows keyboard shortcuts.
- **Explicit start.** `Enter` on a project opens its overview. Only **Start work** launches the one assistant you selected.
- **Connections in one place.** **Connections** groups accounts, models and keys; **More** contains sessions, snapshots and settings.
- **Ask memory.** Ask the project memory a question from the command palette and get cited sources, missing evidence and citation coverage, without writing anything.
- **Hybrid RAG and GraphRAG.** Full-text search, vector search and a source-derived dependency graph, combined with a bounded agent loop.
- **Verified session resume.** Launchers wait for a complete resume package, and writes from a linked session require a real model ACK.
- **Atomic plans and named checks.** Plans are saved in one transaction; named test profiles bind results to the task and to a fingerprint of the current code.
- **Portable memory.** A full project snapshot in `.memory/devbrain/` travels with the repository and can be imported on another machine.

## Architecture

```text
Terminal: tmux + Textual ──► Codex CLI / Claude Code / API agent / Copilot SDK
                           │ MCP: resume / ACK / ask / plans
                           ▼
                 Local DevBrain service
                  │                   │
       Ollama knowledge agent    independent archiving
                  │                   │
          Hybrid RAG + GraphRAG ◄─────┘
                  │
       ArcadeDB: sources, vectors, graph, decisions, tasks
```

- **Knowledge agent:** `qwen3.5:9b` via Ollama, with a 16k-token working context. The coding model of each new profile is chosen independently.
- **Embeddings:** `qwen3-embedding:0.6b`.
- **Search:** ArcadeDB's native full-text index + `LSM_VECTOR`/HNSW + graph traversal.
- **Memory:** versioned records and independent source snapshots, including uncommitted code.
- **Queue:** shared across clients; questions take priority over pending extraction and embedding work.
- **Plans:** the whole plan and its dependencies are saved atomically, with stable step identifiers and a deterministic next step per project/worktree.
- **Sessions:** the launcher waits for a ready resume package; writes from a linked session require a real model ACK that names the identifiers and versions it read.
- **Verification:** named test profiles bind a result to the task definition, the profile and a fingerprint of the current code. Progress (`done`) and evidence freshness are tracked separately.

The way memory and agents are organized is inspired by SynapCores. DevBrain is an independent implementation of the knowledge layer; it is not a copy of that product or of its database engine.

The only data engine is **[ArcadeDB](https://github.com/ArcadeData/arcadedb), Apache 2.0**. DevBrain manages the pinned version `26.11.1` as a local Java process. Documents, the graph, full-text search and the vector index all live in the same engine.

## Requirements

- Windows with WSL2 Ubuntu.
- **Java 21+** and **tmux 3.4+** inside WSL.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Ollama](https://docs.ollama.com/linux) in the same WSL distribution.
- The clients you plan to use, in the same WSL distribution. [Codex CLI](https://developers.openai.com/codex/cli) and [Claude Code](https://code.claude.com/docs/en/setup) are required only for their native profiles.
- Docker is not needed. The terminal package does not require Node.js or a frontend build.

The reference setup uses an RTX 4080 Super with 16 GB of VRAM; check actual memory use and latency on your own machine after installation.

## Installation

The complete walkthrough, from a clean Windows machine to the first project, is in **[docs/INSTALL.md](docs/INSTALL.md)**. In short:

1. On Windows, install a current NVIDIA driver and WSL2. In Ubuntu, check `nvidia-smi`.
2. In WSL, install Java 21 and tmux (`sudo apt install openjdk-21-jre-headless tmux`), [uv](https://docs.astral.sh/uv/getting-started/installation/), [Ollama](https://docs.ollama.com/linux), [Codex CLI](https://developers.openai.com/codex/cli) and [Claude Code](https://code.claude.com/docs/en/setup). Log in to both CLIs using their own mechanisms.
3. Copy the project directory into WSL, for example `~/src/devbrain`, and run:

```bash
cd ~/src/devbrain
bash scripts/install-wsl.sh
```

The installer creates an isolated tool installation via `uv tool`, downloads the pinned ArcadeDB (about 86 MB, verified with SHA-256), starts the engine and initializes the database. It does not download models or the Copilot runtime, and it does not change Codex or Claude configuration. The Copilot runtime is prepared by a separate action in the terminal UI. If the `devbrain` command is not yet on your `PATH`, add the directory the installer prints.

For a manual installation, after `uv tool install --python 3.12 ~/src/devbrain` run:

```bash
devbrain engine install
devbrain init
devbrain engine status
```

Start Ollama in a separate terminal if it is not running as a service:

```bash
ollama serve
```

Then pull the models. This is an explicit step that downloads several GB:

```bash
devbrain models pull
devbrain doctor
```

## First project

```bash
devbrain project add my-app ~/src/my-app
devbrain install my-app
devbrain
```

`devbrain` works from any directory and uses a single project registry in the local DevBrain store. You can also add a project with **Add folder**: type a Linux or Windows path, or pick a directory with the keyboard. The name is optional. `Enter` on the list opens the project for inspection. On the project screen, choose a named assistant and use **Start work**. **Add assistant** adds another connection to the project and keeps existing assignments.

In **Connections**, add a connection, log in to an account or set a key, choose a model and optionally run an explicit test. Copilot uses your GitHub account and needs no API key of its own. Limits and credential references are under **Advanced limits and credentials**. Saving a connection does not generate a response. Opening the environment attaches to existing processes; it does not start every assigned assistant. See [docs/API.md](docs/API.md) for the full API guide.

## Everyday use

### Workspace

The full Cockpit layout needs at least **140 columns × 42 rows**; **180 × 55** is recommended. In a smaller terminal the active pane is enlarged and hidden processes keep running. `Ctrl+B`, then `p`, `1`, `2`, `i` or `g`, selects projects, clients, the inspector or the graph; `z` zooms the pane and `d` detaches the environment. Pressing `Ctrl+B` twice passes the shortcut through to the native client. Without an interactive terminal, DevBrain prints instructions instead of opening a browser or initializing services.

```bash
devbrain workspace status
devbrain workspace detach
devbrain workspace takeover
devbrain workspace stop
devbrain workspace stop --clients
```

Detaching keeps the environment and its processes. `workspace stop` closes the outer layout; `--clients` also explicitly stops the clients adopted by this workspace. Other launches stay independent. Shutting down WSL ends the processes, but saved memory remains available for an explicit resume.

One terminal controls the workspace. A second terminal opens a read-only view; `devbrain workspace takeover` explicitly takes control and switches the previous terminal to view mode, without starting new clients.

The launcher and `devbrain run` refuse to start a client that lacks verified support for MCP, hooks, package handoff and session binding. `devbrain doctor` shows the detection result. A project with existing code needs a Git repository and a first commit; the exception for an empty project explicitly marks that no code has been verified.

### Without the launcher

```bash
devbrain run my-app codex
devbrain run my-app claude
devbrain ask my-app "Why did we change login, and what depends on it?"
devbrain context my-app
devbrain status --project my-app
```

`ask` returns an answer, its sources, knowledge gaps and freshness. When the model is unavailable it reports `unavailable`; it does not pretend that raw search results are an agent answer.

## Plans, tasks and evidence

Saving a plan writes the header, the steps and the final dependency graph in a single transaction. Codex and Claude see the same versions. An active plan is bound to a specific registered worktree. The launcher shows plans, the next step, blockers, evidence freshness and session package readiness.

```bash
devbrain plan save my-app ./plan.json --idempotency-key login-plan-1 --activate
devbrain plan next my-app
devbrain plan list my-app
devbrain task list my-app
```

Progress states are `todo`, `in_progress`, `needs_test`, `needs_review`, `done` and `blocked`. A separate `proposed`/`confirmed` status describes how certain a finding is. A `done` result remains part of the execution history; a later code change can turn its verification to `stale`. The plan format, check profiles and MCP calls are described in [docs/WORKFLOW.md](docs/WORKFLOW.md).

Declare check profiles in `.devbrain/checks.toml`, for example `[checks.unit]` with `argv = ["uv", "run", "pytest"]`. Running a check is an explicit CLI operation:

```bash
devbrain check run my-app unit --task TASK_ID
devbrain check list my-app --task TASK_ID
devbrain eval run
devbrain eval run --clients
devbrain eval run --clients --skip-claude
```

A check runs in the current worktree. Exit code 0 confirms that the given command ran successfully; review and acceptance still require a separate check. Legacy test flags are `reported` declarations, never automatically created run results.

`acceptance_passed` records a documented assessment of the task's criteria; the field alone does not prove human acceptance. In the pilot, the same checkpoint may contain the review and the assessment of all criteria. The plan version also increases when task progress changes, so a review saved before the move to `done` may refer to an earlier plan version; test freshness is checked by `check_status`.

The fingerprint also covers untracked files that are not ignored. Put generated caches and test output in `.gitignore` so that creating them during a check does not count as a change to the input code.

`eval run` exercises 20 scenarios against a real Ollama in a temporary repository and a separate ArcadeDB database. The optional `--clients` flag starts a fresh Codex, then Claude, then Codex again, using existing logins and consuming their quotas. A missing model, client or login yields `not_run`. The report gives accuracy, state errors, latency, the local model's context size, and token counts only when the provider actually reports them.

`--clients --skip-claude` checks only two fresh Codex sessions and does not start a Claude process. The report labels this as a partial test and does not count it as a full cross-client handoff. Codex tool approvals cover only the four operations of the `devbrain_eval` server against the temporary database; the sandbox stays `read-only`.

## Portable project memory

A full snapshot in `.memory/devbrain/` carries one project's memory together with the repository: conversations and the exact source content, findings, plans, tasks, checkpoints, revisions and links. Saving and importing do not require Ollama. The memory panel offers preview, save, import check, import and auto-save settings; the same operations are available from the CLI. Merely detecting the files loads nothing.

```bash
devbrain memory snapshot my-app --preview
devbrain memory snapshot my-app
devbrain memory auto my-app --enable --interval-minutes 15
devbrain memory status my-app
devbrain memory auto my-app --disable
```

Auto-save is off by default. When enabled, it saves changed memory while the service is running, including after the terminal UI is closed. It keeps the five most recent snapshots and shares identical objects between them. It does not run models, make commits or push anything to GitHub. Full conversations may contain private information: review the export before adding `.memory/devbrain/` to the repository. Machine configuration, authentication files and configured keys are not exported; the content of saved conversations is not automatically anonymized.

On a second machine, after installing DevBrain and cloning the repository:

```bash
devbrain memory inspect ~/src/my-app/.memory/devbrain
devbrain memory import ~/src/my-app/.memory/devbrain --root ~/src/my-app --dry-run
devbrain memory import ~/src/my-app/.memory/devbrain --root ~/src/my-app
```

Import keeps the project's identity and does not replace other projects in the database. Re-importing the same state duplicates nothing; diverging changes require conflict resolution. Search indexes are rebuilt locally. Historical `done` states are preserved, but checks carried over from another machine are not local `fresh` evidence. Logins, client configuration and new sessions are set up locally. See [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md) for the format contract, limits and restore rules.

```bash
devbrain eval memory
```

This is an explicit test against a real Ollama: extraction of a fact, a decision and a task, transfer between two isolated databases, and reconstruction of a sourced answer. It requires all expected findings; an empty extraction result fails the scenario. It does not start Codex or Claude. Exit codes: `0` success, `1` failure, `3` required models missing.

## Data and backups

The default data directory is `~/.local/share/devbrain`; change it with `DEVBRAIN_HOME` or the global `--data-dir` option. ArcadeDB data lives in `arcade/databases`. The knowledge service and the engine run in the background for as long as WSL is running. `devbrain stop` stops the knowledge service; once clients are closed, `devbrain engine stop` stops the database without deleting data.

```bash
devbrain export ~/memory.json --project my-app
devbrain export ~/memory.md --project my-app --markdown
devbrain backup ~/devbrain-backup.devbrain.json.gz
```

The ArcadeDB schema stays at version 4, the profile format at 2, and the administrative backup at 5, with support for the earlier formats 2–4. Old plan groups can be adopted explicitly with `plan adopt`. To move a single project, use `memory import`; `restore` replaces the entire store.

Archiving does not wait for the GPU. Status shows archiving, indexing, model processing and the queue separately. The portable backup contains a consistent export of sources and database state; it does not contain credentials or model weights. Automatic backup runs while the service is running and keeps the seven most recent copies.

## Verification and limits

```bash
uv sync --locked --extra dev
uv run pytest
uv run ruff check src tests
devbrain benchmark my-app "Where do we handle login errors?"
```

Tests for storage, full-text search, the graph and the vector index use a real ArcadeDB engine. They require Java 21; the default test session prepares an isolated engine. Automated tests of Ollama answers and client sessions use controlled adapters; a real handoff of work between clients has to be measured in the target environment.

Syntax analysis covers Python, JavaScript and TypeScript. Other languages remain searchable as text. Static analysis does not guarantee a complete graph of dynamic calls. The scanner indexes UTF-8 text files up to 2 MiB, respects `.gitignore` and skips, among others, dependency directories and `.env`/key files. History import preserves the original source content.

`ask` answers and the startup context still consume Codex or Claude tokens. Local memory reduces the need to carry the whole history along; it does not remove context limits or the cost of model work. API and Copilot connections use their own account billing and permissions.

The launcher blocks a client from starting when the resume package is partial or unavailable. An ACK confirms receipt and reading of specific versions, not the model's understanding. DevBrain's write controls do not replace the native tool permissions of Codex and Claude.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md): step-by-step installation, verification, uninstalling and troubleshooting.
- [docs/TERMINAL.md](docs/TERMINAL.md): the terminal environment, layout, navigation and diagnostics.
- [docs/API.md](docs/API.md): API, Ollama and Copilot connections and permissions.
- [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md): the portable memory format and import rules.
- [docs/WORKFLOW.md](docs/WORKFLOW.md): plan format, check profiles, evidence states and MCP calls.
- [docs/EVALUATION.md](docs/EVALUATION.md): the explicit evaluations and the 0.8.0 results.
- [docs/UPGRADING.md](docs/UPGRADING.md): upgrading an existing 0.7.2 installation.
- [CONTRACTS.md](CONTRACTS.md): implementation contracts (ArcadeDB backend, components, data access).
- [CHANGELOG.md](CHANGELOG.md): release history.
