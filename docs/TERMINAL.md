# Cairnmind 0.9.0 terminal workspace

`cairnmind` starts the workspace in the current terminal. `cairnmind tui` is an alias for the same interface. tmux manages panes and detaching, and Textual renders projects, settings, the inspector and the graph. The Codex and Claude panes contain the real native CLIs, with their own screens, keyboard handling, sign-in and approvals. Ollama and API profiles use Cairnmind's own agent, and Copilot uses a separate adapter built on the official SDK.

The default language is English. To switch the Cairnmind interface to Polish, choose **More → Settings → Language → Polski**. The language setting applies to the Cairnmind interface only; project content and the native client interfaces keep their own language.

## Getting started

You need Linux or Ubuntu on WSL2, Java 21+, tmux 3.4+, an installed Cairnmind and an interactive Unicode/UTF-8 terminal. Install the coding clients in the same WSL distribution. Using the prebuilt package does not require Node.js, npm or a frontend build.

In Windows Terminal, open Ubuntu, or enter it from PowerShell:

```powershell
wsl -d Ubuntu
```

Then, in Ubuntu:

```bash
cairnmind
```

The command works from any directory. The project list comes from Cairnmind's shared local registry, not from the directory in which the terminal was opened. A separate `--data-dir` or `CAIRNMIND_HOME` means a separate store and registry.

On first setup, choose **Add folder**. Enter a Linux or Windows path, for example `/home/piotr/src/app` or `C:\Users\piotr\src\app`. **Browse folders** opens a directory picker without shell commands: `Enter` opens a subfolder, `Alt+Up` goes up one level, and `Ctrl+S` selects the displayed directory. The project name can be left empty; it is then taken from the folder name. In the form, `Ctrl+S` saves and `Esc` goes back. You can also use the CLI:

```bash
cairnmind project add my-app ~/src/my-app
cairnmind install my-app
cairnmind
```

For a cloned project that contains `.memory/cairnmind/`, start by previewing and importing its memory so that its existing identity is preserved. Merely detecting a snapshot does not import any data and does not run any commands stored in it.

Starting without an interactive TTY exits with a message and code `2`, before any configuration is created or any service is started. It does not fall back to a browser. The package version can be read without a TTY and without initialization:

```bash
cairnmind version
```

## Layout and size

The default **Focus** layout shows a single view across the full width. Start with **Projects**, open a project and choose **Start work**. The menu opened with `Ctrl+B` followed by `Space` switches between projects, the assistant, memory and the graph. **Ask memory** in that menu sends a question to the selected project's memory and shows the answer with cited sources and missing evidence; it writes nothing. The interface is fully keyboard-operable even at **80 × 24**.

**More → Settings → Layout → Cockpit** enables the multi-pane layout on a larger terminal. The same setting is available through the **Focus view** and **Cockpit view** commands in the menu. The full layout can show five panes:

```text
┌────────────────┬──────────────────────┬──────────────────────┐
│ Projects       │ Codex CLI            │ Claude Code          │
│ Worktree       │                      │                      │
│ Connections    ├─────────────────────────────┬───────────────┤
│ More           │ Memory inspector            │ 3D graph      │
│                │                             │ ASCII XYZ     │
└────────────────┴─────────────────────────────┴───────────────┘
```

The sidebar is 24 columns wide. On the right, the upper area holds the selected clients and agents, and the lower area holds the inspector and the graph. A single selected client uses the whole upper area; with two, the area is split. If you configure more slots, you explicitly choose up to two to show at once. Hiding a slot does not end its process.

The minimum size for the full Cockpit layout is **140 columns × 42 rows**. **180 × 55** or larger is recommended. Below the minimum, the active pane is zoomed; shortcuts still switch between views and hidden processes keep running. Enlarging the window alone does not turn off a selected Focus layout.

Windows Terminal should use a font that includes Polish characters and Unicode box-drawing characters. In Ubuntu, `locale charmap` should report `UTF-8`. Sizes are measured in terminal character cells, not screen pixels.

## Navigation

Press the `Ctrl+B` prefix first, then the listed key:

