# Supervised operator TUI evaluation (prepared, not run)

The real entrypoint `devbrain --data-dir DIR tui` calls `launch_workspace`, which
can start the ordinary service through `BrokerClient.ensure`. It is deliberately
not the launcher for this qualification. The new controller reuses the existing
`create_app(settings, start_worker=False)`, `create_cockpit_app(settings)`, and
`WorkspaceManager.create/refresh/attach`. These are actual production panels,
HTTP/WebSocket routing and native session controls, not synthetic panel stubs.

The controller must run with the qualified literal venv Python under the final
Windows/Linux external wrapper and its unique scope. It requires `--execute`,
`--run-id`, `--artifact-root`, `--source-root`, `--private-root`, `--arcade-dist`,
`--codex-bin`, `--claude-bin`, `--codex-auth`, and `--claude-auth`; `--duration`
defaults to and cannot exceed 1800 seconds. The external timeout must allow
bounded startup/cleanup, with its independent 30-second observation unchanged.
Required evidence adds `interactive-ready.json` and `interactive-results.json`.
The imported `validate-native-account080.py` must be hash-bound by both wrappers.

Use `--operator agent` for the authorized keyboard/visual assessment by Codex;
the default `human` is only for a person's own observations. The private manifest,
ready file, report and every observation bind this choice. An attach-only viewer
inherits it and cannot relabel the run. Agent observations set `agent_reported=true`
and `human_reported=false`; only `agent_acceptance_passed` may pass. Both modes
require the same five tasks once and three actual terminal geometries. No result
is inferred merely from launching or rendering the UI.

Only two synthetic Git projects and their source/fact/task rows are seeded.
Private HOME/config/cache directories are fresh 0700. Codex uses a private OAuth
copy with the common helper's serialized, durable compare-and-swap refresh sync
under the official `auth.lock`. Its private journal binds the owned scope; an
unfinished journal is recovered only after the old scope is proven empty or the
boot changed. The pinned Claude binary uses its official secure-storage-only
override pointing to the original OAuth store, while HOME and configuration
remain private. No API keys, MCP settings, plugins, or whole account directories
are copied. The attach manifest contains only the canonical Claude storage
directory and binary path/hash, never transaction rows, credential hashes,
principals, seeds or file descriptors. Missing onboarding/login is a visible
BLOCKED outcome, not grounds to copy more configuration. Existing model service is not used: Ollama points
to the unused loopback port 1. Native Codex/Claude start only after the operator
chooses Start work. Do not add real projects or enter private account details.

After `interactive-ready.json` exists, root can prepare three launch shortcuts
to `open-interactive-ux080.ps1 -Execute` with the same ready file, installed
Windows Terminal path, and `-Size 80x24`, `120x36`, or `180x55`. The helper uses
literal argument lists, not a shell. Windows Terminal's documented `--size c,r`
sets the initial geometry; the Linux viewer independently checks the real TTY
size before attachment and before accepting any human result. Font/display
constraints can require adjustment; a requested size is never counted as proof.
Source: [Microsoft Terminal arguments](https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments).

Do the five tasks once in 120x36:

1. Browse project A, then explicitly Start work. Browsing alone starts no model.
2. Switch A → B → A and return to the existing session without duplicate work.
3. Locate source `ux://A/evidence` and read `UX_A_ZAZOLC`.
4. Change the selected Codex/Claude connection/model and verify the visible state.
5. Detach with C-b d, then choose `1` in the menu to reconnect to the same session.

After detaching, menu `2` records the declared operator's PASS/FAIL/BLOCKED for one task.
Menu `4` records a separate visual assessment for the current geometry. Inspect
all three geometries; five task runs in each size are not required. Menu `3`
closes only this viewer; use another shortcut for another size. Menu `9` ends
the complete evaluation. No shell command is needed in these journeys. EN/PL
and focus-mode automation remain separate; manual observations are not automatic
latency tests or a claim of eight-hour stability. No terminal/login output is
captured by this controller.
OAuth sync occurs after each recorded task and independently in the outer
`finally`, including failed runs. Leases are always released; journals close only
after confirmed CLI cleanup and successful final sync. Both safe sync/release
verdicts must pass. Failure retains the private recovery journal and prevents
overall qualification even when all interface observations passed.
The application retains its English default; the user may switch to Polish in
Settings. The external instructions and result menu are in Polish.

A real PTY may invoke the ready file's exact `python_launcher -I -B controller
--attach --manifest MANIFEST --size 120x36` directly, without Windows Terminal or
a shell command inside the journey. This is the same attach-only viewer and
private instance. Set the actual PTY geometry before choosing menu `1`; use
C-b d then menu `1` for reconnect, or menu `3` followed by a new sized viewer.
Record separate captured-frame evidence without including login/token output.

The attach-only viewer cannot start/recover a service. It registers for a real
pidfd before attaching. It is outside the controller's cgroup, so the controller
never signals it; shutdown closes the private tmux server and requires actual
viewer exit on all recorded handles. Its menu also watches controller identity
and expiry. All service, pane and native processes are descendants inside the
owned scope. Successful human evidence remains pending until both external
observers prove process exit, empty scope, unchanged source/runtime/power and no
new native failures through the full cleanup grace. Native/session cleanup failure
is a failed run even if the human liked every screen.

## Separate 0.7.2 visual baseline

`run-baseline-ux072.py` is a separate entrypoint in the qualified **backup-python
0.7.2 wheel venv**. It takes the same artifact/private-root/engine/duration inputs
and `--baseline-archive` pointing to the immutable ZIP with SHA256
`6b65e7315895b8c44b84bafc9146dfcdae732c3890f959d0e5e58cbf4fd9aa2c`.
It requires no source-root or OAuth/native-binary arguments. Before data access,
the updater's existing `verify_package` checks the exact qualified base Python,
non-editable installed origin, every package file against the ZIP, and installed
dependency versions against the archival lock. It repeats the package check at
completion and for each attach-only viewer. It never invokes an old interpreter
or the production console.

This mode reuses the lifecycle and three geometry launchers above, with separate
data/ports/sockets. Codex/Claude must be unavailable in its restricted PATH;
OAuth files are not copied, Ollama uses loopback port 1, and cockpit mutations
are rejected except the authentication exchange needed to read the local UI.
Only three visual observations are recorded, with no task results and
`human_acceptance_passed=false` and `agent_acceptance_passed=false` unconditionally. Its status
`baseline_visual_completed_pending_external_watch` means the comparison and
cleanup completed, not that 0.7.2 or 0.8.0 passed acceptance. Old ANSI/SVG captures
remain supplemental, not OS screenshots. The normal 0.8 entrypoint retains its
strict 0.8 installed-origin guard and cannot select baseline through a CLI flag.
