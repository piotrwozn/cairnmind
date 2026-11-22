# Evaluation

Cairnmind ships five explicit evaluations. They use real local models and, optionally, real coding clients, so they are not part of the automated test suite.

| Command | What it checks | Needs |
| --- | --- | --- |
| `cairnmind eval run` | 20 golden recall questions in a disposable Git worktree and database. Pass: factual accuracy ≥ 0.9, every critical fact correct, every deterministic check true. | Ollama with the configured models |
| `cairnmind eval run --clients` | Adds a hand-off through a fresh Codex, Claude Code and a fresh Codex again, each reading and acknowledging the same memory. | Codex CLI and Claude Code signed in; uses their quotas |
| `cairnmind eval repository ROOT` | 20 questions with known answers about a real repository (for Cairnmind: `docs/evaluation/repository-questions.json`), asked in a copy indexed into a throwaway data directory and database. Counts complete, complete and right, complete and wrong. Pass: at least 80% complete and right. | Ollama with the configured models, a running engine |
| `cairnmind eval memory` | Extraction of a fact, a decision and a task; portable snapshot transfer between two isolated databases; cited recall in the new database; refusal to answer about a neighbouring project. | Ollama |
| `cairnmind eval extraction` | What extraction proposes from conversations: invented transcripts of the built-in agent, Claude Code and Codex (`docs/evaluation/extraction-transcripts.json`) with the records a person would keep marked, run three times by default. Counts kept, duplicate and rejected proposals. Pass: every kept record found in every run and no proposal from a protocol or launcher message. Nothing is stored. | The configured chat model on its server |

Exit codes are 0 for passed, 1 for failed and 3 when a required model or client is unavailable (`not_run`). A missing model never counts as a pass.