| Key after `Ctrl+B` | Action |
|---|---|
| `Space` | Searchable menu of all commands |
| `?` | Keyboard help |
| `p` | Projects, profiles and settings pane |
| `1` | First visible client |
| `2` | Second visible client |
| `i` | Memory inspector |
| `g` | Memory graph |
| `n` | Save a finding to the memory of the open project |
| `Tab` | Next view |
| `z` | Zoom the active pane / return to the layout |
| `[` | Scroll and copy terminal history; `q` returns |
| `d` | Detach the workspace and return to the shell |
| `Ctrl+B` | Send `Ctrl+B` through to the native client |

A double `Ctrl+B` is needed when Codex, Claude or a program running in their terminal uses that shortcut itself. All other input, pasting, `Ctrl+C` and approval dialogs in the active client pane go to its real CLI. Use `Tab`, `Shift+Tab`, the arrow keys and `Enter` for Cairnmind buttons and fields; `Esc` closes a dialog or goes back. Save forms with `Ctrl+S`. A field or save error keeps the values you entered and shows the problem; retrying requires a deliberate save.

In the project list, `Enter` opens the project screen for inspection. It does not start an assistant. On that screen, **Start work** starts only the indicated assistant. Outside text fields, `w` selects that action, `p` opens the preview, `r` refreshes and `m` opens memory. In the list, `w` first opens the project screen. Empty search results stay empty; actions never fall back to a previously active project. In the inspector, `h` opens history, `m` opens memory, `r` refreshes and `Esc` returns to the list. Letter shortcuts work only in the focused Cairnmind pane.

`Ctrl+B n` opens a small form over the workspace to write down a finding without leaving it: the text (several lines are fine), a kind (`decision`, `fact`, `constraint`, `question` or `preference`) and a status (`proposed`, the default, or `confirmed`). `Ctrl+S` saves it to the open project and closes the form; the finding belongs to the whole project, so it is found from every worktree of it. The status line then confirms the kind and status for a few seconds. `Esc` or `Ctrl+Q` closes the form without saving, but not while a save is under way; pressing `Ctrl+S` again during a save does nothing. An empty text cannot be saved. If the service refuses the finding, the form stays open with your text and choices and shows the reason. If no clear answer comes back (the form waits 20 seconds), the finding may have been saved: saving again from the same form is safe, because each form sends one key and the service stores at most one finding per key. The finding is written by the memory service like any other record, with a source of its own: a `workspace://finding/…` address, the time it was saved and the origin `workspace_form`, so the memory inspector shows where it came from. The form never reads or attaches what is on a terminal pane, and the text goes to the service as data, never through a shell.

**Start work** in the global menu first shows the project and moves focus to the start button. Check the visible assistant name and press `Enter` to begin. This prevents a delayed menu command from starting an assistant after the project has changed.

If the TUI starts the memory service, its embedding and extraction queue waits for an explicit action. A successful **Start work** enables memory processing for the selected project only. You can also choose **More → Queue → Process memory** to process pending jobs without starting an assistant. Opening a project, reading memory or switching views does not enable the queue. File scanning and configured snapshots still run. If the assistant starts but enabling the queue fails, the message points you to **Process memory**; you do not need to restart the assistant.

Services and sessions that are already running continue their work. A manually started `cairnmind serve` keeps automatic queue processing; `cairnmind serve --defer-background-inference` lets you defer it. An explicit model query remains a separate user action.

Wider forms temporarily zoom the pane so that paths and settings fit. When closed, they restore the previous layout, unless the pane or zoom was changed manually in the meantime.

## Project, assistant and connection

The sidebar lists all registered projects. You can choose a project's registered worktree and scan explicitly specified directories in Ubuntu or on Windows drives reachable through WSL. Scanning does not grant permission to start clients and does not import memory automatically.

1. **Projects**: search for the project and press `Enter`.
2. **Project**: check the folder and the visible assistant name. You can pick another assigned assistant or use **Add assistant**.
3. **Connections**: add a named connection, sign in to an account or set a key, and choose a model. Limits and credential references are in the connection's advanced settings.
4. Go back to the project and choose **Start work**. Only the one indicated assistant starts working.

Connections cover native Codex, Claude Code, the OpenAI/Anthropic agent, OpenAI-compatible APIs, local Ollama and GitHub Copilot. **More → Manage assistants** lets you change order and assignments. Adding an assistant keeps existing assignments. All clients work in the indicated directory; Cairnmind does not create separate worktrees on its own and does not automatically assign a shared task. See [account, key and limit configuration](API.md).

