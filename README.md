# Cairnmind

[![CI](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml/badge.svg)](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fpiotrwozn%2Fcairnmind%2Fbadges%2Fcoverage.json)](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml?query=branch%3Amain+event%3Apush)

**Memory as evidence, not notes — verified project memory for coding agents.**

1. **Your agent does not guess:** `ask` returns a verbatim quote verified against the current code — or `unknown`.
2. **Your agent does not lie that it finished:** marking a task `done` is blocked if the code changed after the last passing test run.

![Cairnmind 60-second demo: without Cairnmind vs. with Cairnmind](docs/assets/demo-60s.svg)

*60-second before & after on [`examples/sample-project`](examples/sample-project/README.md): **0:00–0:20** hallucinated flag vs. `unknown`; **0:20–0:45** stale `done` blocked after a code edit; **0:45–1:00** fresh check run pins the code fingerprint and unlocks verified `done`.*

**Try Cairnmind in 15 seconds (Python + `uv` only — zero external services, zero files written in your directory):**

```bash
uvx cairnmind try
```

Works on Linux, macOS, and native Windows using an ephemeral in-memory SQLite store over the bundled [`examples/sample-project`](examples/sample-project/README.md). You can also inspect the snapshot directly with `cairnmind memory show examples/sample-project/.memory/cairnmind`.

*A cairn is the pile of stones a walker leaves to mark the trail for the next one. Cairnmind is the durable, versioned memory one coding agent leaves for the next — across Codex CLI, Claude Code, GitHub Copilot, API agents and MCP clients. Formerly DevBrain.*

## How it works

Cairnmind runs as a separate local service that coding assistants connect to. It never treats a model's claim as evidence:

- **Verbatim source quotes or `unknown`.** Every `ask` answer must quote the source text verbatim; the service re-checks the quote against the current file and runs a support check before accepting the answer.
- **Read-before-write resume.** A coding session cannot write to memory until it acknowledges the exact plan and task versions it read.
- **Fresh machine proof for `done`.** Test-gated tasks require a passing local check run bound to the current code fingerprint; editing code invalidates earlier runs.
- **Portable memory.** Snapshots and published team memory travel with the repository through Git without credentials.