The evaluations use the models the knowledge agent is configured with, on their servers (`cairnmind models set`, see [API.md](API.md#the-knowledge-agents-models)). A passing `eval run`, `eval memory` or `eval repository` is recorded in `models.evaluated.json` in the data directory, for exactly the pair of model identities and the setup it ran with: each role's server, the chat context window and the reasoning effort (`"models_evaluation_recorded": true` in its output). The `eval memory` report says `"inference": "local_ollama"` with the default local Ollama, and otherwise `"configured_model_servers"` with `model_servers`, which names each role's kind of server and whether its content left this computer. A pair counts as evaluated once `eval run` and `eval memory` have both passed with it; `cairnmind doctor --json` shows the record, and answers given with other models than the release's are labelled `unevaluated` until then. After changing only the chat model, run both again; after changing the embedding model, run `rebuild-vectors` first.

## Running the evaluations

```bash
cairnmind eval run
cairnmind eval run --clients
cairnmind eval run --clients --skip-claude
cairnmind benchmark my-app "Where do we handle login errors?"
```

`eval run` exercises 20 scenarios against a real Ollama in a temporary repository and a separate ArcadeDB database. The optional `--clients` flag starts a fresh Codex, then Claude, then Codex again, using existing logins and consuming their quotas. A missing model, client or login yields `not_run`. The report gives accuracy, state errors, latency, the local model's context size, and token counts only when the provider actually reports them.

`--clients --skip-claude` checks only two fresh Codex sessions and does not start a Claude process. The report labels this as a partial test and does not count it as a full cross-client handoff. Codex tool approvals cover only the four operations of the `cairnmind_eval` server against the temporary database; the sandbox stays `read-only`.

`benchmark` times the first answer to a question against the cached repeat. It measures latency only and does not replace a quality evaluation.

```bash
cairnmind eval memory
```

This is an explicit test against a real Ollama: extraction of a fact, a decision and a task, transfer between two isolated databases, and reconstruction of a sourced answer. It requires all expected findings; an empty extraction result fails the scenario. It does not start Codex or Claude. Exit codes: `0` success, `1` failure, `3` required models missing.

## Results for 0.12.0

The evaluations of the release gate, run once each on 18 November 2026 on the code of the release (b20a79f, with the version set to 0.12.0), with `qwen3.5:9b` (digest 56671c2a…) and `qwen3-embedding:0.6b` (digest ac6da0df…), the same models as 0.11.0, on an NVIDIA RTX 4080 Super under WSL2 with Python 3.12.3 and the ArcadeDB 26.11.1 engine of the distribution, default settings (`ask_timeout` 60). Each command had its own temporary data directory and engine, never an installation. The hand-off with real clients (`eval run --clients`) was not run. `eval repository` indexed a clone of b20a79f. Unlike the 0.11.0 runs, the GPU was shared with a running Cairnmind service.

| Command | Result | Time |
| --- | --- | --- |
| `cairnmind eval run` | passed: 20 of 20, factual accuracy 1.0, every critical fact and deterministic check true | 101 s |
| `cairnmind eval memory` | passed: extraction of the fact, decision and task, portable transfer, cited recall in the second database, abstention about the neighbouring project | 36 s |
| `cairnmind eval extraction --runs 3` | passed: three identical runs, 28 proposals, 8 rejected or duplicate (29%), 15 of 15 kept records found | 122 s |
| `cairnmind eval repository .` | passed: 19 of 20 complete and right, none complete and wrong (440 files, 2,383 chunks indexed) | 320 s |
| `cairnmind eval repository . --questions docs/evaluation/absent-questions.json` | failed (exit 1): 20 of 21 questions about things the project does not have refused, none failed, one answered complete and wrong | 421 s |

Recall, memory and extraction gave the same results as 0.11.0; the extraction prompt is unchanged. In `eval repository .` the question about the sample project's memory was withheld as unsupported: the answer cited only a test file, where in 0.11.0 it also cited `examples/sample-project/README.md`. Two more runs of `eval repository .`, outside the gate, gave 19 and 19, each with one answer withheld and none wrong, so the worst of three runs is 19, as on 0.11.0's final development tree. Each question took two model calls, as in 0.11.0. The absent set failed in the same way as on 0.11.0: the question about the default nginx port, with the name in lower case, was answered with the port of Cairnmind's own service (the deferred part of #245). In the same set, the question about the PostgreSQL version was refused, but the text of its partial answer states a version requirement the project does not have; the scoring does not read that text. Every command took 10 to 15% longer than on 0.11.0, extraction included. The shared GPU is the likely cause; this was not checked on an idle GPU. The two repository commands also indexed a larger tree than on 0.11.0: 440 files and 2,383 chunks instead of 423 and 2,318.

## Results for 0.11.0

The evaluations of the release gate, run once each on 16 November 2026 on the code of the release (46095ba, with the version set to 0.11.0), with `qwen3.5:9b` (digest 56671c2a…) and `qwen3-embedding:0.6b` (digest ac6da0df…) on an NVIDIA RTX 4080 Super under WSL2, default settings (`ask_timeout` 60). Each command had its own temporary data directory and engine, never an installation. The hand-off with real clients (`eval run --clients`) was not run.

| Command | Result | Time |
| --- | --- | --- |
| `cairnmind eval run` | passed: 20 of 20, factual accuracy 1.0, every critical fact and deterministic check true | 89 s |
| `cairnmind eval memory` | passed: extraction of the fact, decision and task, portable transfer, cited recall in the second database, abstention about the neighbouring project | 31 s |
| `cairnmind eval extraction --runs 3` | passed: three identical runs, 28 proposals, 8 rejected or duplicate (29%), 15 of 15 kept records found | 106 s |
| `cairnmind eval repository .` | passed: 20 of 20 complete and right, none complete and wrong (423 files, 2,318 chunks indexed) | 287 s |
| `cairnmind eval repository . --questions docs/evaluation/absent-questions.json` | failed (exit 1): 20 of 21 questions about things the project does not have refused, none failed, one answered complete and wrong | 384 s |

The recall and extraction figures equal the earlier ones (1.0 for 0.8.0; the shipped row of the extraction table below). The three runs of `eval repository .` on the final tree of this release's development gave 19, 19 and 20 ([evaluation/RESULTS.md](evaluation/RESULTS.md)); this one run gave 20. The absent set is not one of the four commands of the gate in [RELEASING.md](RELEASING.md), and its result is the one recorded on the merged tree: the question about the default nginx port, with the name in lower case, was answered with the port of Cairnmind's own service. The subject check of #245 does not read a lower-case name; that part of #245 is deferred.

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

## Extraction from conversations

`cairnmind eval extraction [--transcripts FILE] [--runs N]` gives every message of the evaluation set that the product would extract to the production extraction (the same instruction, schema, repair and checks of quotes) and compares the proposals with the records a person would keep. A proposal that matches a kept record of its own message is kept, one that matches a kept record of another message of the same conversation is a duplicate, and any other one is rejected; a person rejects duplicates too. The figure is the share of proposals a person would reject. Matching uses regular expressions over the proposed text: a kept record's `match` must all occur and none of its `reject` (a request to the assistant or a guess that names the same thing), so read the records in the report as well. The run fails when a kept record is missed, when a protocol or launcher message gives a proposal, or when the extraction of any message failed, because that message was not measured. The report's `extractor` is the stamp the extraction job writes on every record with the same build and model (extractor version, prompt hash and model as name@digest, see [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#how-an-extracted-record-was-made)), so a result here can be matched with records in real projects. The run uses the chat model's server exactly as the extraction job does, also with a model profile set by `cairnmind models set`, and its report says `"inference"` as `eval memory` does.

The set is invented and contains nothing private: the protocol a session goes through (reading and acknowledging the resume packet), questions of the moment answered with a tool, the assistant describing its own actions, the launcher's first message and Codex's environment block, mixed with decisions, constraints, tasks, a finding and an open question stated in conversation, one conversation in Polish, and one project about the resume protocol itself, where the names of the protocol tools are project knowledge.

Measured on 14 November 2026 with `qwen3.5:9b` on an NVIDIA RTX 4080 Super under WSL2: 40 extracted messages, 15 kept records, three runs of each variant (#160). The product asks the model at temperature 0, and the three runs of each variant gave identical results.

| Variant | Proposals | Rejected or duplicate | Share | Kept records found |
| --- | --- | --- | --- | --- |
| 0.10.0 extraction | 55 | 36 | 65% | 15 of 15 |
| A rule for conversation messages | 32 | 14 | 44% | 14 of 15 |
| The rule, keeping problems the person reports | 43 | 23 | 53% | 15 of 15 |
| Protocol messages and tool calls left out | 36 | 17 | 47% | 15 of 15 |
| Also every assistant message that calls a tool left out | 27 | 11 | 41% | 14 of 15 |
| A higher bar for the assistant's messages | 51 | 32 | 63% | 15 of 15 |
| The first rule and protocol left out | 29 | 11 | 38% | 14 of 15 |
| The first rule, protocol left out and the higher bar | 26 | 8 | 31% | 14 of 15 |
| The rule (keeping problems) and protocol left out | 31 | 11 | 35% | 15 of 15 |
| **The rule, protocol left out and the higher bar (shipped)** | **28** | **8** | **29%** | **15 of 15** |

The first rule, alone or combined, lost a problem the person reported together with a request to find its cause, and leaving out every assistant message that calls a tool lost a finding the assistant reported in the same message; neither was shipped. With the shipped change no proposal comes from a protocol message, the launcher's message, a one-off question or the assistant's narration of its actions. The eight left come from messages that also hold kept records: three guesses the text does not state (one inferred from "before the beta", two marked "implied"), two requests to the assistant for that conversation ("Run ruff on the src directory", "Run the linter after switching to Alembic"), the assistant's next step, and two restatements of the person's task. Read by eye, the 20 scored kept are records a person would keep, though two pairs say nearly the same thing (where originals are stored, and the reported report bug as a fact and as a task), so a person would reject about 8 to 10 of 28 (29% to 36%) against about 36 to 38 of 55 (65% to 69%) for 0.10.0.

The scoring was made stricter after a review, with no new model run: requests and guesses that named a kept record had first counted as kept, which gave 62% for 0.10.0 and 14% for the shipped change. The shipped change was run again through the command after that (two runs, the same 28 proposals and 29%).

As a stress test the 0.10.0 and the shipped extraction were also run three times at temperature 0.8, which the product does not use: the 0.10.0 extraction rejected 62%, 68% and 63% and missed the reported problem in two runs; the shipped one rejected 39%, 39% and 30% and missed it in one run.

## Questions about a real repository

`cairnmind eval repository ROOT` needs ROOT to be the top of a Git worktree and indexes only the files Git tracks there: ignored files (secrets, build output, earlier reports) and links are never copied. The copy lives in a temporary directory readable only by its owner. Never indexed, so that the evaluation cannot read its own questions, accepted answers or reported results: the question file, everything under `docs/evaluation/`, and the tests of this evaluation, of the definition lookup and of the question budget (`tests/test_repository_evaluation.py`, `tests/test_definitions.py`, `tests/test_ask_budget.py`), whose fixtures imitate the evaluated questions with other values (the list is `NOT_INDEXED` in `src/cairnmind/repository_evaluation.py`).

Before it copies anything, the command checks that the models and the engine are there and answers `not_run` (exit 3) otherwise; it never starts an engine. On a [connect-only installation](DEPLOYMENT.md#connect-only-installations) `eval run`, `eval memory` and `eval repository` are refused in one sentence (exit 2), because they create and drop databases, which such an installation never does and its database user may not; run them on an installation with the engine on this machine. Each run creates a database named `cairnmind_eval_<hex>` on the running engine, names it in the report (`isolation.database`) and drops it at the end, also after Ctrl+C; when it cannot drop it, the run fails and prints the name. A run that is killed outright can leave such a database behind. Cairnmind has no command to remove one; drop it through the engine, with the URL and the password from the `config.json` of your data directory: `curl -u root:PASSWORD -X POST ARCADE_URL/api/v1/server -d '{"command": "drop database cairnmind_eval_<hex>"}'`.

The results for Cairnmind's own repository are in [evaluation/RESULTS.md](evaluation/RESULTS.md), kept out of the indexed copy.

A question file can also hold questions about things the repository does not have: `"answer": {"absent": true}` in place of a file and an accepted answer. Such a question is right only when it was refused: its answer is not complete and the ask did not simply break (a timeout, an unavailable model, a reply that is not JSON, a broken tool protocol or a spent budget says nothing about the question). Any complete answer to it counts as complete and wrong, and a file with such questions passes only when every one of them was refused (the report counts them as `absent`, `absent_refused` and `absent_failed`). Cairnmind's own set is `docs/evaluation/absent-questions.json`, twenty-one questions with the shapes of the twenty and the name of a product or a setting the project never uses, three of them written in a way the subject check does not read:

```bash
cairnmind eval repository . --questions docs/evaluation/absent-questions.json
```

Every complete answer to a question asked without an answer schema goes through three checks after its citations are verified: the subject check (no model; a name of the question that no quoted source has withholds the answer), the support check (one model call) and the names call (one more model call, issue #245). The names call lists the product and technology names the answer asserts and asks whether the quotes state each; a name they do not state, and a call that fails or returns something unreadable, ends the answer as partial with the reason, never rewritten. It is the one check that reads a lower-case name such as nginx, and it costs **one more model call per complete answer**; it is never made for a partial or unknown answer, for a question asked with an answer schema, or in deterministic-only mode. A deterministic check of the names an answer introduces (`unnamed_answer_names`, run after the support check and before the names call, support_check `names_missing`) is built too and stays off (`CHECK_ANSWER_NAMES`) until `scripts/replay_subject_check.py`, run over kept repository reports with the clone that has their commits, shows that it withholds no right answer. Known gaps: a name the model leaves out of its list, a name a quote only resembles, and the partial answer that carries the model's own text. `docs/evaluation/subject-questions.json` is the question set built for these checks (`cairnmind eval repository . --questions docs/evaluation/subject-questions.json`), and [evaluation/RESULTS.md](evaluation/RESULTS.md) holds the figures before and after, marked NOT MEASURED YET until they are run.

A question whose first pass finds no answer, while a passage was shown to the model only in part, gets one more pass over the best passages read whole, inside the same time limit and without tools. Only a complete answer of that pass, validated and judged like any other, replaces the first result. The answer's diagnostics then say `second_look` (`answered` or `no_answer`), and the report counts the extra calls in that question's `model_calls`.

To measure another chat model, choose it, run the evaluation and choose the earlier model again. The evaluation writes to its own throwaway copy and database, and a running service keeps the models it started with:

```bash
ollama pull MODEL_TAG
cairnmind models set --role chat --provider ollama --model MODEL_TAG --context-window 32768
cairnmind eval repository .
cairnmind models set --role chat --provider ollama --model qwen3.5:9b
```

`--context-window` is optional; without it the model runs with the context size of the settings. One run is one sample: an unchanged tree gives figures up to two or three questions apart from run to run, so compare models by the worst of three runs, and count the complete and wrong answers as well as the right ones. The local models that were tried on this repository, with their time per question and graphics memory, are listed on the results page.

## Replaying logged answers

The answers of your own projects can be kept and asked again. `cairnmind ask PROJECT QUESTION --log` keeps one answer; `"answer_log": true` in the `config.json` of your data directory (read when the service starts) keeps every answer the service gives. Each entry holds the question, the worktree, the answer contract, the commit, branch and code fingerprint, the status with its diagnostic code and support check, the answer, every cited source (id, address, sha256, quote and lines), the chat model with its digest, the embedding model and the timings. The service's token and database password are replaced wherever they occur. The entry is written after the answer is sent.

The log is `answer-log/PROJECT_ID/answers.jsonl` in the data directory, readable only by its owner. It is not in the database: a logged answer is never searched, quoted or cited, and it is not part of a memory snapshot or a backup. `cairnmind forget --delete-project` deletes it with the project. Past 8 MiB the oldest entries are dropped until the log fits in 4 MiB; an entry over 256 KiB is not kept. `cairnmind answers list PROJECT`, `answers show PROJECT ID` and `answers prune PROJECT --keep N` read and trim it.

`cairnmind eval replay PROJECT [--limit N]` asks the newest logged answer of each distinct question again through the running service, with the same worktree and contract, and writes a report next to the other evaluations. It compares the status and the set of cited source addresses, never the wording: `regressed` (was complete, is not), `lost_sources` (no longer cites a source it cited), `improved`, `status_changed`, `sources_added`, `unchanged`, and `not_asked` when the service refused the question. A source whose address stayed but whose content changed is listed under `changed_sources`. The run fails (exit 1) on any regressed, lost or refused answer, and answers `not_run` (exit 3) when nothing was logged. The replayed answers are not logged.

## Limits

These are synthetic fixtures with explicit facts. They show that the local 9B model can carry structured knowledge between sessions and clients; they do not prove recall quality on every real project. An acknowledgement proves that a model received and read back specific versions, not that it understood them.
