# Cairnmind 1.0.0 stable contract

This document states what Cairnmind promises not to break across `1.x` releases without a new major version (`2.0.0`). Everything not listed under [Stable in 1.x](#stable-in-1x) — including surfaces listed under [Preview and experimental](#preview-and-experimental) and [Not part of the stable contract](#not-part-of-the-stable-contract) — may change in any minor or patch release.

## Stability policy and deprecation

- **Additive changes are allowed in minor releases (`1.Y.0`):** new commands, new optional flags with backward-compatible defaults, new optional fields in `--json` or MCP outputs, new read-only diagnostic keys, and new supported platforms or clients.
- **Breaking changes require a new major version (`2.0.0`):** removing or renaming a stable command, option, environment variable, `config.json` setting, `--json` field, MCP tool, MCP parameter, or file format field; changing an exit code or safety guarantee; or breaking upgrade/read compatibility for a published release's store, backup, snapshot, or published memory format.
- **Deprecation window:** any planned removal or incompatible change to a stable item is announced as deprecated in at least one minor release (`CHANGELOG.md` and [UPGRADING.md](UPGRADING.md)) and removed no earlier than the next major version.

## Stable in 1.x

Every item below names where it is specified and the automated test(s) that guard it in CI.

### 1. Command-line interface and `--json` outputs

- **Scope:** documented `cairnmind` commands, subcommands, positional arguments, options, exit codes (`0` success, `1` operational/check failure, `2` usage or policy refusal, `3` evaluation `not_run`), and the exact keys and shapes of `--json` outputs — including core workflow, memory, storage, importer (`importer add|remove|list`, `import --via`), `pulse`, `scan`, `scip status`, `memory heal-audit`, and `mcp generate` commands.
- **Defined in:** [WORKFLOW.md](WORKFLOW.md), [MEMORY.md](MEMORY.md), [STORAGE_AND_MAINTENANCE.md](STORAGE_AND_MAINTENANCE.md), [EXTERNAL_CLIENTS.md](EXTERNAL_CLIENTS.md), [CONTRACTS.md](CONTRACTS.md), and `tests/fixtures/contracts/cli_json/*.json`.
- **Guarded by:** `tests/test_release_contracts.py` (frozen `--json` examples and field-removal self-test), `tests/test_workspace_cli.py`, `tests/test_importers.py`, `tests/test_pulse.py`, `tests/test_pulse_arcade.py`, `tests/test_mcp_presets.py`, `tests/test_quote_healing.py`, `tests/test_scip_manager.py`.

### 2. MCP tools and the resume packet

- **Scope:** the eight MCP tools exposed by `cairnmind mcp` (`ask`, `remember`, `checkpoint`, `save_plan`, `next_task`, `resume_context`, `ack_resume`, `check_status`), strict rejection of undeclared tool arguments, the resume packet (`schema_version: 2`, `state_hash`, `origin` attribution, `omitted_by_origin`, `held_back`), and short readback (`unchanged: true`, `acknowledged: true`, `newer_automatic_proposals`) when unchanged.
- **Defined in:** [CONTRACTS.md](CONTRACTS.md), [V03_CONTRACTS.md](V03_CONTRACTS.md), [EXTERNAL_CLIENTS.md](EXTERNAL_CLIENTS.md), and `tests/fixtures/contracts/mcp_tools/*.json`.
- **Guarded by:** `tests/test_release_contracts.py` (frozen captures of all 8 MCP tools + short resume, and field-removal self-test), `tests/test_resume_mcp_protocol.py`, `tests/test_mcp_unknown_argument.py`, `tests/test_sessions.py`, `tests/test_external_mcp.py`, `tests/test_external_session_writes.py`.

### 3. Formats that outlive a release

| Format | Contract promise | Defined in | Guarded by |
| --- | --- | --- | --- |
| Store schema & upgrades | Opening a store written by any published release (`0.10.0`, `0.11.0`, `0.12.0`, and every later published release) upgrades the schema at most once, writes a `before-schema-*` safety backup before DDL changes, and preserves every exported row. | [ARCADE_CONTRACTS.md](ARCADE_CONTRACTS.md), [UPGRADING.md](UPGRADING.md) | `tests/test_release_contracts.py`, `tests/test_storage_contract.py`, `tests/test_migrations.py` |
| Logical backup (`.cairnmind.json.gz`, `.devbrain.json.gz`) | Backups written by `0.10.0+` restore cleanly into `1.x` (vectorless format 5 requeues embedding jobs; earlier backups with vectors restore intact); interrupted restores leave `restore_unfinished` and keep the `before-restore-*` copy. | [STORAGE_AND_MAINTENANCE.md](STORAGE_AND_MAINTENANCE.md), [DEPLOYMENT.md](DEPLOYMENT.md#backup-and-restore) | `tests/test_release_contracts.py`, `tests/test_backup_without_vectors.py`, `tests/test_backup_restore_stream.py`, `tests/test_restore_staged.py`, `tests/test_restore_network.py` |
| Portable project memory (`.memory/cairnmind/`, format v1) | Snapshots written by `0.10.0+` inspect and import cleanly into `1.x` (older graph rows ignored cleanly; transcripts and secrets gated per privacy rules). | [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#portable-project-memory-version-1), [MEMORY.md](MEMORY.md) | `tests/test_release_contracts.py`, `tests/test_phase2_snapshot_privacy.py`, `tests/test_snapshot_graph_rows.py`, `tests/test_archive_import_shown_list.py` |
| Published team memory (`.memory/cairnmind-published/`, `FORMAT.json` v1) | Published directories written by `0.11.0+` validate, diff, and import cleanly into `1.x` with full secrets-gate and person-act rules. | [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#published-memory-version-1), [MEMORY.md](MEMORY.md) | `tests/test_release_contracts.py`, `tests/test_published_memory.py`, `tests/test_published_memory_format.py`, `tests/test_published_memory_secrets.py`, `tests/test_published_import_person.py` |
| External transcript converters (`importers.json` v1 & JSONL v1) | Out-of-process converter registry and normalised JSONL v1 schema; converted history is always marked `origin_evidence: degraded` and never anchors records or alters native resume. | [CONTRACTS.md](CONTRACTS.md#extension-surfaces) | `tests/test_importers.py`, `tests/test_release_contracts.py` |

### 4. Configuration (`config.json`) and environment variables

- **Scope:** keys persisted in `<data_dir>/config.json` (absent newer keys retain safe upgrade defaults, e.g. `engine_mode` defaults to `native`, `knowledge_agent` defaults to `enabled` for pre-0.13.0 configs) and process-scoped `CAIRNMIND_*` environment variables (which override settings for that process without mutating `config.json`; empty strings count as unset).
- **Legacy `DEVBRAIN_*` compatibility:** `DEVBRAIN_*` environment variables, `.devbrain/checks.toml`, `.memory/devbrain`, and `.devbrain.json.gz` backups remain accepted as deprecated aliases throughout `1.x` and are eligible for removal no earlier than `2.0.0`.
- **Defined in:** [API.md](API.md), [DEPLOYMENT.md](DEPLOYMENT.md), [UPGRADING.md](UPGRADING.md).
- **Guarded by:** `tests/test_environment_names.py`, `tests/test_environment_port.py`, `tests/test_setup_profiles.py`, `tests/test_setup_profiles_store.py`, `tests/test_setup_change.py`, `tests/test_ollama_url_consent.py`.

### 5. Safety rules

| Safety invariant | Contract promise | Defined in | Guarded by |
| --- | --- | --- | --- |
| Only a person confirms | Confirming/disputing/replacing a record, resolving a conflict, confirming a plan, releasing past the secrets gate, recording an impact verdict, accepting/withdrawing transcript record kinds, deleting project memory, adopting confirmed legacy groups, and replacing confirmed/disputed records on import require the open workspace's in-memory person key (tmux server on POSIX; owner-only Named Pipe on native Windows). CLI, MCP tools, and service-token-only HTTP calls are refused. | [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#only-a-person-confirms-a-record), [CONTRACTS.md](CONTRACTS.md) | `tests/test_person_proof.py`, `tests/test_person_pipe.py`, `tests/test_person_routes.py`, `tests/test_person_acts_consent.py`, `tests/test_confirmed_by_person.py`, `tests/test_impact_and_release_need_the_key.py`, `tests/test_restore_claims.py` |
| One writer per store | Each store has one declared writer (`installation.json`); any other installation pointing at the same store operates read-only until a person explicitly runs `cairnmind store takeover --yes`. | [ARCADE_CONTRACTS.md](ARCADE_CONTRACTS.md), [DEPLOYMENT.md](DEPLOYMENT.md) | `tests/test_store_writer.py`, `tests/test_store_writer_race.py`, `tests/test_store_writer_clock.py`, `tests/test_doctor_reader.py` |
| Data egress and sensitivity classes | Remote model URLs require explicit consent (`--allow-remote`, HTTPS); hosted built-in agents obey `memory_remote_classes` (default `["project"]`), holding back conversation and third-party sources on `ask`, `read_source`, `memory_records`, `resume_context`, `remember`, and `checkpoint`. | [CONTRACTS.md](CONTRACTS.md), [API.md](API.md), [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#sensitivity-classes) | `tests/test_hosted_reads.py`, `tests/test_remote_classes.py`, `tests/test_ollama_url_consent.py` |
| Secret masking and publication gate | Registered secrets and high-entropy/well-known tokens are masked before indexing or conversion; publication and portable snapshots block or redact secrets per `.cairnmind/publication.toml` and `.cairnmindignore` unless explicitly released by a person. | [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#secrets-gate) | `tests/test_secret_mask.py`, `tests/test_published_memory_secrets.py`, `tests/test_phase2_snapshot_privacy.py`, `tests/test_inspector_release.py` |
| Evidence-grounded answers and fresh check gates | `ask` cites only verified verbatim quotes from current sources (or returns `unknown`/refuses unsupported answers); `requires_test` tasks cannot be marked `done` without fresh local check runs matching the current code fingerprint. | [CONTRACTS.md](CONTRACTS.md#what-it-guarantees-and-what-it-does-not), [WORKFLOW.md](WORKFLOW.md) | `tests/test_answer_contract.py`, `tests/test_plan_contract.py`, `tests/test_plans.py`, `tests/test_quote_healing_integration.py` |
| No in-process third-party plugins | Cairnmind never loads third-party code into its own processes; all extension surfaces run out of process. | [CONTRACTS.md](CONTRACTS.md#extension-surfaces) | `tests/test_importers.py`, `tests/test_mcp_host.py`, `tests/test_mcp_servers.py` |

## Preview and experimental

The following features ship on `main` and are tested in CI, but their storage schemas, flags, and JSON/HTTP response shapes are **not** locked by the `1.x` stability promise until explicitly promoted in a release note and moved into [Stable in 1.x](#stable-in-1x):

- **Embedded SQLite storage backend and migration:** `cairnmind try`, `cairnmind init --backend sqlite`, the local `state.db` SQLite schema, and `cairnmind migrate` (`tests/test_phase4b_sqlite_try.py`).
- **NLI / VCR verification layer:** optional `cairnmind[nli]` extras and `--verifier-mode` diagnostics (`tests/test_phase3b_nli_cascade.py`).
- **Local web Engineer Cockpit (`cairnmind ui`):** loopback HTTP and SSE endpoints (`/api/ui/*`, `/api/*`) and frontend bundle contract (`tests/test_ui_server.py`).

## Not part of the stable contract

The following may change in any minor or patch release without deprecation:

- The `GET /events` server-sent events stream of the local knowledge service ([CONTRACTS.md](CONTRACTS.md#extension-surfaces)).
- Internal Python modules, classes, function signatures, and internal database table layouts (other than lossless schema upgrades).
- Unstructured human-readable terminal prose (scripts and integrations must pass `--json` or read stdout in a pipe).
- Internal cockpit broker IPC messages and terminal UI widget internals.

## What "Supported" means

Cairnmind's release gate and CI test the following matrix ([INSTALL.md](INSTALL.md), [RELEASING.md](RELEASING.md)):

| Component | Supported versions / platforms | Notes |
| --- | --- | --- |
| Operating systems | Linux (Ubuntu 24.04 tested), native Windows 10/11 (PowerShell / Windows Terminal), Windows WSL2 Ubuntu, macOS on Apple silicon | CI runs `fast-tests` + `full-tests` on Linux, `macos-fast-tests` on Apple silicon, and `windows-fast-tests` + `windows-arcade-tests` on `windows-latest`. |
| Python | `3.12` or newer | Installed in an isolated `uv` environment. |
| Database engine | Pinned **ArcadeDB `26.11.1`** | Managed locally via Java `21+` or Docker (`ghcr.io/piotrwozn/cairnmind-arcadedb` pinned by digest), or remote connect-only. |
| Java / Docker | Java `21` or newer (native engine) **or** Docker Engine / Docker Desktop | At least one is required for the ArcadeDB stack. |
| Terminal multiplexer | `tmux 3.4` or newer (POSIX only, optional) | Required only for the POSIX multi-pane workspace; native Windows uses the standalone Textual workspace; all other CLI commands work without `tmux`. |
| Local models (optional) | Ollama (default `qwen3.5:9b` at 16,384 context and `qwen3-embedding:0.6b`) or OpenAI-compatible HTTPS server | Optional; Cairnmind runs in deterministic-only mode without models. |
| Coding clients | Codex CLI, Claude Code (`>= 2.1.238` for `cairnmind-recorder`), GitHub Copilot SDK, OpenAI/Anthropic/Ollama API profiles, external MCP clients (Cursor, Cline, Zed, VS Code, Windsurf, etc.) | External MCP clients run in `origin_evidence: degraded` sessions. |

## Enforcing contract review on pull requests

Any pull request that modifies `docs/STABLE_CONTRACT.md` or any frozen contract example under `tests/fixtures/contracts/` triggers the `Contract declaration` workflow (`.github/workflows/contract-check.yml`, running `scripts/check_contract_pr.py`, tested by `tests/test_contract_pr_check.py`). The check fails unless the pull request has **both**:

1. the `contract-change` label, and
2. a non-empty `## Contracts` section in the pull request description (outside HTML template comments) explaining the contract change.
