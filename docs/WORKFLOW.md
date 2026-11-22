# Plans, tasks and evidence

Cairnmind separates three things that are easy to conflate: how sure we are about a finding, how far a task has progressed, and whether its tests are still valid for the current code.

| Field | Values | Meaning |
| --- | --- | --- |
| `status` | `proposed`, `confirmed`, `disputed`, `superseded` | Confidence in a finding. Only a person stores `confirmed` (see below) |
| `metadata.state` | `todo`, `in_progress`, `needs_test`, `needs_review`, `done`, `blocked` | Task progress |
| Verification read-out | `fresh`, `stale`, `reported`, `unknown` | Whether test evidence matches the current code |

## A whole plan in one transaction

Example `plan.json`:

```json
{
  "plan": {
    "title": "Login",
    "goal": "Verify login and wrong-password handling",
    "constraints": ["Keep sessions server-side"]
  },
  "steps": [
    {"key": "implement", "content": "Add password validation"},
    {
      "key": "test",
      "content": "Check correct and wrong passwords",
      "depends_on": ["implement"],
      "metadata": {"requires_test": true, "required_profiles": ["unit"]}
    }
  ]
}
```

```bash
cairnmind plan save shop ./plan.json --idempotency-key login-plan-1 --activate
cairnmind plan list shop
cairnmind plan next shop
cairnmind plan activate shop PLAN_ID --worktree ~/src/shop-worktree
cairnmind task list shop
cairnmind task update shop TASK_ID in_progress
```

Saving assigns stable identifiers, validates the complete final dependency graph and commits the header and every step atomically. Repeating a request key with the same content returns the original result; reusing the key for different content is a conflict. Editing an existing plan requires its `expected_version` and the expected versions of the changed steps in `task_versions`, either in the JSON or through `--expected-version` and `--task-versions FILE`. Removed steps stay in history. New steps cannot be declared done or verified just by saving the plan.

The active plan belongs to one project and worktree pair. The next step is chosen deterministically from the plan's steps, taking dependencies and state into account. `needs_test` and `needs_review` remain work to do, and blockers are explicit. Concurrent task updates check both the task version and the plan version.

Older `metadata.plan_id` values are plain group labels. New plans use `metadata.plan_ref` and `step_key`. Adopting an old group is an explicit operation:

```bash
cairnmind plan adopt shop login --idempotency-key adopt-login-1 --title "Login" --activate
```

## Named check profiles

Declare profiles in the project repository, in `.cairnmind/checks.toml`:

```toml
[checks.unit]
argv = ["uv", "run", "pytest", "tests/unit"]
cwd = "."
timeout_seconds = 600
output_limit_bytes = 10485760
extra_inputs = []
```

`argv` runs without a shell. `cwd` must stay inside the registered worktree. `extra_inputs` can explicitly include ignored input files or patterns relative to that directory. Time and output limits end the process as failed; logs and their snapshots are stored outside the repository.

```bash
cairnmind check run shop unit --task TASK_ID
cairnmind check list shop --task TASK_ID
```

A check runs in the **current worktree** with the user's permissions. It does not create an isolated, immutable checkout. The terminal UI and MCP show results; a check runs only through `cairnmind check run` with a profile name. **Run next stage** in the workspace runs that same command for the profile mapped to the `test` stage (see [Plan stages](#plan-stages)). There is no MCP tool for arbitrary commands.

An immutable run record binds the profile, the semantic task definition, and code fingerprints taken before and after the run. The fingerprint covers the content of tracked and non-ignored untracked files, file modes, deletions and symlink targets; the 2 MiB indexing limit does not apply. Incomplete or unstable inputs give `unknown` or a result that cannot count as fresh. The before/after comparison is the best available check of the working directory; it cannot detect every transient change reverted during the run, and it does not track external services or the environment.

Before running checks, make sure generated caches and outputs are ignored, for example `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage` and `htmlcov/`. A new non-ignored file created during a test changes the fingerprint and can make the result stale even with exit code 0. Declare genuinely ignored test inputs through `extra_inputs`.

