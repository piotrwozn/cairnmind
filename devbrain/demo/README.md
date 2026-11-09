# A decision survives the hand-off. Test evidence does not survive a code change.

This demo runs a real Codex process and a new Claude Code process against the
same disposable checkout. Codex reads the sample requirements and saves its
decision and task. Claude recovers the decision, the task ID and a literal source
quote from DevBrain. A real unit check becomes `fresh`; a one-line regression
makes that earlier passing result `stale`.

The 38-second demonstration is available as a video, GIF and an asciinema v2
terminal cast. The script validates both native clients, their required tool
calls and the returned decision, task and source before saving its results.

## Reproduce from a clean checkout

Use Linux or WSL2, Python 3.12+, uv, Git and Java 21+. Install Codex CLI and Claude
Code and sign in to both **in that same Linux/WSL environment**. The run uses your
existing subscriptions and quotas. No Ollama model is needed for this focused
source/record demo; it does not exercise semantic retrieval or extraction.

From the repository's `devbrain/` package directory:

```bash
uv sync --locked --extra dev
uv run devbrain engine install
uv run devbrain init
codex login status
claude auth status --json

# Use a new output directory outside the source checkout for each take.
PYTHONPATH=src uv run python demo/run_demo.py live --output /tmp/devbrain-demo-take1
```

The runner starts the installed engine if necessary and creates a fresh
`devbrain_demo_*` database. It never changes existing projects, replaces CLI
configuration, or copies account credentials into the sample. If you keep
DevBrain settings outside the default location, add `--data-dir /path/to/settings`.
It refuses a nonempty output directory, so interrupted takes remain inspectable.

The sample in [`sample/`](sample/) uses only Python's standard library. To inspect
it without a DevBrain installation:

```bash
cd demo/sample
python -m unittest -v test_checkout
```

Play the terminal demo with
`asciinema play /tmp/devbrain-demo-take1/demo.cast`.

## What the demo verifies

- `Store`, `SessionManager`, `PlanManager`, `run_check` and `verification_status`
  are the shipped DevBrain implementations, using the real pinned ArcadeDB
  engine. Neither persistence nor test status is mocked.
- A small **demo-specific MCP gateway** restricts tools to the isolated project.
  It checks the exact resume packet and task/plan versions through the normal
  ACK implementation. Codex's source-backed decision and plan write occur in
  the same writer transaction as the ACK validation. Claude's phase is read-only.
- This gateway provides direct literal source/record reads, not the production
  `ask`/GraphRAG path. The native processes are ephemeral; this demo proves
  explicit memory hand-off, not production transcript-hook archiving.
- The requirement document is checked in with the sample. The decision wording
  and task title come from the real Codex reply; Claude receives neither in its
  prompt. Its returned decision, task and source are checked against the database,
  and its quote must occur verbatim in the archived source.
- Full tool results, provider replies, elapsed times and verification evidence
  are available locally in `evidence/` after running the script.

## Files and privacy

The output contains `capture.json` and `demo.cast` for publication, plus a private
`memory/` directory, `binding.json`, `evidence/` and the intentionally modified
`checkout/`. **Do not publish the whole output directory**: engine configuration
and raw client diagnostics are local operational data. Only reviewed presentation
artifacts belong in `demo/assets/`. The runner preserves the isolated database
and private evidence for inspection; it does not drop any database automatically.

## Verification

```bash
uv run ruff check demo tests/test_demo_recording.py
uv run pytest tests/test_demo_recording.py
```

These tests use real ArcadeDB and Git, verify the ACK/source boundaries and check
that the showcased mutation really breaks the unit test. They never start a paid
provider session. A successful `live` command separately validates both actual
native clients before writing publishable output.
