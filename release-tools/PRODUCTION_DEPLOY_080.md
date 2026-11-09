# DevBrain 0.8.0 — production deployment and controlled queue recovery

Prepared procedure, **not executed**. Production stays stopped until the completed
release proofs and the new deployment helper qualification are available. The
commands below are argv contracts, not a runnable batch. Resolve every placeholder
from actual hash-bound evidence; never substitute an older release result.

Current application: `app080-20261108T160614Z`, source freeze
`5ea43988e1943c783e802acbe5a39fdfcc3318d41655ed244716a69a0d71b28f`.
The original qualified checkout is
`/home/piotr/.cache/devbrain-runtime-080/postbios-app080-20261108T160614Z/project/devbrain`
and its literal launcher is the sibling `venv/bin/python`. Production source is
`/home/piotr/src/devbrain`; production data is `/home/piotr/.local/share/devbrain`.

The mechanically built v4 updater is SHA256
`c637fe52e9676aee8d43beeb721ca757a633cde41e8c115f085490ffd694d62d`;
the ZIP is `cd9798924042f6eb2abf5be57ad646654908a1e3b2dd8d1cdba51571381bd5be`.
These bytes have been built, but their complete release qualification is pending.
Use `080-final-candidate-build-ready-context-v4-20261108.json` for their physical
paths and the observed build result. Do not reuse the v3 updater or ZIP.

## Admission before any production access

The private admission JSON has `schema_version: 1`,
`scope: "devbrain_production_retry_admission"`, `source_final: true`,
`version: "0.8.0"`, the exact production `data_dir`, final `source_hashes`, six
runtime executable/SHA fields in `runtime`, `runtime_gate_sha256`,
`release_zip_sha256`, `release_payload_sha256`, and `arcade_library_sha256`.
The payload digest is the updater's `digest(PAYLOAD.encode())`, **not** the ZIP
or updater-file digest.

`proofs` must contain exactly: `full_suite`, `soak`, `ux`, `recall`,
`native`, `copilot`, `interactive`, `queue_copy`, `upgrade_smoke`, `tooling_guards`.
Each entry has its original `artifact_root`, `receipt_sha256` and
`verifier: {path, sha256}` pointing to that trial's own frozen
`attest-full-suite-080.py`. Full-suite and soak entries additionally have
`attestation: {path, sha256}` and the original `qualified_source_root`.
Full-suite attestation is recomputed on the immutable qualified checkout;
installed production bytes are verified independently.

The completed UI receipt `6ab41f349292691882e736aadbc8bfca02510a4d9483d6e522474e9bc4d8f23d`
keeps its original qualified 529 instrumentation and verifier. Never retag its
hashes as the new bundle. The new tooling guard proof must cover the complete
current helper map, including deployment, backup and one-job retry guards.
Current UX 377/377 and UI proof do not replace the pending full backend, account,
recall, copy-replay, final smoke or continuous eight-hour proofs.

All private admission files belong in a dedicated canonical owner-only Linux
directory, mode 0700, with regular 0600 files. Raw config, backups, sources,
credentials and process command lines stay out of public artifacts.

## 1. Deploy under the dedicated supervisor contract

After the new deployment helper bundle and its supervision fixtures pass, create
a fresh Windows/Linux mapped artifact root with `gate.json`, then an arguments
JSON for the **new named** `deploy-production080.py`. Use the literal qualified
application venv as `python_launcher`. The wrapper uses the qualified base Python
for its own supervisor. No alias to smoke or a fixture is permitted.

The controller argv is:

```text
--execute --run-id <unique-deploy-run>
--artifact-root <fresh-/mnt/c-artifacts>
--source-root /home/piotr/.cache/devbrain-runtime-080/postbios-app080-20261108T160614Z/project/devbrain
--install-source /home/piotr/src/devbrain
--data-dir /home/piotr/.local/share/devbrain
--admission <private-deployment-admission.json>
--admission-sha256 <actual-private-admission-SHA>
--updater <exact-final-v4-devbrain-update-0.8.0.py>
--backup-python <actual-final-smoke-fixture>/backup-072-venv/bin/python
```

The admission's `operation` is `production_deployment`; `updater` is an exact
`{path, sha256}` reference. No earlier update receipt or post-update backup is
required at this stage. The backup interpreter must come from the **final**
successful smoke's `fixture_directory`, with an immutable 0.7.2 wheel, full
package/lock pins and the same qualified base. Its actual path is pending final
smoke; do not use the preliminary smoke venv merely because it exists.

Required public artifacts are `gate.json`, `controller-report.json`,
`supervisor-report.json`, `production-deployment-admission.json`,
`production-deployment-results.json`. The outer wrapper arguments are its
existing `-ArtifactRoot`, `-LinuxArtifactRoot`, `-PythonExecutable`, `-Controller`,
`-ControllerArgumentsFile`, `-RunId`, `-TimeoutSeconds`, `-CleanupGraceSeconds 30`
and explicit `-Execute`. Use `LANG=LC_ALL=C.UTF-8`, `TMPDIR=/tmp`; do not inject
`DEVBRAIN_ARCADE_*` overrides or JVM tuning variables.

The controller requires initial production quiescence and owns launch locks. It
reserves a permanent private journal before effects, then starts a **new**
qualified `ProductionEngine` with retained pidfd, scope and listener ownership.
The unchanged updater's 0.7.2 backup and installation use that exact server.
The updater receives explicit `--python`, `--backup-python`, `--runtime-gate`,
`--data-dir` and `--source-dir`; it never receives `--open`.

