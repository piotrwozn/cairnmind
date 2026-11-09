# Contributing to DevBrain

Thank you for your interest in DevBrain. This guide explains how to set up a development environment, run the tests and propose a change.

## Ground rules

DevBrain is built around one promise: memory that coding agents can trust. Changes are reviewed against that promise first.

- A model answer never becomes independent evidence. Quotes must stay literal and verifiable.
- Safety contracts are deliberate. If a test asserts that something blocks, such as an unknown transcript record blocking a verified resume, change it only together with the contract in [devbrain/CONTRACTS.md](devbrain/CONTRACTS.md) or [docs/MEMORY_CONTRACTS.md](devbrain/docs/MEMORY_CONTRACTS.md), and explain why in the pull request.
- Prefer removing code to adding a new layer. Every feature has to be maintained by a small team.

## Development environment

DevBrain runs on Linux or WSL2 Ubuntu. You need Python 3.12, [uv](https://docs.astral.sh/uv/), Java 21+ and tmux 3.4+.

```bash
git clone https://github.com/piotrwozn/devbrain.git
cd devbrain/devbrain
uv sync --locked --extra dev
```

Ollama and the coding clients are needed only for the explicit evaluations, never for the automated test suite.

## Running checks

```bash
uv run ruff check src tests
uv run pytest -m "not arcade and not tmux"   # fast tier: no database, no tmux
uv run pytest                                # full suite with the real ArcadeDB engine
```

- Tests that need the real engine are marked `arcade`. New test modules get the marker by default. Add a module to `NO_ARCADE_MODULES` in `tests/conftest.py` only after it passes with no engine running.
- Tests that drive a real tmux server are listed in `TMUX_MODULES` in the same file.
- Keyboard latency budgets target a developer workstation. On slow or shared machines, set `DEVBRAIN_LATENCY_SCALE`, as CI does.

## Evaluations

These run real local models and are not part of CI:

```bash
devbrain eval run              # 20 recall cases with the local knowledge agent
devbrain eval memory           # extraction, portable transfer and cited recall
devbrain eval run --clients    # adds the Codex → Claude Code → Codex hand-off; uses your client quotas
```

If a change touches the knowledge agent, retrieval or extraction, include evaluation results in the pull request.

## Pull requests

1. Open an issue first for anything larger than a bug fix, so the approach can be agreed on.
2. Keep each pull request focused on one change, with tests.
3. Make sure lint and the fast tier pass locally. CI runs the full suite.
4. Describe what you changed, why, and what you could not verify.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
