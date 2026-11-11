# Real-world Cairnmind 0.3 pilot

This kit prepares a small project for a Codex → Claude → fresh Codex handoff.
It contains no ready-made implementation and no pre-passed model results. To run
it you need your own WSL2, a GPU, Ollama and both clients signed in. Install
Cairnmind and its models as described in the [Cairnmind README](../README.md).

## Diagnostics and evaluation

In WSL, check `nvidia-smi`, `codex --version` and `claude --version`, then save
the diagnostics and measure the local model:

```bash
export DEVBRAIN_PILOT_REPORT_DIR="$HOME/devbrain-pilot-reports/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DEVBRAIN_PILOT_REPORT_DIR"
cairnmind doctor > "$DEVBRAIN_PILOT_REPORT_DIR/doctor-before.json"
cairnmind models pull
cairnmind doctor > "$DEVBRAIN_PILOT_REPORT_DIR/doctor.json"
cairnmind eval run --report-dir "$DEVBRAIN_PILOT_REPORT_DIR" > "$DEVBRAIN_PILOT_REPORT_DIR/recall-local.json"
```

`models pull` downloads the models. The evaluation uses its own temporary
repository and database. The evaluation exit code is `0` = passed, `1` = failed,
`3` = `not_run`. A pass requires 20 executed scenarios, >=90% accuracy, no
critical errors and passing stages for both clients. `not_run` means the
measurement was not performed; it is not a success.

Only after the local evaluation has passed, check both clients and their saved
sign-ins. The block below runs `--clients` only if those checks pass; it uses
the existing account limits. It does not sign in and does not print credentials.

```bash
if python3 - "$DEVBRAIN_PILOT_REPORT_DIR" <<'PY'
import json, pathlib, subprocess, sys
folder = pathlib.Path(sys.argv[1])
recall = json.loads((folder / 'recall-local.json').read_text())
doctor = json.loads((folder / 'doctor.json').read_text())
assert recall['status'] == 'passed' and recall['attempted'] == 20, 'Pass the local evaluation first.'
assert all(doctor['clients'][name]['verified_resume'] for name in ('codex', 'claude')), 'Check the CLI capabilities in doctor.json.'
codex = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True, timeout=20)
assert codex.returncode == 0 and 'chatgpt' in (codex.stdout + codex.stderr).lower(), 'Codex is not signed in with ChatGPT.'
claude = subprocess.run(['claude', 'auth', 'status', '--json'], capture_output=True, text=True, timeout=20)
auth = json.loads(claude.stdout) if claude.returncode == 0 else {}
assert auth.get('loggedIn') is True and auth.get('authMethod') in ('claude.ai', 'oauth'), 'Claude is not signed in.'
PY
then
  cairnmind eval run --clients --report-dir "$DEVBRAIN_PILOT_REPORT_DIR" > "$DEVBRAIN_PILOT_REPORT_DIR/recall-clients.json"
else
  printf 'The client stage was not run. Resolve the reported cause and repeat the check.\n' >&2
fi
```

## Create a single fresh repository

Run this from the directory of the unpacked Cairnmind package. The block refuses
to use an existing `~/src/devbrain-pilot`; it neither deletes nor
re-initializes it. On a later attempt, continue the existing pilot or choose a
new directory and project name and change it consistently in the prompts. The
project is named `devbrain-pilot` in memory.

```bash
(
  set -eu
  kit_dir="$(pwd)/pilot"
  pilot_root="$HOME/src/devbrain-pilot"
  if [ -e "$pilot_root" ]; then
    printf 'Directory already exists: %s. Continue the previous pilot.\n' "$pilot_root" >&2
    exit 1
  fi
  test -f "$kit_dir/plan.json"
  mkdir -p "$HOME/src"
  mkdir "$pilot_root"
  mkdir "$pilot_root/.cairnmind"
  cp "$kit_dir/templates/.gitignore" "$pilot_root/.gitignore"
  cp "$kit_dir/templates/README.md" "$pilot_root/README.md"
  cp "$kit_dir/templates/checks.toml" "$pilot_root/.cairnmind/checks.toml"
  cp "$kit_dir/plan.json" "$pilot_root/pilot-plan.json"
  git -C "$pilot_root" init
  git -C "$pilot_root" add .
  git -C "$pilot_root" -c user.name='Cairnmind Pilot' -c user.email='pilot@localhost' commit -m 'Prepare pilot fixture'
  cairnmind project add devbrain-pilot "$pilot_root"
  cairnmind install devbrain-pilot
)
```

