# Upgrading

New installations follow [INSTALL.md](INSTALL.md). This guide is for an existing installation with data you want to keep.

- [From DevBrain 0.8.0 to Cairnmind 0.9.0](#from-devbrain-080-to-cairnmind-090)
- [From 0.9.0 to the next release](#from-090-to-the-next-release)
- [From 0.7.2 to 0.8.0](#from-072-to-080)

A direct upgrade from 0.7.2 to 0.9.0 was not tested. Upgrade a 0.7.2 installation to 0.8.0 first.

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

## From 0.9.0 to the next release

Finish and close running coding sessions and the workspace (`cairnmind workspace stop`), then run the installer and `cairnmind install <project>` for each project. The installer stops the interface broker and the knowledge service of the installed version itself; it refuses to continue while a workspace or a coding session started from it is running, in any data directory, because the new version names its terminal sessions differently and would not find them. Set `CAIRNMIND_INSTALL_STOP_WORKSPACE=1` to let it close them. Scheduled daily backups of both names are pruned together; the safety copies written before a restore or a schema change are never pruned. Upgrade every machine and every collaborator that shares a project's repository together: a version up to 0.9.0 does not read what this one writes there.

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
