# Evaluation

Cairnmind ships four explicit evaluations. They use real local models and, optionally, real coding clients, so they are not part of the automated test suite.

| Command | What it checks | Needs |
| --- | --- | --- |
| `cairnmind eval run` | 20 golden recall questions in a disposable Git worktree and database. Pass: factual accuracy ≥ 0.9, every critical fact correct, every deterministic check true. | Ollama with the configured models |
| `cairnmind eval run --clients` | Adds a hand-off through a fresh Codex, Claude Code and a fresh Codex again, each reading and acknowledging the same memory. | Codex CLI and Claude Code signed in; uses their quotas |
| `cairnmind eval repository ROOT` | 20 questions with known answers about a real repository (for Cairnmind: `docs/evaluation/repository-questions.json`), asked in a copy indexed into a throwaway data directory and database. Counts complete, complete and right, complete and wrong. Pass: at least 80% complete and right. | Ollama with the configured models, a running engine |
| `cairnmind eval memory` | Extraction of a fact, a decision and a task; portable snapshot transfer between two isolated databases; cited recall in the new database; refusal to answer about a neighbouring project. | Ollama |

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

## Questions about a real repository

`cairnmind eval repository ROOT` needs ROOT to be the top of a Git worktree and indexes only the files Git tracks there: ignored files (secrets, build output, earlier reports) and links are never copied. The copy lives in a temporary directory readable only by its owner. Never indexed, so that the evaluation cannot read its own questions, accepted answers or reported results: the question file and everything under `docs/evaluation/` (the list is `NOT_INDEXED` in `src/cairnmind/repository_evaluation.py`).

Before it copies anything, the command checks that the models and the engine are there and answers `not_run` (exit 3) otherwise; it never starts an engine. Each run creates a database named `cairnmind_eval_<hex>` on the running engine, names it in the report (`isolation.database`) and drops it at the end, also after Ctrl+C; when it cannot drop it, the run fails and prints the name. A run that is killed outright can leave such a database behind. Cairnmind has no command to remove one; drop it through the engine, with the URL and the password from the `config.json` of your data directory: `curl -u root:PASSWORD -X POST ARCADE_URL/api/v1/server -d '{"command": "drop database cairnmind_eval_<hex>"}'`.

The results for Cairnmind's own repository are in [evaluation/RESULTS.md](evaluation/RESULTS.md), kept out of the indexed copy.

## Limits

These are synthetic fixtures with explicit facts. They show that the local 9B model can carry structured knowledge between sessions and clients; they do not prove recall quality on every real project. An acknowledgement proves that a model received and read back specific versions, not that it understood them.
