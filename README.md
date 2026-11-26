# Cairnmind

[![CI](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml/badge.svg)](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fpiotrwozn%2Fcairnmind%2Fbadges%2Fcoverage.json)](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)

**Evidence-grounded project memory for coding agents, in your terminal.**

*A cairn is the pile of stones a walker leaves to mark the trail for the next one. Cairnmind is the memory one coding agent leaves for the next. Formerly DevBrain.*

Cairnmind is a local workspace for Codex CLI, Claude Code, GitHub Copilot and API-based coding agents (OpenAI, Anthropic, OpenAI-compatible endpoints and local Ollama). It keeps a durable, versioned memory of each project outside any single conversation, so the next session, or a different assistant, can pick up exactly where the last one stopped. It is not a plugin for one assistant and not a notes store that a model writes into: it is a separate local service that assistants connect to, and it does not treat a model's statement as evidence.

## Quick start

Cairnmind runs on Linux, on Windows (natively on Windows 10/11 or inside WSL2 Ubuntu via `wsl --install -d Ubuntu`; Ubuntu 24.04 is the tested platform) and on macOS with Apple silicon (tested on an M3 Pro with macOS 27; installed with Homebrew).

**What you need**

- git and [uv](https://docs.astral.sh/uv/), which brings Python 3.12: on Ubuntu `sudo apt install -y git curl` and `curl -LsSf https://astral.sh/uv/install.sh | sh`, then open a new terminal; on macOS `brew install uv`; on native Windows `winget install Git.Git astral-sh.uv`.
- One way to run the database engine. **Java 21** is the simplest: `sudo apt install -y openjdk-21-jre-headless` (macOS: `brew install openjdk@21`; native Windows: `winget install EclipseAdoptium.Temurin.21.JDK`). Or **Docker** (Docker Desktop on Windows), running on this machine; the engine then runs in a container (not tested on macOS).

Nothing else: no Ollama, no models, no GPU, no tmux and no coding assistant are needed for the steps below. Each adds something later.

**What gets installed:** the `cairnmind` command; a small local service that starts by itself when a command needs it and keeps running in the background (`cairnmind stop` stops it); and ArcadeDB, the database your project memory is kept in (about 86 MB, checked with SHA-256; with Docker instead, an engine image of about 640 MB on disk, pinned by its SHA-256 digest). Your memory and a native engine are kept in `~/.local/share/cairnmind` on Linux/macOS/WSL2 (or `%LOCALAPPDATA%\cairnmind` on native Windows); the command is a uv tool, and a Docker engine's image lives in Docker's storage. `cairnmind stop` stops the service and `cairnmind engine stop` the engine, which also keeps running in the background once it has started.

```bash
git clone https://github.com/piotrwozn/cairnmind.git ~/src/cairnmind
cd ~/src/cairnmind
bash scripts/install-wsl.sh                    # Linux / macOS / WSL2 (on native Windows: pwsh -File scripts/install-windows.ps1)
cairnmind project add my-app ~/src/my-app      # 2. registers your project's folder and indexes its files
cairnmind remember my-app "Accounts lock after five failed logins."   # 3. stores a first finding, as proposed
cairnmind brief my-app                         # 4. where work stands: your finding is listed, marked as unconfirmed
cairnmind ask my-app "When do accounts lock?"  # 5. finds it again; passages of your files that use the question's words are searched too
```

These commands say in a terminal what they did and what comes next, and a command that stops says what is missing and what to do. With `--json`, or in a pipe, a file or a script, `doctor`, `project add`, `project list`, `remember`, `ask`, `install`, `init`, `stop` and `engine stop` print the full JSON, as scripts expect; `context` and the record commands print JSON always. `cairnmind doctor` repeats the installer's check at any time; it exits 1 when something required is missing.

- If the installer prints `Add this directory to PATH`, do what that line says and open a new terminal.
- `my-app` is any name you choose; `~/src/my-app` stands for the folder of a project you already have. A Git repository with at least one commit is best: `impact`, coding sessions and the commit list of `brief` need Git, and `project add` says when the folder has no repository or no commit.
- `project add` indexes the files in its own process and starts no service. The extraction and embedding jobs it queues are run by the knowledge service: when it is stopped, `project add` says how many jobs wait (its JSON has `jobs`: `queued`, `service_running`, `waiting_because`), and `cairnmind index my-app` starts it and they run. Without models, while an upgrade is in progress, or while a change of setup from the workspace holds the data directory (`waiting_because`: `setup_in_progress`), it says why they wait. `waiting_because: null` means not known to wait: a running service can still hold them, for example one the workspace started or one that started before models were turned on.
- What you and your assistants store is *proposed*, and nothing more is needed to go on: `brief`, `ask` and the assistants already use it, marked as unconfirmed. You can confirm or dispute it later in the memory inspector of the terminal workspace (below; it needs tmux 3.4+).
- `ask` finds passages of your files by the words of the question (`accounts lock` does not find `locked`), and, once models are on, by meaning too, which needs vectors; right after `project add` they are still being built and `ask` says so. An answer cites only what it relies on, so the first one may cite your note alone.
- Without models, `ask` makes no answer: in a terminal it says so and lists the passages that match words of the question as search results, not as evidence; its JSON reports `status: unavailable`. `cairnmind models pull` turns answers on (below).

### Connect Codex or Claude Code

With [Codex CLI](https://developers.openai.com/codex/cli) or [Claude Code](https://code.claude.com/docs/en/setup) installed and signed in, in the same system as Cairnmind:

```bash
cairnmind install my-app          # connects the installed clients to this project
cairnmind run my-app claude       # or codex: starts the client bound to this project's memory
```

`cairnmind install` writes into your project, keeping what is there and saying what it changed: for each installed client only, a marked Cairnmind block (`AGENTS.md` for Codex, `CLAUDE.md` for Claude Code), its MCP entry and its hooks (`.mcp.json` and `.claude/settings.local.json`; `.codex/config.toml` and `.codex/hooks.json`); a client that is not installed gets nothing. Cairnmind leaves its own block out of the project's index, so it never comes back in `ask`. Review the files before committing. On its first start in the project, each client asks you to trust Cairnmind's MCP server; Cairnmind answers no trust question for you.

Cursor, Cline, Zed and other MCP clients use the same memory through `cairnmind mcp --client external`, in a session that is never counted as verified: see [docs/EXTERNAL_CLIENTS.md](docs/EXTERNAL_CLIENTS.md).

To see what Cairnmind keeps before you install anything, read the [sample project memory](examples/sample-project/README.md): a small project with its decisions, a plan and a check run committed next to it, and a read-only command that needs only Python and uv.

**[Step-by-step installation guide](docs/INSTALL.md)**: preparing Windows and WSL2, installing from a release, the engine natively or in Docker, connecting assistants, verifying, upgrading, uninstalling and troubleshooting. Node.js and a frontend build are not needed.

## Optional: local AI (Ollama) and the workspace (tmux)

**Local models.** With [Ollama](https://ollama.com) running ([installation, step 2](docs/INSTALL.md#2-install-system-tools)):

```bash
cairnmind models pull            # downloads qwen3.5:9b and qwen3-embedding:0.6b into Ollama (about 7 GB)
cairnmind stop                   # the service reads the choice when it starts; the next command that needs it starts it again
```

`cairnmind models pull` turns the knowledge agent on; after the restart the waiting extraction and embedding jobs run and `ask` answers with verified quotes. `cairnmind models set` chooses another model server instead: another Ollama or a server with the OpenAI-compatible API; a server on another computer needs HTTPS and your explicit consent, and receives only the sensitivity classes you allow ([docs/API.md](docs/API.md#the-knowledge-agents-models)). The reference setup uses an RTX 4080 Super with 16 GB of VRAM; the 9B model needs roughly 7 GB of GPU memory, and on CPU it works, but answers are slow. Check memory use and latency on your own machine.

**The terminal workspace.** On Linux, WSL2 and macOS, with tmux 3.4+ (`sudo apt install tmux`, or `brew install tmux` on macOS), `cairnmind` opens the multi-pane workspace: Codex and Claude Code run in panes beside the projects, the memory inspector and the graph ([docs/TERMINAL.md](docs/TERMINAL.md)). On native Windows, `cairnmind` opens the standalone Textual workspace directly in Windows Terminal or PowerShell without requiring tmux, backed by an owner-only Windows Named Pipe for person-confirmed actions. In the POSIX workspace, Ctrl+B then i opens the memory inspector, where you confirm or dispute proposed findings (view Proposed); in the native Windows standalone workspace, press `2` for Memory. The default Focus layout works at 80 × 24; the full Cockpit layout needs at least 140 × 42. On POSIX without tmux 3.4+, opening the workspace prints what is missing and the commands that work without it, and exits with code 1.

**The local web UI inspector (`cairnmind ui`).** An optional loopback-only browser inspector (`http://127.0.0.1:4310`) for viewing OCC session alignment, task test gates, verbatim quote provenance with SHA-256 fingerprint diffs, the 4-column dependency & blast-radius graph, and the MCP guardrail audit log alongside a running Cairnmind workspace or service. Build the static frontend bundle once (requires Node.js 20+):

```bash
cd ui && npm install && npm run build
```

Then run `cairnmind ui` in a separate terminal alongside Cairnmind:

```bash
cairnmind ui --port 4310                    # live mode against your local Cairnmind store (latest project)
cairnmind ui --port 4310 --project my-app   # inspect a specific registered project
cairnmind ui --port 4310 --mock             # preview all states with deterministic sample data (no database needed)
```

Pass `--no-open` to skip opening a browser tab automatically. The server binds strictly to loopback (`127.0.0.1`, `localhost`, or `::1`), streams live updates over SSE (`/api/ui/events`), and provides `Ctrl+K` search and `j`/`k`/`1`–`4`/`Esc` keyboard navigation.

## What sets it apart

What sets it apart from a `CLAUDE.md` file or a generic memory layer is that Cairnmind treats memory as evidence, not as notes:

- **Every answer cites its sources.** The local knowledge agent must back an `ask` answer with verbatim quotes. The service checks that each quote occurs literally in the source it read and that the source is still current, and then asks the local model once more whether the quotes state the answer; otherwise the answer is not presented as grounded. When the project does not contain the answer, the agent says `unknown`.
- **Resuming is verified, not assumed.** A bound session can write to memory only after the model acknowledges the exact plan and task versions it read. The check happens in the service, not in a prompt.
- **"Done" requires fresh proof.** For tasks that require tests, completion needs a passing run of each named test profile, bound to the task definition and to a fingerprint of the current code. A later code change marks that evidence stale.
- **Memory travels with the repository.** A checksummed snapshot in `.memory/cairnmind/` moves one project's knowledge, plans and history to another machine without moving credentials.

Cairnmind suits developers who work with Codex CLI or Claude Code every day, switch between assistants or return to projects after a break, and are comfortable running Linux, WSL2 or macOS on Apple silicon. It is a local tool for one person; a team shares the records it agreed on through Git ([Team memory](docs/MEMORY.md#team-memory)), and the database and the models can run on another computer you control. If you only want a lighter notes file for one assistant, a `CLAUDE.md` is simpler.

## How it compares

| | Instruction file (`CLAUDE.md`, `AGENTS.md`) | Memory built into an assistant | Memory plugin or layer | Cairnmind |
|---|---|---|---|---|
| Who writes it | You, by hand | The model | The model | The model and you, each record tied to a source |
| Checked against the source | No | Usually no | Usually no | An answer's quotes must occur literally in a current source |
| Works across assistants | Each assistant reads its own file | No | Depends on the plugin | Codex CLI, Claude Code, Copilot, API agents; other MCP clients, marked unverified |
| "Done" needs proof | No | No | Usually no | For tasks that require tests: a passing check bound to the current code |
| Setup | None | None | Usually one command | Docker or Java 21 for the database, and uv; local models (Ollama) and tmux are optional |

The columns are kinds of tools, not products; a particular tool may do more than its column says. The last row is the price: Cairnmind is the heaviest of the four to install, even without the optional parts.

Shared memory for a team works through Git ([Team memory](docs/MEMORY.md#team-memory)). Planned, not there yet: read-only views above a single project. The steps are on the [features board](https://github.com/users/piotrwozn/projects/2).

## What it guarantees, and what it does not

| It verifies | It does not verify |
|---|---|
| A cited quote occurs literally in the source version that was read, and that source is still current; the quote shown is always the source's own text | That the model interpreted the quote correctly. A second call to the local model, the support check, must agree that the quotes state the answer, or the answer is withheld; that check has known gaps |
| A session acknowledged the exact packet, plan and task versions before writing | That the model understood what it acknowledged |
| A test run passed for this task definition, check profile and code fingerprint, and whether it is still fresh | That passing tests mean every acceptance criterion is met, or that a review happened |
| Where a stored finding came from, and when its evidence existed | That the finding is true |
| What an external MCP client writes through its tools is marked `origin_evidence: degraded` and never shown or counted as verified | That what it reports is true; a `cairnmind` command its agent runs in a shell has no session and is stored unmarked |
| Without models, `ask` makes no answer: it reports `unavailable` and lists full-text matches as search results | That a listed passage answers the question: no citation is made or checked |

Local memory reduces how much history an assistant must carry, but it does not remove the context limits or the cost of the assistants themselves. Portable snapshots can contain full conversation text; review them before sharing. The full rules and their edge cases (what the citation check tolerates, what the support check misses, transcript record kinds a newer client writes, the limits of the analysis) are in [docs/CONTRACTS.md](docs/CONTRACTS.md#what-it-guarantees-and-what-it-does-not) and [Limits](docs/CONTRACTS.md#limits).

## Measured results

Measured with `qwen3.5:9b` and `qwen3-embedding:0.6b` on an RTX 4080 Super, in isolated databases: on release 0.8.0 on 9 November 2026, and on the code of 0.11.0 on 16 November 2026 (one run of each command for the release, and three runs of the repository questions before it). The figures below are 0.11.0's; the release gate of 0.12.0, run on 18 November 2026, gave the same recall, memory and extraction results and 19 of 20 on the repository questions, none wrong (see [docs/EVALUATION.md](docs/EVALUATION.md#results-for-0120)). Recall, portable memory and extraction use synthetic fixtures; the repository questions use Cairnmind's own repository. None of them is proof of behaviour on every real project.

| Evaluation | Result |
|---|---|
| Recall: 20 golden questions, pass threshold 0.9 | 1.0 on 0.11.0; 1.0 in 6 of 6 runs on 0.8.0 |
| Hand-off Codex → Claude Code → fresh Codex, real clients | passed once, before the 0.8.0 fixes; not repeated since |
| Portable memory: extraction, transfer, cited recall, project isolation | passed on 0.11.0; 3 of 3 runs on 0.8.0 |
| Extraction from conversations: share of proposals a person would reject | 29% of 28 proposals on 0.11.0, every kept record found; 65% on 0.10.0 |
| Questions about Cairnmind's own repository, complete and right | 20 of 20 in the release run, 19, 19 and 20 in three runs before it; none complete and wrong |
| Questions about things the repository does not have, refused | 20 of 21; the one answered (a name in lower case) was answered wrong |
| 22 further questions about Cairnmind's own repository, one run before the release | 15 complete and right, 2 complete and wrong |

Nothing was measured in deterministic-only mode: the evaluations need the models and report `not_run` (exit 3) without them. The method, the problems the measurement uncovered and the limits of these numbers are described in [docs/EVALUATION.md](docs/EVALUATION.md) and [docs/evaluation/RESULTS.md](docs/evaluation/RESULTS.md).

## Documentation

Version 0.12.0 is the current release, the first that runs on macOS with Apple silicon; 0.9.0 was the first under the name Cairnmind. New installations use the installer directly. The [changelog](CHANGELOG.md) lists every release, and [docs/UPGRADING.md](docs/UPGRADING.md) says how to upgrade from each. Planned work is tracked in [issues](https://github.com/piotrwozn/cairnmind/issues?q=is%3Aissue+label%3Aroadmap).

- [docs/INSTALL.md](docs/INSTALL.md): step-by-step installation, the engine natively or in Docker, the models, verification, uninstalling and troubleshooting.
- [docs/TERMINAL.md](docs/TERMINAL.md): the terminal workspace, layout, navigation and diagnostics, and the commands to use without it.
- [docs/EXTERNAL_CLIENTS.md](docs/EXTERNAL_CLIENTS.md): Cursor, Cline, Zed and other MCP clients in an evidence-degraded session.
- [docs/WORKFLOW.md](docs/WORKFLOW.md): plan format, check profiles, plan stages, evidence states, MCP calls, the code fingerprint, `brief`, `usage`, the plan contract, the scope of a step, `impact`, [symbol-anchored evidence](docs/WORKFLOW.md#symbol-anchored-evidence), [opt-in code decisions](docs/WORKFLOW.md#opt-in-decisions-from-code-comments-adr-trailers) and [serverless CI `scan`](docs/WORKFLOW.md#serverless-ci-check-cairnmind-scan).
- [docs/MEMORY.md](docs/MEMORY.md): portable snapshots, `memory show`, evidence and symbol states, opt-in `memory decisions`, the review of conflicts and proposed findings, and team memory.
- [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md): the portable and published memory formats and the import rules.
- [docs/STORAGE_AND_MAINTENANCE.md](docs/STORAGE_AND_MAINTENANCE.md): the data directory, backups, the single writer, `store size`, removing replaced chunks, pruning safety backups and rotating the engine log.
- [docs/API.md](docs/API.md): API, Ollama and Copilot connections, the knowledge agent's models, permissions and third-party MCP tools.
- [docs/EVALUATION.md](docs/EVALUATION.md): the explicit evaluations, the results for 0.12.0, 0.11.0 and 0.8.0 and, in [evaluation/RESULTS.md](docs/evaluation/RESULTS.md), the results on Cairnmind's own repository.
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): the database in a container, on a server or in Kubernetes, and connect-only installations.
- [docs/UPGRADING.md](docs/UPGRADING.md): upgrading an existing installation.
- [docs/CONTRACTS.md](docs/CONTRACTS.md): what Cairnmind guarantees and what it does not, its limits, the implementation contracts and the [extension surfaces](docs/CONTRACTS.md#extension-surfaces): what can be added (checks, reactions to events, MCP clients, model connections), what is planned, and why there are no plugins.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it works, what it is built with, the repository layout and the development commands.
- [CHANGELOG.md](CHANGELOG.md): release history.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first, and report vulnerabilities as described in [SECURITY.md](SECURITY.md). The development commands and the test tiers are also in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#development).

## License

Cairnmind is licensed under the [Apache License 2.0](LICENSE). ArcadeDB, which Cairnmind downloads and manages, is licensed separately under the same license; see [NOTICE](NOTICE).
