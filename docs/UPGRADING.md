# Upgrading

New installations follow [INSTALL.md](INSTALL.md). This guide is for an existing installation with data you want to keep.

- [From DevBrain 0.8.0 to Cairnmind 0.9.0](#from-devbrain-080-to-cairnmind-090)
- [From 0.9.0 to 0.10.0](#from-090-to-0100)
- [From 0.10.0 to 0.11.0](#from-0100-to-0110)
- [From 0.11.0 to 0.12.0](#from-0110-to-0120)
- [From 0.12.0 to 0.13.0](#from-0120-to-0130)
- [From 0.7.2 to 0.8.0](#from-072-to-080)

A direct upgrade from 0.7.2 to 0.9.0 was not tested. Upgrade a 0.7.2 installation to 0.8.0 first.

Upgrading from 0.12.0 is a backup and the installer; it changes no schema and no backup format, and on a managed installation the installer's `cairnmind init` adds three settings to `config.json`. Upgrading from 0.11.0 is a backup, the installer and one `cairnmind install` per project; on Linux and WSL it changes no data or configuration. Upgrading from 0.10.0 to 0.11.0 is a backup, the installer and one `cairnmind install` per project; its first start marks the confirmed records that a person's act on this computer covers. Upgrading from 0.9.0 to 0.10.0 is the installer and one `cairnmind install` per project. DevBrain 0.8.0 first goes to 0.9.0, which is a backup, the installer and one `cairnmind install` per project; an existing 0.7.2 installation first goes to 0.8.0 through a guarded updater that requires a passed runtime qualification of the host before it touches existing data. Each has its section below.

## From DevBrain 0.8.0 to Cairnmind 0.9.0

0.9.0 is the first release under the name Cairnmind. The command is `cairnmind` and the Python module is `cairnmind`; `devbrain` remains as a command alias and, for one release, as an import alias. Your memory stays where it is: the data directory `~/.local/share/devbrain`, the `DEVBRAIN_*` variables, `.memory/devbrain` and `.devbrain/checks.toml` keep their names in this release.

### 1. Make a backup with the installed version

```bash
devbrain backup ~/devbrain-backup-0.8.0.devbrain.json.gz
```

### 2. Close the workspace and coding sessions

Close the terminal workspace and any Codex or Claude Code session started from it, then stop what keeps running in the background:

```bash
devbrain workspace stop
devbrain cockpit stop
devbrain stop
```

The interface broker and the knowledge service are background processes that outlive the workspace. The installer also stops these two when it replaces DevBrain, but it does not close a workspace: a workspace or worker that keeps running from before the upgrade keeps its old processes, and they are not migrated. If `cairnmind` later refuses to open because the running broker has another version, run `cairnmind cockpit stop` and open it again.

### 3. Get 0.9.0 and run the installer

```bash
mkdir -p ~/src && cd ~/src
curl -LO https://github.com/piotrwozn/cairnmind/releases/download/v0.9.0/cairnmind-0.9.0.tar.gz
tar -xzf cairnmind-0.9.0.tar.gz
cd cairnmind-0.9.0
bash scripts/install-wsl.sh
```

The installer removes the earlier `devbrain` uv tool, installs `cairnmind` and keeps your memory.

### 4. Reconnect your projects

Hooks and MCP entries written by 0.8.0 name the Python of the removed tool, so they stop working until they are rewritten. The installer ends with one command per registered project, by project name:

```bash
cairnmind install my-app
```

Run each of them before you start Codex or Claude Code in that project. This also rewrites the entries to the new module name.

### 5. Check

```bash
cairnmind version
cairnmind doctor
cairnmind project list
```

### Going back to 0.8.0

There is no automatic rollback.

1. Close the workspace and coding sessions, then run `cairnmind workspace stop`, `cairnmind cockpit stop` and `cairnmind stop`.
2. Remove the new tool, which owns the `devbrain` command: `uv tool uninstall cairnmind`.
3. Run `bash scripts/install-wsl.sh` from the unpacked 0.8.0 release archive.
4. With the service stopped (`devbrain stop`), restore the backup: `devbrain restore ~/devbrain-backup-0.8.0.devbrain.json.gz`.
5. In each project, put back the configuration that 0.8.0 wrote, and do not run `devbrain install` over the entries written by 0.9.0: 0.8.0 does not recognise them and would add its hooks next to them. `cairnmind install` left a copy of every file it changed next to it, named `<file>.devbrain-backup-<number>`: `.mcp.json`, `.codex/config.toml`, `.codex/hooks.json`, `.claude/settings.local.json`, `AGENTS.md` and `CLAUDE.md`. Copy the earliest copy made during the upgrade back over each file; it names the Python of the `devbrain` tool, which exists again.

## From 0.9.0 to 0.10.0

Finish and close running coding sessions and the workspace (`cairnmind workspace stop`), then get 0.10.0 and run the installer:

```bash
mkdir -p ~/src && cd ~/src
curl -LO https://github.com/piotrwozn/cairnmind/releases/download/v0.10.0/cairnmind-0.10.0.tar.gz
tar -xzf cairnmind-0.10.0.tar.gz
cd cairnmind-0.10.0
bash scripts/install-wsl.sh
```

Then run `cairnmind install <project>` for each project. The installer stops the interface broker and the knowledge service of the installed version itself; it refuses to continue while a workspace or a coding session started from it is running, in any data directory, because the new version names its terminal sessions differently and would not find them. Set `CAIRNMIND_INSTALL_STOP_WORKSPACE=1` to let it close them. Scheduled daily backups of both names are pruned together; the safety copies written before a restore or a schema change are never pruned. Upgrade every machine and every collaborator that shares a project's repository together: a version up to 0.9.0 does not read what this one writes there.

- **Data directory.** An existing `~/.local/share/devbrain` is used where it is. Only an installation with no data yet starts in `~/.local/share/cairnmind`. If both directories hold data, the earlier one is used and `cairnmind doctor --json` says so under `environment_notes`.
- **Variables.** `CAIRNMIND_HOME`, `CAIRNMIND_ARCADE_URL` and the others are the names to use. A `DEVBRAIN_*` variable in your shell profile keeps working; when both spellings of a configuration variable are set, the new one wins and `cairnmind doctor` reports a pair that disagrees.
- **Sessions.** The launcher gives a coding client both spellings of the session variables, so hooks and MCP entries written by 0.8.0 or 0.9.0 keep working without `cairnmind install`. A session started before the upgrade stays bound after it. If the two spellings of a session variable ever name different sessions, neither is honoured: commands are refused and the client has to be started again through the launcher.

- **MCP server.** The server is named `cairnmind` in `.mcp.json` and `.codex/config.toml`. `cairnmind install` renames an entry it wrote under `devbrain` where it stands, so the tool approvals Codex keeps below it move with it; an entry you wrote yourself without the markers is left as it is and keeps working. Claude Code asks once more to approve the server and its tools under the new name. A session that was running during the rename has to be started again.
- **Instructions.** The block in `AGENTS.md` and `CLAUDE.md` is replaced where it stands, with the markers `<!-- BEGIN CAIRNMIND -->` and `<!-- END CAIRNMIND -->`. Copies of changed files are named `<file>.cairnmind-backup-<number>`.
- **Portable memory.** New snapshots go to `.memory/cairnmind/`. A package in `.memory/devbrain/` is still read and imported, and is never rewritten or deleted. After the first new snapshot, remove the earlier directory from the repository yourself (`git rm -r .memory/devbrain`) once nobody uses an earlier version; `cairnmind memory status` shows it under `earlier_package` until then.
- **Check profiles.** Declare them in `.cairnmind/checks.toml`. `.devbrain/checks.toml` is still read; move the file with `git mv .devbrain .cairnmind` when every collaborator has upgraded. The move changes a path that is part of the code fingerprint, so check runs made before it count as stale once and have to be run again. If both files exist, checks are refused until one is removed.

- **Backups.** A whole-database backup is written as `<name>.cairnmind.json.gz`. Files named `.devbrain.json.gz` are still restored, and the daily backups of both names are pruned together. A backup written by this version cannot be restored by 0.9.0 or earlier: to go back, restore one made before the upgrade.

### Going back to 0.9.0

1. Close the workspace and coding sessions, then stop everything with the installed version, which is the only one that still finds its own processes: `cairnmind workspace stop --clients`, `cairnmind cockpit stop`, `cairnmind stop`.
2. Run `bash scripts/install-wsl.sh` from the unpacked 0.9.0 release archive.
3. With the service stopped, restore a backup made before the upgrade: 0.9.0 does not read backups written by a later version.
4. In each project, put back the copies named `<file>.cairnmind-backup-<number>` that `cairnmind install` left next to the files it changed; 0.9.0 does not recognise the entries written under the new names.

With a collaborator still on 0.9.0 in the same repository: they do not see snapshots in `.memory/cairnmind/` or profiles in `.cairnmind/`, their `install` adds a second server and a second instruction block under the earlier names, and their version counts `.memory/cairnmind/` as project files.

## From 0.10.0 to 0.11.0

### 1. Make a backup with 0.10.0

```bash
cairnmind backup ~/cairnmind-backup-0.10.0.cairnmind.json.gz
```

This is the backup to go back to: keep it until you are sure you stay on 0.11.0.

### 2. Close the workspace and coding sessions

Finish and close the coding sessions started from the workspace, then stop what keeps running in the background:

```bash
cairnmind workspace stop --clients
cairnmind cockpit stop
cairnmind stop
```

`--clients` also stops the coding sessions that belong to the workspace. Without it, the finished pane of a client run can keep the session's terminal server alive, and the installer then stops and asks you to close it.

### 3. Get 0.11.0 and run the installer

```bash
mkdir -p ~/src && cd ~/src
curl -LO https://github.com/piotrwozn/cairnmind/releases/download/v0.11.0/cairnmind-0.11.0.tar.gz
tar -xzf cairnmind-0.11.0.tar.gz
cd cairnmind-0.11.0
bash scripts/install-wsl.sh
```

The installer runs `cairnmind init`, which makes the one-time step for records a person already confirmed (below). If you upgraded the package another way, run `cairnmind init` yourself before anything else.

### 4. Reconnect your projects

Run `cairnmind install <project>` for each project, before you start Codex or Claude Code in it.

### 5. Check

```bash
cairnmind version
cairnmind doctor
```

`cairnmind init` printed `confirmations_before_rule`: how many confirmed records the one-time step marked as a person's, and how many stay confirmed before the rule.

### What the upgrade does to a store

- **Schema.** The schema stays at version 4: no type, field or index is added, and no safety copy `before-schema-*` is made. The backup file format is unchanged, so 0.11.0 restores a backup of 0.10.0 and 0.10.0 restores one of 0.11.0.
- **Writer.** The first installation that opens the store becomes its writer. It writes `installation.json` into its data directory and names itself in the store (`cairnmind store writer` shows it). For a store used from one machine nothing else changes.
- **Confirmed records.** The one-time step marks the confirmed records that a person's act on this computer covers, and records that it ran (below). It changes no status, version, source or evidence.
- **Acknowledgements.** A resume packet prepared by 0.10.0 is not current any more, so a session acknowledges a new one (below).
- **Nothing else is rewritten.** Text archived before the upgrade is not masked again; records extracted before it have no extractor stamp and count as extractor version 0. The chunks and vectors that 0.10.0 kept for replaced versions of files stay until you run `cairnmind store drop-replaced-chunks`; after that `cairnmind status` counts fewer chunks and indexed sources.
- **While it runs.** Once a day, after the daily backup, completed jobs that last changed more than 30 days ago lose their attempt history (`job_history_days`; the job rows stay). At an engine start, an `engine.log` of 10 MiB or more is moved to `engine.log.1` (`engine_log_bytes`, `engine_log_copies`).

### What changes

- **A database you run yourself.** An installation whose `CAIRNMIND_ARCADE_URL` (or `arcade_url`) is `https://` or names another host is now connect-only ([DEPLOYMENT.md](DEPLOYMENT.md#connect-only-installations)): `cairnmind engine install`, `start` and `stop` do nothing there and exit with status 2 and one sentence instead of an error, `engine status` and `doctor` report where the server is, and a server of another ArcadeDB version is refused with the supported version named. Such an installation no longer creates its database when it is missing: `cairnmind db create-user` does, once. If you point `CAIRNMIND_ARCADE_URL` at a container on `127.0.0.1` over plain `http://`, nothing changes by itself, and a command that writes still starts the local engine when the container is down; set `CAIRNMIND_ARCADE_CONNECT_URL` to the same address instead (and unset the other) to stop that. An installation with the engine on this machine is unchanged. `engine status` has a new field `mode` (`managed` or `connect`), and `status` a new field `storage`.
- **Resume packets.** The packet now says where each record comes from (`origin`) and its `schema_version` is 2 ([CONTRACTS.md](CONTRACTS.md)). A packet prepared by 0.10.0 is never current after the upgrade, so the acknowledgement a coding session gave for it goes stale (`packet_changed`): a session that was running through the upgrade has its writes refused until it calls `resume_context` and acknowledges the new packet. Nothing else has to be done; the next resume does it.

- **Project names on the command line.** A project name that matches no project is now an error that names it. Before, it could act on the project of the working directory. A path is taken only when it is written as a path and exists. A script that passed an unregistered name has to pass the registered name or a path.

From 0.11.0 only a person can store a record as confirmed. The rule and its limits are in [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#only-a-person-confirms-a-record).

- **Records a person already confirmed here.** The first start of 0.11.0 (the installer's `cairnmind init`) runs a one-time step over the records that are confirmed and say nothing about who confirmed them. Such a record is marked as confirmed by a person when a person's act written on this computer covers it as it stands: the review audit of the inspector's confirm or of a conflict decision that chose it (and, in a store a 0.11 development build wrote, of a plan confirmation), when the hashes the audit holds still match the record and the record still says exactly what the version written by that act said: its text, kind, key, replaced record, quoted passage and every field beside the text; or the workspace form, when the form saved the record as confirmed and the record still says what was typed. The mark names that audit or form note as its source, with the time of the act, so the inspector shows "confirmed here by a person: <name>, in the inspector, <day>". An audit written by 0.10.0 does not hold a name, so such a record shows "the local user" in place of the name. A 0.10.0 review of a task is not marked, because its audit does not hash what the task asks for; confirm the task again in the inspector. An audit or note that came in a memory package does not count, nor does a review of an earlier text, quote, key or field, nor a command line confirmation (its note hashes nothing). A record the step cannot read stays as it is and does not stop the store from opening. The step changes no status, version, source or evidence, and `cairnmind init` reports how many records it marked and how many stay before the rule (`confirmations_before_rule`). It runs once: the store records that it ran, and running it again marks nothing new. `cairnmind restore` runs it again over what the backup brings back, since a backup made before 0.11.0 holds such records.
- **Records you already have.** Every record keeps its status. Any other record that was confirmed before the upgrade is shown as "confirmed before only a person could confirm; who confirmed it was not recorded", because up to 0.10.0 a coding session, an MCP client and the built-in agent could store that status too, and the store did not record who did. Such records stay in resume packets and in search as before. To make one yours, open it in the inspector and confirm it; this is allowed once for each such record.
- **Coding sessions, MCP clients and the built-in agent.** A `remember`, a session's finding or any other write that asks for `status: "confirmed"` is refused with HTTP 400 and the message `Only a person can store a record as confirmed, ...`. Leave the status out: the record is stored as proposed and you confirm it in the inspector. A script that sent `confirmed` has to be changed the same way.
- **Command line.** `cairnmind remember --confirmed` and `cairnmind memory confirm-plan` work only in your own terminal: outside a coding session and with a terminal on input and output. In a script, in CI, inside a coding session or from an agent's command they are refused and say what to do instead. `cairnmind task add` still adds a confirmed task in your own terminal; anywhere else it adds a proposed task and prints that it did.
- **Imports.** `memory import`, `memory import-published` and `memory import-commit` keep a record's confirmed status and mark it as confirmed elsewhere, with who the package says confirmed it and where it came from. The dry run of each says how many such records a package brings (`confirmed_records`). An imported confirmation is never shown as yours and is not verification. An import is refused inside a coding session (the dry run is not), so a script that imports memory from a session's shell has to run outside it. An import that would replace a record you confirmed or disputed on this computer with another text or status is refused and lists those records; run it again with `--replace-confirmed` to accept them. Records confirmed before the upgrade count as confirmed by you here, so the first import after the upgrade that changes such records needs the option once.
- **A record you confirmed is changed only by you.** A coding session, an MCP client, the built-in agent and `POST /remember` without a session can no longer change the text of a record you confirmed, add a field beside it, set `proposed`, `disputed` or `superseded` on it, or replace it through `supersedes`. Such a write is refused with HTTP 400 and the message `A person confirmed record <id> ... Only a person takes a confirmation away. Nothing was saved.`, and it says what to do: store what was found as a new proposed record under another key with `metadata.possible_conflicts` naming your record. For a fact or a decision the pair then appears in the conflict inbox and you decide; for other kinds you dispute the old record or confirm the new one in the inspector. Up to 0.10.0 these writes were accepted. Records confirmed before the upgrade are kept the same way, because nothing says they were not yours. A script or an agent prompt that updated confirmed records in place has to be changed. Progress on a confirmed task (`metadata.state`, reported evidence, check runs, scope) is stored as before, and so is a new checkpoint. A write that changes nothing of what you confirmed is stored too, but it only adds the sources it cites (the record keeps the ones it had, your review note among them), and it does not put an external session's origin mark on your record. When a write is refused because it would change what you confirmed, the message names what would differ (`text`, `key`, `metadata.<field>`). A dispute is not kept this way: a record you disputed can be changed by a session, and set back to proposed.
- **Plans.** A plan save that changes or removes a step you confirmed is refused as a whole, names the step and says which field would differ; a save that adds steps or changes steps you did not confirm is stored. A session has to send a confirmed step with every field `plan show` returns for it: criteria sent again without their `criterion_checks` count as a change. While a step still carries the confirmation you gave to the whole plan (`cairnmind memory confirm-plan`), a session's save that changes the plan's title, goal or constraints is refused as well; it can leave them out. To change a confirmed step or these fields, dispute the step in the inspector and let the session save again, or run `cairnmind plan save` in your own terminal (a changed step is then proposed until you confirm the plan again).
- **`plan adopt` and tasks from before the upgrade.** Up to 0.10.0 `cairnmind task add` stored every task as confirmed, so every task of an old group counts as confirmed before the rule. `cairnmind plan adopt` gives each task a plan key, which changes what was confirmed, so on such tasks it works in your own terminal only; from a coding session's shell or an agent's command it is refused and says to run it there. The adopted steps are proposed until you confirm the plan.
- **A confirmation that lapsed.** A confirmation a package brought is not a verdict given here, so a session may still change such a record: it then says "was confirmed at version N; a later write that was not a person's confirmation changed it" until you confirm or dispute it. The same is said by the checkpoint record if you confirmed it and a newer checkpoint was saved. The inspector's list shows "confirmation lapsed" on such rows, and "confirmed elsewhere" or "confirmed before the rule" on records you did not confirm here.
- **Resume packets and tool results.** A confirmed record in a resume packet, in `context` and in search hits has a new field `confirmed_by` (`person`, `import`, `before_rule` or `seed`). A client that reads packets strictly has to accept it.

### Going back to 0.10.0

1. Close the workspace and coding sessions, then stop everything with the installed version: `cairnmind workspace stop --clients`, `cairnmind cockpit stop`, `cairnmind stop`.
2. Run `bash scripts/install-wsl.sh` from the unpacked 0.10.0 release archive.
3. With the service stopped, restore the backup you made in step 1: `cairnmind restore ~/cairnmind-backup-0.10.0.cairnmind.json.gz`. What was written after the upgrade is not in it.
4. Run `cairnmind install <project>` for each project, so its hooks and MCP entries are the ones 0.10.0 writes.

The confirmation rule needs no step of its own: 0.10.0 ignores the `confirmation` field of a record.

## From 0.11.0 to 0.12.0

0.12.0 adds macOS on Apple silicon. On Linux and WSL it changes no data, no configuration and no path; what changes there are a few fixes, listed below.

### 1. Make a backup with 0.11.0

```bash
cairnmind backup ~/cairnmind-backup-0.11.0.cairnmind.json.gz
```

### 2. Close the workspace and coding sessions

```bash
cairnmind workspace stop --clients
cairnmind cockpit stop
cairnmind stop
```

### 3. Get 0.12.0 and run the installer

```bash
mkdir -p ~/src && cd ~/src
curl -LO https://github.com/piotrwozn/cairnmind/releases/download/v0.12.0/cairnmind-0.12.0.tar.gz
tar -xzf cairnmind-0.12.0.tar.gz
cd cairnmind-0.12.0
bash scripts/install-wsl.sh
```

The same script installs on macOS ([INSTALL.md](INSTALL.md)).

### 4. Reconnect your projects

Run `cairnmind install <project>` for each project, before you start Codex or Claude Code in it.

### 5. Check

```bash
cairnmind version
cairnmind doctor
```

### What the upgrade does to a store

- **Schema.** The schema stays at version 4 and the backup file format is unchanged, so 0.12.0 restores a backup of 0.11.0 and 0.11.0 restores one of 0.12.0. No one-time step runs, and no record, source or acknowledgement is rewritten.
- **Writer.** On Linux and WSL an installation is identified as before (`/etc/machine-id`, its data directory), so it stays the writer of its store. On macOS it is now identified by the Mac's hardware UUID instead of its host name (below).

### What changes on Linux and WSL

Nothing in the data directory, `config.json`, the socket paths or the files `cairnmind install` writes changes with the default settings. The question budget stays 60 seconds, the engine and the installer still take `java` from `PATH` whatever `JAVA_HOME` says, and the private tmux sockets stay where 0.11.0 put them. These fixes apply on every system:

- **Codex tool timeout.** `cairnmind install` writes `tool_timeout_sec` into the Cairnmind entry of `.codex/config.toml` as the question budget plus 15 seconds, never less than 75. With the default budget of 60 that is 75, as before. If you raised `ask_timeout` in `config.json`, the next `cairnmind install` writes the larger value.
- **Stopping.** `cairnmind workspace stop --clients` also stops the private terminal server of the last coding-session run, which 0.11.0 left running. `cairnmind engine stop` clears the record of an engine that has already exited instead of failing.
- **Questions.** A question no longer starts a second reading of whole passages when the time left is shorter than the calls it repeats, so it can end sooner with what it has. A model server that refuses structured output is asked again without it, and `cairnmind doctor --json` says so; the refusal was seen with an MLX build of Ollama on Apple silicon.

### What changes on macOS

macOS was not supported before 0.12.0, so most Macs get a new installation from [INSTALL.md](INSTALL.md). If a data directory on a Mac was already used by 0.11.0 or by a build of `main` before 0.12.0:

- **Writer.** A Mac is identified by its hardware UUID (IOPlatformUUID) from 0.12.0, and before #259 it was identified by its host name. Such a data directory therefore no longer matches its own identity: it gets a new installation identifier and reads its store only. `cairnmind store writer` shows this; run `cairnmind store takeover --yes` once to write again. From then on a change of the host name or network does not change the identity.
- **Question budget.** A new installation on macOS gives each question 120 seconds. A data directory created before #264 keeps the 60 its `config.json` holds, which can be too short for a local model on Apple silicon. It limits questions only, not extraction jobs (#266). To use 120, stop the service (`cairnmind stop`), set `"ask_timeout": 120` in `config.json` of the data directory, and run `cairnmind install <project>` again for each project, so Codex waits 135 seconds for a Cairnmind tool.
- **Sockets.** The workspace and coding-session sockets live in `/tmp/cairnmind-<uid>/`, a folder only you can use, instead of under `$TMPDIR`. A workspace started before the upgrade is not found after it; that is why step 2 stops it.
- **Java.** On macOS the engine takes Java from `JAVA_HOME` before `PATH`; a `java_executable` in `config.json` still comes first.

### Going back to 0.11.0

1. Close the workspace and coding sessions, then stop everything with the installed version: `cairnmind workspace stop --clients`, `cairnmind cockpit stop`, `cairnmind stop`.
2. Run `bash scripts/install-wsl.sh` from the unpacked 0.11.0 release archive.
3. Run `cairnmind install <project>` for each project.

The store needs no restore: 0.11.0 reads what 0.12.0 wrote. On Linux and WSL the installation stays the writer. Going back is for Linux and WSL: 0.11.0 does not support macOS.

## From 0.12.0 to 0.13.0

0.13.0 makes the first start lighter: the database engine can run in Docker, tmux is needed only for the workspace, and Cairnmind starts without Ollama. For an existing installation none of this is switched on by itself: it stays on its native engine and its models stay enabled. The schema and the backup format do not change, and on a managed installation `cairnmind init`, which the installer runs, adds three settings to `config.json`.

`cairnmind install` also writes `.claude/agents/cairnmind-recorder.md` for Claude Code 2.1.238 or newer: a subagent that records what the main conversation quotes to it, as its own external session. Claude Code asks you to trust the folder before it starts the subagent's server. The file is replaced whole at each install. To keep the subagent away, run `cairnmind install --no-recorder`: it removes the file Cairnmind wrote (a backup is beside it), writes none, and the choice is remembered for the project in the data directory, so a later plain install keeps it off; `cairnmind install --recorder` turns it on again. A file with your own text, without the two Cairnmind marker lines, is still left unchanged with either flag, and install says so each time. Deleting the file by hand is not enough: the next plain install writes it again (`cairnmind project remove` lists it with the other installed files). On an older Claude Code, or one whose version cannot be read, install writes nothing and removes the file it wrote earlier, leaving a backup beside it. A symlinked `.claude` or `.claude/agents` is never written through.

### 1. Make a backup with 0.12.0

```bash
cairnmind backup ~/cairnmind-backup-0.12.0.cairnmind.json.gz
```

### 2. Close the workspace, coding sessions and editors that use Cairnmind

```bash
cairnmind workspace stop --clients
cairnmind cockpit stop
cairnmind stop
```

Also close every editor that uses `cairnmind mcp --client external` (Cursor, Cline, Zed and others). An editor left open keeps running the MCP process of the version being replaced, and an editor that restarts its MCP server after the installer runs 0.13.0, which starts the knowledge service by itself (#258). Closing such editors is not needed for 0.12.0 itself, but from 0.13.0 on an editor left open can start the service of the version being replaced between `cairnmind stop` and the installer, so close them before every upgrade. The installer of 0.13.0 looks for open `cairnmind mcp --client external` servers of the installed version, names them by process id and stops until they are closed; it cannot see the editors themselves. While it runs it leaves a mark, and no command starts the knowledge service until it has finished. An MCP server that stayed open across an upgrade refuses its calls and says to restart it (#298).

If you use more than one data directory (`--data-dir` or `CAIRNMIND_HOME`), stop each one's service and broker yourself before the installer: `cairnmind --data-dir <dir> stop` and `cairnmind --data-dir <dir> cockpit stop`. The installer stops only the default data directory's. After its stop it names, by process id, any knowledge service of the installed version that still runs, and goes on; it stops none of them. It does not see a service you started yourself with `cairnmind serve`. A service left running keeps the version being replaced: from 0.13.0 on, a command that uses the knowledge service, the workspace and an MCP server refuse to use a service of another version, name both versions and give the command that stops it; `cairnmind stop` and `cairnmind doctor` still work against it, and the next command that needs the service starts it in the new version (an editor's MCP server of the `native` kind never starts it). Commands that open the store themselves (`init`, `brief`, `project add`, `backup`) do not ask the service and are not refused. The versions are compared by number, so a reinstall of the same version is not noticed: stop the services yourself then. If the installer says that `cairnmind stop` or `cairnmind cockpit stop` failed, it asks the new version to stop the knowledge service again once it is installed, and ends there when that service still runs ten seconds later, because it would write the store beside a service of the old version: end that service (the installer names its process), then run the installer again, which does the remaining steps. A `cairnmind run` or workspace session opened while the installer runs is refused (#319).

End every coding session started with `cairnmind run` as well. The installer names one started as `cairnmind run ...` and stops until it is closed; it does not see one started as `cairnmind --data-dir <dir> run ...` or `python -m cairnmind run ...` (#319).

When the installer stops because a workspace or coding session of the installed version is running, it lists each socket with the data directory it belongs to whenever it can ask that terminal server, so a leftover of a test run or of another data directory can be told from your own; a socket it cannot ask is listed without one (#161).

### 3. Get 0.13.0 and run the installer

```bash
mkdir -p ~/src && cd ~/src
curl -LO https://github.com/piotrwozn/cairnmind/releases/download/v0.13.0/cairnmind-0.13.0.tar.gz
tar -xzf cairnmind-0.13.0.tar.gz
cd cairnmind-0.13.0
bash scripts/install-wsl.sh
```

The installer no longer stops when tmux 3.4+ is missing: it says that the workspace is unavailable and goes on. It also no longer stops when Java 21 is missing and Docker answers; that matters for a new installation, because an installation that has a native engine keeps it (below). If Java 21 is no longer on the machine, install it, or move the engine to Docker as in the bullet "The engine in Docker" below; the installer's message that `init` can run the engine in Docker does not apply to an installation that already has a native engine.

The installer now ends with the exit code of `cairnmind doctor`: 1 when something required is missing. A connect-only installation whose database user does not exist yet therefore ends with 1 until `cairnmind db create-user` has run; do not chain other commands to the installer with `&&`. It says that it stopped the service of the installed version once that service has ended, and it no longer announces the download when the pinned engine is already unpacked. It may also print two lines about the removed `devbrain` command; see "The `devbrain` command is gone" below.

The same script installs on macOS ([INSTALL.md](INSTALL.md)).

### 4. Reconnect your projects

Nothing that `cairnmind install` writes for an installed coding client changed in this release, so an existing project needs no new `cairnmind install`. Run it once anyway if you ever started Cairnmind as `devbrain` in a project's client files by hand: it names each such entry (below). Start the editors you closed in step 2 again.

### 5. Check

```bash
cairnmind version
cairnmind doctor
```

`cairnmind doctor` and `cairnmind status` now start with a `topology` report, and `doctor` names the engine mode (`arcadedb.engine`). A script that reads their output meets new keys. With a native engine and a Java 21 or newer that `doctor` could read, `topology.storage` and the engine item of `topology.this_machine` carry `java_major`, and the sentences name that version ("native engine, Java 22"); where no version was read they say "Java 21 or newer" (#368). On a Mac with an Apple chip the summary also names the chip and the unified memory shared with the models (#368). `doctor`'s JSON also has `projects` (the number of registered projects, `null` when the store cannot be read) and, for an engine that refuses this data directory's sign-in, `arcadedb.sign_in_refused: true`.

`cairnmind doctor` exits 1 when something required is missing (the engine, Java or Docker for it, the store writer, a shell whose commands are refused, an invalid `knowledge_agent`, a knowledge service of another version) and 0 otherwise; models, tmux and coding clients never change the exit code. Models or a client that are set up and fail are listed under "Optional and not working". A script that ran `doctor` and expected 0 must accept 1 and read the report (`cairnmind doctor --json`).

### What the upgrade does to a store

- **Schema.** The schema stays at version 4 and the backup file format is unchanged (`cairnmind-arcadedb-backup`, version 1), so 0.13.0 restores a backup of 0.12.0 and 0.12.0 restores one of 0.13.0. No one-time step runs, and no record or source is rewritten.
- **Writer.** An installation is identified as before, so it stays the writer of its store.
- **Acknowledgement.** An acknowledgement given before the upgrade is not cancelled, and the packet format is unchanged (`schema_version` 2). But the state an acknowledgement is compared with now leaves out proposals that the extraction worker wrote and nobody has touched (#256). An acknowledgement given under 0.12.0 is therefore refused once, with `resume_required`, if the project held an untouched automatic proposal that 0.12.0 still counted (any other than the session's own words restated); the session then calls `resume_context` and `ack_resume` for the new packet. If the project held none and nothing else changed, the old acknowledgement still holds. Any record a person or a session writes, the plan, the task and the code make an acknowledgement stale, as before.
- **Memory snapshots.** A snapshot no longer carries the code graph, which indexing rebuilds from the project's files (#261). The snapshot format number stays 1. A snapshot written by 0.12.0, with graph rows, still imports and shows; its graph rows are ignored, and the import result, `memory status` and `impact` say that the code graph is not built yet until indexing has built it. The first `cairnmind memory snapshot` after the upgrade, for a project whose last snapshot had graph rows, writes a new snapshot instead of reporting no change, because the package holds fewer objects. Its parent is the earlier snapshot, and its state hash is the same as long as nothing in the project's memory changed in between. A repository whose graph made the manifest too big for 0.12.0 can now be snapshotted; `cairnmind memory snapshot --preview` shows the exact figures and warns before anything is written.
- **Backups carry no vectors (#187).** A backup written by 0.13.0 is smaller: it holds the text and database state but no vectors, in the same format. That holds for every backup, the daily one, `cairnmind backup` and the `before-restore-*` and `before-schema-*` safety copies, so undoing a restore with its `before-restore-*` copy also rebuilds the vectors. A restore is therefore followed by a rebuild of the vectors, which needs the models: `cairnmind restore` and `cairnmind setup local --backup` set the completed embedding jobs back to pending, say how many (`embed_jobs_requeued`), and the service embeds the chunks in the background once it runs with the models; the plan of `setup local --backup` says so before `--yes`. Until then search uses the text only and says so (`cairnmind status` shows the jobs left), and without models (deterministic-only mode) the jobs wait until they are turned on. An answer's warnings now also say "Vectors are still being built ... embedding jobs left: N" whenever the project has embedding jobs pending, for example while a new project is indexed. When the jobs of a rebuild have failed and nothing is pending, it says instead that vectors are missing for chunks whose embedding failed, with their number, and that `cairnmind retry-jobs` tries again. These warnings are never the ones cut when an answer keeps only its last eight. A backup written by 0.12.0 or earlier still restores with its vectors, and nothing is requeued. `cairnmind export` leaves the vectors out as well. Restoring a 0.13.0 backup with 0.12.0 (tried on the 0.12.0 code) restores it without vectors, and a search with a query vector warns that the native vector index is not ready until the first vector exists, and is silent after that. Its service queues one embedding job per chunk without a vector and rebuilds them when it runs with the models, and stays silent while the rest are built; without models it rebuilds nothing.
- **Embedding jobs.** Each chunk now has one embedding job (#262). An embedding job that 0.12.0 left in the queue under the old key and the new one embed the chunk once between them; no work is done twice.
- **Restore writes in bounded transactions (#362).** `cairnmind restore` and `cairnmind setup local --backup` used one transaction for the whole store, and the engine holds a transaction's changed pages in its heap until it commits: a store the size of Cairnmind's own repository (81,488 graph vertices, 101,428 edges) ran the engine out of memory on its default 1 GB heap. A restore now deletes and writes at most 5,000 rows, 8 MiB of text or 5 seconds per transaction, so it needs no more heap for a larger store. Two things follow. A command that opens the store anew, on any installation of it, is refused while a restore runs (it saw the old data until the commit before), with a sentence that names the restore. And a restore that is cut off (a dropped connection, a killed process, a dead engine) no longer leaves the old data: it leaves a mark in the store (`devbrain_meta`, `restore_unfinished`, never in a backup), and every command refuses the store, naming the backup and the `before-restore-*` copy, until `cairnmind restore FILE` finishes it or `cairnmind restore` of that copy goes back. `cairnmind doctor` shows the mark as `restore_unfinished` (`null` when there is none) and exits 1. A restore that fails while it runs puts the store back from its `before-restore-*` copy by itself and says so, and what becomes of the vectors as the knowledge agent setting decides (that copy has none). If the installation that started a restore is gone, any other installation of the store finishes it: `cairnmind store takeover --yes`, then `cairnmind restore FILE` with any valid backup. **A 0.12.0 client does not know the mark.** Opening a store that a 0.13.0 restore left unfinished, it opens it without a word, reads what the restore had reached (part of a backup) and writes into it, and the mark stays (checked by running 0.12.0 on such a store: it listed the project and wrote a new source). The mark does not protect a store from a client older than the release that wrote it, and nothing in 0.12.0 can be made to refuse it. So finish the restore (`cairnmind restore FILE`, or the `before-restore-*` copy to go back to the rows the store held before) with 0.13.0 before any 0.12.0 command runs against that store, and do not go back to 0.12.0 while one is unfinished. **`store prune-backups` now reads the mark** before it deletes a `before-restore-*` copy, which takes the database engine: with the engine stopped it keeps every such copy and says so (start it with `cairnmind engine start`, then prune); the `before-schema-*` copies are pruned as before, with no engine.

### What changes in `config.json`

0.12.0 wrote none of the following settings, and a `config.json` without them keeps working:

- **`engine_mode`.** Absent counts as `native`. The first `cairnmind init` saves `"engine_mode": "native"` (with `"engine_docker_build": false`) for an installation that has an `arcade/` folder in its data directory, and says so; it never switches an existing installation to Docker, even when Java is gone and Docker answers. A connect-only installation (a database address in `arcade_connect_url`) has no engine of its own and gets no mode.
- **`knowledge_agent`.** Absent counts as `enabled` when a `config.json` exists, so an installation from before 0.13.0 keeps its models, and a configured model server that is down is still reported as a fault. When `init` saves `config.json` for the engine mode it writes `"knowledge_agent": "enabled"` as well. The upgrade never switches an existing installation to deterministic-only mode. To turn the knowledge agent off yourself, set `knowledge_agent` to `disabled` and restart the service; `cairnmind models pull` or `cairnmind models set` turns it on again. What each mode does is in [API.md](API.md#the-knowledge-agents-models).

### What changes for an existing installation

- **The engine in Docker.** To move to Docker: `cairnmind stop`, `cairnmind engine stop`, then `cairnmind engine start --docker`. To move back: the same, ending with `cairnmind engine start --native`. A start refuses while the other engine runs on the data directory and names it, and both engines use the same data directory. Docker must run on this machine and Cairnmind must not run as root; see [INSTALL.md](INSTALL.md#running-the-database-in-a-container-instead).
- **Ports and the model address from the environment.** `CAIRNMIND_PORT`, `CAIRNMIND_COCKPIT_PORT` and `CAIRNMIND_OLLAMA_URL` (also with the prefix `DEVBRAIN_`) are read like the database address: the process that has the variable uses it, and it is never written to `config.json`. 0.12.0 ignored them, so a shell that already exports one now changes the port or the model address. An empty variable is ignored, and an empty `CAIRNMIND_ARCADE_CONNECT_URL` counts as unset: it no longer turns a connect-only installation into a managed one. See [DEPLOYMENT.md](DEPLOYMENT.md#two-data-directories-on-one-machine).
- **A model server on another computer.** An `ollama_url` that is not a loopback address (127.0.0.1, localhost or ::1), in `config.json` or in `CAIRNMIND_OLLAMA_URL`, is now refused like a model profile without consent (#288): `doctor`, `init`, `models pull` and `models list` send nothing to it and report the refusal. 0.12.0 already did not start the service with such an address and never sent questions or memory text there; what it did send was the request of `doctor` and `init` asking that server which models it has, and the request of `models pull` asking it to download them. To keep using such a server, run `cairnmind models set --role chat|embedding --provider ollama --model NAME --base-url URL --allow-remote` for each role (the address must use HTTPS), or set `ollama_url` back to `http://127.0.0.1:11434`.
- **The built-in agent on a hosted model reads less of memory (#183).** A built-in agent on a Copilot connection or on an API profile whose address is not loopback (OpenAI, Anthropic, a model server on your network) is now held back by sensitivity class, with the setting `memory_remote_classes`, which defaults to `["project"]`. 0.12.0 gave it every class: it read conversation text through `read_source`, `memory_records`, the quotes of `ask` and the record previews of its resume packet. Now it gets code and documentation only: `read_source` and `memory_records` say which class they hold back, `ask` and the resume packet leave held passages and previews out and count them under `held_back`, and the active plan, task and blockers stay. To give such an agent the decisions and notes of earlier conversations again, add `"conversation"` to `memory_remote_classes` in `config.json` and start Cairnmind again; the setting also allows that class for a memory model server on another computer. A built-in agent on a local Ollama, Codex CLI, Claude Code and external MCP clients are not changed (their provider receives what they read; see [EXTERNAL_CLIENTS.md](EXTERNAL_CLIENTS.md)). A session prepared under 0.12.0 gets the held packet at its next preparation.
- **One more model call per complete answer (#245).** A complete answer to a question asked without an answer schema is now checked by one more call of the chat model, after the support check: an answer that asserts a product or technology name its quotes do not state (also in lower case, such as nginx) comes back as `partial`, with "The quoted sources do not state the names the answer gives:" and the names in its unknowns, and so does one whose names call fails, gives an unreadable reply or does not finish in the time limit. Expect a complete answer to take one call longer, and a few answers that 0.12.0 gave as complete to be partial; `diagnostics.model_calls` now lists up to three calls, the last with phase `names`, and `support_check` can be `names_unstated` or `names_failed`. Deterministic-only mode and typed answers are not changed.
- **A longer wait before an extraction job is called failed (#266).** The time one extraction job has for its model call and its single correction rose from 120 to 180 seconds, so a slow model server (an MLX build on Apple silicon) no longer fails a job whose two calls take about a minute each. An extraction call no longer uses `ask_timeout` as its limit: each call waits as long as the job has left of the 180 seconds, whatever `config.json` holds, so a call of 63 seconds completes on a data directory with 60. A model that never answers ends the job at 180 seconds as a timeout, and a model server that cannot be reached still fails it at once as `model_unavailable`. A model that hangs holds the extraction worker for up to 3 minutes, and a question asked meanwhile waits behind it in the same queue, up to its own time limit. A job that was interrupted by a stop or a restart is taken up again 270 seconds after it was claimed instead of 210. There is no setting for it. The measurement is in [evaluation/RESULTS.md](evaluation/RESULTS.md#extraction-on-apple-silicon-266).
- **tmux.** Only the terminal workspace needs tmux 3.4+. Without it `cairnmind`, `cairnmind tui` and `cairnmind workspace takeover` print one message and exit with code 1, `doctor` reports `cockpit: unavailable (tmux 3.4+ not found)` and exits 0, and every other command works. With tmux 3.4+ nothing changes.
- **External MCP clients.** `cairnmind mcp --client external` starts the knowledge service when a tool call finds it not running, so an editor works after a reboot without `cairnmind serve`. On an installation that only reads the store (a reader) nothing is started, and the error gives the reason. A tool call that carries an argument the tool does not declare is now refused with an error that names the argument and lists the tool's arguments, and nothing is done; 0.12.0 dropped such an argument silently (`remember` with `state` beside `record` saved the task as todo). A client or script of your own that sends extra arguments has to leave them out (#161). See [EXTERNAL_CLIENTS.md](EXTERNAL_CLIENTS.md).
- **The workspace.** `cairnmind workspace stop` now ends the workspace process with exit code 0; a tmux server killed from outside still gives a non-zero code, so a script that took 1 as the sign of a stop needs updating. The approval dialog of the built-in agent starts with focus on Cancel when the request fits on the screen. After `workspace stop`, the terminal that was attached prints that the workspace was stopped, after tmux's own "[server exited unexpectedly]". The pane of a coding session that ended says "Session ended (exit status N). Resume it from Sessions." instead of tmux's "Pane is dead" (#161).
- **Memory.** `cairnmind memory reextract PROJECT` extracts again the sources whose untouched proposals come from an older extractor version; it needs the models, and `--dry-run` does not. See [MEMORY.md](MEMORY.md#how-extraction-fares-in-review).
- **A project inside the data directory.** `cairnmind project add` refuses a folder inside Cairnmind's data directory and says why (#295). A project that 0.12.0 registered there stays registered and indexes no files; its indexing message now says how to unregister it with `cairnmind project remove`, keeping its memory.
- **What `project add` says about its jobs (#366).** `cairnmind project add` still indexes in its own process and starts no service. It now says how many extraction and embedding jobs of the project wait and why (the service is stopped, and `cairnmind index NAME` starts it; no models; an invalid `knowledge_agent`; an upgrade in progress), and its `--json` output always has a `jobs` field (`queued`, `service_running`, `waiting_because`; `null` means not known to wait). A script that compares the exact keys of that output must allow `jobs`.
- **Model servers and `models set` (#367).** An error about a model server on another computer no longer calls it local ("could not connect to the runtime", "Required model ... is not installed", "Ollama returned no chat message"); for an Ollama on this machine the words are as before, except that three messages that began with "Local model" now begin with "Local Ollama" ("Local Ollama returned no chat message", "Local Ollama model list is invalid", "Local Ollama model has no digest"). `cairnmind models set --provider ollama --base-url URL` with the address of `ollama_url` under another loopback name (`localhost` for `127.0.0.1`, or the other way round, same port) now saves no profile, as with the address itself, so the role keeps following `ollama_url`. Its last step no longer tells you to run the evaluations when the pair of models already passed both, or is the release's.
- **Removing a project.** `cairnmind project remove PROJECT` (a dry run; `--yes` applies it, `--root FOLDER` removes one extra folder) unregisters a project and keeps its memory; no schema or backup format changes. 0.12.0 does not know a removed project: before going back to 0.12.0, bring each one back with `cairnmind project add`, or delete its memory in the workspace (More, then Delete project memory) after `cairnmind backup DIR/`; `cairnmind project list --removed` lists them. Left in the store, a removed project makes 0.12.0's background indexing stop with an error on every pass, before its daily backup (read from the 0.12.0 code, not tried).
- **A model check cut at the length limit.** When the test call of `cairnmind models set`, or of `cairnmind setup` when it moves a role, gets a chat reply that ended at the length limit, the message now says so instead of saying that the model returned no structured JSON output. For an OpenAI-compatible chat role without `--reasoning-effort none`, `models set` also says to run it again with that option added. Nothing is saved, as before; a script that matched the old sentence sees the new one in this case only.
- **A folder this machine cannot read (#371).** An installation that cannot read a project's folder (it is on another machine, missing or not permitted) keeps the project's code graph; 0.12.0 emptied it at the next scan. The graph now follows the project's current file sources, so it is also kept when a subfolder cannot be listed. Each folder is named when it is not available: `cairnmind index PROJECT` now exits 1 and prints a sentence on standard error (its JSON gains `unavailable`), `cairnmind status` gains `unavailable_folders`, `cairnmind doctor` gains `project_folders` and a note (its exit code does not change), and the workspace project panel says it; a script that treated every exit code 0 of `index` as indexed has to read it. The `project_roots` rows gain `machine` and `host` (the hash and the name of the machine that registered the folder; no schema version change, and 0.12.0 ignores them and restores backups that carry them). Another machine no longer indexes such a folder, even at the same path; `cairnmind project root PROJECT FOLDER` moves it to the machine that runs it. Folders registered before the upgrade have no machine and are stamped by their first index that reads the whole folder. After restoring a backup on another machine, run that command for each folder that is there; `cairnmind restore` names the folders it left with the old machine. Going back to 0.12.0 brings the old behaviour back, including the emptied graph. See [DEPLOYMENT.md](DEPLOYMENT.md#a-projects-folders-belong-to-the-machine-that-added-them). If a graph was already emptied by the trial's scan, `cairnmind index PROJECT` on the machine that has the folder rebuilds it. Two machines that have a folder at the same path: index on the machine that owns it first, because during the upgrade the first machine that reads the path and completes an index stamps it and replaces the project's files with its own copy; if the other machine indexed first, run `cairnmind project root PROJECT FOLDER` on the owner and then `cairnmind index PROJECT`. Two machines on one store that upgrade at different times: a 0.12.0 installation does not know the two fields, indexes every folder as before and can still empty the graph of a folder it cannot see, so upgrade every installation of a store before relying on this. Only an index that reads a whole folder stamps it; `status`, `doctor`, a reader and a scan that finds nothing to read never do, and a machine that cannot read the folder never does. A machine with no machine id or platform UUID (only a host name) neither stamps nor refuses a folder. A machine that is not the one that registered a folder sees it as elsewhere; the sentence names `cairnmind project root PROJECT FOLDER`, which moves it, and that is also the command after a reinstalled system, a new WSL distribution or a restore on other hardware.
- **`cairnmind setup`** is new. It shows where storage and the models run, and changes no setting of an initialized installation.
- **`cairnmind setup local|remote-db|remote`** moves storage and the memory models to one of three profiles: the engine on this machine, a database this installation only connects to, or that database with both models on a server on another computer (see [INSTALL.md](INSTALL.md)). It is refused while the knowledge service runs (`cairnmind stop` first) and in a coding session, tests each target first, and changes nothing without `--yes`. Going back to `local` with `--backup FILE` restores that backup under the rule of `cairnmind restore`: the person's confirmations are kept only when the workspace is open and holds its key; otherwise each becomes the backup's claim and accepted transcript record kinds are withdrawn. The plan and the output say which.
- **The README is short.** Its detail moved under `docs/`, so a link to a README anchor other than "How it compares", "Quick start", "What it guarantees, and what it does not", "Measured results", "Documentation", "Contributing" and "License" no longer reaches a heading. The text is now in [ARCHITECTURE.md](ARCHITECTURE.md) (how it works, built with, repository layout, development), [EXTERNAL_CLIENTS.md](EXTERNAL_CLIENTS.md) (Cursor, Cline, Zed and other MCP clients), [MEMORY.md](MEMORY.md) (portable project memory, team memory), [STORAGE_AND_MAINTENANCE.md](STORAGE_AND_MAINTENANCE.md) (data and backups), [CONTRACTS.md](CONTRACTS.md#limits) (limits), [TERMINAL.md](TERMINAL.md#everyday-use-of-the-workspace) (the workspace, everyday use, your first project), [API.md](API.md#third-party-mcp-tools) (third-party MCP tools for the built-in agent) and [WORKFLOW.md](WORKFLOW.md) (plans, tasks and evidence). The headings "Without the launcher", "Look before you install", "Who it is for" and "Status and roadmap" were dropped or folded into the Quick start.
- **Sentences in a terminal, JSON in a pipe.** In a terminal, `doctor`, `project add`, `project list`, `remember`, `ask`, `install`, `init`, `stop` and `engine stop` print what they did and the next step instead of JSON. With `--json`, or in a pipe, a file or a script, they print the full JSON as before; in a terminal, add `--json` to see it. The sentences of `project add`, `remember` and `install` no longer go to standard error. In a terminal, `cairnmind stop` also says when the interface broker keeps running (`cairnmind cockpit stop` stops it) (#161).
- **What `cairnmind install` writes and indexes.** A coding client that is not installed gets nothing in the project, not even its block in `AGENTS.md` or `CLAUDE.md`; a block an earlier version wrote there stays until you remove it, or install the client and run `cairnmind install` again. Cairnmind's own block in those two files is no longer indexed, so it no longer comes back in `ask`; a block indexed earlier leaves the index the next time the project is indexed (`cairnmind index PROJECT`).
- **Shorter answers to MCP clients (#325).** The `resume_context` tool of `cairnmind mcp` (native and `--client external`) answers with a short form while the session's acknowledged packet is current: `unchanged: true`, `acknowledged: true`, `packet_id`, `packet_hash` and `newer_automatic_proposals`, and no `packet`. `full_packet: true` returns the whole packet; a packet that is new, changed or not yet acknowledged always comes whole. `remember` and `checkpoint` answer with the record's id, version, status, kind, key, source ids and what the service set or did not keep, not the stored record with its content and metadata, and `save_plan` no longer repeats the plan's title. A client or script that read `packet` from every `resume_context`, or `content` or `metadata` from a write answer, now asks for `full_packet: true` or reads the record with `ask` or `cairnmind records`. The HTTP routes answer as before unless asked (`POST /sessions/context` with `"short": true`; `POST /remember` and `POST /checkpoint` add `metadata_dropped` only with `"name_dropped": true`), so the command line, the built-in agent and the start hooks see no change.

### The `devbrain` command is gone

0.13.0 installs one command, `cairnmind`. The `devbrain` alias that 0.9.0 to 0.12.0 installed beside it is removed; uv takes it out of its tool folder (usually `~/.local/bin`) when the installer reinstalls Cairnmind. If the installer prints "Cairnmind no longer installs the devbrain command, but ... is still there", nothing removed that file: remove it with the `rm` command it prints, unless something else of yours uses it. If it stops with "An earlier DevBrain uv tool is in ... but uv does not list it", stop what that tool may still run with the commands it prints, run `uv tool uninstall devbrain` (your memory is kept) and run the installer again.

What you change: wherever you start Cairnmind as `devbrain` yourself, write `cairnmind` with the same arguments. Look in shell scripts, shell aliases, cron or service files, and the MCP settings of editors that you wrote by hand (`devbrain mcp --client external` becomes `cairnmind mcp --client external`). Until you do, those calls fail with `devbrain: command not found`. In a project's `.mcp.json`, `.claude/settings.local.json`, `.codex/config.toml` and `.codex/hooks.json`, `cairnmind install` names each MCP server or hook that starts `devbrain` and leaves it unchanged: where it has just written its own entry, remove yours; otherwise write `cairnmind` in it.

What you do not change:

- The files `cairnmind install` writes never call the `devbrain` command; they start Cairnmind's own Python. It still rewrites the entries and blocks that DevBrain 0.8.0 wrote under the `devbrain` name.
- Your data keeps its names: the data directory `~/.local/share/devbrain` (if your installation started there), the database `devbrain_<hash>` (a new installation, from 0.13.0, names its database `cairnmind_<hash>`; one that has a config.json or database files keeps the earlier name, and `devbrain_meta` and the other type names inside the database are not renamed), `.memory/devbrain`, `.devbrain/checks.toml`, backups ending in `.devbrain.json.gz` and the `DEVBRAIN_*` variables are still read.
- `python -m devbrain` still works for project files of DevBrain 0.8.0.

Going back to 0.12.0 with its installer brings the `devbrain` alias back.

### Only a person confirms, in the open workspace

Close the workspace before the upgrade (step 2) and open it again afterwards: a workspace that 0.12.0 started holds no key.

- **A person's acts.** A review, a conflict decision, replacing a record, a plan confirmation, accepting or withdrawing a transcript record kind, the finding form, adopting an old task group, deleting a project's memory and an import that replaces records you confirmed or disputed need a key that the open workspace keeps in its tmux server; the service token alone is refused and nothing is written. The inspector and the finding form send the key without asking you anything. Panels that 0.12.0 started do not send it: after upgrading, run `cairnmind workspace stop` and open the workspace again with `cairnmind`. `cairnmind workspace status` shows `person_key`: `present`, `absent` (a workspace an earlier version started) or `unavailable`.
- **How a person acts in the workspace.** Every one of these acts but the finding form takes two steps: a form that collects the reason, note or title (its button says **Continue** and sends nothing; an import runs its check instead), then a consent dialog that shows the record, plan or import, what the act does and what you typed. Only the dialog's approval button sends the act, and the dialog counts only keys and clicks made after it appeared, so a key typed ahead never makes an act. The finding form still saves with `Ctrl+S` and sends only what was typed and chosen in it.
- **The command line confirms nothing.** `cairnmind remember --confirmed`, `cairnmind memory confirm-plan` and `cairnmind record-kind accept` or `withdraw` are refused with exit code 2 and say that the person does it in the inspector of the workspace; `remember --confirmed` stores nothing, not even a proposed record. `cairnmind task add` stores a proposed task and says so. A script or a document that used `--confirmed` has to store the record without it and confirm it in the inspector. Records that 0.12.0 confirmed this way keep their mark.
- **Plans.** Confirm a plan in the inspector: open Plans, choose the plan, then Confirm. `cairnmind plan save` and `cairnmind plan adopt` can no longer change a step you confirmed, or a confirmed plan's title, goal and constraints: dispute the step in the inspector first. A group of tasks that 0.10.0 or earlier added with `task add` counts as confirmed before the rule: adopt it in the workspace (More, then Old task groups, choose the group, give the plan a title, then Adopt in the consent dialog), which makes its tasks the steps of a new plan in one act; `cairnmind plan adopt` still adopts a group that holds nothing you confirmed.
- **Imports.** `--replace-confirmed` on `memory import`, `memory import-published` and `memory import-commit` is refused by the service. Run such an import from the memory view of the workspace: Import for an archive, Team memory for a published folder or a commit. Both list every record you confirmed or disputed that the import replaces, in the check and in the consent dialog, and import only that set: if the archive, the published memory or this computer's memory changed so that another set would be replaced, nothing is imported; check again.
- **Impact verdicts and releases.** `cairnmind impact-verdict` and `cairnmind memory release` are refused with exit code 2 and say where the person does it: a verdict in the inspector's Impact view (Verdict), a release on the open record in the inspector (Release). `POST /inspector/impact/verdict` and `POST /inspector/records/{id}/release` need the open workspace's key like the other person's acts.
- **Deleting a project's memory.** `cairnmind forget PROJECT --delete-project` is refused with exit code 2 and deletes nothing, with or without `--delete-project`: a script that called it now gets `Error: The command line no longer deletes a project's memory. ...` on standard error. The person deletes a project's memory in the workspace: More, then Delete project memory, choose the project (removed projects are listed too), read what is deleted, then Delete in the consent dialog. It is refused while the project has a session in the workspace or a job in work. Backups are not changed, as before, and neither are the project's folders. `cairnmind status` lists each deletion under `deleted_projects`. `POST /inspector/projects/forget` needs the open workspace's key like the other person's acts. If the workspace cannot open, see "Deleting a project's memory" in [STORAGE_AND_MAINTENANCE.md](STORAGE_AND_MAINTENANCE.md#deleting-a-projects-memory).
- **Replacing a record.** In the inspector, Replace on an open record saves a new text as a new record confirmed by you and supersedes the one you opened, in one act with an audit note and the record version check (#222). It needs the open workspace's key (`POST /inspector/records/{id}/replace`); the command line has no such command, and a session's `remember` with `supersedes` still stores only a proposed replacement. Nothing in an existing store changes.
- **Restore.** `cairnmind restore` keeps the person's confirmations of a backup when the workspace is open and holds its key; any program that runs the restore at that moment keeps them too, as [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md) says under "What the rule does not claim" (`cairnmind workspace status` shows `person_key: present`): leave the workspace open, run `cairnmind stop`, then `cairnmind restore FILE`. Without it (the store is broken and the workspace cannot start, a server, CI) every row is still restored, but each person's confirmation becomes the backup's claim, shown as "..., as the backup says; restored from <file> on <day> without the person's workspace; not verified here", and every accepted transcript record kind is withdrawn; the output says which way it went (`person_confirmations`: `kept` or `claims_of_the_backup`) and how many, and the exit code is 0 either way. Confirm again in the inspector what still holds, or restore the `before-restore-*` copy with the workspace open to undo it. Restoring with an earlier version, as the going-back steps of earlier releases do, is not affected by this rule.
- **What stays possible for a program of your own user.** docs/MEMORY_CONTRACTS.md, "What the rule does not claim", lists it.

### Going back to 0.12.0

Going back was not tried on a real installation. From the code: the schema and the backup format are the same, so the store needs no restore, and 0.12.0 ignores the three new settings in `config.json`. 0.12.0 does not know a container, so if you moved the engine to Docker, run `cairnmind stop`, `cairnmind engine stop` and `cairnmind engine start --native` with 0.13.0 first. Then close the workspace and coding sessions as in step 2, run `bash scripts/install-wsl.sh` from the unpacked 0.12.0 release archive, and run `cairnmind install <project>` for each project.

## From 0.7.2 to 0.8.0

The runtime qualification this upgrade requires is produced with maintainer tooling that is not part of this repository.

The guarded updater `devbrain-update-0.8.0.py` is attached to the [0.8.0 release](https://github.com/piotrwozn/cairnmind/releases/tag/v0.8.0). It supports **0.7.2** sources that match the archived 0.7.2 package, and it refuses to overwrite or delete any file you changed locally.

### 1. Check the files only

```bash
python3 /path/to/devbrain-update-0.8.0.py --source-dir ~/src/devbrain --check-only
```

`--check-only` inspects files only. It starts no subprocesses or services, opens no database and creates no backup. Passing it does not prove that the environment is stable.

### 2. Apply the update

Applying requires all four arguments:

```bash
/exact/path/to/qualified/python3 /path/to/devbrain-update-0.8.0.py \
  --source-dir ~/src/devbrain \
  --python /exact/path/to/qualified/python3 \
  --backup-python /separate/backup-072-venv/bin/python \
  --runtime-gate /path/to/passed/gate.json \
  --data-dir ~/.local/share/devbrain
```

- `--runtime-gate` is a passed runtime qualification report for this machine. It binds the exact Python, Java and tmux binaries by SHA-256 and lists the mandatory checks. Without a passed gate the updater stops before it reads any data configuration or runs status, stop or backup commands.
- `--backup-python` points to the launcher of a separate venv holding an unmodified **0.7.2** wheel, built from the archived ZIP with its pinned SHA. The venv must live outside the updated sources and the data directory. The updater verifies the base Python, package version, origin, full file manifest and dependency versions before it uses this interpreter for status, stop and backup.
- You can prepare that venv with `uv venv --python <qualified-base> <backup-venv>`, then `uv export --directory <immutable-072-source> --locked --no-dev --no-emit-project --format requirements-txt --output-file <constraints>`, then `uv pip install --python <backup-venv>/bin/python --constraints <constraints> <immutable-072-source>`. Do not use `--editable` or add unpinned dependencies.

### What the updater does

1. Refuses to touch files changed locally, then stops the interface hosts and the knowledge service. Native client sessions keep running.
2. Saves every file it will change or delete, plus a database backup, under `~/.local/state/devbrain-updates/`, and verifies the backup's format, checksum and private permissions before changing sources.
3. Writes new files and deletes files removed in 0.8.0, such as the old browser cockpit and `tui.py`, only when they still match the 0.7.2 baseline.
4. Runs the installer with the qualified Python and verifies the installed package against the exact 0.8.0 file manifest.

Credentials, models, profiles and native client permissions are outside the updated files. There is no automatic rollback after a failed installation; restore from the saved files and database backup. Repeating an identical update after success is a no-op.

## Release notes

See the [changelog](../CHANGELOG.md).
