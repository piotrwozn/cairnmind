# Prepared qualification contract

The runtime gates are blocked. The new controller, attestor and guard regressions
are prepared code and have not been executed. Do not interpret these files as a
full-suite, deployment or eight-hour qualification.

`run-full-suite-080.py` accepts `--execute --run-id ID --artifact-root /mnt/c/...`
plus `--source-root`, `--gate-report`, `--java-home`, `--tmux-bin`, and
`--arcade-dist`. It must be a child of the external supervised wrapper with
`DEVBRAIN_QUALIFICATION_RUN_ID=ID`. Use the exact qualified editable Python
installation. Inherited database overrides and pytest selection options are
removed; the repository fixture owns its isolated engine and database namespace.
The controller does not use the private production database or live model calls.

Before test collection it records all files in `src`, `tests`, and optional
repository `data`, plus `pyproject.toml` and `uv.lock`. Package data is included;
Python bytecode caches are excluded. Symlinks are rejected. The manifest binds
the qualified Python/Java/tmux binary paths/hashes and gate-file SHA. A child
started with `-I -B` verifies the actual installed package origin, entrypoint hash
and Python prefix. `PYTHONPATH` cannot substitute the panel or test package.

The controller performs a complete `tests` collection and a separate complete
execution, without `-k`, selectors or deselections. The execution collection must
exactly match the first collection. Every JUnit testcase carries an exact `nodeid`
property, including classes and parametrization. Collection errors, duplicate or
missing nodeids, failure/error elements, inconsistent JUnit counters and nonzero
process exits prevent qualification. All skips/xfails are unexpected by default.
An optional `--expected-skips` JSON object maps exact nodeids to exact JUnit skip
messages; this plan is frozen before collection. Approved skips remain reported
as skips, never passes.

Required full-suite artifacts in one Windows-backed evidence directory:

- `gate.json`, `frozen-source.json`, `collection.json`, `pytest.xml`
- `controller-report.json`, `supervisor-report.json`, `receipt.json`

Controller report v1 has scope `devbrain_final_full_suite_run`, matching `run_id`,
`runtime` (the six executable/SHA fields), `runtime_gate_sha256`,
`frozen_manifest_sha256`, `collection_sha256`, `junit_sha256`, `source_hashes`,
`source_root`, `isolated_package`, zero `collection_returncode` and
`pytest_returncode`, `source_unchanged=true` and `cleanup_complete=true`. Its final
status is `full_suite_completed_pending_attestation`; `controller_exit_verified`
and `final_full_suite_pass` remain false in this self-written report. The Linux
supervisor must independently establish that all owned descendants have stopped.
The whole Linux supervisor/controller tree runs in a new UUID systemd scope.
Membership survives deliberate environment clearing by native login children;
`DEVBRAIN_QUALIFICATION_RUN_ID` alone is not cleanup evidence. The supervisor reads
all cgroup descendants and signals only pidfds after birth, UID, executable and
cgroup checks. A forced cleanup invalidates the run. The full-suite controller's
local check excludes only its positively identified parent and that parent's
exact bounded `dmesg --raw` probe; the Windows final scope check excludes nothing.
The controller also records its literal `python_launcher` (`sys.executable`);
this must match both external reports while resolving to the qualified base SHA.
Set the optional `python_launcher` key in the wrapper arguments JSON to the
qualified venv's literal `bin/python` path, so `-I` retains the editable installation.

The Linux supervisor report must have `schema_version=1`, scope
`devbrain_linux_process_supervision`, matching `run_id`, `passed=true`, `child_returncode=0`,
`controller_exit_verified=true`, `cleanup_complete=true`, `forced_cleanup=false`,
`native_failure=false`, empty `errors`/`new_wsl_dumps`/`new_kernel_fatals`, matching
boot IDs, the same six `runtime` fields, `controller_report` equal to
`controller-report.json`, and its exact `controller_report_sha256`.
It also contains identical `helper_sha256_start`/`helper_sha256_end` maps keyed by
basename, covering the controller, `attest-full-suite-080.py`,
`validate-stability-080.py`, `supervise-080.py` and the shared
`qualification-power-080.ps1`. The Windows receipt has the same
maps plus `run-supervised-080.ps1`; the attestor checks current bytes against them.

The Windows receipt must have `schema_version=1`, scope
`devbrain_external_process_watch`, matching `run_id`, `passed=true`,
`child_returncode=0`, `supervisor_returncode=0`, `controller_exit_verified=true`,
`supervisor_exit_verified=true`,
`cleanup_complete=true`, `forced_cleanup=false`, `native_failure=false`,
`cleanup_grace_seconds>=30`, identical nonempty `initial_boot_id`/`final_boot_id`,
and empty `new_wsl_dumps`, `new_kernel_fatals`, `errors`. Its `artifact_sha256` map
uses relative POSIX paths and covers every required artifact except the receipt
itself. The Windows parent creates it only after observing actual Linux supervisor
exit and the post-exit grace; `passed` is not accepted without all these bindings.

Both external reports bind `scope_unit`, `control_group` and
`scope_cgroup_identity` (device/inode). The Linux report requires
`scope_membership_verified`, `scope_descendants_stopped` and no remaining scope
processes. The Windows receipt additionally requires `scope_initially_absent`,
`scope_empty_verified` and `scope_cleanup_grace_seconds>=30`. Missing or unreadable
cgroup evidence is a failure; a vanished cgroup is accepted only with confirmed
unit disappearance. An observed replacement group cannot authorize cleanup.
The requested systemd memory/swap/runtime limits are reported as `*_requested`,
not presented as kernel readback. The independent empty-scope observation and
power samples must continue at least 30 seconds after both supervisor exit and
verified cleanup.