Exit code 0 confirms that the configured command succeeded. It does not prove that every criterion is met, that coverage is sufficient or that a review happened. Completing a task that requires tests needs fresh results for every required profile; review and acceptance have separate evidence requirements.

`acceptance_passed` with `acceptance_source_ids` records an assessment of the task criteria. It is not proof of human acceptance. Changing a step's progress raises the plan version but does not change the semantic task definition; `check_status` decides whether results are fresh.

After a change to the code, the profile or the task content, a result can become `stale`. Earlier `done` progress stays in history and does not reopen tasks automatically. Legacy `test_passed` flags and source claims remain `reported`; migration never turns them into runner results. A model cannot create a machine result through `remember` or `checkpoint`.

## Plan stages

The state of the next step names the work it waits for: `todo` and `in_progress` are the `implement` stage, `needs_review` is `review` and `needs_test` is `test`. Per project, you can map `implement` and `review` to one of the project's assistants and `test` to a check profile, in the workspace under **More → Plan stages**. Any assistant the workspace can start can be chosen: Codex, Claude Code, the built-in API agent (OpenAI, Anthropic, an OpenAI-compatible server, Ollama) or GitHub Copilot. Nothing is mapped by default, so no stage starts a paid provider unless you chose that profile for that stage. Saving a stage stores the assistant slot together with the profile and client it named at that moment and, for the built-in API agent or Copilot, the provider, model and address the profile pointed at (never its credential reference or key). If the slot later names another profile (after **Replace connection**, or a change of the default assistants for a project without its own), or that agent profile's provider, model or address is edited in place, the stage starts nothing and says that its assistant changed since it was mapped and what changed, in words (the profile, the provider, the model, the address); **Plan stages** marks it "not confirmed", and saving the stage again accepts the new one. Codex and Claude Code profiles are compared by profile and client only. A map saved before this version stored only the slot: it still follows a Codex or Claude Code slot as before, but a stage whose slot now holds the built-in agent or Copilot has to be saved once before it starts. The map is an owner preference of this machine, kept with the other cockpit preferences (`project_stages` in `cockpit/preferences.json` of the data directory), not in the repository; the check profile itself is still read from the worktree's `.cairnmind/checks.toml` when it runs.

**Run next stage** reads the next step of the active plan for the open folder and does what is mapped to its stage:

- for `implement` or `review`, it starts that one assistant exactly as **Start work** does. The assistant gets a fresh resume packet and must acknowledge it before it writes. If that assistant is already running (or starting) in this folder, for example because `implement` and `review` name the same one, nothing is started or restarted and it receives no new packet; the message says so, and the next stage needs that session ended or another assistant mapped. If it cannot start (not installed, signed out), the reason is shown;
- for an `implement` or `review` stage mapped to the built-in API agent or Copilot, the same: **Run next stage** starts it as **Start work** does, in a panel of the workspace, with the profile's own key and limits (model calls, output tokens and command time; for Copilot, tool calls and turn time), and its journal is kept as for a run started by hand. It sends nothing to the model until you write to it in its panel; the agent then has to read its resume packet and acknowledge it before it can change a file or save anything, exactly as when you start it by hand. Before starting, the stage checks what can be seen without asking the provider: a profile whose key is missing, cannot be read or is set to text that is not a valid key (the same check the launcher makes; a Copilot profile that names a credential which is not available is told to provide it or to clear the reference, so that the signed-in GitHub account is used), and a Copilot runtime that is not prepared, are shown as such and nothing is started. Whether the key is valid, or the Copilot account signed in, shows only when the agent sends its first request;
- for `test`, it runs `cairnmind check run PROJECT PROFILE --task TASK --worktree DIR` and shows the result. The run is recorded like any other check run, under the same rules for fresh and stale evidence, and with the same session rule: inside a coding session the command refuses until that session has acknowledged its packet.

