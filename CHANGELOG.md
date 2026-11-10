# Changelog

All notable changes are listed here, newest first. Releases up to 0.8.0 were published as DevBrain.

## Unreleased

- The Python module is `cairnmind`; `devbrain` remains as an import alias for one release, so `python -m devbrain` in the hooks and MCP entries of existing projects keeps working. `cairnmind install` rewrites those entries to the new name. Environment variables, the data directory and the portable memory format keep their `devbrain` names for now (#40).
- A question no longer re-indexes an unchanged project: the tree is compared by file sizes and times without opening the indexed files, and the answer cache stamp is a change counter instead of a scan of seven collections. On a project with 5,000 files the indexing part of a question fell from 52 s to 0.05 s. The background scan of the service skips unchanged projects the same way. A question still hashes the worktree once for its code identity (#24), and after any edit the whole project is indexed again (#62). A directory that cannot be listed is now reported instead of having its files deactivated (#23).
- The help page of every command and the messages written in the command line module are in English. Messages raised by the workspace, cockpit and service modules are still Polish (#37).
- `cairnmind doctor --contract --format text` prints one aligned line per client and check: `ok` or `failed`, with the evidence line or with what was needed and found, and a `verified_resume` line per client. A failed check now also records what it found (the line that says otherwise, or the exit code of the command), in JSON as well. JSON stays the default (#38).
- The `mcp` capability check no longer accepts the flag example `--debug='!hooks,!mcp'` in a client's help text as the `mcp` command.
- The memory inspector shows when a record's evidence was stated or observed, and warns when a record replaced a finding with newer evidence (#39).
- Records have a derived evidence state: `anchored`, `evidence_replaced` (with whether a verified quoted passage survives), `orphaned`, or `unverified` for imported file evidence before local indexing. The memory inspector shows it, a newly prepared resume packet warns about orphaned and changed records without blocking the resume, and `cairnmind memory orphans PROJECT` lists them. The state is computed when read and is not stored, so snapshots and the portable state hash do not change (#6).
- `cairnmind brief PROJECT` and **Brief** in the command palette show where work stopped in at most 40 lines: checkpoint and its age, next task and blockers, check evidence of tasks in progress, commits dated after the checkpoint and proposed findings. No model is asked and the command does not start the service; every line below the title and the legend is marked `machine` or `reported` (#3).
- `cairnmind memory show PATH` prints a portable snapshot's records and plans with their source addresses as text, with `--kind`, `--status` and `--grep` filters. It needs no Java, engine, model or service and writes nothing; inside a coding session the running service authorizes the package first (#2).
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
