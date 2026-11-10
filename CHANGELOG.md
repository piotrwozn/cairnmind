# Changelog

All notable changes are listed here, newest first. Releases up to 0.8.0 were published as DevBrain.

## Unreleased

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
