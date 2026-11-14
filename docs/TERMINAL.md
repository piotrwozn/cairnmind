# Cairnmind 0.10.0 terminal workspace

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

An **activity** column, 32 columns wide, sits beside the focused view in the Focus layout (the default) from **160 columns**, and on the right of the Cockpit layout from **213 columns**, where every pane is still as wide as at the recommended 180. In Focus the focused view keeps the rest of the width (127 columns at 160). The other views keep running in the same tmux window, out of sight to the right of your terminal and exactly as large as the focused view, so switching views resizes no pane: an assistant you are not looking at is not redrawn for a new size. Below 160 columns, or with **More → Settings → Activity column → Off**, Focus zooms the focused view as before; the choice is remembered and is on by default. A read-only viewer whose terminal is wider than yours also sees the start of the next view to the right. If the column's process ends, it is started again in place within a few seconds. For the selected project it shows, read-only: **Running now** (the started sessions of the workspace with their state, and the stage of the next plan step), **Plan** (the active plan with its steps done of total, and plans whose every step is done), **Tasks** grouped by state (in progress, needs review, needs test, blocked, to do, done recently, each with its count and its newest few) and **Notices** (the latest notices of the sidebar, inspector and graph across the workspace, newest first, with their kind as a word; a notice of another project is labelled with that project). It follows a project switch and updates when memory changes or a panel posts a notice, without a key. `Ctrl+B a` moves to it, and the key of the view you came from (for example `Ctrl+B 1`) moves back; where the column does not exist, `Ctrl+B a` opens the same panel over the workspace, and `a`, `q` or `Esc` closes it. In it, `Tab` jumps to the next task or plan of the next section (and into the notices), the arrows move between items, `r` refreshes, and `Enter` on a task or plan opens it in the memory inspector. The panel writes no record, review, verdict, task state or plan. Tab in the workspace (`Ctrl+B Tab`) still moves between the five views only.

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
| `a` | Activity panel: notices, plans and tasks of the open project (a popup when the column is not shown) |
| `Tab` | Next view |
| `z` | Zoom the active pane / return to the layout |
| `[` | Scroll and copy terminal history; `q` returns |
| `d` | Detach the workspace and return to the shell |
| `Ctrl+B` | Send `Ctrl+B` through to the native client |

A double `Ctrl+B` is needed when Codex, Claude or a program running in their terminal uses that shortcut itself. All other input, pasting, `Ctrl+C` and approval dialogs in the active client pane go to its real CLI. Use `Tab`, `Shift+Tab`, the arrow keys and `Enter` for Cairnmind buttons and fields; `Esc` closes a dialog or goes back. Save forms with `Ctrl+S`. A field or save error keeps the values you entered and shows the problem; retrying requires a deliberate save.

In the project list, `Enter` opens the project screen for inspection. It does not start an assistant. On that screen, **Start work** starts only the indicated assistant. Outside text fields, `w` selects that action, `p` opens the preview, `r` refreshes and `m` opens memory. In the list, `w` first opens the project screen. Empty search results stay empty; actions never fall back to a previously active project. In the inspector, `h` opens history, `m` opens memory, `r` refreshes and `Esc` returns to the list. Letter shortcuts work only in the focused Cairnmind pane.

`Ctrl+B n` opens a small form over the workspace to write down a finding without leaving it: the text (several lines are fine), a kind (`decision`, `fact`, `constraint`, `question` or `preference`) and a status (`proposed`, the default, or `confirmed`). `Ctrl+S` saves it to the open project and closes the form; the finding belongs to the whole project, so it is found from every worktree of it. The status line then confirms the kind and status for a few seconds. `Esc` or `Ctrl+Q` closes the form without saving, but not while a save is under way; pressing `Ctrl+S` again during a save does nothing. An empty text cannot be saved. If the service refuses the finding, the form stays open with your text and choices and shows the reason. If no clear answer comes back (the form waits 20 seconds), the finding may have been saved: saving again from the same form is safe, because each form sends one key and the service stores at most one finding per key. If you changed the text, kind or status before saving again, the form refuses and keeps your edit: the first version is already in memory, and the changed one needs a new form. The finding is written by the memory service like any other record, with a source of its own: a `workspace://finding/…` address, the time it was saved and the origin `workspace_form`, so the memory inspector shows where it came from. The form never reads or attaches what is on a terminal pane, and the text goes to the service as data, never through a shell.

**Start work** in the global menu first shows the project and moves focus to the start button. Check the visible assistant name and press `Enter` to begin. This prevents a delayed menu command from starting an assistant after the project has changed.

**Run next stage** on the project screen reads the next step of the active plan and does what **More → Plan stages** maps to its stage: it starts that one assistant as **Start work** does, or runs the mapped check with `cairnmind check run` and shows the result. Nothing is mapped by default. A missing mapping, or a mapped assistant that is no longer configured or cannot be launched for a step, is shown with a pointer to **Plan stages** and nothing else starts; a folder without an active plan is told how to activate or save one instead. A mapped assistant that is already running in this folder is not restarted and gets no new resume packet; the message says so. Choosing another project cancels a stage that is being prepared. A check that is already running keeps running: its result is listed in **Notices** under the project it ran for when it ends, and it is put on the notice line only if that project is still shown when it arrives (like any notice, it stays there until the next one replaces it). It never changes the step's state. In the global menu, **Run next stage** first shows the project and moves focus to its button, like **Start work**. See [Plan stages](WORKFLOW.md#plan-stages).

