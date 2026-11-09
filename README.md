# DevBrain

[![CI](https://github.com/piotrwozn/devbrain/actions/workflows/ci.yml/badge.svg)](https://github.com/piotrwozn/devbrain/actions/workflows/ci.yml)

**Evidence-grounded project memory for coding agents, in your terminal.**

DevBrain is a local workspace for Codex CLI, Claude Code, GitHub Copilot and API-based coding agents (OpenAI, Anthropic, OpenAI-compatible endpoints and local Ollama). It keeps a durable, versioned memory of each project outside any single conversation, so the next session, or a different assistant, can pick up exactly where the last one stopped.

What sets it apart from a `CLAUDE.md` file or a generic memory layer is that DevBrain treats memory as evidence, not as notes:

- **Every answer cites its sources.** The local knowledge agent answers `ask` questions only with verbatim quotes from archived sources, re-checked against the current file content. When the project does not contain the answer, it says `unknown`.
- **Resuming is verified, not assumed.** A session can write to memory only after the model acknowledges the exact plan and task versions it read. The check happens in the service, not in a prompt.
- **"Done" requires fresh proof.** Named test profiles bind a passing run to the task definition and to a fingerprint of the current code. A later code change marks the evidence stale.
- **Memory travels with the repository.** A checksummed snapshot in `.memory/devbrain/` moves one project's knowledge, plans and history to another machine without moving credentials.

## How it works

```text
Terminal: tmux + Textual ──► Codex CLI / Claude Code / API agent / Copilot SDK
                           │ MCP: resume / ACK / ask / plans
                           ▼
                 Local DevBrain service
                  │                   │
       knowledge agent (Ollama)    independent archiving
                  │                   │
          Hybrid RAG + GraphRAG ◄─────┘
                  │
       ArcadeDB: sources, vectors, graph, decisions, tasks
```

- **Storage:** a single pinned [ArcadeDB](https://github.com/ArcadeData/arcadedb) engine holds documents, the code dependency graph, full-text and vector indexes.
- **Retrieval:** full-text search, HNSW vectors and graph traversal, fused and re-checked against source content.
- **Knowledge agent:** `qwen3.5:9b` through Ollama, with `qwen3-embedding:0.6b` embeddings. Coding assistants are chosen independently.
- **Code graph:** tree-sitter analysis of Python, JavaScript and TypeScript. Other languages remain searchable as text.

## Measured results

Measured on 9 November 2026 with `qwen3.5:9b` on an RTX 4080 Super, in isolated databases with synthetic fixtures. These are evaluations of the shipped code, not proof of behaviour on every real project.

| Evaluation | Result |
|---|---|
| Recall: 20 golden questions, pass threshold 0.9 | 1.0 in 6 of 6 runs |
| Hand-off Codex → Claude Code → fresh Codex, real clients | passed |
| Portable memory: extraction, transfer, cited recall, project isolation | 3 of 3 runs on the final code |

The method, the problems the measurement uncovered and the limits of these numbers are described in [docs/EVALUATION.md](devbrain/docs/EVALUATION.md).

## Repository layout

| Path | Contents |
|---|---|
| [`devbrain/`](devbrain/) | The Python package, tests, documentation and pilot material. Start with its [README](devbrain/README.md). |
| [`.github/`](.github/) | Continuous integration, issue and pull request templates. |

## Quick start

DevBrain runs on Linux, or on Windows inside WSL2 Ubuntu, with Java 21+, tmux 3.4+, uv and Ollama. With those in place:

```bash
git clone https://github.com/piotrwozn/devbrain.git ~/src/devbrain
cd ~/src/devbrain/devbrain
bash scripts/install-wsl.sh     # installs DevBrain and ArcadeDB, initialises local memory
devbrain models pull            # downloads the local models (about 7 GB)
devbrain project add my-app ~/src/my-app
devbrain install my-app         # connects Codex / Claude Code to this project
devbrain                        # opens the terminal workspace
```

**[Step-by-step installation guide](devbrain/docs/INSTALL.md)**: preparing Windows and WSL2, installing from a release, connecting assistants, verifying, upgrading, uninstalling and troubleshooting.

Everyday commands and limits are in [devbrain/README.md](devbrain/README.md). Terminal usage is in [docs/TERMINAL.md](devbrain/docs/TERMINAL.md), connections and permissions in [docs/API.md](devbrain/docs/API.md).

## Development

```bash
cd devbrain
uv sync --locked --extra dev
uv run ruff check src tests
uv run pytest -m "not arcade and not tmux"   # fast tier, no database or tmux
uv run pytest                                # full suite, needs Java 21 and tmux
```

Test modules that need the real ArcadeDB engine are marked `arcade`, and those that drive a real tmux server are marked `tmux`. CI runs lint, the fast tier and the full suite on every push.

## Status and roadmap

Version 0.8.0 is the current release; see the [changelog](devbrain/CHANGELOG.md). New installations use the installer directly. Upgrading an existing 0.7.2 installation goes through a guarded updater that requires a passed runtime qualification of the host before it touches existing data; see [docs/UPGRADING.md](devbrain/docs/UPGRADING.md).

Planned work is tracked in [issues](https://github.com/piotrwozn/devbrain/issues?q=is%3Aissue+label%3Aroadmap).

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first, and report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## License

DevBrain is licensed under the [Apache License 2.0](LICENSE). ArcadeDB, which DevBrain downloads and manages, is licensed separately under the same license; see [NOTICE](NOTICE).
