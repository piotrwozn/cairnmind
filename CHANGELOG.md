# Changelog

All notable changes are listed here, newest first. Releases up to 0.8.0 were published as DevBrain.

## Unreleased

- `cairnmind usage --by week` groups the usage by ISO week in UTC (`2026-W46`) instead of by day; the text says that weeks are ISO weeks in UTC. Every JSON row has a `period` next to the `day` of day rows (week rows have no `day`), and the JSON has `by`. The 60-row limit, the lower bounds and the totals are unchanged. `usage` also reports Codex (rows have a `client`, `api` or `codex`): its token counts are read from the transcript lines that history import stored for the project (so the project is the one import attributed each event to, and a moved rollout changes nothing), each response counted once; Codex records still waiting in the ingestion inbox make the report partial, and no cost is ever computed for Codex. Claude Code is not covered. `--warn-over TOKENS`, or `warn_over_tokens` under `[usage]` in `.cairnmind/checks.toml` (a tracked file, so changing it makes check evidence stale), adds a `WARNING:` line (JSON: `warning`) when the period's input plus output tokens pass it, saying "at least" for a lower bound; nothing is blocked. If the default threshold cannot be read, the report is still printed with a `Warning threshold not read` line (#9).

- `cairnmind engine start` waits up to 30 seconds, twice, for Java to answer `-version` (it was 5 seconds, once), so a first JVM start on a cold or busy machine no longer fails. When Java still does not answer, or cannot be run, the command says so in one line instead of showing a traceback (#112).
- `Ctrl+B n` in the workspace opens a small form to save a finding without leaving it: text, kind (decision, fact, constraint, question or preference) and status (proposed or confirmed). `Ctrl+S` saves and the status line confirms; `Esc` closes without saving, and a failed save keeps what you typed. Saving again after an unclear answer never stores the finding twice. The record belongs to the whole project and is written by the memory service with a source of its own, `workspace://finding/…` with the time and the origin `workspace_form`, instead of an anonymous `manual://` note. Nothing is read from a terminal pane (#14).
- `cairnmind usage PROJECT --since 7d` sums the tokens used by the built-in API agent per UTC day, profile and model, from the session journals. Only the last report of each turn counts. A counter nobody reported stays `unknown`; a row or total with a turn that did not report full usage is marked incomplete and shows its sums as lower bounds ("at least N"). A cost is shown only as an estimate, never an invoice, and only where every turn was complete and priced by the profile's rates; a report built on an unreadable run registry or with skipped sessions is marked partial. Asks no model, starts no service and writes nothing to the journals or the memory; it opens the store to resolve the project name. Built-in API agent only (#9).
- `cairnmind impact` also lists documentation pages (`.md`, `.mdx`, `.rst`, `.txt`) that mention a definition the parser knows (in Python, JavaScript and TypeScript files: functions, classes and methods; in JavaScript and TypeScript also interfaces, type aliases, enums and variables holding a function) whose text changed or that was removed, with up to 3 line numbers each and at most 50 pages. Added and moved definitions are not looked up, nor are names under 4 characters; a plain word that is not distinctive counts only when written as code. A mention means "worth checking", not that the page is wrong. Read-only, as before; the JSON gets a `documentation` object with `documents` and counters of what was left out (#8).
- A sample project memory in `examples/sample-project/`: decisions with their sources, one of them replaced by a later one, a plan with three steps and a check run. `cairnmind memory show` and `memory inspect` read it with only Python and uv installed, on Linux or WSL2. The walkthrough says what that view shows and what it leaves out (#36).
- First indexing sends about 13 database requests per file instead of 47: source, chunks and jobs of new files are written together, up to eight files in one transaction, and the code graph reads once what it used to look up row by row. Measured on 5,000 files: 43 s instead of 257 s, and 391 s with 5 ms added to every request, as for a database on another machine. What is stored and what the journal records is unchanged (#71).
- Faster database access: the connection to the database is kept open between requests instead of being opened for every statement, and lookups by source, file path, edge end, task and session have indexes. Measured on a project of 5,000 files: first indexing 257 s to 182 s, indexing again after every file changed 532 s to 334 s. The indexes are created when a database is first opened by this version; the schema version is unchanged (#71).
- A plan step can declare `scope`, the path patterns it means to touch. Once the task is `in_progress`, `cairnmind plan next`, `cairnmind plan contract`, `cairnmind brief`, `check_status` and `next_task` warn about files changed outside it since the task started, by name. A finished task keeps the answer it had when it was done. A warning only: nothing is blocked, and the note of the starting state never leaves the machine (#11).
- An acceptance criterion can name the check that proves it: write it as `{"text": ..., "check": PROFILE}`. `check_status`, `cairnmind plan next` and the new `cairnmind plan contract PROJECT` list each criterion as `machine_fresh`, `machine_stale`, `machine_missing`, `machine_unknown`, `manual_pending` or `manual_reported`. `plan contract --write CODE` adds check profiles from commands proposed with the criteria; the code is printed with the diff and names exactly the proposal that was shown. It runs nothing and never changes a declared profile (#13).
- The names of running processes are `cairnmind` now: terminal sessions and their sockets, the identity the service and the broker announce, cookies and temporary files. Whole-database backups are written as `.cairnmind.json.gz`; `.devbrain.json.gz` files are still restored. Because a new version does not find a workspace started by an earlier one, the installer stops the broker and the service of the installed version and refuses to continue while a workspace is running (#40).
- **The names a project shares are `cairnmind` now.** The MCP server in `.mcp.json` and `.codex/config.toml`, the markers in `AGENTS.md` and `CLAUDE.md`, snapshots in `.memory/cairnmind/` with the format name `cairnmind-portable-memory`, and check profiles in `.cairnmind/checks.toml`. `cairnmind install` converts what an earlier version wrote where it stands, keeping tool approvals; snapshots in `.memory/devbrain/` and profiles in `.devbrain/checks.toml` are still read. Versions up to 0.9.0 do not read the new names, so everyone who shares a repository upgrades together; see [docs/UPGRADING.md](docs/UPGRADING.md) (#40).
- Environment variables are spelled `CAIRNMIND_*`. The earlier `DEVBRAIN_*` spelling is still read; for a configuration variable the new one wins when both are set, and `cairnmind doctor` reports a pair that disagrees. A new installation keeps its data in `~/.local/share/cairnmind`; an existing `~/.local/share/devbrain` is used where it is and is never moved or copied. The launcher gives coding clients both spellings of the session variables, so project files written by earlier versions keep working (#40).
- `cairnmind impact PROJECT --since COMMIT` lists the decisions, facts and tasks linked to the files and symbols changed since a commit, each with the reason for the link: the record's evidence names the changed file or definition, or the record cites a snapshot of the changed file. It is read-only and asks no model (#8).
- After an edit, indexing opens only the files whose size, times or inode changed since this process last indexed them, and writes only the graph rows that differ instead of replacing the whole graph. On a project with 5,000 files the indexing part of the first question after editing one file fell from 159 s to about 1 s. The first indexing after the service starts still reads every file (#62).

## 0.9.0

The first release under the name Cairnmind.

- The Python module is `cairnmind`; `devbrain` remains as an import alias for one release, so hooks and MCP entries that start `python -m devbrain` keep working wherever the Python they name is still installed. After the installer has replaced the earlier `devbrain` uv tool, run `cairnmind install <project>` for each project as the installer lists them; that also rewrites the entries to the new module name. Environment variables, the data directory and the portable memory format keep their `devbrain` names for now (#40).
- The code fingerprint behind acknowledgements, questions and check evidence no longer resolves the path of every file: whether a directory lies inside the worktree is decided once per directory. On 5,000 files it fell from 0.45 s to 0.18 s, and from 0.91 s to 0.57 s when the files are 100 KB each. File content is still always read, so any edit changes the fingerprint; the README documents the cost for large worktrees (#24).
- A question no longer re-indexes an unchanged project: the tree is compared by file sizes and times without opening the indexed files, and the answer cache stamp is a change counter instead of a scan of seven collections. On a project with 5,000 files the indexing part of a question fell from 52 s to 0.05 s. The background scan of the service skips unchanged projects the same way. A question still hashes the worktree once for its code identity (#24), and after any edit the whole project is indexed again (#62). A directory that cannot be listed is now reported instead of having its files deactivated (#23).
- The help page of every command and the messages written in the command line module are in English. Messages raised by the workspace, cockpit and service modules are still Polish (#37).
- `cairnmind doctor --contract --format text` prints one aligned line per client and check: `ok` or `failed`, with the evidence line or with what was needed and found, and a `verified_resume` line per client. A failed check now also records what it found (the line that says otherwise, or the exit code of the command), in JSON as well. JSON stays the default (#38).
- The `mcp` capability check no longer accepts the flag example `--debug='!hooks,!mcp'` in a client's help text as the `mcp` command.
- **Conflicts** view in the memory inspector: pairs of facts or decisions that extraction marked as possibly contradictory are shown side by side with evidence times, scope and sources. You resolve a pair by letting one finding supersede the other, by keeping both for different scopes, or by dismissing the alarm; every decision is saved with an audit note and no source is removed (#7).
- The audit note of a review or of a conflict decision no longer counts as evidence time. A record gets the corrected time the next time it is written; the Conflicts view reads the times from the sources directly.
- The memory inspector shows when a record's evidence was stated or observed, and warns when a record replaced a finding with newer evidence (#39).
- Records have a derived evidence state: `anchored`, `evidence_replaced` (with whether a verified quoted passage survives), `orphaned`, or `unverified` for imported file evidence before local indexing. The memory inspector shows it, a newly prepared resume packet warns about orphaned and changed records without blocking the resume, and `cairnmind memory orphans PROJECT` lists them. The state is computed when read and is not stored, so snapshots and the portable state hash do not change (#6).
- `cairnmind brief PROJECT` and **Brief** in the command palette show where work stopped in at most 40 lines: checkpoint and its age, next task and blockers, check evidence of tasks in progress, commits dated after the checkpoint and proposed findings. No model is asked and the command does not start the service; every line below the title and the legend is marked `machine` or `reported` (#3).
- `cairnmind memory show PATH` prints a portable snapshot's records and plans with their source addresses as text, with `--kind`, `--status` and `--grep` filters. It needs no Java, engine, model or service and writes nothing; inside a coding session the running service authorizes the package first (#2).
- A step-by-step installation guide is in [docs/INSTALL.md](docs/INSTALL.md), and the installer's messages and help are in English. When it replaces DevBrain, the installer stops the earlier knowledge service and interface broker before it removes the earlier tool.
- The package moved to the repository root: after cloning, run the installer and the tests from the top-level directory. The contract documents are in `docs/`.
- **Renamed to Cairnmind.** The repository moved to `piotrwozn/cairnmind`, the package is `cairnmind` and the command is `cairnmind`; `devbrain` remains an alias for a transition period. The installer removes an earlier `devbrain` uv tool, keeps your memory, and lists the `cairnmind install <project>` commands that reconnect existing projects. Data paths, environment variables and the portable memory format keep their `devbrain` names until a compatible migration (#40).
- Records carry the time of their evidence (`evidence_earliest`, `evidence_latest`, `evidence_basis`) and a warning when older stated evidence supersedes newer (#4).
- Search ranks records by status: at similar relevance a confirmed finding comes before proposed and disputed ones (#5).

## 0.8.0

- **Focus** view is now the default: one view across the full terminal width. The multi-pane **Cockpit** layout remains available as an option.
- English is the default interface language; **More → Settings → Language → Polski** switches to Polish.
- Searchable command palette: `Ctrl+B`, then `Space`. `Ctrl+B`, then `?`, shows keyboard shortcuts.
- **Ask memory** in the command palette: query the project memory and get cited sources, missing evidence and citation coverage of the answer, without writing anything.
- A citation must be at least 12 characters long.
- Findings are linked to code symbols, not only to files.
- The answer-format repair step no longer drops verified citations.
- The knowledge agent explicitly answers "unknown" when the project does not contain the requested information.
- `devbrain doctor --contract` shows each Codex and Claude compatibility check together with the output line that confirmed it.
- Removed the old `tui.py` interface, the browser cockpit and the `devbrain cockpit open` command. The local API broker remains and serves the terminal panels.

## 0.7.2

- Fixed GitHub Copilot account login in WSL: consent to store credentials locally is passed in the format the runtime requires.
- The Copilot profile ignores inherited BYOK and offline-mode settings.
- Runtime integrity is checked by file content.
- The memory acknowledgement tool has an exact schema for plan and task fields, so the model does not have to guess the ACK format.
- Fixed the native Codex test configuration, diagnostics for extraction write errors, and the build-tool dependencies of the optional web cockpit.

## 0.7.1

- Fixed a graph display bug.
- Added lit 3D ASCII nodes with perspective and surface occlusion. The real memory graph rotates, much like the classic terminal donut; it is not demo data.
- Added an updater from 0.7.0 that checks the files it changes, backs up sources and the database, and preserves data, models, API keys, credentials and native permissions. It makes no paid requests and does not download models or the Copilot runtime.

## 0.7

- Added DevBrain's own coding agent for OpenAI Responses, Anthropic Messages, OpenAI-compatible APIs and local Ollama, plus a separate adapter for the official GitHub Copilot SDK.
- Profiles and panels are selected in the terminal.
- The new engines use shared memory, a real model ACK, an action log and one-time command approvals. See [docs/API.md](docs/API.md).

## 0.6

- Brought the 0.5 foundations directly into the terminal. `devbrain` and `devbrain tui` open the same tmux/Textual layout: a projects and settings panel, up to two visible clients, the inspector and a true XYZ projection of the graph.
- Codex and Claude keep their own interfaces, logins and approvals. See [docs/TERMINAL.md](docs/TERMINAL.md).

## 0.5

- Introduced managed client sessions, profiles, project search, the inspector and a local browser cockpit (the browser cockpit was removed in 0.8.0).

## 0.3.10

- Extraction generation is limited to the source under examination; earlier records are not included in the model input.
- The generation schema restricts citations to verbatim fragments of that source; independent validation still rejects citations that do not exist.
- The report separates the reasons why proposals were omitted.
- The explicit `jobs retry --include-omissions` flag allows retrying a completed extraction when all proposals were rejected and nothing was saved.

## 0.3.9

- Limited the context of a single `ask` answer repair.
- Added a schema and a single repair attempt for extraction, and atomic saving of its proposals.
- Diagnostics keep error causes and distinguish the history of failed attempts from the confirmed archive range.
- Jobs can be read and retried individually within a project, with attempt version control and a preserved audit trail.

## 0.3.8

- A failed first attempt to read the history size is resolved when a later event in the same session supplies a valid range that passes full archive verification.
- The original error is kept on record; measured, conflicting or unverified ranges still block resume.

## 0.3.7

- Recognizes 11 additional auxiliary entry types from Claude Code 2.1.318, including session mode, attachment metadata and API request metadata.
- Archives their full raw content and fixes the classification of previously saved entries without changing identifiers or hashes.
- Invalid schemas and provenance conflicts still block readiness.

## 0.3.6

- Preserves MCP tool approvals saved by the native client and the permissions of existing configuration files.
- A restart updates only the managed DevBrain connection fields.
- When Codex saves a new persistent approval, it still changes the configuration and may require a package refresh and a new ACK.

## 0.3.5

- The history range is fixed for a given resume. Retrying the package read does not move that range past current tool calls.
- The startup hook reads the exact launcher package without substituting another one; a code change still requires a refresh and a new ACK.

## 0.3.4

- Recognizes the technical `world_state` and `token_usage_record` entries from Codex 0.166 history.
- Verified resume and the importer can re-recognize such entries saved by an older parser, preserving their original content, identifiers and hashes.
- Unknown or corrupted entries still mark the history range as incomplete.

## 0.3.3

- Fixed passing session identifiers from the launcher to MCP.
- Fixed the permissions of the four tools of the temporary Codex test server.
- Added an explicit Claude skip when the account quota is unavailable. The skipped stage stays `not_run`.

## 0.3.2

- Extends the bounded answer repair from 0.3.1 with an optional `answer_schema` contract: required keys and data types are checked before a result is accepted.
- The evaluation report includes diagnostics for model answers and Ollama errors.
- The pass threshold and source requirements are unchanged.

## 0.3

- Added verified session resume, atomic plans, execution of named test profiles and recall measurement on isolated data.