If the TUI starts the memory service, its embedding and extraction queue waits for an explicit action. A successful **Start work** enables memory processing for the selected project only. You can also choose **More → Queue → Process memory** to process pending jobs without starting an assistant. Opening a project, reading memory or switching views does not enable the queue. File scanning and configured snapshots still run. If the assistant starts but enabling the queue fails, the message points you to **Process memory**; you do not need to restart the assistant.

Services and sessions that are already running continue their work. A manually started `cairnmind serve` keeps automatic queue processing; `cairnmind serve --defer-background-inference` lets you defer it. An explicit model query remains a separate user action.

Wider forms temporarily zoom the pane so that paths and settings fit. When closed, they restore the previous layout, unless the pane or zoom was changed manually in the meantime.

## Project, assistant and connection

The sidebar lists all registered projects. You can choose a project's registered worktree and scan explicitly specified directories in Ubuntu or on Windows drives reachable through WSL. Scanning does not grant permission to start clients and does not import memory automatically.

1. **Projects**: search for the project and press `Enter`.
2. **Project**: check the folder and the visible assistant name. You can pick another assigned assistant or use **Add assistant**.
3. **Connections**: add a named connection, sign in to an account or set a key, and choose a model. Limits and credential references are in the connection's advanced settings.
4. Go back to the project and choose **Start work**. Only the one indicated assistant starts working.

Connections cover native Codex, Claude Code, the OpenAI/Anthropic agent, OpenAI-compatible APIs, local Ollama and GitHub Copilot. **More → Manage assistants** lets you change order and assignments. Adding an assistant keeps existing assignments. **More → Plan stages** maps the plan stages of the project to its assistants (Codex, Claude Code, the built-in API agent or GitHub Copilot) and a check profile; the map is saved with the other workspace preferences on this machine. Removing an assistant keeps the map, and **Run next stage** then reports the stage's assistant as unknown until you map another. All clients work in the indicated directory; Cairnmind does not create separate worktrees on its own and does not automatically assign a shared task. See [account, key and limit configuration](API.md).

