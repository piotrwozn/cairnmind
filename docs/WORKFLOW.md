# Plans, tasks and evidence

Cairnmind separates three things that are easy to conflate: how sure we are about a finding, how far a task has progressed, and whether its tests are still valid for the current code.

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
cairnmind plan save shop ./plan.json --idempotency-key login-plan-1 --activate
cairnmind plan list shop
cairnmind plan next shop
cairnmind plan activate shop PLAN_ID --worktree ~/src/shop-worktree
cairnmind task list shop
cairnmind task update shop TASK_ID in_progress
```

Saving assigns stable identifiers, validates the complete final dependency graph and commits the header and every step atomically. Repeating a request key with the same content returns the original result; reusing the key for different content is a conflict. Editing an existing plan requires its `expected_version` and the expected versions of the changed steps in `task_versions`, either in the JSON or through `--expected-version` and `--task-versions FILE`. Removed steps stay in history. New steps cannot be declared done or verified just by saving the plan.

The active plan belongs to one project and worktree pair. The next step is chosen deterministically from the plan's steps, taking dependencies and state into account. `needs_test` and `needs_review` remain work to do, and blockers are explicit. Concurrent task updates check both the task version and the plan version.

Older `metadata.plan_id` values are plain group labels. New plans use `metadata.plan_ref` and `step_key`. Adopting an old group is an explicit operation:

```bash
cairnmind plan adopt shop login --idempotency-key adopt-login-1 --title "Login" --activate
```

## Named check profiles

Declare profiles in the project repository, in `.cairnmind/checks.toml`:

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
cairnmind check run shop unit --task TASK_ID
cairnmind check list shop --task TASK_ID
```

A check runs in the **current worktree** with the user's permissions. It does not create an isolated, immutable checkout. The terminal UI and MCP show results; a check runs only through `cairnmind check run` with a profile name. **Run next stage** in the workspace runs that same command for the profile mapped to the `test` stage (see [Plan stages](#plan-stages)). There is no MCP tool for arbitrary commands.

An immutable run record binds the profile, the semantic task definition, and code fingerprints taken before and after the run. The fingerprint covers the content of tracked and non-ignored untracked files, file modes, deletions and symlink targets; the 2 MiB indexing limit does not apply. Incomplete or unstable inputs give `unknown` or a result that cannot count as fresh. The before/after comparison is the best available check of the working directory; it cannot detect every transient change reverted during the run, and it does not track external services or the environment.

Before running checks, make sure generated caches and outputs are ignored, for example `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage` and `htmlcov/`. A new non-ignored file created during a test changes the fingerprint and can make the result stale even with exit code 0. Declare genuinely ignored test inputs through `extra_inputs`.

Exit code 0 confirms that the configured command succeeded. It does not prove that every criterion is met, that coverage is sufficient or that a review happened. Completing a task that requires tests needs fresh results for every required profile; review and acceptance have separate evidence requirements.

`acceptance_passed` with `acceptance_source_ids` records an assessment of the task criteria. It is not proof of human acceptance. Changing a step's progress raises the plan version but does not change the semantic task definition; `check_status` decides whether results are fresh.

After a change to the code, the profile or the task content, a result can become `stale`. Earlier `done` progress stays in history and does not reopen tasks automatically. Legacy `test_passed` flags and source claims remain `reported`; migration never turns them into runner results. A model cannot create a machine result through `remember` or `checkpoint`.

## Plan stages

The state of the next step names the work it waits for: `todo` and `in_progress` are the `implement` stage, `needs_review` is `review` and `needs_test` is `test`. Per project, you can map `implement` and `review` to one of the project's assistants and `test` to a check profile, in the workspace under **More → Plan stages**. Any assistant the workspace can start can be chosen: Codex, Claude Code, the built-in API agent (OpenAI, Anthropic, an OpenAI-compatible server, Ollama) or GitHub Copilot. Nothing is mapped by default, so no stage starts a paid provider unless you chose that profile for that stage. Saving a stage stores the assistant slot together with the profile and client it named at that moment and, for the built-in API agent or Copilot, the provider, model and address the profile pointed at (never its credential reference or key). If the slot later names another profile (after **Replace connection**, or a change of the default assistants for a project without its own), or that agent profile's provider, model or address is edited in place, the stage starts nothing and says that its assistant changed since it was mapped and what changed, in words (the profile, the provider, the model, the address); **Plan stages** marks it "not confirmed", and saving the stage again accepts the new one. Codex and Claude Code profiles are compared by profile and client only. A map saved before this version stored only the slot: it still follows a Codex or Claude Code slot as before, but a stage whose slot now holds the built-in agent or Copilot has to be saved once before it starts. The map is an owner preference of this machine, kept with the other cockpit preferences (`project_stages` in `cockpit/preferences.json` of the data directory), not in the repository; the check profile itself is still read from the worktree's `.cairnmind/checks.toml` when it runs.

