# Installing Cairnmind

This guide takes you from a clean machine to a working Cairnmind workspace. It takes about 15 minutes plus the model download.

Cairnmind runs on Linux and on macOS with Apple silicon (tested on an M3 Pro with macOS 27). On Windows it runs inside WSL2 Ubuntu, together with Ollama and the coding clients you use with it. On a Mac, follow the macOS parts of steps 2 and 4.

## What gets installed

| Component | Where it comes from | Size |
| --- | --- | --- |
| Cairnmind | this repository, installed as an isolated `uv` tool | a few MB |
| ArcadeDB 26.11.1 | downloaded by the installer from the official release, verified with SHA-256 | about 86 MB |
| `qwen3.5:9b` knowledge model | Ollama, on request | about 6.6 GB |
| `qwen3-embedding:0.6b` embeddings | Ollama, on request | about 640 MB |

The installer never changes your Codex or Claude Code configuration, never downloads models unless you ask, and never downloads the GitHub Copilot runtime.

## Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| Linux, or Windows 11 with WSL2 Ubuntu | Ubuntu 24.04 tested | Commands below run inside Ubuntu |
| or macOS on Apple silicon | macOS 27 tested | The commands run in Terminal |
| Java | 21 or newer, or Docker | Runs the ArcadeDB engine; without Java 21, the engine runs in Docker |
| tmux | 3.4 or newer, optional | Only for the terminal workspace (`cairnmind`); the service, `run`, `mcp`, `brief` and the other commands work without it |
| uv | current | Installs Cairnmind with Python 3.12 |
| Ollama | current, optional | Runs the knowledge agent's local models; without models Cairnmind works in deterministic-only mode |
| GPU | optional | The 9B model needs roughly 7 GB of GPU memory; on CPU it works, but answers are slow |
| Terminal | 80 × 24 or larger | The multi-pane Cockpit layout needs 140 × 42 |

## 1. Prepare Windows (skip on Linux and macOS)

In PowerShell, install WSL2 with Ubuntu and restart when asked:

```powershell
wsl --install -d Ubuntu
```

If you have an NVIDIA GPU, install the current Windows driver. Do not install an NVIDIA driver inside Ubuntu. Then open Ubuntu and check that the GPU is visible:

```bash
nvidia-smi
```

## 2. Install system tools

Inside Ubuntu:

```bash
sudo apt update
sudo apt install -y openjdk-21-jre-headless tmux git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://ollama.com/install.sh | sh
```

Open a new terminal so that `uv` is on your `PATH`, then check the versions:

```bash
java -version
tmux -V
uv --version
ollama --version
```

### macOS

