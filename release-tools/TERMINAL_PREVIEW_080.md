# Synthetic terminal preview

Run in an isolated editable installation of the source under review, on Linux/WSL.
The Python interpreter of that installation also launches every real panel process.
Do not use a production data directory. The runner accepts only a new empty directory.

```sh
python -m devbrain.terminal_preview \
  --data-dir /home/piotr/.cache/devbrain-preview/example-new-run \
  --scenario normal --size 180x55 --duration 120 \
  --screenshots /path/to/evidence
```

Scenarios: `normal`, `empty`, `offline`, `slow`, `unicode-many`.
`--size` accepts `COLSxROWS`; `--duration` is a positive, bounded lifetime in seconds.
`--tmux-bin` optionally selects an explicit tmux >=3.4 binary.
`--attach` displays the session in the invoking terminal. Without it, the runner
holds a passive real PTY attachment so rendering continues; a user can attach with:

```sh
python -m devbrain.terminal_preview \
  --data-dir /home/piotr/.cache/devbrain-preview/example-new-run --attach-only
```

The unmodified sidebar, inspector, and graph render in their own processes. Client
panes display the application's real idle placeholder. The authenticated HTTP/SSE
broker is synthetic. It contains no Store, database, model, native runtime, or private
project data. All operational writes, including client start, login, model probe,
import, snapshots and configuration changes, receive HTTP 403. Authentication tickets
and sessions are the only accepted POST endpoints. Navigation state lives in the
preview directory. No production service is started, stopped or adopted.

The runner records `preview-status.json`, source hashes, request metadata (never
request bodies or credentials), and optional ANSI/SVG pane captures. Captures contain
synthetic data. Closing the runner stops its owned tmux server and HTTP thread.
An unexpected dead panel marks the run failed; there is no silent recovery loop.

## Validation

Run the standalone tests with `--noconftest` because the repository-wide conftest
automatically starts ArcadeDB even for unrelated tests:

```sh
python -m pytest --noconftest tests/test_terminal_preview.py -vv --junitxml=preview.xml
```

The tests cover operational write denial, authentication, source identity, real
Textual renders at 80x24/120x36/180x55, a real five-pane tmux session, keyboard pane
selection, resize, explicit panel recovery, and input while the synthetic backend
is slow or unavailable. Headless SVGs complement actual terminal inspection; they
are not proof of font appearance in Windows Terminal.

## Optional passive observation

Prepare the target preview with an appropriately long duration, then explicitly
start the monitor after the runtime gate has passed:

```sh
python release-tools/validate-stability-080.py --pid PREVIEW_PID \
  --preview-dir /home/piotr/.cache/devbrain-preview/example-new-run \
  --wsl-crash-dir /mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes \
  --duration 28800 --interval 30 --output /path/to/evidence/new-soak.jsonl
```

The monitor does not start Java, a database, a model, or GPU work. It records boot
identity, process birth identity, resident memory, CPU ticks, pressure, and pane
liveness. It requires readable `dmesg --raw` and WSL crash-file metadata, compares
against its initial inventory, and latches new dumps or fatal kernel messages even
when the affected child has already exited. Missing observation access fails closed.
At exit it continues observing for at least 30 seconds (`--cleanup-grace`), reports
whether its target has stopped, and never labels passive time an active soak pass.
If the target is still running, its future cleanup remains outside this observation.
The historical preview `complete` field records control flow only; it does not
prove crash-free cleanup. Earlier successful images remain visual evidence.

## Active soak, prepared but not executed

`soak-workload-080.py` is the active controller, distinct from the passive monitor.
It requires a runtime gate with `status: "pass"` and
`scope: "bounded_runtime_qualification"`, with all runtime checks explicitly passing
(including native children). Both gate and final-suite reports must match the
resolved Python/Java/tmux executable paths and their SHA-256 binary hashes. The final
suite also needs `final_full_suite_pass: true`, `runtime_gate_sha256` equal to the
exact gate file hash, and `source_hashes` equal to `source_manifest(source_root)`.
That manifest includes `pyproject.toml`, `uv.lock`, all files under `src`, `tests` and optional repository `data`
except compiled Python caches, including package data; external symlinks are rejected.
The current imported package must be the qualified editable checkout.
A blocked runtime gate refuses execution before any
directory, database, or JVM is created. Explicit `--execute --enable-local-models`
is also required. The current preparation does not constitute an eight-hour pass.

