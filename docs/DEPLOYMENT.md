# Running the database in a container

By default Cairnmind installs ArcadeDB itself and starts it on `127.0.0.1:2480` when it needs it (see [INSTALL.md](INSTALL.md)). This page is for running that database yourself instead, in a container: on a server, on a laptop for development, or in Kubernetes. It covers the image, a Compose file for one machine, Kubernetes manifests, [installations that only connect to such a database](#connect-only-installations), backups and restores, and what to do when the pinned ArcadeDB version changes.

One database serves one person: one installation writes it and any number read it. A team shares memory through publication in the repository, not through one live database.

## What is in `deploy/`

| File | What it is |
| --- | --- |
| `deploy/arcadedb/Dockerfile` | The ArcadeDB release Cairnmind pins (26.11.1) on Java 21 (Eclipse Temurin). The release archive is checked against its published SHA-256, as `cairnmind engine install` does. It is also the image `cairnmind engine start --docker` runs. |
| `deploy/arcadedb/entrypoint.sh` | Starts the server with the same JVM options and server settings as `cairnmind engine start`: 1 GB heap, the `low-ram` profile, and the vector module, plus ArcadeDB's production mode (see [What the server exposes](#what-the-server-exposes)); `cairnmind engine start --docker` runs it in development mode, like the native engine. The server runs as user `arcadedb` (uid 10001), or as the user the container is started with. |
| `deploy/arcadedb/healthcheck.sh` | The container health check: `GET /api/v1/ready`. |
| `deploy/compose.yaml` | One server with a named volume, publishing the HTTP port on `127.0.0.1` only. |
| `deploy/kubernetes/` | Plain Kubernetes manifests (no Helm chart) for one server with TLS: see [Kubernetes](#kubernetes). |

No password is built into the image or written in any of these files. The server reads the root password from a file at run time, and the Compose file takes that file's path from the environment.

The image is published (by hand, from `main`) as `ghcr.io/piotrwozn/cairnmind-arcadedb` (linux/amd64 and linux/arm64), which `cairnmind engine start --docker` runs, pinned by digest. For this page you build it from this repository.

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

## Two data directories on one machine

Each data directory needs its own service port and cockpit port. These settings may come from the environment, like the database address: `CAIRNMIND_PORT` (`port`, default 8765), `CAIRNMIND_COCKPIT_PORT` (`cockpit_port`, default 8766) and `CAIRNMIND_OLLAMA_URL` (`ollama_url`). A variable beats `config.json` for the process that reads it and is never written to `config.json`; a variable that is empty is ignored, and a port that is not a number from 1 to 65535 is refused. A service started by a command takes the variable from that command's environment, so a later command without it looks at the port in `config.json`: for a second data directory set `port` and `cockpit_port` in its `config.json`, or export the variables in every shell that uses it. Nothing changes for an installation that sets none of the variables.

When a service cannot bind its port, the command that started it says so, with the port and the setting to change. A command that reaches a service that does not accept its token says that the service most likely belongs to another data directory; the token is never printed.

## Connect-only installations

A connect-only installation uses a database that runs somewhere else: the container of this page, a server, or a cluster. It never installs, starts or stops an ArcadeDB engine, never creates a database, and needs no Java. Everything else stays on your machine: the knowledge service, the terminal workspace, the coding clients, parsing of your files and, unless you moved them too to a model server as well, the models.

### The setting

| Setting | Variable | Meaning |
| --- | --- | --- |
| `arcade_connect_url` | `CAIRNMIND_ARCADE_CONNECT_URL` | The address of a database this installation only connects to. When it is set, it is the address used. |
| `arcade_url` | `CAIRNMIND_ARCADE_URL` | The address of the database, as before. Plain `http://` on `127.0.0.1`, `localhost` or `::1` means the engine Cairnmind manages itself: it is started there when nothing answers. |

An installation is connect-only when `arcade_connect_url` is set, on any host, this machine included; or when `arcade_url` names an address the managed engine could never serve (another host, or `https://`). So an installation that already pointed `CAIRNMIND_ARCADE_URL` at an `https://` server keeps working and is now connect-only. For a container on `127.0.0.1` use `arcade_connect_url`: with `arcade_url` alone, a command that writes would start a local engine on that port whenever the container is down.

Setting both to different addresses is refused, naming both. `config.json` keeps the `arcade_url` it had, so removing `arcade_connect_url` brings the managed engine back with its data untouched.

**The address is saved.** The first command that runs with `CAIRNMIND_ARCADE_CONNECT_URL` set writes the address to `config.json` of the data directory as `arcade_connect_url`, and says so once. From then on the data directory is connect-only for every command, also one started without the variable: another shell, a terminal workspace or a service started before the `export`. Without this, such a command would find an ordinary installation and start a local engine with an empty store. When `config.json` already names an address, a different one in the variable is used by that command only and the file is not changed. The other settings (user, database, password file, authority file) are not saved from variables. A command started without the password is refused, `engine status` included, and starts nothing and sends nothing to the server; its message names the password and also says when the user is left at `root` or the database is not set, with what would be used in their place: `root` and a database name derived from the data directory's path. Those two are refused only where the rules above refuse them: on a host other than this machine the message starts with the refusal of `root`, then names the password and the database. So set all of them in `config.json` and nothing depends on the shell a command starts from. To go back to the engine Cairnmind manages, run `cairnmind setup local --backup FILE` (a checked backup of the remote store is written into the new file and restored into the store on this machine, under the same rule as `cairnmind restore` for a person's confirmations, described under [Logical backup](#logical-backup-cairnmind-backup-and-cairnmind-restore)) or `cairnmind setup local --empty` (the store on this machine without that data); the remote store is only read, and never changed or deleted. The hand-written way still works: remove `arcade_connect_url` from `config.json` and unset the variable. The two databases are not copied into each other: move data with `cairnmind backup` and `cairnmind restore`.

The rules for reaching a database on another host are unchanged ([SECURITY.md](../SECURITY.md)): `https://` with certificate and host name verification (a private authority through `arcade_ca_file`), a dedicated user and never `root`, and the password from `CAIRNMIND_ARCADE_PASSWORD` or a private file named by `arcade_password_file`, never from `config.json`. On `127.0.0.1` plain `http://` is allowed. No password is generated for a connect-only installation: it is the one set where the server runs.

Set the database name explicitly (`arcade_database`, `CAIRNMIND_ARCADE_DATABASE`). Without it the name is derived from the path of the data directory, so two machines would look for two different databases; `cairnmind doctor --json` says when the name is a derived one.

### What the commands do

| Command | On a connect-only installation |
| --- | --- |
| `cairnmind engine status` | Says `"mode": "connect"`, the host, the user, whether the connection is encrypted, whether the server answers and which version it runs. It looks for no Java and no installed engine. Exit status 1 when the server's version is not supported, or, with `"available": null` and nothing sent, when a setting it needs is missing, such as the password. |
| `cairnmind engine install`, `start`, `stop` | Do nothing and say so in one sentence (exit status 2), whether or not the knowledge service runs: "This installation only connects to ArcadeDB at ...; it does not start an engine." `stop` still stops an engine that this data directory started before it was switched. |
| `cairnmind db create-user` | The one step that signs in as `root`: it creates the database and the dedicated user, after checking the server's version. Run once per database; run again for the same user and database it changes nothing, and for a user that exists without its database it creates the database. Each database gets its own user: a user that already exists and cannot use this database with the password this installation reads is refused, nothing is created, and the message says to set `arcade_user` to a new name. |
| `cairnmind init` and every command that writes | Open the database they were given. When it does not exist they stop and name `cairnmind db create-user`; they never create it. When the server does not answer they say where they looked and that no engine is started here. |
| `cairnmind doctor --json` | The `arcadedb` block says `mode`, `host`, `url`, `user`, `transport`, `password_source`, the server's `version` and `supported_versions`; `java` says it is not needed; `store_writer` says whether this installation writes the store or only reads it. `topology`, first in the report, says it in three lines (storage with the writer role, the memory models, what runs on this machine) and names the variable a value came from. |
| `cairnmind status` | Asks the running service, which only the installation that writes the store runs; adds `storage` (mode, host, address) and `topology` (the three lines, with the evaluation "not checked") beside `writer`. On a reader it says so and names `cairnmind store writer` and `cairnmind doctor`, which show the role there. |
| `cairnmind store size` | Counts rows as always; says that the database files and the server log are on the server and cannot be measured from here. |
| `cairnmind eval run`, `eval memory`, `eval repository` | Refused: they create and drop temporary databases. Run them on an installation with the engine on this machine. |

**The server's version.** Each Cairnmind release works with one ArcadeDB version, the one it pins (`supported_versions` in `engine status` and `doctor --json`; 26.11.1 in this release). A server of any other version is refused before anything is read or written, by every command, `cairnmind db create-user` included (it asks for the version before it signs in as `root` to create anything), with the message "ArcadeDB at ... runs version X, which this release of Cairnmind does not support. Supported: 26.11.1." Upgrade the server and Cairnmind together: [When the pinned ArcadeDB version changes](#when-the-pinned-arcadedb-version-changes).

### From nothing: the Compose server on this machine

You have followed [Start it on one machine](#start-it-on-one-machine); the server answers on `127.0.0.1:2480` and its root password is in `$HOME/.config/cairnmind-db/root-password`.

1. Install Cairnmind without an engine. With the variable set, the installer downloads no ArcadeDB and does not ask for Java:

   ```bash
   export CAIRNMIND_ARCADE_CONNECT_URL=http://127.0.0.1:2480
   bash scripts/install-wsl.sh
   ```

   The installer says once that it saved the address in `config.json` (see [The setting](#the-setting)), and ends with a note that the database is not ready yet: that is the next step. The installer then exits with status 1, because its last check, `cairnmind doctor`, finds the database missing; that is expected until step 3, so do not chain later commands to it with `&&`. (Without the installer: `uv tool install --python 3.12 .`, then any command with the variable set saves the address, for example `cairnmind engine status`. At this step it says that it saved the address and then stops with exit status 1, because the password is not set yet; step 2 sets it.)

2. Choose the user's password, in a private file, and describe the connection in `config.json` of the data directory (`~/.local/share/cairnmind/config.json` unless you chose another one). The installer has written `arcade_connect_url` there; add the other three, with the absolute path of the password file:

   ```bash
   (umask 077; openssl rand -hex 24 > "$HOME/.config/cairnmind-db/user-password")
   ```

   ```json
   "arcade_connect_url": "http://127.0.0.1:2480",
   "arcade_user": "cairnmind",
   "arcade_database": "cairnmind",
   "arcade_password_file": "/home/you/.config/cairnmind-db/user-password"
   ```

   Instead of editing the file, `cairnmind setup remote-db` writes those four settings and tests them first (stop the service before; it refuses while it runs, and while a variable such as `CAIRNMIND_ARCADE_CONNECT_URL` is still exported, which it names: `unset` it, the address is in `config.json` already). Create the password file as above, then:

   ```bash
   cairnmind setup remote-db --url http://127.0.0.1:2480 --user cairnmind --database cairnmind \
     --password-file "$HOME/.config/cairnmind-db/user-password" --admin-password-file "$HOME/.config/cairnmind-db/root-password"
   cairnmind setup remote-db ... --yes   # the same command with --yes: creates the user and database, tests, saves
   ```

   Without `--yes` it tests and prints the plan and changes nothing; with `--admin-password-file` and `--yes` it makes the database and user first (step 3 below does the same by itself) and never saves the root password. Step 3 is then done; step 4 follows.

   Settings in `config.json` hold for every command of this data directory, wherever it is started. The alternative is the same names as variables in your shell profile (`CAIRNMIND_ARCADE_USER`, `CAIRNMIND_ARCADE_DATABASE`, `CAIRNMIND_ARCADE_PASSWORD_FILE`); they hold only for what is started from a shell that has them, and anything else is refused for the missing password with a message that names each setting it lacks:

   ```bash
   export CAIRNMIND_ARCADE_USER=cairnmind
   export CAIRNMIND_ARCADE_DATABASE=cairnmind
   export CAIRNMIND_ARCADE_PASSWORD_FILE="$HOME/.config/cairnmind-db/user-password"
   ```

3. Create the database and the user, once, signing in as `root` for this step alone:

   ```bash
   cairnmind db create-user --admin-password-file "$HOME/.config/cairnmind-db/root-password"
   ```

4. Create the store and check:

   ```bash
   cairnmind init
   cairnmind engine status   # "mode": "connect", "available": true, "version_matches": true
   cairnmind doctor --json   # arcadedb: "mode": "connect", "database_available": true, "user": "cairnmind"
   cairnmind project add myproject ~/code/myproject
   ```

**The server on another machine.** This Compose file serves plain HTTP, which Cairnmind accepts on loopback only, and publishes the port on `127.0.0.1` of the server. Forward the port over SSH and use the same settings: `ssh -N -L 2480:127.0.0.1:2480 you@server`. When the tunnel is down, commands say that the server at `http://127.0.0.1:2480` is unavailable; nothing is started in its place. To reach a server over the network without a tunnel it must serve TLS: use the [Kubernetes](#kubernetes) manifests, or put a TLS endpoint in front of the container and use an `https://` address with `CAIRNMIND_ARCADE_CA_FILE`. Do not publish port 2480 on a public address.

### From nothing: the server in Kubernetes, from your machine

You have followed [Kubernetes](#kubernetes) up to [Start it](#start-it): the StatefulSet is ready and `$HOME/cairnmind-k8s` holds `ca.pem` and `root-password`.

1. Install Cairnmind without an engine, as above (`CAIRNMIND_ARCADE_CONNECT_URL=https://localhost:2490 bash scripts/install-wsl.sh`).

2. Open a port-forward and keep it open while you work. The certificate of this page names `localhost`, so the address is `https://localhost:2490`:

   ```bash
   kubectl -n cairnmind-db port-forward svc/arcadedb 2490:2490 &
   ```

3. The settings, the user and the store. The installer saved the address in `config.json`; keep the other four there too (`arcade_user`, `arcade_database`, `arcade_password_file`, `arcade_ca_file`, as in the [Compose walk-through](#from-nothing-the-compose-server-on-this-machine), step 2), or as variables in your shell profile, which hold only for what is started from a shell that has them:

   ```bash
   (umask 077; openssl rand -hex 24 > "$HOME/cairnmind-k8s/user-password")
   export CAIRNMIND_ARCADE_CONNECT_URL=https://localhost:2490
   export CAIRNMIND_ARCADE_USER=cairnmind
   export CAIRNMIND_ARCADE_DATABASE=cairnmind
   export CAIRNMIND_ARCADE_PASSWORD_FILE="$HOME/cairnmind-k8s/user-password"
   export CAIRNMIND_ARCADE_CA_FILE="$HOME/cairnmind-k8s/ca.pem"
   cairnmind db create-user --admin-password-file "$HOME/cairnmind-k8s/root-password"
   cairnmind init
   cairnmind doctor --json   # arcadedb: "mode": "connect", "transport": "https (certificate verification on)",
                             # "password_source": "file", "database_available": true
   ```

A port-forward ends when the pod restarts or the connection to the cluster drops. Commands then report the server as unavailable; start the port-forward again and repeat the command. For a cluster that gives the Service an address reachable from your network (an Ingress with TLS passthrough or a LoadBalancer), use that host name in the certificate and in `CAIRNMIND_ARCADE_CONNECT_URL` instead.

### From nothing: a client inside the cluster

A pod that runs Cairnmind reaches the server at `https://arcadedb.cairnmind-db.svc:2490`. It needs:

- the label `cairnmind.io/database-client: "true"`, or the NetworkPolicy keeps it out;
- the settings as environment variables, with `CAIRNMIND_ARCADE_CONNECT_URL=https://arcadedb.cairnmind-db.svc:2490`;
- the user's password as a mounted Secret and `ca.pem` as a ConfigMap, named by `CAIRNMIND_ARCADE_PASSWORD_FILE` and `CAIRNMIND_ARCADE_CA_FILE` (see [Cairnmind in a pod](#cairnmind-in-a-pod-the-password-from-a-mounted-secret));
- a writable data directory (`--data-dir` or `CAIRNMIND_HOME`), which holds the installation's identity, its settings and its backups. Keep it on a volume if the pod is to stay the writer across restarts: a new, empty data directory is a new installation, and it only reads until a person runs `cairnmind store takeover --yes`.

`deploy/kubernetes/check/connection-check.yaml` is such a pod as a Job, and `connection-check.sh` is what it runs: `create-user`, `engine status`, `init`, `doctor`, indexing a small project, and the engine commands that must refuse. The image `deploy/kubernetes/check/Dockerfile` installs Cairnmind only: no Java and no ArcadeDB.

### One writer, any number of readers

A database has one writing installation, named in the database itself; every other installation that connects to it reads only ([CONTRACTS.md](CONTRACTS.md)). This holds between machines because it does not depend on anything local:

- The first installation that opens a database becomes its writer. A second one, on any machine, opens it as a reader: `project list`, `records`, `impact` and the other reading commands work; a command that writes stops with one line naming the writer; the knowledge service does not start there.
- `cairnmind store takeover --yes [--note TEXT]` on the reader makes it the writer. It is a person's decision; a coding session cannot run it. The previous writer is refused at its next write, and its running jobs go back to the queue.
- Teams share memory through publication in the repository (`cairnmind memory publish`), not through one live database.

The lock files in the data directory still exist and still mean what they did, on one machine. What guards the database between machines:

| Local file | What it serialises on one machine | Between machines |
| --- | --- | --- |
| `database-writer.lock` | The transactions of the processes of one installation. | The writer row: only one installation writes, and a commit by an installation that lost the role fails in the database (the journal head is read and updated in every writing commit). |
| `locks/project-*.lock`, `locks/transcript-*.lock` | A whole indexing or import run, from reading the files to writing them. | The writer row: a reader's indexing is refused at its first write. |
| `service.lock` | One knowledge service per data directory; `restore` takes it too. | The writer row: the service starts only on the writer, and only the writer restores. |
| `config.lock`, `installation.json` | The settings file and the installation's identity. | Not database state. A copied data directory gets a new identity and so cannot write as the original. |
| `engine.lock`, `runtime/install.lock`, `arcade/` | Installing and starting the managed engine. | Never created by a connect-only installation. |

Not covered by the writer row, as on one machine: a schema statement (`CREATE ... IF NOT EXISTS`) checks the writer just before it runs, not in the same step, so one such statement of an installation that loses the role at that moment can still run; it adds an empty type or index and changes no data.

### Backups

- **Logical backup, from your machine.** `cairnmind backup DESTINATION` reads the whole store through the connection and writes the file where you say, on the machine that runs the command. The dedicated user can take it, and a reader can too. `cairnmind restore FILE` runs on the writer only, with its service stopped (`cairnmind stop`); readers on other machines may stay connected, and see the old data until the restore commits and the restored data after.
- **Automatic copies.** The copy made before a restore (`before-restore-*`), the one made before a schema upgrade (`before-schema-*`) and the service's daily backup are written to `backups/` in the data directory of the installation that does the work: on the laptop, not on the server. Include that folder in the laptop's own backup, or copy the files you want to keep.
- **Volume snapshot, on the server.** The snapshots of this page ([Compose](#volume-snapshot), [Kubernetes](#backup-and-restore-1)) copy what the server holds: every database, the server's configuration and its users, the dedicated one included. They do not contain anything from a data directory: not `config.json`, the service token, `installation.json`, client credentials, the hook spool or the backups above. After restoring a snapshot onto a new server, the writer recorded in it is still the installation that wrote it; that installation goes on writing, and any other one takes over explicitly.

### What stays on the laptop

The data directory keeps everything that is not in the database: `config.json` (the service token, the settings), `installation.json`, client credentials and API keys, the terminal workspace state, session contexts, the hook spool, the answer log, logs and local backups. Two machines that use one database each have their own; none of it is shared or copied by the database. A coding session started on one machine is not resumed on the other.

### Why the knowledge service stays on your machine

Decided on 22 November 2026, from reading the code of 0.13.0 (the answer was first written on 14 November from 0.10.0; nothing in it was measured): the knowledge service, the process that owns the queue and extracts, embeds and answers, stays on the machine where you work. Cairnmind plans no split of it before 1.0.0, and no headless worker for the extraction and embedding jobs only. The database and the models can already be elsewhere (see above); the service is what ties them to your checkout. The reasons, each with the module that causes it:

| Reason | Module |
| --- | --- |
| Indexing walks the checkout on disk, and roots and worktrees are stored as paths of this machine; a question is answered for a worktree by comparing them. | `ingest.index_project`, `retrieval.Retriever.source_in_scope` |
| The code fingerprint and the state of checks and changed files are read from the worktree and from `git`; answering a question already asks for the check status. | `verification.capture_code_state`, `scope.path_states` |
| Hook events are files in the data directory's spool; hooks and the MCP bridge reach the service on a loopback address with the token of `config.json`, and the service refuses any other address. | `integrations.drain_hook_spool`, `service.create_app` |
| Transcripts of the coding assistants are read from the home directory, with cursors that are paths and offsets. | `ingest.import_history` |
| Published team memory is written to `.memory/` inside the worktree. | `memory_published_*` |
| One declared writer per store runs the service and claims jobs; a second process that claims jobs would contradict it, and a lease is timed by the clock of the machine that claims. | `store_writer`, `Store.claim_job` |
| What a person confirms needs a key held by the open workspace's tmux server on that machine; the service token alone is refused. | `person_proof`, `workspace` |
| Teams share memory through published files in git, not through one live service. | `memory_published_git` |

The three options that were compared, with the verdict:

| Option | What it would need | Verdict |
| --- | --- | --- |
| A full split: the service on a server, the laptop sends what it reads | A new authenticated, non-loopback protocol for parsed sources, fingerprints, hook events and transcript segments, with TLS and tokens; in effect a rewrite of the service's API | Not built. Large, and it contradicts one writer per store. |
| The service stays local; the database and the models are remote (what `cairnmind setup remote-db` and `setup remote` set up) | Nothing more | The supported way. What stays on the laptop is Python parsing, `git`, hashing and the service's many small database requests; over a network the requests matter more than CPU (inferred, not measured: [the wall times](#how-long-indexing-takes-over-a-connection) are 39 s on a loopback and 229 s with 20 ms added). What a running service costs while idle is measured in [What a running service costs over a connection](#what-a-running-service-costs-over-a-connection). |
| A remote service with its own clone | A server that clones the project | Not built. Every answer and check status would describe the server's clone, not your work; fit only for read-only questions at a pinned commit. |

A headless worker process for extraction and embedding on a server is not planned and has no issue: the models already run on a server, so it would move only the service's HTTP calls and database writes, and it needs a second writer.

`cairnmind index` only asks the service to index, so the figures below are the service's own: its CPU and peak memory while the command runs, and in a fixed interval after the command returned, while it works off its queue.

| Measured | While `cairnmind index` runs (CPU, peak memory) | After it returned, working off the queue (CPU, peak memory) |
| --- | --- | --- |
| **NOT MEASURED YET.** One run is still to be made with `scripts/measure_service_spend.py` (a throwaway data directory and its own ports, the service started just before it, the database connect-only, both model roles remote). | NOT MEASURED YET | NOT MEASURED YET |

The request counts of indexing over a connection are measured; they are in the table of [How long indexing takes over a connection](#how-long-indexing-takes-over-a-connection).

### When the connection drops

Every change is one server transaction that commits whole or not at all, so a dropped connection leaves whole steps, never half of one:

| During | What the database holds afterwards | What to do |
| --- | --- | --- |
| Indexing | The files whose transaction committed (up to eight small files each), each with all its chunks; nothing of the transaction that was cut. | Run the command again: it indexes what is left. |
| `restore` | The data from before the restore, or the whole backup if the server had already committed. Never a mixture. The `before-restore-*` copy is a whole file on your machine. | Run `cairnmind restore FILE` again. |
| A schema upgrade (`cairnmind init` after upgrading Cairnmind) | The old schema version, with some new empty types or indexes; the `before-schema-*` copy is a whole file on your machine. The version is written last. | Run the command again: it repeats the statements, which skip what exists. |
| Creating the store (`cairnmind init`) | A database without a schema version: commands that read say it has no schema yet. | Run `cairnmind init` again. |

When the connection is cut after the server received a commit and before the answer arrived, the command reports the server as unavailable although the change is stored. Running the same command again is safe in each case above: indexing skips the files that are already current, and a restore replaces everything again. A write is never sent twice by itself. `tests/test_connect_only_network.py` cuts the connection at each of these points.

A transaction that was cut stays open on the server until its session times out; it holds no lock that stops other work.

### How long indexing takes over a connection

Over a connection, the time of a command is its number of database requests times the time of one request. The numbers below are for Cairnmind's own source (128 files, about 26,000 graph nodes and 32,000 edges), indexed by a connect-only installation against the Compose server of this page, on 15 November 2026:

| Way to the server | First indexing | Indexing again after every file changed |
| --- | --- | --- |
| `http://127.0.0.1`, the container's published port | 2,313 requests, 16 s | 8,101 requests, 39 s (4.8 ms each) |
| `https://`, through a TLS endpoint on the machine's own address | 2,313 requests, 17 s | 8,101 requests, 64 s (7.9 ms each) |
| The same, with 20 ms added to every request | 2,313 requests, 65 s | 8,101 requests, 229 s (28.2 ms each) |

Client, TLS endpoint and server were on one machine, so the first two rows contain no network latency at all, and the TLS endpoint was a test relay written in Python, which costs about 3 ms a request by itself; a real TLS listener costs less. The third row shows how the request count turns into time: 8,101 requests at 20 ms are 162 s, which is the difference to the row above. For your network, multiply the request counts by the round-trip time you measure (`ping`, or the time of `cairnmind engine status`). Not measured: a real network with loss or varying delay, and a server under load from other clients.

A logical backup of that store took 119 requests and 6 s. Its restore took 297,454 requests and 397 s on `127.0.0.1` in a development build of 0.11.0 before #219, which wrote the graph and the chunks one row at a time. Since #219 restore writes them in groups of a few hundred rows per request, as indexing does. Measured again on 15 November 2026 with the test engine of the test suite on one machine, for a store of the same source (about 139 files, 27,400 graph nodes and 33,700 edges, a 6.3 MB backup; the runs after the change indexed one file more, the new module, which differs by under 0.2%):

| Restore | Row by row (before #219) | In groups |
| --- | --- | --- |
| Over the store it replaces, `http://127.0.0.1` | 281,451 requests, 239 s | 1,140 requests, 18 s |
| Into an empty database, through the test relay | 281,390 requests, 216 s | 1,079 requests, 6 s |
| The same, with 20 ms added to every request | about 94 minutes more (computed from the request count, not measured) | 1,079 requests, 28 s |

The first row includes the safety copy and the deletion of the rows it replaces inside the restore's one transaction. A large store can now be restored from the laptop over the network; for your network, add the round-trip time times about 1,100 requests for a store of this size.

### What a running service costs over a connection

A running knowledge service talks to the database even when nobody works: every 10 seconds
(`poll_seconds`) it looks for changed files, followed transcripts, hook events and archive jobs; every
2 seconds, while no job waits, it asks for one; and every minute it checks that each passage without a
vector has an embedding job. Until #363 that check cost 11 requests for every passage without a vector,
and every passage lacks one after a first indexing, after `cairnmind restore` (backups carry no vectors),
after `rebuild-vectors` and while the embedding model is slow; transcripts cost 4 requests each per look.
Measured on 22 November 2026 with the test engine of the test suite on one machine, for a store of
Cairnmind's own repository, on `127.0.0.1` and through the test relay `tests/database_relay.py` with
20 ms added to every request. The "Now" column was measured on 557 files, 2,978 passages, 82,353 graph
nodes and 102,547 edges; the "Before #363" column was measured on the same repository before the change,
at 555 files and 2,973 passages. The seconds are from a machine shared with other work; the request counts
are exact:

| Work | Before #363 | Now |
| --- | --- | --- |
| The embedding check, every passage without a vector | 32,706 requests; 53 s on `127.0.0.1`; about 12 minutes with 20 ms added (computed from 200 passages measured: 2,201 requests, 49 s), longer than the minute between two checks | 9 requests; 0.1 s on `127.0.0.1`; 10 requests, 0.27 s with 20 ms added |
| One look for changes, 40 followed transcripts | 172 requests; 4.2 s with 20 ms added | 12 requests; 0.39 s with 20 ms added |
| Asking for a job while none waits | 4 requests | 4 requests |
| An idle minute in all, any store size | grows with passages without a vector and with transcripts | about 214 requests (computed from the rounds above: 6 looks of 12, 6 memory checks of 2, 30 asks for a job of 4, one embedding check of 10), about 5.5 s of waiting with 20 ms added |
| `cairnmind index` with nothing changed, in a service that just started | 14,810 requests; 34 s on `127.0.0.1` | 3,485 requests; 14 s on `127.0.0.1`; 89 s with 20 ms added |

While the check ran, no job of the queue ran. Now it writes nothing when every passage already has its job
(a check that finds 1,313 finished jobs without a vector and 171 failed ones takes 251 requests, once, and
leaves every passage with a pending job), and the requests of an idle service grow by at most one request
per 500 passages without a vector (the jobs are read in groups of 500), not by a request per row. What is left of `index` in a new process is about 6 requests per file, once per start of the
service. Not measured: a real network (another measurement does that), and the requests of an open
workspace, which asks the service for its status on its own.

### The engine tests against this server

The engine tests of Cairnmind (the tests marked `arcade`) can run against a server instead of an engine they start themselves, through connect-only mode: set `CAIRNMIND_TEST_ARCADE_SERVER_URL` to its address (plain http on this machine) and `CAIRNMIND_TEST_ARCADE_SERVER_PASSWORD_FILE` to a private file (mode 0600) holding its root password, then run `uv run pytest -m arcade -rs`. The tests install, start and stop no engine; each creates its databases as root and drops them. The job has not yet run against the image, so whether every test passes there, and how long it takes, is not known. The non-required `server-tests` CI job does this on pushes to main and by hand, against the published image at the digest pinned in `src/cairnmind/config.py`, in production mode. Tests that cannot run against a server (they start an engine, read its files, or run a command a connect-only installation refuses) are skipped, each with its reason; the table is `LEFT_OUT_ON_A_SERVER` in `tests/conftest.py`, and the job summary prints it.

## Backup and restore

There are two kinds of backup. Take the logical one regularly; add a volume snapshot when you want the whole server as it was.

### Logical backup: `cairnmind backup` and `cairnmind restore`

The logical backup is a checksummed, compressed JSON export of everything Cairnmind keeps in the database: projects, records and their history, sources, the graph and jobs. It carries no vectors (#187): they are derived from the text and several times its size, so the backup is rebuilt from the text instead (below). It is read over the HTTP API, so it works the same against a container as against the local engine, and it does not depend on the ArcadeDB version.

```bash
cairnmind backup "$HOME/backups/"                       # a folder (created when missing): a new timestamped file in it
cairnmind backup "$HOME/backups/before-upgrade.cairnmind.json.gz"
```

A destination that is an existing folder, or a path ending in `/` (a missing folder is then created, readable by you only), gets a new file with a dated name; any other path is the file itself. A reader can make a backup too: it only reads the store.

To restore it into a server, for example a new empty one:

1. Stop the Cairnmind service of the installation that writes this database: `cairnmind stop`. Only that installation can restore; on any other one `restore` stops and names the writer ([One writer, any number of readers](#one-writer-any-number-of-readers)).
2. Point the environment at the server, as in [Connect-only installations](#connect-only-installations), and restore:

   ```bash
   cairnmind init
   cairnmind restore "$HOME/backups/before-upgrade.cairnmind.json.gz"
   cairnmind project list
   ```

`restore` replaces everything in the database with the backup. Before it does, it writes a backup of the current contents to `backups/` in the data directory.

A backup holds no vectors, so after a restore the vectors are rebuilt. In the restore's own transaction every completed embedding job of a current chunk goes back to pending, and the output says how many (`embed_jobs_requeued`) and says that search uses the text only until the rebuild ends. Text search, records, plans, tasks and the graph work at once. Once the service runs with the models, it embeds the chunks one job at a time, and `cairnmind status` shows the jobs left (`pipeline_jobs.indexing`); until they are done, an answer that needs vectors says that its vectors are not ready, and does not read as if the project had no such text. In deterministic-only mode (no models) the jobs wait, as every embedding job does, until the models are turned on. A backup written by 0.12.0 or earlier carries its vectors and is restored with them, as before; then nothing is requeued.

A restore keeps the person's confirmations in the backup only when this installation's workspace is open on the machine that runs `restore` and holds its key ([MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#a-restore-without-the-workspace)). On a server, in CI or with no workspace, every row is still restored, each confirmation becomes the backup's claim, not verified here, and accepted transcript record kinds are withdrawn; the output says which (`person_confirmations`) and how many, and the exit code is 0 either way. `cairnmind setup local --backup FILE`, the way back from a store this installation only connects to, restores under the same rule, through the same code.

What the logical backup does not contain: the vectors (the `embeddings` rows and the `vector_*` index types), the files of the data directory (`config.json` with the local token and the database password, client credentials and API keys, the terminal workspace state, the hook spool, logs), ArcadeDB's own server configuration and users, and model weights.

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

The `root` password and the dedicated user are the ones stored in the snapshot, not the ones in your files. Cairnmind finds its database by name (`CAIRNMIND_ARCADE_DATABASE`; `cairnmind doctor --json` shows it as `arcadedb.database`): use the same name, then run `cairnmind init`. A new data directory opens the restored database as a reader, because the snapshot still names the installation that wrote it; `cairnmind store takeover --yes` makes it the writer. What a snapshot does not cover: everything in Cairnmind's data directory (as for the logical backup), and other ArcadeDB versions: open it with the same image, or move between versions with a logical backup.

Both procedures are run by the `deployment-image` CI job ([`scripts/deployment_check.py`](../scripts/deployment_check.py)), with connect-only installations that sign in as the dedicated user: a project and a record are written to one server, backed up and restored into a second, empty one, and the first server's volume is archived and unpacked into a third. Each server then holds the same project and records.

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

The `kubernetes-manifests` CI job, beside the other jobs and not a required check, runs [`scripts/kubernetes_check.py`](../scripts/kubernetes_check.py) on a kind cluster: it checks every manifest against the cluster's schemas (`kubectl apply --dry-run=server --validate=strict`), creates the Secrets as below, applies the manifests, runs the connection check as the Job (a connect-only installation inside the cluster, with both passwords mounted from Secrets) and again from the runner through `kubectl port-forward` (a second connect-only installation, which reads only until it takes over writing), takes a logical backup and restores it, and restores a volume snapshot into a new volume. You can run the script against any disposable cluster.

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

Cairnmind refuses `root`, plain HTTP and a password from `config.json` for a database on another host (see [SECURITY.md](../SECURITY.md)). Create a user with rights on the Cairnmind database only, once, from your machine through a port-forward. Its password comes from a file of your user with mode `0600`. (The same steps, with installing Cairnmind first, are in [From nothing: the server in Kubernetes, from your machine](#from-nothing-the-server-in-kubernetes-from-your-machine).)

```bash
kubectl -n cairnmind-db port-forward svc/arcadedb 2490:2490 &
(umask 077; openssl rand -hex 24 > "$HOME/cairnmind-k8s/user-password")
export CAIRNMIND_ARCADE_CONNECT_URL=https://localhost:2490
export CAIRNMIND_ARCADE_USER=cairnmind
export CAIRNMIND_ARCADE_DATABASE=cairnmind
export CAIRNMIND_ARCADE_PASSWORD_FILE="$HOME/cairnmind-k8s/user-password"
export CAIRNMIND_ARCADE_CA_FILE="$HOME/cairnmind-k8s/ca.pem"
cairnmind db create-user --admin-password-file "$HOME/cairnmind-k8s/root-password"
```

`create-user` signs in as `root` for that step alone, over the same verified TLS, creates the database and the user in its `admin` group, and then signs in as the new user. Each database gets its own user: for a second database, run it again with a new `arcade_user` and its password; a user of another database is refused, and rights on a second database are never added to it. After several failed sign-ins ArcadeDB refuses that user for about 30 seconds, even with the right password, and answers as it does for a user that does not exist; when a password that worked before is refused, wait half a minute and try again. Then the connection check:

```bash
cairnmind engine status   # "mode": "connect", "available": true, "version_matches": true
cairnmind init
cairnmind doctor --json   # arcadedb: "mode": "connect", "database_available": true, "user": "cairnmind",
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

**Logical backup.** Through the port-forward and the settings above, as for any server: `cairnmind backup "$HOME/backups/"`, and `cairnmind restore` as described in [Backup and restore](#backup-and-restore), on the installation that writes the database, after stopping its service. The dedicated user can do both.

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
