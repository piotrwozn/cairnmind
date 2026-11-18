# Installing Cairnmind

This guide takes you from a clean machine to a working Cairnmind workspace. It takes about 15 minutes plus the model download.

Cairnmind runs on Linux and on macOS on Apple silicon. On Windows it runs inside WSL2 Ubuntu, together with Ollama and the coding clients you use with it. On a Mac, follow the macOS parts of steps 2 and 4.

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
| or macOS on Apple silicon | M1 or later | Intel Macs are not supported; the commands run in Terminal |
| Java | 21 or newer | Runs the ArcadeDB engine |
| tmux | 3.4 or newer | Hosts the terminal workspace |
| uv | current | Installs Cairnmind with Python 3.12 |
| Ollama | current | Runs the local knowledge agent |
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

Homebrew's Java is on no `PATH`, and `java` on a Mac without a JDK only says that no Java Runtime is found. Cairnmind and the installer look for Java in `JAVA_HOME` first, so add it to your shell profile:

```bash
echo 'export JAVA_HOME="$(brew --prefix openjdk@21)/libexec/openjdk.jdk/Contents/Home"' >> ~/.zprofile
```

Open a new terminal, then check the versions:

```bash
"$JAVA_HOME/bin/java" -version
tmux -V
uv --version
ollama --version
```

A JDK installed from a package instead (Temurin, Zulu, Oracle) goes to `/Library/Java/JavaVirtualMachines` and is found without `JAVA_HOME`.

## 3. Get Cairnmind

Choose one option.

**From a release (recommended).** Download the source archive of the [latest release](https://github.com/piotrwozn/cairnmind/releases/latest) and unpack it:

```bash
mkdir -p ~/src && cd ~/src
curl -LO https://github.com/piotrwozn/cairnmind/releases/download/v0.11.0/cairnmind-0.11.0.tar.gz
tar -xzf cairnmind-0.11.0.tar.gz
cd cairnmind-0.11.0
```

**From source.** Clone the repository to follow the latest `main`:

```bash
git clone https://github.com/piotrwozn/cairnmind.git ~/src/cairnmind
cd ~/src/cairnmind
```

## 4. Run the installer

**Upgrading from DevBrain.** The installer removes the earlier `devbrain` uv tool and keeps your memory in `~/.local/share/devbrain`. Projects set up by DevBrain still point their hooks and MCP entries at the removed Python, so the installer then lists one `cairnmind install <project>` command per registered project; run them before starting Codex or Claude Code in those projects. The new package also installs `devbrain` as an alias for `cairnmind`.

From the directory that contains `pyproject.toml`:

```bash
bash scripts/install-wsl.sh
```

The same script installs on macOS, with the bash that macOS ships.

The installer:

1. checks Java and tmux versions; on macOS it first checks that the Mac has Apple silicon and that the terminal does not run under Rosetta;
2. installs Cairnmind as a `uv` tool, with dependency versions pinned by `uv.lock`;
3. downloads and verifies ArcadeDB, starts the engine and initialises your local memory in `~/.local/share/cairnmind` (or keeps using `~/.local/share/devbrain` if an earlier version created it);
4. runs `cairnmind doctor` and prints anything that still needs attention.

If the installer prints `Add this directory to PATH`, add that directory to your shell profile and open a new terminal.

The Java it checks is the one the engine runs: the one in `JAVA_HOME` when that has `bin/java`, otherwise `java` on `PATH`. On macOS, when `JAVA_HOME` is not set, the one that `/usr/libexec/java_home -v 21+` finds comes before `PATH`. A `java_executable` set in `config.json` of the data directory comes before all of them.

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

To run ArcadeDB yourself in a container, on a server or on a laptop for development, see [DEPLOYMENT.md](DEPLOYMENT.md). It also covers backups, restores and upgrading the database.

An installation that uses such a database is connect-only: it never installs, starts or stops an engine and needs no Java. Set `CAIRNMIND_ARCADE_CONNECT_URL` to the database address before you run the installer, and it skips the Java check and the ArcadeDB download; then follow [Connect-only installations](DEPLOYMENT.md#connect-only-installations) to create the database user and the store. The rest of this page (models, coding assistants, the first project) is the same.

## 5. Download the models

Ollama usually runs as a service after installation. If it does not, start it in a separate terminal with `ollama serve`. Then:

```bash
cairnmind models pull
cairnmind doctor
```

`cairnmind doctor` reports the Python, Java, tmux, ArcadeDB, Ollama and coding-client state. It is read-only: no logins, downloads or model sessions.

## 6. Connect your coding assistants

Install and sign in to the clients you want to use, in the same Ubuntu distribution, with their own installers:

- [Codex CLI](https://developers.openai.com/codex/cli)
- [Claude Code](https://code.claude.com/docs/en/setup)

Then check that Cairnmind can drive them safely:

```bash
cairnmind doctor --contract
```

Each check shows the line of client output that proved it. A client that fails a check is not launched, because a verified resume cannot be guaranteed.

API agents (OpenAI, Anthropic, OpenAI-compatible endpoints, Ollama) and GitHub Copilot are configured later in the workspace under **Connections**; see [API.md](API.md).

## 7. Open your first project

```bash
cairnmind project add my-app ~/src/my-app
cairnmind install my-app
cairnmind
```

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
- For later releases, read the release notes first, then run the installer from the new source directory.

## Uninstalling

```bash
cairnmind workspace stop
cairnmind stop
cairnmind engine stop
uv tool uninstall cairnmind
```

This keeps your memory in the data directory that `cairnmind doctor` shows as `data_dir`. To remove it too, first make a backup with `cairnmind backup ~/cairnmind-backup.cairnmind.json.gz`, then delete the directory. Deleting it cannot be undone.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `cairnmind: command not found` | Add the directory printed by the installer to `PATH`, or run `uv tool update-shell` and open a new terminal. |
| `Java 21+ is required` | `sudo apt install openjdk-21-jre-headless`, and check that `java -version` reports 21 or newer. When the message names `JAVA_HOME`, that Java is too old: point `JAVA_HOME` at Java 21 or unset it. On macOS: `brew install openjdk@21` and the `JAVA_HOME` line of step 2. |
| `Java not found` or `macOS answers so when no JDK is installed` on macOS | No Java 21 is found. If Homebrew's `openjdk@21` is installed, the installer prints the `export JAVA_HOME=...` line to add to `~/.zprofile`; open a new terminal after adding it. |
| `This Mac has an Intel processor` | Cairnmind runs on Apple silicon Macs only. |
| `This shell runs under Rosetta` | The terminal app is set to open using Rosetta: turn that off in its Get Info window, or run `arch -arm64 /bin/bash scripts/install-wsl.sh`. |
| `Version 3.4+ is required` for tmux | Ubuntu 24.04 ships tmux 3.4. On older releases, upgrade Ubuntu or build tmux 3.4. On macOS: `brew upgrade tmux`. |
| `Cannot reach Ollama` | Start `ollama serve`, then run `cairnmind models pull` again. |
| `cairnmind` exits with code 2 | It needs an interactive terminal. Run it directly in Windows Terminal or another terminal, not through a pipe. |
| A client is not launched | Run `cairnmind doctor --contract`; the failing check names the missing capability. |
| The layout looks cramped | Enlarge the terminal, or stay in the default Focus view, which works at 80 × 24. |