**More → MCP servers** is the one place where a person gives the built-in API agent of the selected project the tools of a stdio MCP server. It lists the project's servers (at most four) as literal text, **Add server** opens a form for an id, the program's absolute path, arguments (one per line) and literal environment values (`NAME=value` per line, no secrets: names that look like credentials are refused, and so are the variables the form lists that change which code a program loads, such as `PATH`, `HOME`, `LD_*`, `PYTHONPATH` and `NODE_OPTIONS`), and choosing a server offers to remove it after a confirmation. The form says what a server can do: it is a program you run under your own account, Cairnmind is not a sandbox for it, and its text can steer the agent into editing files in this folder. Saving changes only this project's list, and only if it is still the list the form showed; if it was changed elsewhere meanwhile, the form says so and nothing is saved. The list is saved with the other workspace preferences on this machine and applies from the next agent run of the project; a server whose program, an argument or a value names a path inside the project folder is then not started; other projects, Copilot runs and native Codex or Claude Code sessions do not get it. In the agent's pane, each call of such a tool opens **Approve one third-party tool call** with the server, the tool and the exact arguments, and runs only after **Call once**; a call whose request is too long to show in full is refused before the dialog. A server that does not start is named in the status line with a fixed code and the agent works on without it. See [Third-party MCP tools](API.md#third-party-mcp-tools).

Opening a workspace or restoring its layout attaches existing processes. You start new work explicitly from the sidebar. Selecting data for inspection, including moving from the global graph to a project, does not start a client. A profile change takes effect the next time the process starts.

Client status distinguishes a missing program, configuration, declared sign-in, connectivity and an observed limit. A signed-in client can have an exhausted limit; a network error does not mean it was signed out. Checking status does not make a paid model request.

**More** contains sessions, assistant management, folder search, snapshots, memory import and settings. The main views show readable summaries; **Technical details** opens the full diagnostic data. Memory state, session activity and terminal control rights are reported separately.

The sidebar has one notice line, and each new notice replaces the one before. When another project is shown, from the sidebar or from anywhere else, a notice about the earlier project leaves the line. The sidebar therefore keeps the notices it showed, each with its time and the project and folder it was shown for, and **More → Notices** (also **Notices** in the `Ctrl+B Space` menu) lists them, newest first. A notice of another project than the one now shown is labelled with that project, so a result is not read as belonging to the open one; a repeated notice is one line with a count. When a notice was replaced since you last opened the list, a mark such as `[Notices: 2]` appears under the notice line, and opening the list clears it. The history is bounded (200 notices and a total size; the oldest fall out), is kept in memory only for as long as the sidebar runs, and is never written to disk, to the workspace state or to a log. The other panes (memory inspector, graph) record their notices the same way but have no list yet. For the activity panel, each of the three panels also keeps its newest 30 notices, cut short, as an option of its own pane on the workspace's private tmux server; that copy is in tmux's memory, not on disk, and goes when the pane closes. Any process of your user that can reach that private tmux socket can also write such a copy (it can already send keys to the panes), so these notices are shown to you only and never go to a model, a record or a review; the activity panel reads each panel's copy only from the pane the workspace recorded for it.

Signing in runs the selected client's own procedure in a separate operational terminal. Device codes and the output of that terminal do not become project memory or graph nodes. Refresh the status when it finishes. Projects, MCP, hooks and tools of the native clients are approved through the Codex or Claude mechanisms. Cairnmind's own agent and the Copilot adapter show a one-time approval for the exact command. API keys are entered in a masked field; saving them locally is optional and the stored value is not encrypted.

## Own agent and Copilot pane

After the selected profile starts, the pane replays history without querying the model. Send a message with **Send** or `Ctrl+Enter`. Replies, tool calls and tool results are displayed verbatim; control sequences from sources do not control the terminal. Tokens and usage are labeled as observed, and prices as an estimate based on the profile's rates or as **no data**.

`Ctrl+C` or **Interrupt** stops the current operation. An action without a confirmed result requires **Resolve** (`Ctrl+R`) and a note after you have checked it yourself. Save the note with `Ctrl+S`. This action does not re-run the command. A new message is a separate continuation. The model must read the current memory and perform an exact ACK before any action that requires restoring it.

For details on limits, file access, Copilot sign-in and memory, see [API.md](API.md).

## Inspector and portable memory

The inspector shows records, full sources, raw and normalized conversations, metadata, version history, plans, tasks, test results and the queue. Large lists and sources are split into pages or chunks. Browsing alone does not call a model, run tests or retry jobs.

**Confirm** and **Dispute** record an explicit assessment with an audit entry and a record version check. Disputing requires a justification; a managed task also requires the current plan version. A conflict means you must re-read the record before making another deliberate assessment. Finding confidence, task progress and test freshness are shown separately: confirming a finding does not set `done` and does not create test evidence.

**Impact** lists the records worth re-checking after a code change, as `cairnmind impact` does, for the project and worktree open in the workspace. **Compare with** holds the commit to compare the worktree with, `HEAD` by default; type another commit, tag or branch and press `Enter` to read the list again. Against `HEAD` the list covers uncommitted edits and untracked files only, so in a clean worktree it is empty and the view says so: type the branch you started from (for example `main`) to see everything the current branch changed. **Pending only** leaves out the entries that already have a verdict. Each row shows the record, the changed files and definitions it is linked to, and whether it was checked; `Enter` opens the entry with every link and its reason, the evidence state and the `checked:` line with its date and note. A link means "worth checking", not that the record is wrong. The list is computed with Git each time it is read and memory updates do not reload it: press `r` to read it again.

**Verdict** on an open entry asks whether the record is still valid, needs an update or is not related, then for a note (required for "needs update") and saves the verdict for the entry exactly as it was shown. If the code or the record changed since the list was read, nothing is saved: the view says so and reads the list again, and you choose again if the verdict still holds. Any other refusal is shown as text; nothing is retried on its own. A verdict is an audit note: it changes no record, status, revision or task. Only the person at the workspace gives one: it is sent through the workspace's own broker session, like **Confirm** and **Dispute**, and a coding session or the built-in agent has no tool or command for it.

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

`memory status` checks only the newest manifest (its format, identity and checksum), so it answers quickly for a large project; its `latest` says `manifest_valid` and `objects_checked: false`. `memory inspect` and every import check each object as well. Automatic snapshots remain opt-in per project. A snapshot contains the project's stored memory and history, excluding operational credentials and model weights. Raw conversations may contain private data. Import does not overwrite other projects, and transferred tests remain historical until they are checked locally. See the [memory contracts](MEMORY_CONTRACTS.md) for the full portable-memory format, limits and restore rules.

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

`detach` or `Ctrl+B d` detaches the terminal while keeping the workspace and running processes. `Ctrl+Q` in a Cairnmind view (projects, memory, graph or activity) does the same for the terminal that pressed it; in an assistant pane the key goes to the assistant. No key ends a Cairnmind view, and a view whose process ended anyway starts again when you ask for it with `Ctrl+B p`, `i`, `g`, `Tab` or the menu. The next `cairnmind` attaches to the existing workspace. `workspace stop` ends the outer terminal layout; `--clients` additionally stops only the client identifiers adopted by this workspace. It does not stop every client on the machine or arbitrary sessions outside its registry. A run that has already ended is listed under `clients_already_ended`, and one the client service no longer knows at all under `clients_unknown`; both are forgotten by the workspace and neither is an error. For the same reason an assistant pane whose run the client service no longer knows ends with "The session changed." instead of showing offline and retrying.

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
