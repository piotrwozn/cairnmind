# Cairnmind

[![CI](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml/badge.svg)](https://github.com/piotrwozn/cairnmind/actions/workflows/ci.yml)

**Evidence-grounded project memory for coding agents, in your terminal.**

*A cairn is the pile of stones a walker leaves to mark the trail for the next one. Cairnmind is the memory one coding agent leaves for the next. Formerly DevBrain.*

Cairnmind is a local workspace for Codex CLI, Claude Code, GitHub Copilot and API-based coding agents (OpenAI, Anthropic, OpenAI-compatible endpoints and local Ollama). It keeps a durable, versioned memory of each project outside any single conversation, so the next session, or a different assistant, can pick up exactly where the last one stopped.

What sets it apart from a `CLAUDE.md` file or a generic memory layer is that Cairnmind treats memory as evidence, not as notes:

- **Every answer cites its sources.** The local knowledge agent must back an `ask` answer with verbatim quotes. The service checks that each quote occurs literally in the source it read and that the source is still current; otherwise the answer is not presented as grounded. When the project does not contain the answer, the agent says `unknown`.
- **Resuming is verified, not assumed.** A bound session can write to memory only after the model acknowledges the exact plan and task versions it read. The check happens in the service, not in a prompt.
- **"Done" requires fresh proof.** For tasks that require tests, completion needs a passing run of each named test profile, bound to the task definition and to a fingerprint of the current code. A later code change marks that evidence stale.
- **Memory travels with the repository.** A checksummed snapshot in `.memory/devbrain/` moves one project's knowledge, plans and history to another machine without moving credentials.

## Who it is for

Cairnmind 0.8 suits developers who work with Codex CLI or Claude Code every day, switch between assistants or return to projects after a break, and are comfortable running Linux or WSL2 with a local model through Ollama. It is a single-user, local tool. If you only want a lighter notes file for one assistant, a `CLAUDE.md` is simpler.

## What it guarantees, and what it does not

Cairnmind checks things a program can check:

| It verifies | It does not verify |
|---|---|
| A cited quote occurs literally in the source version that was read, and that source is still current | That the model interpreted the quote correctly. Quote coverage of an answer is reported for information only |
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
- **Knowledge agent:** `qwen3.5:9b` through Ollama, with `qwen3-embedding:0.6b` embeddings. Coding assistants are chosen independently.
- **Code graph:** tree-sitter analysis of Python, JavaScript and TypeScript. Other languages remain searchable as text.

## Measured results

Measured on 9 November 2026 with `qwen3.5:9b` on an RTX 4080 Super, in isolated databases with synthetic fixtures. These are evaluations of the shipped code, not proof of behaviour on every real project.

| Evaluation | Result |
|---|---|
| Recall: 20 golden questions, pass threshold 0.9 | 1.0 in 6 of 6 runs |
| Hand-off Codex → Claude Code → fresh Codex, real clients | passed once, before the 0.8.0 fixes; not repeated on the final code |
| Portable memory: extraction, transfer, cited recall, project isolation | 3 of 3 runs on the final code |

The method, the problems the measurement uncovered and the limits of these numbers are described in [docs/EVALUATION.md](devbrain/docs/EVALUATION.md).

## Repository layout

| Path | Contents |
|---|---|
| [`devbrain/`](devbrain/) | The Python package, tests, documentation and pilot material. Start with its [README](devbrain/README.md). |
| [`.github/`](.github/) | Continuous integration, issue and pull request templates. |

## Quick start

Cairnmind runs on Linux, and on Windows inside WSL2 Ubuntu (Ubuntu 24.04 is the tested platform). It needs Java 21+, tmux 3.4+, uv and Ollama. With those in place:

```bash
git clone https://github.com/piotrwozn/cairnmind.git ~/src/cairnmind
cd ~/src/cairnmind/devbrain
bash scripts/install-wsl.sh     # installs Cairnmind and ArcadeDB, initialises local memory
cairnmind models pull            # downloads the local models (about 7 GB)
cairnmind project add my-app ~/src/my-app
cairnmind install my-app         # connects Codex / Claude Code to this project
cairnmind                        # opens the terminal workspace
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

The automated tests need no Ollama, no models and no signed-in coding clients; those are used only by the explicit evaluations. Test modules that need the real ArcadeDB engine are marked `arcade`, and those that drive a real tmux server are marked `tmux`. CI runs lint, the fast tier and the full suite on every push.

## Status and roadmap

Version 0.8.0 is the current release; see the [changelog](devbrain/CHANGELOG.md). New installations use the installer directly. Upgrading an existing 0.7.2 installation goes through a guarded updater that requires a passed runtime qualification of the host before it touches existing data; see [docs/UPGRADING.md](devbrain/docs/UPGRADING.md).

Planned work is tracked in [issues](https://github.com/piotrwozn/cairnmind/issues?q=is%3Aissue+label%3Aroadmap).

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first, and report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## License

Cairnmind is licensed under the [Apache License 2.0](LICENSE). ArcadeDB, which Cairnmind downloads and manages, is licensed separately under the same license; see [NOTICE](NOTICE).