If the project name is already taken, do not overwrite its registration. Check
`cairnmind project list` and continue in the correct repository. Save file
copies, measurements and reports in `DEVBRAIN_PILOT_REPORT_DIR` so they do not
change the code fingerprint.

## Three real sessions

Start the clients one after another from the same WSL terminal so they inherit
the report directory. Do not use `resume` and do not carry conversation history
between clients. Paste only the matching short prompt from the package's
`pilot/prompts` directory:

1. `cairnmind run devbrain-pilot codex` — [01-codex.txt](prompts/01-codex.txt).
   The model saves the plan through MCP, writes the code and tests, saves a
   checkpoint and leaves the task in `needs_test`. Exit the native client.
2. `cairnmind run devbrain-pilot claude` — [02-claude.txt](prompts/02-claude.txt).
   The new session restores the state, runs the `unit` profile and reads the
   real stdout/stderr logs. It performs a review and updates the task through
   MCP. Exit the client.
3. `cairnmind run devbrain-pilot codex` — [03-fresh-codex.txt](prompts/03-fresh-codex.txt).
   The third session restores the state, sources, tests and remaining work.

On `partial`/`unavailable` the launcher stops the start: check
`cairnmind status --project devbrain-pilot`, the archiving reason and
`cairnmind doctor`, then retry once the cause is resolved. Do not bypass the ACK
or the profile requirements. Reproducing identifiers is a read of the state; an
ACK does not prove that the model understood it.

## Compaction, restart and stale results

- In the third session, trigger the client's native conversation compaction.
  Ask it to restore the goal, versions and sources again. The model must re-read
  the packet and perform an ACK.
- Exit the client, run `cairnmind stop`, then `cairnmind context devbrain-pilot`.
  This restarts the service. Start one more fresh Codex session and use the
  read-only prompt; the state should be preserved.
- Only after reaching `done` and `fresh`, change the function code in the
  pilot's `token_auth.py` so that it rejects a valid token. Do not change the
  task or its evidence. `cairnmind task list devbrain-pilot` and
  `cairnmind check list devbrain-pilot --task REAL_TASK_ID`
  should show the historical `done` but `stale` for its verification. Read the
  state in a fresh session too; the model must point out the mismatch. Do not
  remove this result from the report.
- If you restore the correct code, run the `unit` profile again and note the new
  run ID. The report must distinguish the individual observations instead of
  declaring the whole attempt passed based only on the last result.

## Measurements and pass criteria

Record `cairnmind doctor`, the client versions and the model list, both
evaluation reports, the plan/task/run IDs, the review and sources, and the
observations before and after compaction, restart and the code change. Reports
may contain conversation content — share only the diagnostic data that is
needed, without credential configuration.

For three questions, use the existing `cairnmind benchmark devbrain-pilot "QUESTION"`:

- "What is the goal of the pilot and what are its constraints?"
- "What is the state of the token validation task and what evidence does it have?"
- "Where is the token validation located and which cases do the tests cover?"

Save the results in the report directory. The benchmark compares the first
request with a cached repeat; it does not measure a cold model start. While
working, watch the GPU with
`nvidia-smi --query-gpu=timestamp,memory.used,memory.total --format=csv -l 1`
in a second terminal and record the observed VRAM usage outside the repository.

The pilot passes only when the real models pass the evaluation, the three
sessions restore consistent states backed by sources, real tests and the review
are recorded, and the compaction, restart and `stale` checks give the expected
results. Record gaps, timeouts and wrong answers as such. Real latency and VRAM
are reported as measurements; technical tests or a ready-made set of files do
not replace them.
