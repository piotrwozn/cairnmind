# Upgrading

New installations follow [INSTALL.md](INSTALL.md). This guide is for an existing installation with data you want to keep.

- [From DevBrain 0.8.0 to Cairnmind 0.9.0](#from-devbrain-080-to-cairnmind-090)
- [From 0.9.0 to 0.10.0](#from-090-to-0100)
- [From 0.10.0 to 0.11.0](#from-0100-to-0110)
- [From 0.11.0 to 0.12.0](#from-0110-to-0120)
- [From 0.7.2 to 0.8.0](#from-072-to-080)

A direct upgrade from 0.7.2 to 0.9.0 was not tested. Upgrade a 0.7.2 installation to 0.8.0 first.

Upgrading from 0.11.0 is a backup, the installer and one `cairnmind install` per project; on Linux and WSL it changes no data or configuration. Upgrading from 0.10.0 to 0.11.0 is a backup, the installer and one `cairnmind install` per project; its first start marks the confirmed records that a person's act on this computer covers. Upgrading from 0.9.0 to 0.10.0 is the installer and one `cairnmind install` per project. DevBrain 0.8.0 first goes to 0.9.0, which is a backup, the installer and one `cairnmind install` per project; an existing 0.7.2 installation first goes to 0.8.0 through a guarded updater that requires a passed runtime qualification of the host before it touches existing data. Each has its section below.

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

- **Data directory.** An existing `~/.local/share/devbrain` is used where it is. Only an installation with no data yet starts in `~/.local/share/cairnmind`. If both directories hold data, the earlier one is used and `cairnmind doctor` says so under `environment_notes`.
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
- **Questions.** A question no longer starts a second reading of whole passages when the time left is shorter than the calls it repeats, so it can end sooner with what it has. A model server that refuses structured output is asked again without it, and `cairnmind doctor` says so; the refusal was seen with an MLX build of Ollama on Apple silicon.

### What changes on macOS

macOS was not supported before 0.12.0, so most Macs get a new installation from [INSTALL.md](INSTALL.md). If a data directory on a Mac was already used by 0.11.0 or by a build of `main` before 0.12.0:

- **Writer.** A Mac is identified by its hardware UUID (IOPlatformUUID) from 0.12.0, and before #259 it was identified by its host name. Such a data directory therefore no longer matches its own identity: it gets a new installation identifier and reads its store only. `cairnmind store writer` shows this; run `cairnmind store takeover --yes` once to write again. From then on a change of the host name or network does not change the identity.
- **Question budget.** A new installation on macOS gives each question 120 seconds. A data directory created before #264 keeps the 60 its `config.json` holds, which can be too short for a local model on Apple silicon. To use 120, stop the service (`cairnmind stop`), set `"ask_timeout": 120` in `config.json` of the data directory, and run `cairnmind install <project>` again for each project, so Codex waits 135 seconds for a Cairnmind tool.
- **Sockets.** The workspace and coding-session sockets live in `/tmp/cairnmind-<uid>/`, a folder only you can use, instead of under `$TMPDIR`. A workspace started before the upgrade is not found after it; that is why step 2 stops it.
- **Java.** On macOS the engine takes Java from `JAVA_HOME` before `PATH`; a `java_executable` in `config.json` still comes first.

### Going back to 0.11.0

1. Close the workspace and coding sessions, then stop everything with the installed version: `cairnmind workspace stop --clients`, `cairnmind cockpit stop`, `cairnmind stop`.
2. Run `bash scripts/install-wsl.sh` from the unpacked 0.11.0 release archive.
3. Run `cairnmind install <project>` for each project.

The store needs no restore: 0.11.0 reads what 0.12.0 wrote. On Linux and WSL the installation stays the writer. Going back is for Linux and WSL: 0.11.0 does not support macOS.

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
