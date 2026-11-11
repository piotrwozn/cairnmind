# Upgrading

New installations follow [INSTALL.md](INSTALL.md). This guide is for an existing installation with data you want to keep.

- [From DevBrain 0.8.0 to Cairnmind 0.9.0](#from-devbrain-080-to-cairnmind-090)
- [From 0.7.2 to 0.8.0](#from-072-to-080)

A direct upgrade from 0.7.2 to 0.9.0 was not tested. Upgrade a 0.7.2 installation to 0.8.0 first.

## From DevBrain 0.8.0 to Cairnmind 0.9.0

0.9.0 is the first release under the name Cairnmind. The command is `cairnmind` and the Python module is `cairnmind`; `devbrain` remains as a command alias and, for one release, as an import alias. Your memory stays where it is: the data directory `~/.local/share/devbrain`, the `DEVBRAIN_*` variables, `.memory/devbrain` and `.devbrain/checks.toml` keep their names in this release.

### 1. Make a backup with the installed version

```bash
devbrain backup ~/devbrain-backup-0.8.0.devbrain.json.gz
```

### 2. Close the workspace and coding sessions

Close the terminal workspace and any Codex or Claude Code session started from it. A workspace or worker that keeps running from before the upgrade keeps its old processes; they are not migrated.

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

There is no automatic rollback. To go back, reinstall 0.8.0 from its release archive and restore the backup.

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