After installation, complete source/package identity, the exact new 0.7.2 backup
and every exported database/vector/journal value are checked. Preferences,
model-lock and client assignments must remain unchanged. Only `java_executable`
and `tmux_executable` are merged into the existing config; unknown keys, models,
endpoints and local secrets are preserved. The config change uses its existing
lock and atomic private write. No models are downloaded and no account login or
model turn is requested. Installer doctor may perform local readiness/version
probes, as in the final smoke.

The qualified values persisted are:

```text
java_executable=/home/piotr/.cache/devbrain-runtime-080/runtimes/jdk-21.0.13+9/bin/java
tmux_executable=/home/piotr/.cache/devbrain-runtime-080/runtimes/tmux-3.8/bin/tmux
```

Success requires updater actual exit, own JVM graceful pidfd termination and
actual exit, no surviving children, no forced cleanup, a 30-second controller
quiet interval, and the independent Windows observer's post-exit proof. Ordinary
`devbrain engine stop` is intentionally not used: its cmdline check followed by a
numeric PID signal is weaker than this retained-pidfd ownership contract.

Any failure/unknown result leaves the per-payload journal and private logs in
place. A new run ID does not authorize repeating the same deployment. Inspect the
actual journal/evidence before deciding any manual recovery; do not run an
automatic retry or rollback.

## 2. Create a fresh post-update backup

Only after actual deployment receipt PASS and quiescence, use the now-installed
noneditable 0.8.0 literal Python at the updater receipt's
`target_environment/bin/python` as the next controller's `python_launcher`.
Do not guess this path or use the editable test venv for production backup/retry.
The private updater receipt is `~/.local/state/devbrain-updates/installed-<first20
SHA256-of-canonical-source-path>.json`, format 2. Verify its actual path/hash.

Use named `backup-production080.py`, with all eleven release proofs and
`operation: "post_update_backup"`, plus `update_receipt: {path, sha256}`:

```text
--execute --run-id <unique-backup-run>
--artifact-root <fresh-/mnt/c-artifacts>
--source-root <original-qualified-final-source>
--data-dir /home/piotr/.local/share/devbrain
--release-admission <private-backup-admission.json>
--release-admission-sha256 <actual-SHA>
--destination /home/piotr/.local/state/devbrain-updates/post-update-<unique-backup-run>/database.devbrain.json.gz
```

Required reports are `production-backup-admission.json` and
`production-backup-results.json` plus the three standard gate/controller/supervisor
artifacts. The helper starts and stops its own new qualified JVM, uses the
existing store without initialization, exports and rereads the complete backup,
and publishes private `backup-report.json` only after actual engine cleanup.
That private report has scope `devbrain_post_update_backup`, status
`backup_completed`, version/run/data path, update receipt and archive SHA,
`backup_validation_passed`, `engine_cleanup_complete`, source/runtime bindings.
Its hash is bound by the public controller report.

The operator must also verify the actual external backup receipt and its late
watch before admitting a retry. A private `backup_completed` file alone does not
prove supervisor/Windows completion. This step requires no previous post-update
backup, so the deployment → backup → retry chain is not recursive.

## 3. Retry exactly one historical job per observed run

Select the six historical IDs and current attempt numbers privately from the
validated archive/current backup. They are deliberately not invented here.
Use `retry-production-queue-080.py` with one explicit `--job-id`:

```text
--execute --run-id <unique-one-job-run>
--artifact-root <fresh-/mnt/c-artifacts>
--source-root <original-qualified-final-source>
--data-dir /home/piotr/.local/share/devbrain
--admission <private-one-job-admission.json>
--admission-sha256 <actual-SHA>
--job-id <one-exact-historical-UUID> --expected-attempts <actual-current-integer>
```

The admission repeats the eleven proofs, update receipt, exact job ID/attempt,
and `{path, sha256}` references for `post_update_backup`,
`post_update_backup_report` and `historical_backup`. The historical archive is
pinned to SHA `698591d33252a1f0e802a01fac78feb90f56ef37fc0c5f717ed26667f635ac70`.
The post-update backup must be newer than the update receipt, at most one hour
old, and exactly equal to the current database export. Therefore create another
fresh observed backup before the next job after any database-changing attempt.

Required reports are `production-admission.json` and
`production-retry-results.json`, plus the three standard artifacts. A permanent
per-job journal, exact transactional claim and attempt/CAS checks prevent
automatic repeats and avoid claiming unrelated pending jobs. Run the six jobs
as six explicit serial decisions; no shell loop and no general `retry-jobs` or
normal queue worker. On a failed or unknown result, stop and preserve it.

## 4. Installed acceptance and normal resume

After the controlled recovery phase, use the installed literal interpreter:

```text
<installed-python> -I -B -m devbrain --data-dir /home/piotr/.local/share/devbrain version
<installed-python> -I -B -m devbrain --data-dir /home/piotr/.local/share/devbrain doctor
<installed-python> -I -B -m devbrain --data-dir /home/piotr/.local/share/devbrain workspace status
<installed-python> -I -B -m devbrain --data-dir /home/piotr/.local/share/devbrain tui
```

The last command needs a real interactive terminal. Verify the installed version
and actual selected Python/JDK/tmux; preserved model names, profiles and
assignments; memory consistency; keyboard navigation; five status indicators;
remembered assistant per project/worktree; and truthful Start/Resume behavior.
Account usability is supported by the prior real-account proofs, not by merely
seeing a cached process. Do not expose credential payloads in diagnostics.

Normal TUI entry uses deferred background inference. Do not start plain `serve`
or a bulk retry worker before controlled recovery: that could claim the pending
queue. Explicit Start work remains the project-level inference action.

Installed TUI is a **post-deployment acceptance** step, not one of the eleven
pre-deployment proof entries. Its absence therefore does not create a circular
dependency with post-update backup or one-job retry. Conversely the pre-deploy
synthetic UI receipt is not evidence that installed production TUI has passed.