**Run next stage** reads the next step of the active plan for the open folder and does what is mapped to its stage:

- for `implement` or `review`, it starts that one assistant exactly as **Start work** does. The assistant gets a fresh resume packet and must acknowledge it before it writes. If that assistant is already running (or starting) in this folder, for example because `implement` and `review` name the same one, nothing is started or restarted and it receives no new packet; the message says so, and the next stage needs that session ended or another assistant mapped. If it cannot start (not installed, signed out), the reason is shown;
- for an `implement` or `review` stage mapped to the built-in API agent or Copilot, the same: **Run next stage** starts it as **Start work** does, in a panel of the workspace, with the profile's own key and limits (model calls, output tokens and command time; for Copilot, tool calls and turn time), and its journal is kept as for a run started by hand. It sends nothing to the model until you write to it in its panel; the agent then has to read its resume packet and acknowledge it before it can change a file or save anything, exactly as when you start it by hand. Before starting, the stage checks what can be seen without asking the provider: a profile whose key is missing, cannot be read or is set to text that is not a valid key (the same check the launcher makes; a Copilot profile that names a credential which is not available is told to provide it or to clear the reference, so that the signed-in GitHub account is used), and a Copilot runtime that is not prepared, are shown as such and nothing is started. Whether the key is valid, or the Copilot account signed in, shows only when the agent sends its first request;
- for `test`, it runs `cairnmind check run PROJECT PROFILE --task TASK --worktree DIR` and shows the result. The run is recorded like any other check run, under the same rules for fresh and stale evidence, and with the same session rule: inside a coding session the command refuses until that session has acknowledged its packet.

When nothing is mapped to the stage, when the mapped assistant is no longer configured for the project, or when it cannot be launched for a step, this is shown and nothing else is started in its place: a native client that fails never makes the stage start an API profile, and an API profile without a key never makes it start another assistant. Opening another project while a stage is being prepared cancels it, and a check result that arrives after that is recorded for the earlier project: it is listed in the workspace's **Notices** under that project, never on the notice line of another one. The router never changes a task, a plan or an acknowledgement: after a stage, the assistant or you move the step on with `task update` or through MCP (the built-in agent with its `remember` tool, after its ACK), and completing a step that requires tests still needs fresh check evidence. Because recording a step's new state raises the plan version, the session that reviews a step acknowledges the same plan at a later version than the session that implemented it.

```bash
cairnmind stage next shop
cairnmind stage next shop --worktree ~/src/shop-worktree --json
```

`cairnmind stage next` shows the same decision and starts nothing. For the `test` stage it prints the `cairnmind check run …` command. For an assistant it prints `cairnmind run …` only when that command does exactly what the map says: the assistant's profile sets no model, no other assistant of the project uses the same client, and the step belongs to the folder's active plan (`cairnmind run` knows no slot, profile or model, and a session reads the active plan). Otherwise it names the assistant and says to start the stage from the workspace with **Run next stage**; in `--json`, `command` is then `null` and `command_reason` is `profile_model`, `shared_client` or `inactive_plan`. The built-in API agent and Copilot have no such command (`cairnmind run` starts only Codex and Claude Code): `stage next` names the profile with its provider and model and says to start the stage from the workspace, with `command_reason` `agent_client`, or it says that the key is missing or the Copilot runtime is not prepared (`problem` is `credential_missing`, `credential_invalid`, `credential_unavailable`, `copilot_credential_unavailable` or `runtime_missing`; a stage whose slot names another profile than the one saved, or whose agent profile's provider, model or address changed, is `assistant_changed` with the fields in `changed`, and an agent stage saved before this version is `assistant_unconfirmed`). It reads the key's state from the running cockpit, which also holds keys saved only for its session, or else from the local key store; it never contacts the provider. Each request to the cockpit waits at most 5 seconds. When the cockpit answers with an error, the key counts as not usable (`credential_unavailable`), as in the workspace; when it does not answer at all, `credential` is `unknown` and the output says that the key and runtime could not be checked there. `GET /api/stage` of the cockpit gives the same decision, with `provider`, `model`, `credential` and `runtime`.

## MCP calls

Connected clients get `ask`, `remember`, `checkpoint`, `save_plan`, `next_task`, `resume_context`, `ack_resume` and the read-only `check_status`. A client started with `cairnmind mcp --client external` (Cursor, Cline, Zed) gets the same tools in an evidence-degraded session: what it saves is marked as written by an external session and never counts as verified (see the README). Updating a step of a managed plan passes the real versions:

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
cairnmind checkpoint shop ./resume-point.json
cairnmind remember shop "We use server-side sessions" --kind decision --confirmed
```