When nothing is mapped to the stage, when the mapped assistant is no longer configured for the project, or when it cannot be launched for a step, this is shown and nothing else is started in its place: a native client that fails never makes the stage start an API profile, and an API profile without a key never makes it start another assistant. Opening another project while a stage is being prepared cancels it, and a check result that arrives after that is recorded for the earlier project: it is listed in the workspace's **Notices** under that project. It is put on the notice line only if it arrives while that project is still shown, and like any notice it stays there until the next notice replaces it or another project is shown, wherever it was opened (it then stays in **Notices**). The router never changes a task, a plan or an acknowledgement: after a stage, the assistant or you move the step on with `task update` or through MCP (the built-in agent with its `remember` tool, after its ACK), and completing a step that requires tests still needs fresh check evidence. Because recording a step's new state raises the plan version, the session that reviews a step acknowledges the same plan at a later version than the session that implemented it.

```bash
cairnmind stage next shop
cairnmind stage next shop --worktree ~/src/shop-worktree --json
```

`cairnmind stage next` shows the same decision and starts nothing. For the `test` stage it prints the `cairnmind check run …` command. For an assistant it prints `cairnmind run …` only when that command does exactly what the map says: the assistant's profile sets no model, no other assistant of the project uses the same client, and the step belongs to the folder's active plan (`cairnmind run` knows no slot, profile or model, and a session reads the active plan). Otherwise it names the assistant and says to start the stage from the workspace with **Run next stage**; in `--json`, `command` is then `null` and `command_reason` is `profile_model`, `shared_client` or `inactive_plan`. The built-in API agent and Copilot have no such command (`cairnmind run` starts only Codex and Claude Code): `stage next` names the profile with its provider and model and says to start the stage from the workspace, with `command_reason` `agent_client`, or it says that the key is missing or the Copilot runtime is not prepared (`problem` is `credential_missing`, `credential_invalid`, `credential_unavailable`, `copilot_credential_unavailable` or `runtime_missing`; a stage whose slot names another profile than the one saved, or whose agent profile's provider, model or address changed, is `assistant_changed` with the fields in `changed`, and an agent stage saved before this version is `assistant_unconfirmed`). It reads the key's state from the running cockpit, which also holds keys saved only for its session, or else from the local key store; it never contacts the provider. Each request to the cockpit waits at most 5 seconds. When the cockpit answers with an error, the key counts as not usable (`credential_unavailable`), as in the workspace; when it does not answer at all, `credential` is `unknown` and the output says that the key and runtime could not be checked there. `GET /api/stage` of the cockpit gives the same decision, with `provider`, `model`, `credential` and `runtime`.

## MCP calls

Connected clients get `ask`, `remember`, `checkpoint`, `save_plan`, `next_task`, `resume_context`, `ack_resume` and the read-only `check_status`. A client started with `cairnmind mcp --client external` (Cursor, Cline, Zed) gets the same tools in an evidence-degraded session: what it saves is marked as written by an external session and never counts as verified (see [EXTERNAL_CLIENTS.md](EXTERNAL_CLIENTS.md)). Updating a step of a managed plan passes the real versions:

```json
{
  "project": "PROJECT_ID",
  "record": {
    "id": "TASK_ID",
    "expected_version": 2,
    "expected_plan_version": 3,
    "metadata": {"state": "needs_test"}
  },
  "sources": ["EXISTING_SOURCE_ID"]
}
```

Sources must belong to the same project. A conflict requires a fresh read, never an overwrite of newer work. A bound session must have acknowledged the current resume packet first. `save_plan` accepts the same header, steps, idempotency key and versions as the plan-saving API. `checkpoint` records state with the goal, results and next step:

```bash
cairnmind checkpoint shop ./resume-point.json
cairnmind remember shop "We use server-side sessions" --kind decision
```

Only a person can store a record as confirmed, and the person does it in the workspace: confirm the record in the inspector, or type it into the finding form. The command line confirms nothing (since 0.13.0): `--confirmed` is refused with exit code 2 and stores nothing, `cairnmind memory confirm-plan` and `cairnmind record-kind accept` are refused too, and `cairnmind task add` adds a proposed task and says so. MCP `remember`, `checkpoint` and `save_plan` always store proposed records; a request with `status: "confirmed"` is refused with HTTP 400. A record you confirmed is yours to change: a session, an MCP client or the built-in agent that tries to change its text, add a field beside it, give it another status or replace it through `supersedes` is refused with HTTP 400, and nothing is stored. The refusal tells the caller what to do: store what it found as a new proposed record that names yours in `metadata.possible_conflicts`, so that the pair waits for you in the conflict inbox (facts and decisions; for other kinds you dispute or confirm in the inspector). Progress on a confirmed task is stored as before. A plan save that changes or removes a step you confirmed is refused with the step named; dispute the step in the inspector first. A memory import is run by a person, outside a coding session; one that replaces records you confirmed or disputed is run from the workspace. The rule, the routes a person has and what the rule does not protect against are in [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#only-a-person-confirms-a-record).

## Plans and checks from the command line

Saving a plan writes the header, the steps and the final dependency graph in a single transaction. Codex and Claude see the same versions. An active plan is bound to a specific registered worktree. The launcher shows plans, the next step, blockers, evidence freshness and session package readiness.

```bash
cairnmind plan save my-app ./plan.json --idempotency-key login-plan-1 --activate
cairnmind plan next my-app
cairnmind plan list my-app
cairnmind task list my-app
```

The format of `plan.json`, with an example, is in [A whole plan in one transaction](#a-whole-plan-in-one-transaction).

Progress states are `todo`, `in_progress`, `needs_test`, `needs_review`, `done` and `blocked`. A separate `proposed`/`confirmed` status describes how certain a finding is. Only a person confirms: a coding session, an MCP client or the built-in agent stores a finding as proposed, and cannot change a record a person confirmed ([MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#only-a-person-confirms-a-record)). A `done` result remains part of the execution history; a later code change can turn its verification to `stale`. The plan format, check profiles and MCP calls are described on this page.

A step's state also names its stage: `implement` (`todo`, `in_progress`), `review` (`needs_review`) or `test` (`needs_test`). In the workspace, **More → Plan stages** maps each stage of a project to one of its assistants (Codex, Claude Code, the built-in API agent or GitHub Copilot), or `test` to a check profile, and **Run next stage** starts the mapped assistant like **Start work** (with its own resume packet to acknowledge) or runs the mapped check with `cairnmind check run`. It never moves a step on and never starts another assistant in place of a missing one or of the one you chose (a stage whose slot now names another profile waits until you save it again), and an API or Copilot profile without its key or runtime is shown as such with nothing started; `cairnmind stage next my-app` shows the same decision without starting anything. See [Plan stages](#plan-stages).

Declare check profiles in `.cairnmind/checks.toml`, for example `[checks.unit]` with `argv = ["uv", "run", "pytest"]`. Running a check is an explicit operation, with the CLI or with **Run next stage** for the profile mapped to the `test` stage:

```bash
cairnmind check run my-app unit --task TASK_ID
cairnmind check list my-app --task TASK_ID
cairnmind brief my-app
```

A check runs in the current worktree. Exit code 0 confirms that the given command ran successfully; review and acceptance still require a separate check. Legacy test flags are `reported` declarations, never automatically created run results.

`acceptance_passed` records a documented assessment of the task's criteria; the field alone does not prove human acceptance. In the pilot, the same checkpoint may contain the review and the assessment of all criteria. The plan version also increases when task progress changes, so a review saved before the move to `done` may refer to an earlier plan version; test freshness is checked by `check_status`.

## The code fingerprint

The code fingerprint is taken before every write that needs a current acknowledgement, for every question and around every check. It lists the worktree with Git and reads every listed file twice, so its cost grows with the size of the worktree: measured on a local Linux disk, 5,000 files of 4 KB take about 0.2 s and 5,000 files of 100 KB about 0.6 s. The content is always read on purpose. Remembering a file by its size and times would be faster but would miss an edit made through a memory mapping, which changes neither. On a Windows drive mounted in WSL every file operation is slower and the cost has not been measured; keep large generated files out of the worktree or in `.gitignore`.

The fingerprint also covers untracked files that are not ignored. Put generated caches and test output in `.gitignore` so that creating them during a check does not count as a change to the input code.

## Where work stopped

`brief` prints where work stopped in at most 40 lines: the latest checkpoint and its age, the next task and blockers of the active plan, the check evidence of tasks in progress, commits dated after the checkpoint was saved, and proposed findings waiting for review. It is built only from stored data and Git, asks no model and does not start the service. Below a title and a one-line legend, each line is marked `[machine]` when Cairnmind measured it now, or `[reported]` when it repeats a stored claim that was not verified; a stored title on a `[machine]` line always comes last, after `|`. A checkpoint stores no commit, so commits are selected by date: commits pulled later but dated before the checkpoint are not listed. Tasks are not counted among the proposed findings. The same brief is in the command palette as **Brief**; its text is English in both interface languages. `--json` prints the brief as an object with `project`, `worktree`, `generated_at`, `lines` (each with `basis` and `text`) and `text`.

## Token usage

`usage PROJECT --since 7d` sums the tokens the built-in API agent used, per UTC day, profile and model, from the session journals of that project; `--since` takes `Nd` or `Nh`. `--by week` groups by ISO week in UTC (`2026-W46`, Monday to Sunday; the first days of January can belong to week 52 or 53 of the old year) instead of by day; the row limit, the lower bounds and the totals work the same. Only the last usage report of each turn counts, because a turn reports running totals. A counter nobody reported stays `unknown`. A turn is complete only if every model call it started reported both counts; a row or total that holds a turn which is not complete is marked `incomplete`, its sums are lower bounds and are printed as `at least N` (JSON: `"tokens_are_lower_bound": true`). A cost appears only for a row in which every turn was complete and was priced by the profile's rates when it ran; it is an estimate, not an invoice, and quota left in a subscription is never guessed or turned into money. The report says `PARTIAL REPORT` above the rows (JSON: `"partial": true`, totals not complete, cost unknown) when the run registry cannot be read (`data.registry` is `ok`, `missing` or `unreadable`) or when sessions were skipped because their project cannot be determined. The usage is read from the session journals and nothing is written to them or to the memory; the command asks no model and starts no service. It reads the project, its stored Codex records and the ingestion inbox from the store, which needs the database engine running; unlike `brief`, it never starts the engine (see [Data and backups](STORAGE_AND_MAINTENANCE.md#data-and-backups)). Unreadable journal lines are skipped and counted. It covers the built-in API agent (client `api`) and Codex (client `codex`); Claude Code and Copilot sessions are not covered. Codex usage is read from the memory, not from rollout files: history import stores every rollout line as a source `transcript://codex/...` of the project it attributed the event to (by the event's working directory), and the `token_usage_record` lines of Codex 0.166 among them are the data. Attribution is import's own, and a rollout that was moved or deleted after import changes nothing. In such a record `usage` is one response and `thread_token_usage` the running sum of its thread, so the usage of the period is the sum of its responses, each counted once even if the rollout was imported from two paths; a response missing between two stored ones (the sum grew by more than the response says) or an unreadable record makes the rows incomplete. One Codex "turn" in the counts is one model response, and its profile and model are `unknown`. A cost is never computed for Codex (a subscription client): it is `unknown`, so the overall total has no cost when Codex rows exist and each client has its own total row. What the report does not see: rollouts that were never imported, and Codex usage records of the period still waiting in the ingestion inbox (older ones, and lines that only mention the name, do not count; only those of a working directory that belongs to this project or of none known); their number is shown and makes the report `PARTIAL` (`data.codex_waiting`). `--warn-over TOKENS` adds a first line `WARNING: ...` (JSON: a `warning` object with `threshold_tokens`, `observed_tokens` and `tokens_are_lower_bound`) when the input plus output tokens of the period, over all clients, are above `TOKENS`; the line says "at least" when the total is a lower bound. Without the option the threshold is `warn_over_tokens` under `[usage]` in `.cairnmind/checks.toml`, the file that holds the check profiles; that file is tracked in the repository, so adding or changing `[usage]` there makes existing check evidence stale like any other tracked change. If the default cannot be read (two checks files, a parse error, a file nested too deeply, a wrong `[usage]` value) the report is still printed, with the line `Warning threshold not read: <reason>` and the JSON key `warning_unavailable`; the option overrides the file and skips reading it. It is a warning only: nothing is blocked and the exit code does not change. `--json` prints the same data with the keys `project`, `since`, `timezone`, `from`, `to`, `partial`, `groups`, `totals`, `totals_by_client`, `data`, `cost_note`, `by` and `text`. Every row has a `period` (the day or the week) and a `client`; day rows also keep `day`, week rows have no `day`.

## Which check proves each criterion

A plan can say which check proves each acceptance criterion. Write a criterion as text, or as `{"text": ..., "check": "unit"}` to link it to a check profile; add `"argv": [...]` to propose the command of a profile that is not declared yet. Then:

```bash
cairnmind plan contract my-app
cairnmind plan contract my-app --write 3f9a1c0b7d2e
```

The first command lists every criterion of the active plan with how it stands (`machine_fresh` when a passed run of its check matches the current code, `machine_stale`, `machine_missing`, `machine_unknown`, or `manual_pending` and `manual_reported` for a criterion with no check) and shows, as a diff, the profiles it would add to `.cairnmind/checks.toml` from the proposed commands. To add them, repeat the command with `--write CODE`, where the code is printed under the diff and names exactly the proposal you looked at; if the plan or the file changed since, the write is refused. Adding profiles changes the worktree, so check runs made before count as stale. Neither form runs anything: a proposed command is text until you run the check yourself, and a profile that is already declared is never changed. `check_status` and `cairnmind plan next` report the same per-criterion list.

## The files a step means to touch

A plan step can also say which files it means to touch: `"scope": ["src/auth/**", "docs/login.md"]`, path patterns as in an ignore file. When the task enters `in_progress`, Cairnmind notes the state of every file of the worktree. From then on `cairnmind plan next`, `cairnmind plan contract`, `cairnmind brief`, `check_status` and `next_task` warn when a file that matches no pattern has changed since, and name it. It is a warning and never a block: a task can still be completed. When the task is done the comparison is made one last time and kept, so what other tasks change later is not counted against it.

What is compared is the content of the worktree, not Git's index: staging or committing a file changes nothing. A file that was already modified when the task started is not reported. Files that Git ignores are not seen, and neither are the files Cairnmind writes itself (`.cairnmind/`, `.mcp.json`, `AGENTS.md`, `CLAUDE.md`, the client configuration it installs). A task that has not started answers `not_started`; a task that started on another machine or in another worktree answers `unknown`, never "in scope", and a task that was finished elsewhere answers `not_recorded`. A task taken up again after it was done starts from a new note. Declaring a scope on a task that is already being worked on notes the tree at that moment. Unchanged files are recognised by size, times and inode without being read, so a change that leaves all of these untouched is not seen; a check run always reads every file. The note stays in the local data directory: it is in no snapshot and no backup.

## What a code change affects

After a code change, `cairnmind impact` lists the records that are linked to what changed since a commit, with the reason for each link:

```bash
cairnmind impact my-app --since HEAD~3
```

It compares the worktree with that commit, including uncommitted edits and files that Git does not track yet (ignored files and nested repositories are left out; a rename made without `git mv` or `git add` shows as a deleted and an added file). It follows two kinds of stored links: a record whose evidence names a changed file or a function, class, method, module constant or class field whose definition or decorators changed, and a record that cites an earlier snapshot of a changed file as its evidence. A link means "worth checking", not that the record is wrong. The command asks no model, starts no service and writes nothing, so repeating it duplicates nothing and it never changes a task's state. Which record is linked to what comes from the index, so index the project first if files changed since the last indexing. `--json` prints the same list with the changed files and symbols.

You can give each listed record a verdict: still valid, needs update (with a note on what to update) or not related. In the workspace, the **Impact** view of the memory inspector (`Ctrl+B i`) shows the list for the open project, compared with `HEAD` or a commit you type, with a filter for entries without a verdict, and **Verdict** saves one (see [TERMINAL.md](TERMINAL.md)); the memory service takes it at `POST /inspector/impact/verdict` only with the key of the open workspace, and `cairnmind impact-verdict` is refused since 0.13.0; the view reads the list from `GET /inspector/impact`, which takes the parameters of `impact` and answers what `--json` prints. A verdict is a person's note: only the person at the workspace gives one, and it never changes the record, its status, its revisions or a task's state. It is stored as an audit note (`review://impact/…`) that never counts as evidence and is never searched or quoted in answers. A later `impact` shows `checked: still valid (date)` under the entry instead of asking again, and `--pending` lists only the entries without a verdict. A verdict holds for the entry as it was listed: the record's text, its links to the changed code and the current content of that code. Editing the linked definition (or file) again, or the record's content, makes the entry unreviewed; so does a comparison with another commit that links the record to other changes, because that list asks a different question. Saving the same verdict again stores nothing; a different one replaces it and the earlier one is kept as history. A verdict is saved with the project memory, like a review note: it is a local change of the memory, so the next `memory snapshot` (or auto-save) writes a new package with it, and a newer package from another machine can be imported only after that.

The report also lists documentation pages that mention a changed definition, even when no record points at them: for `refresh_token()` changed, a page `docs/login.md` that says "renewed by refresh_token" is named, with the line. Documentation files are those ending in `.md`, `.mdx`, `.rst` or `.txt` that Git tracks or that are untracked and not ignored; `.memory/`, `.cairnmind/`, `.devbrain/`, `.git/` and the directories the indexer skips (`node_modules/`, `vendor/`, `.claude/`, ...) are never read, and neither are secret-looking files, links, files outside the worktree, files over 1 MB or files that are not UTF-8; the report counts the files it did not read. The names come from the parser, so only a definition the parser knows (in Python, JavaScript and TypeScript files: functions, classes and methods; in Python also module constants and class fields, an assignment to one name directly in a module or a class body, and parameters with a default other than None, named `function.parameter`; in JavaScript and TypeScript also interfaces, type aliases, enums and variables holding a function; in TOML files other than lock files every key, named `table.key` when it is in a table) whose text changed or that was removed is looked up; a class field, a parameter and a key in a table are named with a dot and are therefore not looked up in pages: added and moved definitions are not, and a changed file that cannot be compared contributes no names. A name must stand alone as an identifier (`refresh_token_v2` is not a mention of `refresh_token`). To keep the list worth reading, a name must be at least 4 characters long, even when written as code (dunder names such as `__init__`, `constructor` and `#private` members are skipped), and it must either contain an underscore or a capital letter after the first (`refresh_token`, `SessionStore`) or be written as code: after an odd number of backtick runs on the same line (a code span that continues over several lines is not recognised), in a fenced block of a Markdown file (the fence must start a line after at most three spaces; a fence indented further or inside a quote counts as text), or directly followed by `(`. So `login` in a sentence is not listed, while `` `login` `` and `login()` are. At most 50 pages are shown and at most 3 line numbers per page and name, and a run reads at most 2,000 files, stops reading once 20 MB have been read, and looks up at most 500 names; the report says how much was left out. A page that was itself changed in the same range is listed too and marked. As with records, a mention means "worth checking", not that the page is wrong. In the JSON, `documentation` is an object: `documents` holds the pages, and counters (`omitted_documents`, `omitted_lines`, `skipped_documents`, `not_scanned`, `names_not_searched`, `unavailable`) say what was left out or could not be done.