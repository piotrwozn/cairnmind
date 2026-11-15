# Running the database in a container

By default Cairnmind installs ArcadeDB itself and starts it on `127.0.0.1:2480` when it needs it (see [INSTALL.md](INSTALL.md)). This page is for running that database yourself instead, in a container: on a server, or on a laptop for development. It covers the image, a Compose file for one machine, backups and restores, and what to do when the pinned ArcadeDB version changes.

What is not here yet, and why:

| Not yet | Waits for |
| --- | --- |
| Kubernetes manifests, a Secret with the credentials, a dedicated database user instead of `root` | [#72](https://github.com/piotrwozn/cairnmind/issues/72), the database credentials |
| Connecting over the network with TLS | [#72](https://github.com/piotrwozn/cairnmind/issues/72); today Cairnmind connects over plain HTTP to a loopback address only |
| An installation that only connects and never starts a local engine | [#68](https://github.com/piotrwozn/cairnmind/issues/68) |
| A restore that is safe while another machine writes to the same database | [#70](https://github.com/piotrwozn/cairnmind/issues/70) |

## What is in `deploy/`

| File | What it is |
| --- | --- |
| `deploy/arcadedb/Dockerfile` | The ArcadeDB release Cairnmind pins (26.11.1) on Java 21 (Eclipse Temurin). The release archive is checked against its published SHA-256, as `cairnmind engine install` does. |
| `deploy/arcadedb/entrypoint.sh` | Starts the server with the same JVM options and server settings as `cairnmind engine start`: 1 GB heap, the `low-ram` profile, and the vector module. The server runs as user `arcadedb` (uid 10001). |
| `deploy/arcadedb/healthcheck.sh` | The container health check: `GET /api/v1/ready`. |
| `deploy/compose.yaml` | One server with a named volume, publishing the HTTP port on `127.0.0.1` only. |

No password is built into the image or written in any of these files. The server reads the root password from a file at run time, and the Compose file takes that file's path from the environment.

The image is not published to a registry. You build it from this repository.

## Start it on one machine

You need Docker with Compose v2 (`docker compose version`). Run these commands from the root of a Cairnmind checkout of the version you use, so that the image matches the ArcadeDB version that version pins.

1. Create the root password file. It stays on this machine and is never committed:

   ```bash
   mkdir -p "$HOME/.config/cairnmind-db"
   (umask 077; openssl rand -base64 32 > "$HOME/.config/cairnmind-db/root-password")
   ```

   ArcadeDB needs at least 8 characters. A trailing newline in the file is ignored.

2. Build and start the server, and wait until it reports healthy:

   ```bash
   export ARCADEDB_ROOT_PASSWORD_FILE="$HOME/.config/cairnmind-db/root-password"
   docker compose -f deploy/compose.yaml up -d --build --wait
   ```

   Optional settings, also from the environment:

   | Variable | Default | Meaning |
   | --- | --- | --- |
   | `ARCADEDB_BIND` | `127.0.0.1` | Host address of the published port. Keep it on loopback; see below. |
   | `ARCADEDB_PORT` | `2480` | Host port. |
   | `ARCADEDB_JAVA_OPTS` | empty | Extra JVM options, for example `-Xmx4g` for a larger heap. |

3. Check that it answers:

   ```bash
   curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2480/api/v1/ready   # 204
   docker compose -f deploy/compose.yaml ps                                          # healthy
   ```

The password is read only on the first start, when ArcadeDB creates the `root` user and stores it in the volume. Changing the file later does not change the password.

The databases, the server configuration and the server log are in the volume `cairnmind-db_arcadedb-data`. `docker compose -f deploy/compose.yaml down` stops and removes the container and keeps the volume. `down --volumes` deletes the data, and that cannot be undone.

## Connect Cairnmind to it

Cairnmind reads the database address and credentials from the environment:

```bash
export CAIRNMIND_ARCADE_URL=http://127.0.0.1:2480
export CAIRNMIND_ARCADE_USER=root
export CAIRNMIND_ARCADE_PASSWORD="$(cat "$HOME/.config/cairnmind-db/root-password")"
cairnmind engine status    # "available": true, "version_matches": true
cairnmind init
cairnmind doctor           # the "arcadedb" block: available, version_matches, database_available
```

When a server of the pinned version answers at that address, Cairnmind uses it and starts no engine of its own. A server of a different version is refused, with "ArcadeDB release does not match the pinned" version.

Things to know today:

- **Loopback only.** Cairnmind accepts only a plain-HTTP address on `127.0.0.1`, `localhost` or `::1`. To use a container on another machine, forward the port over SSH and point Cairnmind at the local end: `ssh -N -L 2480:127.0.0.1:2480 you@server`. Connecting over the network with TLS comes with [#72](https://github.com/piotrwozn/cairnmind/issues/72). Do not publish the port on a public address: the HTTP API with the root password would be readable on the network.
- **The password is saved in the data directory.** The first command in a new data directory writes its `config.json`, including the password it was given, readable only by you.
- **One database per data directory.** The database name comes from the data directory's path unless you set `CAIRNMIND_ARCADE_DATABASE`; `cairnmind doctor` shows it as `arcadedb.database`. Two data directories that should use the same database must set the same name.
- **No local engine to stop.** `cairnmind engine stop` stops only an engine Cairnmind started itself. Stop the container with `docker compose`.
- **A local engine can take the container's place.** If you followed [INSTALL.md](INSTALL.md) first, the data directory has a local engine installed. Until [#68](https://github.com/piotrwozn/cairnmind/issues/68), a command that writes (`cairnmind init`, `cairnmind remember`, `cairnmind project add`, `cairnmind restore`) starts that engine when nothing answers at `CAIRNMIND_ARCADE_URL`: on the same port, with the container's password, and it writes there instead of into the container. Afterwards the container cannot publish its port. The other way round, a local engine still running keeps `docker compose up` from publishing port 2480, and Cairnmind reaches that engine with the container's password, which it refuses. So before you start the container, stop the local one (`cairnmind stop`, then `cairnmind engine stop`), and check `cairnmind engine status` before each write while the container is down, or use a separate `--data-dir` that has no engine installed.

## Backup and restore

There are two kinds of backup. Take the logical one regularly; add a volume snapshot when you want the whole server as it was.

### Logical backup: `cairnmind backup` and `cairnmind restore`

The logical backup is a checksummed, compressed JSON export of everything Cairnmind keeps in the database: projects, records and their history, sources, the graph, jobs and stored vectors. It is read over the HTTP API, so it works the same against a container as against the local engine, and it does not depend on the ArcadeDB version.

```bash
cairnmind backup "$HOME/backups/"                       # an existing folder: a new timestamped file in it
cairnmind backup "$HOME/backups/before-upgrade.cairnmind.json.gz"
```

To restore it into a server, for example a new empty one:

1. Stop every Cairnmind service that uses this database: `cairnmind stop` on each machine. `restore` checks only the service of its own data directory; a writer on another machine is not detected (that is [#70](https://github.com/piotrwozn/cairnmind/issues/70)).
2. Point the environment at the server, as in [Connect Cairnmind to it](#connect-cairnmind-to-it), and restore:

   ```bash
   cairnmind init
   cairnmind restore "$HOME/backups/before-upgrade.cairnmind.json.gz"
   cairnmind project list
   ```

`restore` replaces everything in the database with the backup. Before it does, it writes a backup of the current contents to `backups/` in the data directory.

What the logical backup does not contain: the files of the data directory (`config.json` with the local token and the database password, client credentials and API keys, the terminal workspace state, the hook spool, logs), ArcadeDB's own server configuration and users, and model weights.

### Volume snapshot

A copy of the whole volume: the database files, the server configuration and its users. Take it with the server stopped, so the files are consistent:

```bash
docker compose -f deploy/compose.yaml stop
docker run --rm --entrypoint tar -v cairnmind-db_arcadedb-data:/var/lib/arcadedb:ro -v "$HOME/backups:/backup" \
    cairnmind-arcadedb:local --numeric-owner -C /var/lib/arcadedb -czf /backup/arcadedb-volume.tgz .
docker compose -f deploy/compose.yaml start
```

To restore it into a new, empty volume (on this machine after `down --volumes`, or on another one with the same image):

```bash
docker compose -f deploy/compose.yaml up --no-start
docker run --rm --entrypoint tar -v cairnmind-db_arcadedb-data:/var/lib/arcadedb -v "$HOME/backups:/backup:ro" \
    cairnmind-arcadedb:local --numeric-owner -C /var/lib/arcadedb -xzf /backup/arcadedb-volume.tgz
docker compose -f deploy/compose.yaml up -d --wait
```

The `root` password is the one stored in the snapshot, not the one in the file. Cairnmind finds its database by name, and the default name follows the path of its data directory: from another data directory, set `CAIRNMIND_ARCADE_DATABASE` to the name `cairnmind doctor` showed as `arcadedb.database` where the snapshot was taken, then run `cairnmind init`. What a snapshot does not cover: everything in Cairnmind's data directory (as for the logical backup), and other ArcadeDB versions: open it with the same image, or move between versions with a logical backup.

Both procedures are run by the `deployment-image` CI job: a project and a record are written to one server, backed up and restored into a second, empty one, and the first server's volume is archived and unpacked into a third. Each server then holds the same project and records.

## When the pinned ArcadeDB version changes

Each Cairnmind release pins one ArcadeDB version (`arcade_version`, shown by `cairnmind engine status` as `expected_version`). The release notes say when it changes. Cairnmind refuses a server of another version, so the server and Cairnmind are upgraded together:

1. With the old version still running, take a logical backup: `cairnmind backup "$HOME/backups/"`. Stop the Cairnmind services, then take a volume snapshot.
2. Upgrade Cairnmind, and update the checkout this page runs from to the same version.
3. Rebuild and restart the server: `docker compose -f deploy/compose.yaml up -d --build --wait`. ArcadeDB opens the existing database files with the new version.
4. Check with `cairnmind engine status` and `cairnmind doctor`.

If the new server does not start or reports errors, either go back to the old checkout and image with the volume snapshot, or start the new version on an empty volume and `cairnmind restore` the logical backup.