The full architecture taxonomy (`origin_evidence`, resume packets, plan versions, check profiles, guarantees and limits) lives in [docs/CONTRACTS.md](docs/CONTRACTS.md#architecture-taxonomy).

## Quick start

Cairnmind runs on Linux, macOS (Apple silicon), and native Windows (lightweight CLI/MCP/SQLite mode; the `tmux` terminal workspace and person-proof inspector run on Linux, macOS, and WSL2).

**1. Try the zero-setup sandbox (15 seconds):**

```bash
uvx cairnmind try
```

**2. Use Cairnmind on your own repository (embedded SQLite — zero Java/Docker required):**

```bash
uv tool install cairnmind
cairnmind init --backend sqlite                # 1. initializes local SQLite WAL + FTS5 store in data_dir/state.db
cairnmind project add my-app ~/src/my-app      # 2. registers your project's folder and indexes its files
cairnmind remember my-app "Accounts lock after five failed logins."   # 3. stores a first finding, as proposed
cairnmind brief my-app                         # 4. where work stands: your finding is listed, marked as unconfirmed
cairnmind ask my-app "When do accounts lock?"  # 5. finds it again; passages of your files that use the question's words are searched too
```

**Need large-repo / multi-agent concurrency?** Scale losslessly to ArcadeDB (**Java 21** is the simplest choice, or Docker) at any time with `cairnmind migrate --to server` (or install with the full ArcadeDB script `bash scripts/install-wsl.sh`). Nothing else is required upfront: Ollama, `tmux`, NLI (`cairnmind[nli]`), and GitHub Copilot (`cairnmind[copilot]`) are all optional and covered under [Optional integrations](#optional-integrations).

In a terminal, commands explain what happened and how to fix blocks in plain language; with `--json` (or in a pipe/script), `stdout` emits pure JSON while hints and warnings go strictly to `stderr` (`NO_COLOR=1` disables ANSI styling). `cairnmind doctor` repeats the installer's check at any time and exits 1 when something required is missing.

- If the installer prints `Add this directory to PATH`, do what that line says and open a new terminal.
- `my-app` is any name you choose; `~/src/my-app` stands for a project folder (a Git repo with at least one commit unlocks `impact`, coding sessions and commit history in `brief`).
- Findings stored via CLI or assistants start as *proposed* and are immediately usable in `brief` and `ask`; you can confirm or dispute them later in the memory inspector (see [Optional integrations](#optional-integrations)).
- Without local models, `ask` searches passages deterministically without fabricating an answer (`status: unavailable` in `--json`). Enable local models below to turn on cited answers.

### Connect Codex or Claude Code

With [Codex CLI](https://developers.openai.com/codex/cli) or [Claude Code](https://code.claude.com/docs/en/setup) installed and signed in:

```bash
cairnmind install my-app          # connects the installed clients to this project
cairnmind run my-app claude       # or codex: starts the client bound to this project's memory
```

`cairnmind install` writes only for installed clients: a marked Cairnmind block (`AGENTS.md` for Codex, `CLAUDE.md` for Claude Code), its MCP entry and its hooks (`.mcp.json` and `.claude/settings.local.json`; `.codex/config.toml` and `.codex/hooks.json`). Cairnmind leaves its own block out of the project's index. Review the files before committing.

Cursor, Cline, Zed and other MCP clients connect via `cairnmind mcp --client external` in an evidence-degraded session: see [docs/EXTERNAL_CLIENTS.md](docs/EXTERNAL_CLIENTS.md).

**[Step-by-step installation guide](docs/INSTALL.md)**: preparing Windows and WSL2 (Ubuntu 24.04 is the tested platform for the full ArcadeDB + tmux setup; macOS and native Windows lightweight mode are also supported), installing from a release, native or Docker engine, connecting assistants, upgrading, uninstalling and troubleshooting. The bundled sample project lives in `examples/sample-project`.

## Optional integrations

### Local models (Ollama)

With [Ollama](https://ollama.com) running ([installation, step 2](docs/INSTALL.md#2-install-system-tools)):

```bash
cairnmind models pull            # downloads qwen3.5:9b and qwen3-embedding:0.6b into Ollama (about 7 GB)
cairnmind stop                   # the service reads the choice on next start
```

`cairnmind models pull` enables cited answers, semantic vectors and background extraction. `cairnmind models set` points at another Ollama or an OpenAI-compatible HTTPS server ([docs/API.md](docs/API.md#the-knowledge-agents-models)).

### Terminal workspace (tmux 3.4+)

With tmux 3.4+ (`sudo apt install tmux`, or `brew install tmux` on macOS), `cairnmind` opens the multi-pane workspace beside Codex, Claude Code, the memory inspector and the graph ([docs/TERMINAL.md](docs/TERMINAL.md)). Press `Ctrl+B then i` to open the memory inspector and confirm or dispute proposed findings. Every other CLI command works without `tmux`.

## How it compares

| | Instruction file (`CLAUDE.md`, `AGENTS.md`) | Memory built into an assistant | Memory plugin or layer | Cairnmind |
|---|---|---|---|---|
| Who writes it | You, by hand | The model | The model | The model and you, each record tied to a source |
| Checked against the source | No | Usually no | Usually no | Every cited quote must occur literally in a current source |
| Works across assistants | Each assistant reads its own file | No | Depends on the plugin | Codex CLI, Claude Code, Copilot, API agents; external MCP clients |
| "Done" needs proof | No | No | Usually no | Fresh local check run bound to the current code fingerprint |
| Setup | None | None | Usually one command | Java 21 or Docker + `uv`; Ollama and `tmux` are optional |

For the complete guarantees-vs-non-guarantees table and known limits, see [docs/CONTRACTS.md](docs/CONTRACTS.md#what-it-guarantees-and-what-it-does-not). Shared team memory works through Git ([Team memory](docs/MEMORY.md#team-memory)).

## Measured results

Measured with `qwen3.5:9b` and `qwen3-embedding:0.6b` on an RTX 4080 Super in isolated databases (0.11.0 / 0.12.0 release gate; see [docs/EVALUATION.md](docs/EVALUATION.md) and [docs/evaluation/RESULTS.md](docs/evaluation/RESULTS.md)):

| Evaluation | Result |
|---|---|
| Recall: 20 golden questions (threshold 0.9) | 1.0 on 0.11.0 and 0.12.0; 1.0 in 6 of 6 runs on 0.8.0 |
| Portable memory: extraction, transfer, cited recall, project isolation | passed on 0.11.0 and 0.12.0; 3 of 3 runs on 0.8.0 |
| Extraction from conversations (rejected share) | 29% of 28 proposals (down from 65% on 0.10.0), every kept record found |
| Questions about Cairnmind's own repository (complete and right) | 19–20 of 20; none complete and wrong |
| Questions about absent features, refused | 20 of 21 (24 of 24 after subject/names checks; see [RESULTS.md](docs/evaluation/RESULTS.md)) |

## Documentation

Version 0.12.0 is the current release. See [CHANGELOG.md](CHANGELOG.md), [docs/UPGRADING.md](docs/UPGRADING.md) and the [roadmap issues](https://github.com/piotrwozn/cairnmind/issues?q=is%3Aissue+label%3Aroadmap).

- [docs/CONTRACTS.md](docs/CONTRACTS.md): architecture taxonomy, guarantees, limits, implementation contracts and extension surfaces.
- [docs/INSTALL.md](docs/INSTALL.md): installation, native or Docker engine, models, verification and troubleshooting.
- [docs/WORKFLOW.md](docs/WORKFLOW.md): plans, check profiles, code fingerprints, evidence states, MCP tools, `brief`, `usage` and `impact`.
- [docs/MEMORY.md](docs/MEMORY.md) & [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md): portable snapshots, team memory, secrets gate and import rules.
- [docs/TERMINAL.md](docs/TERMINAL.md): terminal workspace, memory inspector and keyboard shortcuts.
- [docs/EXTERNAL_CLIENTS.md](docs/EXTERNAL_CLIENTS.md): Cursor, Cline, Zed and other external MCP clients.
- [docs/STORAGE_AND_MAINTENANCE.md](docs/STORAGE_AND_MAINTENANCE.md): data directory, backups, single writer and maintenance.
- [docs/API.md](docs/API.md): API, Ollama, Copilot connections, sensitivity classes and third-party MCP tools.
- [docs/EVALUATION.md](docs/EVALUATION.md): explicit evaluations and measured results.
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): containers, remote servers, Kubernetes and connect-only installations.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): system architecture, dependencies, repository layout and dev commands.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first, and report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## License

Cairnmind is licensed under the [Apache License 2.0](LICENSE). ArcadeDB is licensed separately under the same license; see [NOTICE](NOTICE).
