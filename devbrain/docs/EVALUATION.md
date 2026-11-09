# Evaluation

DevBrain ships three explicit evaluations. They use real local models and, optionally, real coding clients, so they are not part of the automated test suite.

| Command | What it checks | Needs |
| --- | --- | --- |
| `devbrain eval run` | 20 golden recall questions in a disposable Git worktree and database. Pass: factual accuracy ≥ 0.9, every critical fact correct, every deterministic check true. | Ollama with the configured models |
| `devbrain eval run --clients` | Adds a hand-off through a fresh Codex, Claude Code and a fresh Codex again, each reading and acknowledging the same memory. | Codex CLI and Claude Code signed in; uses their quotas |
| `devbrain eval memory` | Extraction of a fact, a decision and a task; portable snapshot transfer between two isolated databases; cited recall in the new database; refusal to answer about a neighbouring project. | Ollama |

Exit codes are 0 for passed, 1 for failed and 3 when a required model or client is unavailable (`not_run`). A missing model never counts as a pass.

## Results for 0.8.0

Measured on 9 November 2026 with `qwen3.5:9b` and `qwen3-embedding:0.6b` on an NVIDIA RTX 4080 Super under WSL2, Codex CLI 0.166.0 and Claude Code 2.1.319. Every run used its own data directory and engine, never a production installation.

| Evaluation | Before the 0.8.0 fixes | 0.8.0 |
| --- | --- | --- |
| Recall, 20 questions | 1.0 | 1.0 in 6 of 6 runs |
| Hand-off Codex → Claude Code → Codex | passed | not repeated |
| Portable memory | failed: no abstention | 3 of 3 runs on the final code |

The measurement found three problems, all fixed in 0.8.0:

- **Abstention.** Asked about information the project does not contain, the knowledge agent wrote a confident answer instead of `unknown`. The instruction now states exactly how to abstain.
- **Lost citation during repair.** The format-repair step could drop a verified citation, which cost one run a single recall case. Repair now carries over draft citations that already verify.
- **Decision classified as fact.** Extraction once labelled an explicitly chosen decision as a fact. The extraction instruction now defines each record kind.

## Limits

These are synthetic fixtures with explicit facts. They show that the local 9B model can carry structured knowledge between sessions and clients; they do not prove recall quality on every real project. An acknowledgement proves that a model received and read back specific versions, not that it understood them.