Opening a workspace or restoring its layout attaches existing processes. You start new work explicitly from the sidebar. Selecting data for inspection, including moving from the global graph to a project, does not start a client. A profile change takes effect the next time the process starts.

Client status distinguishes a missing program, configuration, declared sign-in, connectivity and an observed limit. A signed-in client can have an exhausted limit; a network error does not mean it was signed out. Checking status does not make a paid model request.

**More** contains sessions, assistant management, folder search, snapshots, memory import and settings. The main views show readable summaries; **Technical details** opens the full diagnostic data. Memory state, session activity and terminal control rights are reported separately.

Signing in runs the selected client's own procedure in a separate operational terminal. Device codes and the output of that terminal do not become project memory or graph nodes. Refresh the status when it finishes. Projects, MCP, hooks and tools of the native clients are approved through the Codex or Claude mechanisms. Cairnmind's own agent and the Copilot adapter show a one-time approval for the exact command. API keys are entered in a masked field; saving them locally is optional and the stored value is not encrypted.

## Own agent and Copilot pane

After the selected profile starts, the pane replays history without querying the model. Send a message with **Send** or `Ctrl+Enter`. Replies, tool calls and tool results are displayed verbatim; control sequences from sources do not control the terminal. Tokens and usage are labeled as observed, and prices as an estimate based on the profile's rates or as **no data**.

`Ctrl+C` or **Interrupt** stops the current operation. An action without a confirmed result requires **Resolve** (`Ctrl+R`) and a note after you have checked it yourself. Save the note with `Ctrl+S`. This action does not re-run the command. A new message is a separate continuation. The model must read the current memory and perform an exact ACK before any action that requires restoring it.

For details on limits, file access, Copilot sign-in and memory, see [API.md](API.md).

## Inspector and portable memory

The inspector shows records, full sources, raw and normalized conversations, metadata, version history, plans, tasks, test results and the queue. Large lists and sources are split into pages or chunks. Browsing alone does not call a model, run tests or retry jobs.

**Confirm** and **Dispute** record an explicit assessment with an audit entry and a record version check. Disputing requires a justification; a managed task also requires the current plan version. A conflict means you must re-read the record before making another deliberate assessment. Finding confidence, task progress and test freshness are shown separately: confirming a finding does not set `done` and does not create test evidence.

The global inbox of unassigned entries keeps sources that have no certain project. Reading them or selecting them in the graph does not assign them to the current project.

**Memory** opens the record overview. **More → Snapshots and export** opens snapshot preview, saving, import check, import and automation settings. The existing commands also work:

```bash
cairnmind memory snapshot my-app --preview
cairnmind memory snapshot my-app
cairnmind memory auto my-app --enable --interval-minutes 15
cairnmind memory status my-app
cairnmind memory inspect ~/src/my-app/.memory/cairnmind
cairnmind memory show ~/src/my-app/.memory/cairnmind --kind decision
cairnmind memory import ~/src/my-app/.memory/cairnmind --root ~/src/my-app --dry-run
```

Automatic snapshots remain opt-in per project. A snapshot contains the project's stored memory and history, excluding operational credentials and model weights. Raw conversations may contain private data. Import does not overwrite other projects, and transferred tests remain historical until they are checked locally. See the [memory contracts](MEMORY_CONTRACTS.md) for the full portable-memory format, limits and restore rules.

## 3D graph in the terminal

The graph uses **X/Y/Z** coordinates, rotation in space and perspective projection. Version **0.7.1** renders nodes as lit solids drawn with ASCII characters: brightness conveys shape, and nearer surfaces occlude farther ones. A depth buffer and correction for the terminal cell aspect ratio keep the image readable while rotating, much like the classic terminal donut animation. The scene shows real memory nodes and links; it does not replace them with a donut or demo data. The scene limit is **300 nodes and 800 edges**, and the refresh limit is **10 frames per second**. This limits the scene, not the size of memory.

Groups cover projects, sessions and data types. You can enter a group, page through it and select a specific entry or revision. All stored messages and history remain available through groups and the inspector. A raw source, a technical client entry, a model proposal and a confirmed finding have different meanings. Structural grouping of projects does not create implied semantic relations between their memories.

