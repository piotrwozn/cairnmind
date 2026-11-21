# How Cairnmind is built

## How it works

```text
Terminal: tmux + Textual ──► Codex CLI / Claude Code / API agent / Copilot SDK
                           │ MCP: resume / ACK / ask / plans
                           ▼
                 Local Cairnmind service
                  │                   │
       knowledge agent (Ollama, optional)    independent archiving
                  │                   │
          Hybrid RAG + GraphRAG ◄─────┘
                  │
       ArcadeDB: sources, vectors, graph, decisions, tasks
```

- **Storage:** a single pinned [ArcadeDB](https://github.com/ArcadeData/arcadedb) engine holds documents, the code dependency graph, full-text and vector indexes. Cairnmind installs and runs it on your machine, natively with Java 21 or in a Docker container it starts itself ([INSTALL.md](INSTALL.md#running-the-database-in-a-container-instead)), or only connects to one you run in a container, on a server or in Kubernetes ([connect-only installations](DEPLOYMENT.md#connect-only-installations)).
- **Retrieval:** full-text search, HNSW vectors and graph traversal, fused and re-checked against source content.
- **Knowledge agent:** `qwen3.5:9b` through Ollama with a 16k-token working context, and `qwen3-embedding:0.6b` embeddings, by default. `cairnmind models set` can put either role on another Ollama or a server with the OpenAI-compatible API; a server on another computer needs HTTPS and your explicit consent, and receives only the sensitivity classes you allow ([API.md](API.md#the-knowledge-agents-models)). Both models are optional: without them Cairnmind works in deterministic-only mode ([INSTALL.md](INSTALL.md#5-download-the-models-optional)). Coding assistants are chosen independently.
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

## Repository layout

| Path | Contents |
|---|---|
| [`src/cairnmind/`](../src/cairnmind/) | The Python package. `src/devbrain/` is an import alias of the earlier module name, kept for one release so that entries starting `python -m devbrain` keep working until `cairnmind install` rewrites them. |
| [`tests/`](../tests/) | Automated tests; see [CONTRIBUTING.md](../CONTRIBUTING.md) for the tiers and a code map. |
| [`docs/`](../docs/) | Guides and implementation contracts. |
| [`scripts/`](../scripts/) | The installer. |
| [`deploy/`](../deploy/) | The database image, a Compose file and Kubernetes manifests ([docs/DEPLOYMENT.md](DEPLOYMENT.md)). |
| [`pilot/`](../pilot/) | Material for a hand-off pilot between coding assistants. |

## Development

```bash
# .python-version pins Python 3.12, the version the lock file and CI cover
uv sync --locked --extra dev
uv run ruff check src tests
uv run pytest -m "not arcade and not tmux"   # fast tier, no database or tmux
uv run pytest                                # full suite, needs Java 21 and tmux
uv run pytest -n 4                           # the full suite in 4 parallel processes
```

The automated tests need no Ollama, no models and no signed-in coding clients; those are used only by the explicit evaluations. Test modules that need the real ArcadeDB engine are marked `arcade`, and those that drive a real tmux server are marked `tmux`. CI runs lint, the fast tier on Linux and on macOS, and the full suite on every pull request and on every push to `main`.
