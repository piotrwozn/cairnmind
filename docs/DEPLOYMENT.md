# Running the database in a container

By default Cairnmind installs ArcadeDB itself and starts it on `127.0.0.1:2480` when it needs it (see [INSTALL.md](INSTALL.md)). This page is for running that database yourself instead, in a container: on a server, on a laptop for development, or in Kubernetes. It covers the image, a Compose file for one machine, Kubernetes manifests, backups and restores, and what to do when the pinned ArcadeDB version changes.

What is not here yet, and why:

| Not yet | Waits for |
| --- | --- |
| An installation that only connects and never starts a local engine | [#68](https://github.com/piotrwozn/cairnmind/issues/68) |
| A restore that is safe while another machine writes to the same database | [#70](https://github.com/piotrwozn/cairnmind/issues/70) |

## What is in `deploy/`

| File | What it is |
| --- | --- |
| `deploy/arcadedb/Dockerfile` | The ArcadeDB release Cairnmind pins (26.11.1) on Java 21 (Eclipse Temurin). The release archive is checked against its published SHA-256, as `cairnmind engine install` does. |
| `deploy/arcadedb/entrypoint.sh` | Starts the server with the same JVM options and server settings as `cairnmind engine start`: 1 GB heap, the `low-ram` profile, and the vector module, plus ArcadeDB's production mode (see [What the server exposes](#what-the-server-exposes)). The server runs as user `arcadedb` (uid 10001). |
| `deploy/arcadedb/healthcheck.sh` | The container health check: `GET /api/v1/ready`. |
| `deploy/compose.yaml` | One server with a named volume, publishing the HTTP port on `127.0.0.1` only. |
| `deploy/kubernetes/` | Plain Kubernetes manifests (no Helm chart) for one server with TLS: see [Kubernetes](#kubernetes). |

No password is built into the image or written in any of these files. The server reads the root password from a file at run time, and the Compose file takes that file's path from the environment.

The image is not published to a registry. You build it from this repository.

### What the server exposes

The container may be reached from other hosts, so it differs from the local engine (which listens on loopback only and is not changed) in two ways:

- **Production mode** (`-Darcadedb.server.mode=production`). Compared with ArcadeDB's default development mode, the server does not serve the Studio web tool (`GET /` answers 404), leaves the error details out of HTTP responses and writes them to its own log instead (Cairnmind then reports, for example, `ArcadeDB HTTP 400: Cannot execute command` with no detail: look in the server log), flushes the write-ahead log on each commit (`arcadedb.txWalFlush=1`), and refuses `LOAD CSV` of local files. Cairnmind uses none of what development mode adds; every check on this page passes in production mode. To go back, add `-Darcadedb.server.mode=development` to `ARCADEDB_JAVA_OPTS`.
- **No MCP plugin.** ArcadeDB 26.11.1 starts its MCP plugin whenever the jar is present (no setting leaves it out) and serves `/api/v1/mcp`. That endpoint asks for the same credentials as the rest of the HTTP API and is switched off by default, but `root` can switch it on at run time (`POST /api/v1/mcp/config`) for itself and other users. Cairnmind does not use it, so the image leaves the jar out and the endpoint does not exist.

What remains is ArcadeDB's HTTP API, with its own users and passwords: `/api/v1/ready` and `/api/v1/health` answer without a password (no data), everything else needs one. Who can reach it is set by where you publish the port: `127.0.0.1` only in the Compose file, and in Kubernetes only pods labelled `cairnmind.io/database-client: "true"`, on the TLS port 2490 (see [TLS: where it ends](#tls-where-it-ends) for what holds when the network plugin does not enforce the policy).

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

- **Loopback only for plain HTTP.** This Compose file serves plain HTTP, which Cairnmind accepts only on `127.0.0.1`, `localhost` or `::1`. To use a container on another machine, forward the port over SSH and point Cairnmind at the local end: `ssh -N -L 2480:127.0.0.1:2480 you@server`. For a server reached over the network, use TLS, a dedicated user and a password from outside `config.json`, as in [Kubernetes](#kubernetes) (the same settings work for any server with TLS). Do not publish the port on a public address: the HTTP API with the root password would be readable on the network.
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

## Kubernetes

`deploy/kubernetes/` holds plain manifests for one ArcadeDB server, not a Helm chart. The image is the one of `deploy/arcadedb`, built from this repository; nothing is published.

| File | What it is |
| --- | --- |
| `00-namespace.yaml` | The namespace `cairnmind-db`. Its pods must meet the `restricted` Pod Security Standard. |
| `10-networkpolicy.yaml` | Lets in only pods labelled `cairnmind.io/database-client: "true"`, on the TLS port 2490; the server opens no connection itself. |
| `20-service.yaml` | The headless Service `arcadedb`, port 2490 (TLS) only. |
| `30-statefulset.yaml` | One server as uid 10001 with no capabilities and a read-only root file system, a 10 GiB volume claim for `/var/lib/arcadedb`, and the image's health check (`GET /api/v1/ready`) as its startup, readiness and liveness probes. |
| `templates/secrets.yaml` | The shape of the Secrets, with every value empty. Never write a value into it. |
| `backup/` | A pod that holds the volume while the server is stopped, and an empty claim to restore into. |
| `check/` | The connection check as a Job, the script it runs and the image it runs in. |

`kubectl apply -f deploy/kubernetes` reads only the top folder, so it never applies the template, the backup pod or the check.

The `kubernetes-manifests` CI job, beside the other jobs and not a required check, runs [`scripts/kubernetes_check.py`](../scripts/kubernetes_check.py) on a kind cluster: it checks every manifest against the cluster's schemas (`kubectl apply --dry-run=server --validate=strict`), creates the Secrets as below, applies the manifests, runs the connection check as the Job (with both passwords mounted from Secrets) and again from the runner through `kubectl port-forward`, takes a logical backup and restores it, and restores a volume snapshot into a new volume. You can run the script against any disposable cluster.

### What to change

| What | Where |
| --- | --- |
| The image | `image:` in `30-statefulset.yaml` (and `backup/volume-pod.yaml`): build it with `docker build -t REGISTRY/cairnmind-arcadedb:VERSION deploy/arcadedb` and push it where the cluster pulls from, or load it into a local cluster (`kind load docker-image cairnmind-arcadedb:local`). Build it from the checkout of the Cairnmind version you use, so that the ArcadeDB versions match. |
| Storage | `volumeClaimTemplates` in `30-statefulset.yaml`: the size, and `storageClassName` for a class other than the default. `backup/data-claim.yaml` must stay equal. |
| Memory | The heap is 1 GB (`-Xmx1024m`). For more, add `-Xmx4g` to `ARCADEDB_JAVA_OPTS` and raise `resources` with it. |
| The namespace | `namespace:` in every file, and the certificate's names below. |
| Who may connect | The `from` of `10-networkpolicy.yaml`: today any namespace; add a `matchLabels` to its `namespaceSelector` to narrow it. |

### TLS: where it ends

TLS ends at the ArcadeDB server itself. The pinned release, 26.11.1, has its own HTTPS listener (the `arcadedb.ssl.*` settings), so no proxy or other software is needed: the StatefulSet turns it on through `ARCADEDB_JAVA_OPTS`, on port 2490, with the key and certificate of the Secret `arcadedb-tls` as one PKCS#12 store.

ArcadeDB's plain HTTP listener (port 2480) cannot be turned off and listens on the same addresses. Only the probes use it, from inside the pod; the Service does not offer it and the NetworkPolicy does not let anything reach it. A NetworkPolicy is enforced only by a network plugin that supports it: without one, another pod that knows the server's address can reach port 2480, unencrypted (it still needs a password). The CI job reports whether its cluster enforces the policy: that a pod without the label cannot open port 2490, and that a labelled pod can open 2490 but not 2480. The network plugin of kind v0.27.0 enforces it.

Make a private certificate authority and the server's store with `openssl`, on a machine you trust, in a folder only you can read. The certificate must name the address clients use; these names cover the Service and `kubectl port-forward`:

```bash
mkdir -p "$HOME/cairnmind-k8s" && cd "$HOME/cairnmind-k8s" && umask 077
openssl req -x509 -newkey rsa:3072 -nodes -keyout ca.key -out ca.pem -days 825 \
    -subj "/CN=Cairnmind database authority" \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign
openssl req -newkey rsa:3072 -nodes -keyout tls.key -out tls.csr -subj "/CN=arcadedb"
printf '%s\n' "subjectAltName=DNS:arcadedb.cairnmind-db.svc,DNS:arcadedb.cairnmind-db.svc.cluster.local,DNS:localhost,IP:127.0.0.1" \
    "basicConstraints=CA:FALSE" "extendedKeyUsage=serverAuth" > tls.ext
openssl x509 -req -in tls.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 397 -out tls.crt -extfile tls.ext
openssl rand -hex 24 | tr -d '\n' > store-password
openssl pkcs12 -export -in tls.crt -inkey tls.key -certfile ca.pem -name arcadedb \
    -out keystore.p12 -passout file:store-password
```

The store password must be letters and digits (as here): it reaches the JVM on its command line, where whoever may read the Secret can also see it. Keep `ca.key` off the cluster. Cairnmind trusts `ca.pem` through `arcade_ca_file`, always with host name verification. The server reads its store when it starts: to renew the certificate, replace the Secret and run `kubectl -n cairnmind-db rollout restart statefulset/arcadedb`.

### Start it

1. The namespace, the root password and the Secrets, from files (still in `$HOME/cairnmind-k8s`):

   ```bash
   kubectl apply -f deploy/kubernetes/00-namespace.yaml
   (umask 077; openssl rand -hex 24 | tr -d '\n' > root-password)
   kubectl -n cairnmind-db create secret generic arcadedb-root-password --from-file=password=root-password
   kubectl -n cairnmind-db create secret generic arcadedb-tls --from-file=keystore.p12 --from-file=store-password
   ```

   As with Compose, the root password is read only on the first start, when ArcadeDB creates `root` and stores it in the volume. The server reads the mounted files through its group (`fsGroup: 10001`, `defaultMode: 0440`).

2. The server, and wait until it is ready:

   ```bash
   kubectl apply -f deploy/kubernetes
   kubectl -n cairnmind-db rollout status statefulset/arcadedb --timeout=300s
   ```

### The dedicated user and the connection check

Cairnmind refuses `root`, plain HTTP and a password from `config.json` for a database on another host (see [SECURITY.md](../SECURITY.md)). Create a user with rights on the Cairnmind database only, once, from your machine through a port-forward. Its password comes from a file of your user with mode `0600`:

```bash
kubectl -n cairnmind-db port-forward svc/arcadedb 2490:2490 &
(umask 077; openssl rand -hex 24 > "$HOME/cairnmind-k8s/user-password")
export CAIRNMIND_ARCADE_URL=https://localhost:2490
export CAIRNMIND_ARCADE_USER=cairnmind
export CAIRNMIND_ARCADE_DATABASE=cairnmind
export CAIRNMIND_ARCADE_PASSWORD_FILE="$HOME/cairnmind-k8s/user-password"
export CAIRNMIND_ARCADE_CA_FILE="$HOME/cairnmind-k8s/ca.pem"
cairnmind db create-user --admin-password-file "$HOME/cairnmind-k8s/root-password"
```

`create-user` signs in as `root` for that step alone, over the same verified TLS, creates the database and the user in its `admin` group, and then signs in as the new user. Then the connection check:

```bash
cairnmind engine status   # "available": true, "version_matches": true
cairnmind init
cairnmind doctor          # arcadedb: "database_available": true, "user": "cairnmind",
                          # "transport": "https (certificate verification on)", "password_source": "file"
```

A store has one writer: the installation that first opened it. If another installation ran `init` on this database before yours (the connection check Job below does), yours only reads, and a write is refused with the name of the writer. `cairnmind store writer` shows which installation writes, and `cairnmind store takeover --yes` makes yours the writer; the other one stops at its next write.

From inside the cluster the address is `https://arcadedb.cairnmind-db.svc:2490`, and the pod needs the label `cairnmind.io/database-client: "true"`. `check/connection-check.yaml` is that check as a Job; `check/connection-check.sh` lists the settings it uses.

### Cairnmind in a pod: the password from a mounted Secret

Kubernetes mounts a Secret as links: the key `password` is a link to `..data/password`, and `..data` is a link to a folder named after the time of the last update, which holds the real file. Cairnmind follows exactly that layout for `arcade_password_file` (and for `--admin-password-file`) and nothing wider: the file at the end must be a regular file inside the folder of the link, belong to the user that runs Cairnmind, and be unreadable by group and others. Any other link, one that leaves the folder, points to a folder or loops, and a file that others can read, are refused with a message that names the rule.

Kubernetes makes the files of a Secret volume owned by `root`; `fsGroup` changes only their group and adds read access for it. So:

- **Cairnmind running as root in its container** reads the mounted file directly. Mount the Secret with `defaultMode: 0400` and set no `fsGroup` on that pod, as `check/connection-check.yaml` does (it also drops every capability).
- **Cairnmind running as another user** cannot own the mounted file. Copy it first, in an init container of the same pod with `fsGroup` and `defaultMode: 0440` on the Secret, into an `emptyDir`: `install -m 0600 /run/secrets/cairnmind-db-user/password /run/cairnmind/password`. Then set `arcade_password_file` to `/run/cairnmind/password`, a regular file of that user. This variant is not run by CI.

Keep the root password out of the namespaces of Cairnmind's pods: create the user once from your machine, as above. (The CI check copies it into its own namespace only because its Job runs `create-user`.)

### Backup and restore

**Logical backup.** Through the port-forward and the settings above, as for any server: `cairnmind backup "$HOME/backups/"`, and `cairnmind restore` as described in [Backup and restore](#backup-and-restore), after stopping every Cairnmind service that uses the database. The dedicated user can do both.

**Volume snapshot.** Stop the server, hold its volume with the pod of `backup/volume-pod.yaml`, archive it, and start the server again:

```bash
kubectl -n cairnmind-db scale statefulset/arcadedb --replicas=0
kubectl -n cairnmind-db wait --for=delete pod/arcadedb-0 --timeout=180s
kubectl apply -f deploy/kubernetes/backup/volume-pod.yaml
kubectl -n cairnmind-db wait --for=condition=Ready pod/arcadedb-volume --timeout=180s
kubectl -n cairnmind-db exec arcadedb-volume -- tar --numeric-owner -C /var/lib/arcadedb -czf - . \
    > "$HOME/backups/arcadedb-volume.tgz"
kubectl -n cairnmind-db delete pod arcadedb-volume
kubectl -n cairnmind-db scale statefulset/arcadedb --replicas=1
```

To restore it into a new, empty volume: with the server scaled to 0 and no claim `data-arcadedb-0` left (in a new namespace, or after `kubectl -n cairnmind-db delete pvc data-arcadedb-0`, which deletes the data and cannot be undone), create the claim and unpack the archive into it, then start the server:

```bash
kubectl apply -f deploy/kubernetes/backup/data-claim.yaml
kubectl apply -f deploy/kubernetes/backup/volume-pod.yaml
kubectl -n cairnmind-db wait --for=condition=Ready pod/arcadedb-volume --timeout=180s
kubectl -n cairnmind-db exec -i arcadedb-volume -- tar --numeric-owner --no-overwrite-dir \
    -C /var/lib/arcadedb -xzf - < "$HOME/backups/arcadedb-volume.tgz"
kubectl -n cairnmind-db delete pod arcadedb-volume
kubectl -n cairnmind-db scale statefulset/arcadedb --replicas=1
```

Keep `--no-overwrite-dir`: the pod runs as the image's user, and on most storage classes the top folder of a new volume belongs to `root`, so without it `tar` fails to set that folder's time from the archive and stops with an error. The StatefulSet uses the claim of that name. As with Compose, `root` and the dedicated user are the ones stored in the snapshot, and a snapshot opens only with the same ArcadeDB version. If your storage class offers volume snapshots (CSI `VolumeSnapshot`), you can use them instead of `tar`, with the server scaled to 0.

**Upgrades** follow [When the pinned ArcadeDB version changes](#when-the-pinned-arcadedb-version-changes): back up, build and push the image of the new version, change `image:`, and `kubectl apply -f deploy/kubernetes`.

## When the pinned ArcadeDB version changes

Each Cairnmind release pins one ArcadeDB version (`arcade_version`, shown by `cairnmind engine status` as `expected_version`). The release notes say when it changes. Cairnmind refuses a server of another version, so the server and Cairnmind are upgraded together:

1. With the old version still running, take a logical backup: `cairnmind backup "$HOME/backups/"`. Stop the Cairnmind services, then take a volume snapshot.
2. Upgrade Cairnmind, and update the checkout this page runs from to the same version.
3. Rebuild and restart the server: `docker compose -f deploy/compose.yaml up -d --build --wait`. ArcadeDB opens the existing database files with the new version.
4. Check with `cairnmind engine status` and `cairnmind doctor`.

If the new server does not start or reports errors, either go back to the old checkout and image with the volume snapshot, or start the new version on an empty volume and `cairnmind restore` the logical backup.