Updates use committed database changes; identifiers and the camera are preserved during refresh. Data fetching and the knowledge model do not handle native client keyboard input. Selecting a node opens its data in the inspector. When the memory service is unavailable, data views show the problem, while native processes may keep running.

After switching to the graph with `Ctrl+B g`:

| Key in the graph | Action |
|---|---|
| Arrow keys | Rotate the XYZ view |
| `+` / `-` | Zoom in / zoom out |
| `Space` | Pause / resume motion |
| `r` | Reset the camera |
| `]` / `[` | Select the next / previous node |
| `Tab` / `Shift+Tab` | Move between interface elements |
| `Enter` | Inspect an entry or expand a group |
| `g` | Group list; inside it `n`/`p` switches pages and `Esc` closes |
| `n` / `p` | Next / previous scene page |
| `a` | Toggle between global and project view |
| `Backspace` | Return to the parent scope |

Motion pauses when tmux is detached or when the graph is hidden by zooming another pane or by the compact view. When you return, the camera and the selected scope are preserved.

**More → Settings → Reduce motion** turns off automatic graph rotation. You can still rotate the view manually and select nodes.

## Detaching, stopping and resuming

```bash
cairnmind workspace status
cairnmind workspace detach
cairnmind workspace takeover
cairnmind workspace stop
cairnmind workspace stop --clients
```

`detach` or `Ctrl+B d` detaches the terminal while keeping the workspace and running processes. The next `cairnmind` attaches to the existing workspace. `workspace stop` ends the outer terminal layout; `--clients` additionally stops only the client identifiers adopted by this workspace. It does not stop every client on the machine or arbitrary sessions outside its registry.

Only one terminal can type at a time. When the workspace is already open in another terminal, `cairnmind` offers a choice: **Observe (read only)**, **Take control** or **Return to shell**. The default `Enter` chooses observing. Taking control explicitly hands the keyboard to the new terminal and switches the previous one to view-only mode. It does not start a second copy of the clients or a new coding session. From view-only mode you can also detach with `Ctrl+B d` and run `cairnmind workspace takeover`.

The knowledge service and the database have their own commands, `cairnmind stop` and `cairnmind engine stop`. Stopping the UI does not delete data. Shutting down the whole WSL distribution ends all processes, including tmux and the clients. After restarting, you choose in the sidebar whether to start work, resume a saved session or begin a new one. Resuming a native client requires its verified identifier; a missing identifier is shown explicitly. Cairnmind's own API and Copilot sessions use their journal and the normal memory restore protocol, without re-granting earlier approvals.

Attaching a terminal and displaying memory are not an ACK. A new or resumed session still goes through the normal bundle preparation, `resume_context`, readiness check and exact `ack_resume`. `partial` or `unavailable` blocks writes to shared memory. A code change made from a second pane may require a new bundle and ACK. None of these operations replaces the native tools' own approvals.

## Upgrading and diagnostics

Version **0.8.0** simplifies navigation and separates opening a project from starting an assistant. The interface change does not grant earlier tool approvals and does not replace the memory protocol. For installation, upgrade and diagnostics, see the [Cairnmind README](../README.md).

```bash
cairnmind version
cairnmind workspace status
cairnmind doctor
tmux -V
locale charmap
```

If tmux is missing, install it in Ubuntu with `sudo apt install tmux`. If the screen is too small or the graph shows unreadable characters, check the window size, the font and UTF-8. Read memory, model, archive-scope and sign-in errors separately; a message about an exhausted client limit does not by itself mean memory was lost.

Developer tests may use real tmux and ArcadeDB together with controlled CLI stand-ins. They do not measure paid APIs or Codex/Claude/Copilot accounts and do not confirm behavior on the user's physical WSL machine. Such results require a separate run in the target environment.

The shortcut translation layer is matched to the pinned Textual 8. Textual provides a footer refresh, but no public setter for the descriptions of existing bindings; [its documentation describes the limits of dynamic bindings](https://textual.textualize.io/guide/input/#dynamic-bindings). A small `localize_bindings` adapter changes only their descriptions and keeps keys, actions, priorities and identifiers. When upgrading Textual, run the language and keyboard-routing regression tests.

The 0.5 browser interface and the `cairnmind cockpit open` command were removed in 0.8.0. The terminal workspace described here replaces them.