```sh
python release-tools/soak-workload-080.py \
  --run-id EXPLICIT_RUN_ID --artifact-root /mnt/c/path/to/new-evidence \
  --data-dir /home/piotr/.cache/devbrain-soak/new-owned-run \
  --gate-report evidence/runtime-080-gate.json \
  --suite-report evidence/final-full-suite.json \
  --java-home /path/to/qualified/jdk \
  --tmux-bin /path/to/qualified/tmux \
  --arcade-dist /path/to/pinned/arcadedb \
  --source-root /path/to/qualified/devbrain-checkout \
  --wsl-crash-dir /mnt/c/Users/piotr/AppData/Local/Temp/wsl-crashes \
  --duration 28800 --cycle-seconds 900 \
  --execute --enable-local-models
```

These are controller arguments for the external supervised wrapper, not a direct
standalone launch: `DEVBRAIN_QUALIFICATION_RUN_ID` must be supplied by that wrapper.
See [FULL_SUITE_ATTESTATION_080.md](FULL_SUITE_ATTESTATION_080.md) for mandatory
collection/JUnit proof, the Windows receipt and the post-exit attestation contract.

Each cycle commits synthetic source/record data to one isolated real Store,
reads the resume packet, validates its exact plan/task ACK, publishes and validates
a memory snapshot, performs one local embedding/chat request, then exercises a
controlled HTTP 503 interval and keyboard resize/focus paths. The TUI uses cached
projections of those committed rows through the same read-only HTTP/SSE preview
broker. No native account or production database participates. Health samples are
flushed every 30 seconds; cycles and pane captures have source/process metadata.
Fatal reports, an unavailable owned engine, dead panels, or changed process identity
fail the run. The owned engine stops on exit; its data remains for diagnosis.
An eight-hour qualification requires at least 32 complete cycles at a 900-second
cadence, including 32 embedding/chat observations; elapsed clock alone cannot pass.
The final suite must independently attest observation through its own cleanup:

```json
"native_fatal_qualification": {
  "passed": true, "cleanup_complete": true, "native_failure": false,
  "initial_boot_id": "same-nonempty-id", "final_boot_id": "same-nonempty-id",
  "new_wsl_dumps": [], "new_kernel_fatals": [], "errors": []
}
```

The active controller observes startup and cleanup plus a minimum 30-second grace.
It can only mark `workload_passed_pending_external_watch`: initial/final boot IDs
must match, owned JVM/tmux identities must be stopped, no new dump/kernel fatal or
owned JVM error log may exist, source/runtime/controller hashes and input reports
must remain unchanged, and all cleanup must succeed. `eight_hour_qualification`
remains false until the separate attestor validates the Windows parent receipt.
Any new host fatal blocks
qualification conservatively; the monitor does not claim that DevBrain caused it.
Only metadata and matching kernel fatal lines are saved, never crash-dump contents.
The original gate and suite reports are read-only inputs and are never overwritten.

This workload exercises Store, ACK/snapshots, real panels and synthetic HTTP/SSE
reconnection. It does not kill or qualify a native bridge, use account-based
Copilot/Codex/Claude, exercise live approval/tool execution, or measure Windows
Terminal font rendering. The local Ollama service is shared but receives synthetic
input only; the controller neither downloads models nor stops that service. Python
and Java/tmux executable hashes do not attest every shared library or installed package.
These are explicit scope limits, and this prepared controller has not run for eight
hours. The current runtime gate remains blocked.

A release qualification launcher must also observe the controller's own process
exit and continue its external crash watch afterward. A process cannot attest a
native failure during its own final interpreter shutdown after it writes a report.
