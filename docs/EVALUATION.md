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

## Questions about Cairnmind's own repository

`cairnmind eval repository .` with the 20 questions of [evaluation/repository-questions.json](evaluation/repository-questions.json), measured on 13 November 2026 at commit 333c692 (the code of this release; only this page changed after it) with `qwen3.5:9b` (digest 56671c2a…) and `qwen3-embedding:0.6b` (digest ac6da0df…) on an NVIDIA RTX 4080 Super under WSL2. Each run indexed its own copy into its own data directory and database (242 files, 1,307 chunks) and took about 5 minutes.

| Run | Complete | Complete and right | Complete and wrong | Not complete |
| --- | --- | --- | --- | --- |
| 1 | 16 | 16 | 0 | 4 |
| 2 | 17 | 17 | 0 | 3 |
| 3 | 17 | 17 | 0 | 3 |

Complete and right: 16 to 17 of 20 (worst run 16, best 17). Complete means that every quote is a literal passage of the current source and that the support check agreed; right means that the expected value, read from the code at run time where it can change, appears in the answer. A complete answer without it would count as wrong; none did in these runs. A fourth run stopped while indexing with a database error before it asked anything and is not counted; it was repeated.

Not complete in every run: the Python version (stated only in `pyproject.toml`; the answer was refused as unparseable, not literal or `unknown`), the Java heap (`-Xmx1024m` is a literal inside a function; the quotes were not literal, or not accepted by the support check) and the journal's event limit (twice the model answered from the neighbouring constant `NEW_FILE_EVENTS_PER_TRANSACTION`, which the support check rejected; once it found no evidence). Run 1 also lost the service port to a quote that was not literal.

What the support check does and does not catch, as measured while this was built: it rejects a value quoted from a test, a lock file or a comment that only mentions a number ("far below the number at which …"), and lets through a value of a neighbouring setting or a number used for something else, such as a display limit. The questions above did not need it to tell those apart in these runs, because the definition of the asked setting was shown to the model; on another repository it may.

## Limits

These are synthetic fixtures with explicit facts. They show that the local 9B model can carry structured knowledge between sessions and clients; they do not prove recall quality on every real project. An acknowledgement proves that a model received and read back specific versions, not that it understood them.