The entire helper bundle and artifact root must be outside OneDrive/reparse-point
ancestors; moving only output is insufficient. The existing path rejection is
intentional. A new post-BIOS gate must contain `power_profile` from the shared
read-only power observer. Historical blocked gates remain unchanged.

The Windows wrapper requires Balanced (`381b4222-f694-41f0-9685-ff5bb260df2e`)
with the same canonical AC/DC fingerprint as that gate. It samples every 30 seconds,
including cleanup and at least 30 seconds after supervisor exit. `power-samples.jsonl`
is hashed into the receipt. Missing samples, changed settings, gaps over 45 seconds,
or an incomplete final observation reject the result. The canonical representation
excludes localized names and uses sorted subgroup/setting GUIDs plus AC/DC values.
The wrapper never changes a plan. `SetThreadExecutionState` temporarily requests
only `ES_SYSTEM_REQUIRED | ES_CONTINUOUS`; the previous state is restored in a
nested `finally`. Acquisition/restoration are mandatory receipt evidence.

The latest PowerShell and Python guard additions are prepared for a qualified
runtime; static syntax checks do not establish guard or wrapper behavior. After
the runtime gate passes, run the pure guards and controlled normal/nonzero/timeout/
parent-loss fixtures before the full suite and eight-hour workload. No intentional
native crash is needed for those fixtures.

`test_scope_080_guards.py` contains pure synthetic process-identity/cgroup cases.
`test_supervision080_fixture.py` is an opt-in controller for actual external-wrapper
checks after a passed gate, using fresh artifact roots and `--execute --run-id ID
--artifact-root /mnt/c/... --mode MODE` in the normal arguments JSON. It starts no
database, JVM, tmux or model. The required artifacts are the three core reports;
its report scope is a test fixture and can never qualify the app full suite/soak.

| Mode | Expected external result |
| --- | --- |
| `normal` | Exit zero, clean scope, full grace, receipt PASS for the fixture only |
| `nonzero` | Child exit 7; receipt FAIL with clean, observed final scope |
| `timeout` | Use a short wrapper timeout (e.g. 2 seconds); forced cleanup and receipt FAIL |
| `orphan` | A detached child empties its environment; cgroup still finds/stops it and receipt FAIL despite the deliberate local cleanup claim |
| `parent-loss` | With a 60-second timeout, terminate only the owned Windows observer handle after child readiness; Linux heartbeat expires, all scoped children stop, no successful receipt exists |

For parent loss, an independent test harness must verify the recorded scope is
empty and watch global native evidence for 30 seconds after cleanup. Child and
controller sleeps have an additional 180-second ceiling. These scenarios have
been authored but not run; a missing receipt or native artifact always prevents
a test qualification, even when the expected controller failure occurred.

After the Windows wrapper has finished, qualify the evidence with the read-only
attestor (the command is illustrative, not executed):

```text
python attest-full-suite-080.py --artifact-root /mnt/c/.../run \
  --source-root /path/to/frozen-checkout --output /mnt/c/.../run/final-full-suite.json
```

The attestor rehashes current source and actual runtime binaries, parses raw JUnit,
compares the exact collection, and validates both external reports. It writes a
new output only; existing evidence is never overwritten. A rejected report has
`final_full_suite_pass=false` and exits nonzero. The caller must also check the
attestor exit, not merely the existence of an output file.

For active soak the required artifacts are `gate.json`, `final-full-suite.json`,
`controller-report.json`, `supervisor-report.json`, `receipt.json`. The controller
ends as `workload_passed_pending_external_watch`, `workload_criteria_passed=true`,
`eight_hour_qualification=false`. Only `attest-full-suite-080.py --mode soak`
with a valid external receipt can grant the eight-hour qualification. It also
revalidates the full-suite artifacts referenced by `final-full-suite.json`.
Synthetic broker recovery remains distinct from real native-client reconnect or
account authorization; those are outside this soak workload.

Every soak cycle now also performs C-b d on the real PTY client, requires its
actual exit zero, creates a fresh attachment, and compares the original tmux
server/pane PID and birth identities, workspace selection, exact single ACK,
and source/record/ACK/job operation counts. Fresh database queries read back
the known synthetic source content/hash and record content/version/provenance
before and after reconnect. Both `terminal_reconnect_cycles` and
`committed_readback_cycles` must equal completed cycles in the controller and
external attestor. This does not add model calls or change 900-second cadence
or the 28800-second qualification duration. These additions are prepared with
pure guards; they have not been executed.

The external launcher also admits the explicit account controllers
`validate-native-account080.py` and `validate-copilot-account080.py`. Their required
artifacts include `native-results.json` or `copilot-results.json`, respectively.
Both reports bind the shared native lifecycle helper; Copilot additionally binds
the actual `validate-copilot-account-e2e.py` child helper at start and after cleanup.
`replay-failed-queue-080.py` is a separate historical-copy controller and binds
`run-full-suite-080.py`, which it imports for isolation/cleanup. These result
statuses cannot stand in for a full-suite or eight-hour qualification.

Windows JSON publication retries only transient Win32 5/32/33 I/O access errors
for at most 1000 milliseconds using the same closed temporary file and payload.
Readers permit delete sharing, but replacing an open target may still be denied.
A retry never renews the heartbeat timestamp or the independent 15-second lease;
failed publication preserves the previous complete document. The Windows-only
`test_atomic_json080.ps1` fixture verifies brief and permanent collisions for both
launchers, actual reader handles, complete snapshots, and temporary-file cleanup.
Its 18/18 result is recorded in `evidence/080-atomic-io-final-20261107.json`; it
qualifies this I/O behavior only, not Linux runtime, controller execution or soak.
