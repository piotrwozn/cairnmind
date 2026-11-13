# Contributing to Cairnmind

Thank you for your interest in Cairnmind. This guide explains how to set up a development environment, run the tests and propose a change.

## Ground rules

Cairnmind is built around one promise: memory that coding agents can trust. Changes are reviewed against that promise first.

- A model answer never becomes independent evidence. Quotes must stay literal and verifiable.
- Safety contracts are deliberate. If a test asserts that something blocks, such as an unknown transcript record blocking a verified resume, change it only together with the contract in [docs/CONTRACTS.md](docs/CONTRACTS.md) or [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md), and explain why in the pull request.
- Prefer removing code to adding a new layer. Every feature has to be maintained by a small team.

## Development environment

Cairnmind runs on Linux or WSL2 Ubuntu. You need Python 3.12, [uv](https://docs.astral.sh/uv/), Java 21+ and tmux 3.4+.

```bash
git clone https://github.com/piotrwozn/cairnmind.git
cd cairnmind
uv sync --locked --extra dev
```

Ollama and the coding clients are needed only for the explicit evaluations, never for the automated test suite.

## Running checks

```bash
uv run ruff check src tests
uv run pytest -m "not arcade and not tmux"   # fast tier: no database, no tmux
uv run pytest                                # full suite with the real ArcadeDB engine
uv run pytest -n 4                           # the same full suite in 4 parallel processes
```

- `-n N` (pytest-xdist) runs the tests in N processes that share one ArcadeDB engine; each process uses its own databases. Use at most 4 on a workstation or in WSL, so that the run does not hold every core for its whole length; `-n auto` takes one process per core. Without `-n` the suite runs in one process, as before.
- Tests that need the real engine are marked `arcade`. New test modules get the marker by default. Add a module to `NO_ARCADE_MODULES` in `tests/conftest.py` only after it passes with no engine running.
- Tests that drive a real tmux server are listed in `TMUX_MODULES` in the same file.
- Keyboard latency budgets target a developer workstation. On slow or shared machines, set `CAIRNMIND_LATENCY_SCALE`, as CI does.

### Coverage

Coverage is measured only when you ask for it. The same two numbers CI shows on a pull request, the coverage of all of `src/cairnmind` and of the lines your branch adds or changes since it left `origin/main`:

```bash
uv run pytest -n 4 --cov --cov-report=xml && uv run python scripts/coverage_summary.py
```

`--compare-branch` names another base; `--no-patch` prints the total only. Run `git fetch origin` first so that `origin/main` is current. Python processes that the tests start are measured too; a process ended by a signal, such as a terminal pane closed with its tmux server, records nothing, so the terminal modules read lower than they are exercised. Coverage adds about a third to the run time. Without `--cov` nothing is measured and no coverage file is written.

## Where to start

Looking for a first task? Issues labelled [`good first issue`](https://github.com/piotrwozn/cairnmind/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) touch a small part of the code and can be verified without models.

### Code map

All modules are in `src/cairnmind/`, all tests in `tests/`. **Fast** tests run with `uv run pytest -m "not arcade and not tmux"` and need only Python; **engine** tests start the real ArcadeDB and need Java 21.

| You want to change | Start in | Run | Tier |
|---|---|---|---|
| How `ask` answers are validated, cited or repaired | `agent.py`, `answer_contract.py` | `test_knowledge.py`, `test_memory_stabilization.py`, `test_answer_contract.py` | engine; `test_answer_contract.py` is fast |
| What gets extracted from conversations into records | `agent.py` (extraction) | `test_extraction_grounding.py`, `test_extraction_safety.py` | engine |
| Search ranking and the context sent to assistants | `retrieval.py` | `test_knowledge.py` | engine |
| Records, revisions, sources, backups | `store.py` | `test_store.py`, `test_migrations.py` | engine |
| Resume packets and ACK | `sessions.py`, `mcp_server.py` | `test_sessions.py`, `test_session_lifecycle.py`, `test_resume_mcp_protocol.py` | engine |
| Plans and the next task | `plans.py` | `test_plans.py` | engine |
| Check profiles and test freshness | `verification.py` | `test_verification.py` | engine |
| Portable memory snapshots and import | `memory.py` | `test_portable_memory.py`, `test_memory_adversarial.py` | engine |
| Indexing code and importing transcripts | `ingest.py`, `parsing.py` | `test_ingest.py`, `test_transcript_compatibility.py`, `test_parsing.py` | engine; `test_parsing.py` is fast |
| Codex and Claude Code detection, hooks, MCP config | `integrations.py`, `client_auth.py` | `test_integrations.py`, `test_client_auth.py` | engine; `test_client_auth.py` is fast |
| Terminal panels, forms, command palette | `terminal_panels.py`, `terminal_ui.py`, `terminal_commands.py` | `test_terminal_panels.py`, `test_terminal_ux080.py`, `test_terminal_commands.py` | fast |
| Memory inspector and graph views | `terminal_inspector.py`, `terminal_graph.py` | `test_terminal_panels.py`, `test_terminal_graph.py` | fast |
| tmux workspace layout | `workspace.py` | `test_workspace.py`, `test_workspace_cli.py` | fast; `test_workspace.py` needs tmux |
| The built-in API agent and its tools | `api_agent.py`, `agent_tools.py`, `api_providers.py` | `test_api_agent.py`, `test_agent_tools.py`, `test_api_providers.py` | `test_api_agent.py` is engine; the others are fast |
| GitHub Copilot adapter | `copilot_adapter.py` | `test_copilot_adapter.py` | fast |
| The local broker API used by the panels | `cockpit.py` | `test_cockpit_server.py` | fast |
| CLI commands and help | `cli.py` | `test_cli.py`, `test_diagnostic_cli.py` | engine |
| Evaluations | `evaluation.py`, `memory_evaluation.py` | `test_evaluation.py`, `test_memory_evaluation.py` | engine |

Run a single file with, for example, `uv run pytest tests/test_terminal_commands.py`.

A few names still say `devbrain` on purpose: the data directory of an existing installation, the directory and format name of snapshots written by earlier versions, the prefix of the database name and the `devbrain_meta` type inside it, and the spellings that are read for compatibility. Environment variables are `CAIRNMIND_*`, with `DEVBRAIN_*` still read. That is expected until the compatible migration in [#40](https://github.com/piotrwozn/cairnmind/issues/40). The Python module is `cairnmind`; `src/devbrain/` is only an import alias kept for one release, so do not import from it.

## Evaluations

These run real local models and are not part of CI:

```bash
cairnmind eval run              # 20 recall cases with the local knowledge agent
cairnmind eval memory           # extraction, portable transfer and cited recall
cairnmind eval run --clients    # adds the Codex → Claude Code → Codex hand-off; uses your client quotas
```

If a change touches the knowledge agent, retrieval or extraction, include evaluation results in the pull request.

## Pull requests

1. Open an issue first for anything larger than a bug fix, so the approach can be agreed on.
2. Keep each pull request focused on one change, with tests.
3. Make sure lint and the fast tier pass locally. CI runs the full suite.
4. Describe what you changed, why, and what you could not verify.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