With [Homebrew](https://brew.sh) installed, in Terminal:

```bash
brew install openjdk@21 tmux uv ollama
brew services start ollama
```

Homebrew's programs (tmux, uv) are found only in a shell that has `/opt/homebrew/bin` on its PATH. If `tmux -V` says the command is not found, add Homebrew to your shell profile with the `brew shellenv` line its installer printed ("Next steps"), then open a new Terminal window. For the current window only:

```bash
eval "$(/opt/homebrew/bin/brew shellenv)"
```

macOS does not find Homebrew's Java on its own: `java` on a Mac without a JDK only says that no Java Runtime is found. Link it where macOS looks for Java, as Homebrew advises. Then `java` finds it in every program, also in one that a desktop app starts without reading your shell profile:

```bash
sudo ln -sfn "$(brew --prefix openjdk@21)/libexec/openjdk.jdk" /Library/Java/JavaVirtualMachines/openjdk-21.jdk
```

Check the versions:

```bash
/usr/libexec/java_home -v 21+
java -version
tmux -V
uv --version
ollama --version
```

A JDK installed from a package instead (Temurin, Zulu, Oracle) goes to `/Library/Java/JavaVirtualMachines` by itself and needs no link.

## 3. Get Cairnmind

Choose one option.

**From a release (recommended).** Download the source archive of the [latest release](https://github.com/piotrwozn/cairnmind/releases/latest) and unpack it:

```bash
mkdir -p ~/src && cd ~/src
curl -LO https://github.com/piotrwozn/cairnmind/releases/download/v0.12.0/cairnmind-0.12.0.tar.gz
tar -xzf cairnmind-0.12.0.tar.gz
cd cairnmind-0.12.0
```

**From source.** Clone the repository to follow the latest `main`:

```bash
git clone https://github.com/piotrwozn/cairnmind.git ~/src/cairnmind
cd ~/src/cairnmind
```

## 4. Run the installer

**Upgrading from DevBrain.** The installer removes the earlier `devbrain` uv tool and keeps your memory in `~/.local/share/devbrain`. Projects set up by DevBrain still point their hooks and MCP entries at the removed Python, so the installer then lists one `cairnmind install <project>` command per registered project; run them before starting Codex or Claude Code in those projects. Since 0.13.0 the package installs only the `cairnmind` command; the `devbrain` alias is gone.

From the directory that contains `pyproject.toml`:

```bash
bash scripts/install-wsl.sh
```

The same script installs on macOS, with the bash that macOS ships.

The installer:

1. checks Java and tmux versions; without Java 21 it goes on when Docker answers, and without tmux 3.4+ it says the terminal workspace is unavailable and goes on;
2. installs Cairnmind as a `uv` tool, with dependency versions pinned by `uv.lock`;
3. downloads and verifies ArcadeDB, starts the engine and initialises your local memory in `~/.local/share/cairnmind` (or keeps using `~/.local/share/devbrain` if an earlier version created it); with Docker, the ArcadeDB archive is not downloaded; `cairnmind init` downloads Cairnmind's engine image instead;
4. runs `cairnmind doctor` and prints its summary: what is required and present, required and missing, optional and off, and the next step.

If the installer prints `Add this directory to PATH`, add that directory to your shell profile and open a new terminal.

The Java it checks is the one the engine runs: on Linux `java` on `PATH`; on macOS the one in `JAVA_HOME` when that has `bin/java`, otherwise the one that `/usr/libexec/java_home -v 21+` finds, otherwise `java` on `PATH`. A `java_executable` set in `config.json` of the data directory comes before all of them.

### Manual installation

The same steps can be run by hand from the directory that contains `pyproject.toml`, for example on a system where you do not want to run the script:

```bash
uv tool install --python 3.12 .
cairnmind engine install
cairnmind init
cairnmind engine status
```

This simpler form lets `uv` resolve dependency versions itself; the installer additionally pins them to `uv.lock`.

### Running the database in a container instead

**The engine in Docker, started by Cairnmind.** With Docker and without Java 21, the installer and `cairnmind init` choose Docker and say so: `cairnmind engine start` then runs Cairnmind's image (`ghcr.io/piotrwozn/cairnmind-arcadedb`, pinned by digest) on `127.0.0.1:2480`, with the data directory mounted and its files owned by you. Choose it yourself with `cairnmind engine start --docker` (or `--docker --build` to build the image on this machine), and go back with `cairnmind engine start --native`. The choice is saved as `engine_mode` in `config.json`; the two engines never run on one data directory at once. A data directory that already holds a native engine stays native until you choose Docker. Docker must run on this machine: a remote `DOCKER_HOST` or docker context, rootless Docker and Podman are not supported.

To run ArcadeDB yourself in a container, on a server or on a laptop for development, see [DEPLOYMENT.md](DEPLOYMENT.md). It also covers backups, restores and upgrading the database.

An installation that uses such a database is connect-only: it never installs, starts or stops an engine and needs no Java. Set `CAIRNMIND_ARCADE_CONNECT_URL` to the database address before you run the installer, and it skips the Java check and the ArcadeDB download; then follow [Connect-only installations](DEPLOYMENT.md#connect-only-installations) to create the database user and the store. The rest of this page (models, coding assistants, the first project) is the same.

## 5. Download the models (optional)

Ollama usually runs as a service after installation. If it does not, start it in a separate terminal with `ollama serve`. Then:

```bash
cairnmind models pull
cairnmind doctor
```

Models are optional. When `cairnmind init` finds no model server, or not both models, it saves `knowledge_agent: disabled` in `config.json` and says so: plans, tasks, checks, `brief`, `impact`, memory and file indexing work as usual, extraction and embedding jobs wait, `cairnmind status` shows `models: not configured (deterministic-only mode)` and how many jobs wait, and `cairnmind ask` is a full-text search of the project's memory: it makes no answer (`unavailable`) and lists the passages that match words of the question. `cairnmind models pull` (or `cairnmind models set`) turns the knowledge agent on; restart the service afterwards and the waiting jobs run. To turn it off again, set `knowledge_agent` to `disabled` in `config.json` and restart the service. An installation from before 0.13.0 keeps its models.

`cairnmind doctor` checks Python, Java, tmux, the engine, Ollama and the coding clients, read-only (no logins, downloads or model sessions), and exits 0. In a terminal it prints a summary: required and present, required and missing, optional and off (without tmux 3.4+ the terminal workspace, without models the local models), and the next step. `cairnmind doctor --json` prints the full report, which is also what a pipe, a file or a script gets; there, without tmux 3.4+ it has `cockpit: unavailable (tmux 3.4+ not found)`, and with no models `knowledge_agent: disabled (deterministic-only mode)`.

`cairnmind setup` shows, in three lines, where storage runs (and whether this installation writes it), where each memory model runs, where memory text goes, whether the models were evaluated, and what runs on this machine; `--json` prints the structured form. It changes no setting of an initialized installation: it ends with the commands that do. Like every command, it first creates a missing `config.json` and saves a database address seen for the first time in `CAIRNMIND_ARCADE_CONNECT_URL`; an empty variable counts as unset. `cairnmind doctor` starts with the same lines as its `topology` key, and `cairnmind status` has it for the running service. A value that comes from a variable (`CAIRNMIND_ARCADE_CONNECT_URL`, `CAIRNMIND_OLLAMA_URL`, `CAIRNMIND_PORT`) names the variable; "config.json" in the report means "not from a variable".

## 6. Connect your coding assistants

Install and sign in to the clients you want to use, in the same Ubuntu distribution, with their own installers:

- [Codex CLI](https://developers.openai.com/codex/cli)
- [Claude Code](https://code.claude.com/docs/en/setup)

Then check that Cairnmind can drive them safely:

```bash
cairnmind doctor --contract
```

Each check shows the line of client output that proved it. A client that fails a check is not launched, because a verified resume cannot be guaranteed.

`cairnmind doctor --contract --format text` prints the client compatibility checks as one line per client and check, with the output line that confirmed each one or, for a failed check, what was needed and what was found. A check that needs no output shows its requirement in parentheses. Each installed client ends with a `verified_resume` line; the command exits with 1 only when that line is `failed`. A client that is not installed gets a single `installed  missing` line and does not change the exit code. JSON stays the default.

API agents (OpenAI, Anthropic, OpenAI-compatible endpoints, Ollama) and GitHub Copilot are configured later in the workspace under **Connections**; see [API.md](API.md).

## 7. Open your first project

```bash
cairnmind project add my-app ~/src/my-app
cairnmind remember my-app "Accounts lock after five failed logins."
cairnmind brief my-app
cairnmind install my-app
cairnmind
```

`cairnmind remember` stores a first finding as proposed, and `cairnmind brief` shows it waiting for review: neither needs models, tmux or a coding assistant.

`cairnmind install` adds the project's MCP server entry, session hooks and agent instructions for the installed clients, keeping your existing configuration. In the project it creates or updates `.mcp.json`, `AGENTS.md`, `CLAUDE.md`, `.claude/` and `.codex/`; review them before committing. `cairnmind` opens the workspace. Choose the project, check the assistant, then select **Start work**. See [TERMINAL.md](TERMINAL.md) for navigation.

## Verify the installation (optional)

These evaluations use your local models in isolated databases and never touch your projects:

```bash
cairnmind eval run       # 20 recall questions with the local knowledge agent
cairnmind eval memory    # extraction, portable transfer and cited recall
```

See [EVALUATION.md](EVALUATION.md) for what they measure.

## Upgrading

- From DevBrain 0.8.0, or from 0.7.2 with existing data: follow [UPGRADING.md](UPGRADING.md).
- For later releases, read the release notes first, then run the installer from the new source directory. Close editors that use `cairnmind mcp --client external` first: their next tool call starts the service again, from the version being replaced.

## Uninstalling

```bash
cairnmind workspace stop
cairnmind stop
cairnmind engine stop
uv tool uninstall cairnmind
```

Close editors that use `cairnmind mcp --client external` before this: their next tool call starts the service again.

This keeps your memory in the data directory that `cairnmind doctor` names ("Memory is kept in ..."). To remove it too, first make a backup with `cairnmind backup ~/cairnmind-backup.cairnmind.json.gz`, then delete the directory. Deleting it cannot be undone.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `cairnmind: command not found` | Add the directory printed by the installer to `PATH`, or run `uv tool update-shell` and open a new terminal. |
| `Java 21+ is required` | `sudo apt install openjdk-21-jre-headless`, and check that `java -version` reports 21 or newer. On macOS: `brew install openjdk@21` and the `sudo ln -sfn` line of step 2; when the message names `JAVA_HOME`, that Java is too old: point `JAVA_HOME` at Java 21 or unset it. |
| `Docker is on the path but did not answer` | No Java 21, and `docker` is installed but its daemon does not answer (Docker Desktop stopped, or its WSL integration off). Start Docker and run the installer again, or install Java 21 as above. |
| `Java not found` or `macOS answers so when no JDK is installed` on macOS | No Java 21 is found. If Homebrew's `openjdk@21` is installed, the installer prints the `sudo ln -sfn ...` line that links it where macOS finds it. |
| `Version 3.4+ is required` for tmux, or `The terminal workspace needs tmux 3.4 or newer` | Only the terminal workspace needs tmux 3.4+; `cairnmind serve`, `run`, `mcp` and `brief` work without it. Ubuntu 24.04 ships tmux 3.4. On older releases, upgrade Ubuntu or build tmux 3.4. On macOS: `brew upgrade tmux`. |
| `Cannot reach Ollama` | Start `ollama serve`, then run `cairnmind models pull` again. |
| `cairnmind` exits with code 2 | It needs an interactive terminal. Run it directly in Windows Terminal or another terminal, not through a pipe. |
| A client is not launched | Run `cairnmind doctor --contract`; the failing check names the missing capability. |
| The layout looks cramped | Enlarge the terminal, or stay in the default Focus view, which works at 80 × 24. |
