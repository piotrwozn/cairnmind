# How Cairnmind is built

## How it works

```text
Interfaces:
  - POSIX tmux + Textual workspace / native Windows standalone Textual workspace (PAL)
  - Loopback-only React Web Cockpit (`cairnmind ui`, bundled in wheels, per-launch session + CSRF auth)
  - CLI (`cairnmind ...`) and MCP stdio server (`cairnmind mcp`)
                           │
                           ▼
             Codex CLI / Claude Code / API agent / Copilot SDK / external MCP clients
                           │ MCP: resume / ACK / ask / plans / checkpoints
                           ▼
                 Local Cairnmind service (FastAPI)
                  │                                   │
       knowledge agent (Ollama / HTTPS, optional)  independent archiving,
       + optional NLI / VCR & quote healing        pulse, serverless scan
                  │                                   │
          Hybrid RAG + GraphRAG ◄─────────────────────┘
                  │
       Sealed StorageBackend DAL (no raw SQL outside storage/)
         ├── ArcadeDB (native Java 21, Docker, remote HTTPS / Kubernetes)
         └── Embedded SQLite (WAL + FTS5 + cosine / optional sqlite-vec)
```

- **Storage:** configurable dual-backend storage behind a sealed `StorageBackend` data-access layer (`src/cairnmind/storage/`). Users choose per installation via `storage_backend` (`arcade` or `sqlite`):
  - **ArcadeDB** ([ArcadeDB](https://github.com/ArcadeData/arcadedb)): pinned single engine holding documents, the code dependency graph, full-text and HNSW vector indexes — run natively with Java 21, in a managed Docker container ([INSTALL.md](INSTALL.md#running-the-database-in-a-container-instead)), or connect-only on a remote server or Kubernetes ([connect-only installations](DEPLOYMENT.md#connect-only-installations)).
  - **Embedded SQLite** (`--backend sqlite`, `cairnmind try`): zero-service local store (`state.db`) using SQLite WAL mode, FTS5 full-text search, temporary-table bound queries (safe beyond `SQLITE_MAX_VARIABLE_NUMBER`), and C-accelerated cosine search (plus optional `cairnmind[vec]` / `sqlite-vec`). Migrate losslessly to ArcadeDB at any time with `cairnmind migrate --to server`.
- **Platform Abstraction Layer (`pal/`):** isolates OS-specific file locking (`flock` / `LockFileEx`), private file permissions (`0600`/`0700` / NTFS Protected DACLs), process tree control (`killpg` / Win32 Job Objects), case-insensitive path identity, and person-proof IPC (POSIX tmux server key / owner-only Windows Named Pipe) so Cairnmind runs natively on Linux, macOS, and Windows 10/11 without WSL2.
- **Retrieval & verification:** full-text search, vector similarity and graph traversal, fused and re-checked verbatim against current source content (`quote_healing` tracks harmless formatting shifts without altering confirmed statuses; optional `cairnmind[nli]` enables local ONNX NLI / VCR verification). `cairnmind pulse` runs a read-only consistency micro-audit (< 500 ms) and `cairnmind scan --diff` checks symbol-anchored drift serverlessly in CI.
- **Knowledge agent:** `qwen3.5:9b` through Ollama with a 16k-token working context, and `qwen3-embedding:0.6b` embeddings, by default. `cairnmind models set` can put either role on another Ollama or a server with the OpenAI-compatible API; a server on another computer needs HTTPS and your explicit consent, and receives only the sensitivity classes you allow ([API.md](API.md#the-knowledge-agents-models)). Both models are optional: without them Cairnmind works in deterministic-only mode ([INSTALL.md](INSTALL.md#5-download-the-models-optional)). Coding assistants are chosen independently.
- **Code graph:** tree-sitter analysis of Python, JavaScript and TypeScript, plus automatic multi-language [SCIP](https://github.com/sourcegraph/scip) indexing for Go, Rust, Java, Kotlin, C, C++, C#, PHP and Ruby (`scip-go`, `rust-analyzer`, `scip-java`, `scip-clang`, `scip-dotnet`, `scip-php`, `scip-ruby`). SCIP indexers run asynchronously in a dedicated background worker (`scip_mode`: `"auto"`, `"manual"`, or `"off"`), write into the configured storage backend's code graph (`defines`, `imports`, `calls`, `references`, `test_imports`, `external_module`), and are also importable offline via `cairnmind scip import`. Other languages remain searchable as text.
- **Local Web Cockpit (`cairnmind ui`):** loopback-only React + TypeScript inspector bundled into release wheels under `cairnmind/ui_dist/` (no Node.js required for end users). Standalone launches mint a per-launch 256-bit bootstrap token, issue an `HttpOnly; SameSite=Strict` session cookie plus per-session CSRF token (`X-Cairnmind-UI-Token`), enforce strict `Host`/`Origin`/`Sec-Fetch-Site` checks, and respect `person_proof` for confirmed acts; mounted inside the main service, `/api/ui/*` is strictly read-only.
- **Memory & plans:** versioned records, symbol-anchored AST fingerprints (`syntax-v4`), independent source snapshots (including uncommitted code), one shared inference queue prioritizing live questions, and atomic plans with deterministic next steps per project and worktree.

The way memory and agents are organized is inspired by SynapCores. Cairnmind is an independent implementation of the knowledge layer; it is not a copy of that product or of its database engine.

## Built with

Cairnmind stands on other people's work. In one place:

- [ArcadeDB](https://github.com/ArcadeData/arcadedb) and [SQLite](https://www.sqlite.org/) (WAL + FTS5, plus optional [sqlite-vec](https://github.com/asg017/sqlite-vec)): the configurable storage backends behind documents, the code graph, full-text search and vectors.
- [Ollama](https://ollama.com) with the Qwen models `qwen3.5:9b` and `qwen3-embedding:0.6b`: the local knowledge agent and the embeddings.
- [tree-sitter](https://tree-sitter.github.io/tree-sitter/) and [SCIP](https://github.com/sourcegraph/scip): code-graph analysis across Python, JS/TS, Go, Rust, Java, Kotlin, C/C++, C#, PHP and Ruby.
- [Textual](https://github.com/Textualize/textual), [tmux](https://github.com/tmux/tmux) and [React](https://react.dev/) / [Vite](https://vite.dev/): the terminal workspaces and the local Web Cockpit inspector.
- [Model Context Protocol](https://modelcontextprotocol.io) and its [Python SDK](https://github.com/modelcontextprotocol/python-sdk): how coding assistants reach the memory.
- [FastAPI](https://fastapi.tiangolo.com), [Uvicorn](https://www.uvicorn.org), [HTTPX](https://www.python-httpx.org) and [Typer](https://typer.tiangolo.com): the local service, its clients and the command line.
- [GitHub Copilot SDK](https://pypi.org/project/github-copilot-sdk/): the Copilot connection of the built-in agent.
- [uv](https://github.com/astral-sh/uv), [pytest](https://pytest.org), [Ruff](https://github.com/astral-sh/ruff), [Pyright](https://github.com/microsoft/pyright) and [Vitest](https://vitest.dev/): packaging, tests, linting and static type checking.

The coding assistants it works with, Codex CLI and Claude Code among them, are not part of Cairnmind; it starts them and keeps their project memory.

## Repository layout

| Path | Contents |
|---|---|
| [`src/cairnmind/`](../src/cairnmind/) | The Python package: 6 top-level facades (`__init__`, `__main__`, `config`, `cli`, `store`, `service`) over 9 domain subpackages (`pal/`, `storage/`, `ui/`, `cli_cmds/`, `terminal/`, `mcp/`, `memory/`, `agents/`, `scip/`) with acyclic imports enforced by `tests/test_import_graph.py`. |
| [`ui/`](../ui/) | React + TypeScript + Vite frontend for `cairnmind ui`, tested with Vitest and bundled into `cairnmind/ui_dist/` when building release wheels (`CAIRNMIND_BUILD_UI=1`). |
| [`tests/`](../tests/) | Automated tests and architectural guards (DAL isolation, import graph, Windows/UTF-8, HTTPX TLS, contract fixtures); see [CONTRIBUTING.md](../CONTRIBUTING.md) for the tiers and a code map. |
| [`docs/`](../docs/) | Guides and implementation contracts. |
| [`scripts/`](../scripts/) | Installers (`install-wsl.sh`, `install-windows.ps1`) and release/contract helpers. |
| [`deploy/`](../deploy/) | The database image, a Compose file and Kubernetes manifests ([docs/DEPLOYMENT.md](DEPLOYMENT.md)). |
| [`pilot/`](../pilot/) | Material for a hand-off pilot between coding assistants. |

## Development

```bash
# .python-version pins Python 3.12, the version the lock file and CI cover
uv sync --locked --extra dev
uv run ruff check src tests hatch_build.py
uv run pyright
uv run pytest -m "not arcade and not tmux"   # fast tier, no database or tmux
uv run pytest                                # full suite, needs Java 21 and tmux
uv run pytest -n 4                           # the full suite in 4 parallel processes

# Frontend (`ui/`)
cd ui && npm ci && npm run typecheck && npm test && npm run build
```

The automated tests need no Ollama, no models and no signed-in coding clients; those are used only by the explicit evaluations. Test modules that need the real ArcadeDB engine are marked `arcade`, and those that drive a real tmux server are marked `tmux`. CI runs Ruff + Pyright lint, frontend typecheck/Vitest/wheel `ui_dist` checks, the fast tier on Linux, macOS and Windows, and the full ArcadeDB suite on every pull request and push to `main`.
