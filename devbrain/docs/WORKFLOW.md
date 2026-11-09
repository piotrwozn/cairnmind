# Plans, tasks and evidence

DevBrain separates three things that are easy to conflate: how sure we are about a finding, how far a task has progressed, and whether its tests are still valid for the current code.

| Field | Values | Meaning |
| --- | --- | --- |
| `status` | `proposed`, `confirmed`, `disputed`, `superseded` | Confidence in a finding |
| `metadata.state` | `todo`, `in_progress`, `needs_test`, `needs_review`, `done`, `blocked` | Task progress |
| Verification read-out | `fresh`, `stale`, `reported`, `unknown` | Whether test evidence matches the current code |

## A whole plan in one transaction

Example `plan.json`:

```json
{
  "plan": {
    "title": "Login",
    "goal": "Verify login and wrong-password handling",
    "constraints": ["Keep sessions server-side"]
  },
  "steps": [
    {"key": "implement", "content": "Add password validation"},
    {
      "key": "test",
      "content": "Check correct and wrong passwords",
      "depends_on": ["implement"],
      "metadata": {"requires_test": true, "required_profiles": ["unit"]}
    }
  ]
}
```

```bash
devbrain plan save shop ./plan.json --idempotency-key login-plan-1 --activate
devbrain plan list shop
devbrain plan next shop
devbrain plan activate shop PLAN_ID --worktree ~/src/shop-worktree
devbrain task list shop
devbrain task update shop TASK_ID in_progress
```

Saving assigns stable identifiers, validates the complete final dependency graph and commits the header and every step atomically. Repeating a request key with the same content returns the original result; reusing the key for different content is a conflict. Editing an existing plan requires its `expected_version` and the expected versions of the changed steps in `task_versions`, either in the JSON or through `--expected-version` and `--task-versions FILE`. Removed steps stay in history. New steps cannot be declared done or verified just by saving the plan.

The active plan belongs to one project and worktree pair. The next step is chosen deterministically from the plan's steps, taking dependencies and state into account. `needs_test` and `needs_review` remain work to do, and blockers are explicit. Concurrent task updates check both the task version and the plan version.

Older `metadata.plan_id` values are plain group labels. New plans use `metadata.plan_ref` and `step_key`. Adopting an old group is an explicit operation:

```bash
devbrain plan adopt shop login --idempotency-key adopt-login-1 --title "Login" --activate
```

## Named check profiles

Declare profiles in the project repository, in `.devbrain/checks.toml`:

```toml
[checks.unit]
argv = ["uv", "run", "pytest", "tests/unit"]
cwd = "."
timeout_seconds = 600
output_limit_bytes = 10485760
extra_inputs = []
```

`argv` runs without a shell. `cwd` must stay inside the registered worktree. `extra_inputs` can explicitly include ignored input files or patterns relative to that directory. Time and output limits end the process as failed; logs and their snapshots are stored outside the repository.

```bash
devbrain check run shop unit --task TASK_ID
devbrain check list shop --task TASK_ID
```

A check runs in the **current worktree** with the user's permissions. It does not create an isolated, immutable checkout. The terminal UI and MCP show results, but running a check always requires the explicit CLI command with a profile name. There is no MCP tool for arbitrary commands.

An immutable run record binds the profile, the semantic task definition, and code fingerprints taken before and after the run. The fingerprint covers the content of tracked and non-ignored untracked files, file modes, deletions and symlink targets; the 2 MiB indexing limit does not apply. Incomplete or unstable inputs give `unknown` or a result that cannot count as fresh. The before/after comparison is the best available check of the working directory; it cannot detect every transient change reverted during the run, and it does not track external services or the environment.

Before running checks, make sure generated caches and outputs are ignored, for example `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage` and `htmlcov/`. A new non-ignored file created during a test changes the fingerprint and can make the result stale even with exit code 0. Declare genuinely ignored test inputs through `extra_inputs`.

Exit code 0 confirms that the configured command succeeded. It does not prove that every criterion is met, that coverage is sufficient or that a review happened. Completing a task that requires tests needs fresh results for every required profile; review and acceptance have separate evidence requirements.

`acceptance_passed` with `acceptance_source_ids` records an assessment of the task criteria. It is not proof of human acceptance. Changing a step's progress raises the plan version but does not change the semantic task definition; `check_status` decides whether results are fresh.

After a change to the code, the profile or the task content, a result can become `stale`. Earlier `done` progress stays in history and does not reopen tasks automatically. Legacy `test_passed` flags and source claims remain `reported`; migration never turns them into runner results. A model cannot create a machine result through `remember` or `checkpoint`.

## MCP calls

Connected clients get `ask`, `remember`, `checkpoint`, `save_plan`, `next_task`, `resume_context`, `ack_resume` and the read-only `check_status`. Updating a step of a managed plan passes the real versions:

```json
{
  "project": "PROJECT_ID",
  "record": {
    "id": "TASK_ID",
    "expected_version": 2,
    "expected_plan_version": 3,
    "metadata": {"state": "needs_test"}
  },
  "sources": ["EXISTING_SOURCE_ID"]
}
```

Sources must belong to the same project. A conflict requires a fresh read, never an overwrite of newer work. A bound session must have acknowledged the current resume packet first. `save_plan` accepts the same header, steps, idempotency key and versions as the plan-saving API. `checkpoint` records state with the goal, results and next step:

```bash
devbrain checkpoint shop ./resume-point.json
devbrain remember shop "We use server-side sessions" --kind decision --confirmed
```
