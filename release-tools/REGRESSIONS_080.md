# Controlled regressions for 0.8

Status: **prepared / not_run**. The saved helpers and AST checks are not test results. Do not run them before the runtime gate has completed with `pass`. Each phase requires its own fresh evidence directory and must be launched through `run-supervised-080.ps1`; `python_launcher` is the literal venv 0.8 interpreter whose base/SHA match the gate. A final result requires an external `receipt.json` written after the processes have actually exited and an empty scope has been observed.

Controller: `run-regressions-080.py`. Common arguments:

```text
--execute --phase guards|ux|recall --run-id ID
--artifact-root /mnt/c/.../ID --source-root /home/piotr/.../frozen-devbrain
--private-root /home/piotr/.cache/devbrain-regressions-080
```

`--private-root` must already exist as an owned Linux directory with mode 0700. The controller creates a new random directory and a private HOME inside it. A short owned directory `/tmp/r8-<uuid>` (mode 0700) serves as the shared TMPDIR for the parent and child processes and as the `tempfile` cache, so that tmux sockets fit within the 100-byte limit. It does not open the production database, profiles or OAuth.

| Phase | Additional arguments | Scope and condition |
|---|---|---|
| `guards` | `--updater /.../preflight-candidate/devbrain-update-0.8.0.py` | All 8 current 0.8 guard modules, collection and execution through the literal venv with `-I`, `pytest --noconftest`, JUnit and zero skips. No engine or models; the updater ownership tests may spawn short-lived, harmless Python processes. The manifest binds the tests, dependent helpers and the SHA of the explicit candidate. The archived 071/072 tests, smoke tests and the interactive supervision fixture are not run. The preflight candidate is neither a publication nor a release qualification. |
| `ux` | `--arcade-dist /.../pinned-distribution` | Full modules `test_terminal*`, `test_workspace*`, `test_cockpit*`, `test_graph*`, `test_api_workspace_e2e.py`, `test_api_worker.py`, `test_client_runtime.py`, `test_runtime_pane.py`. Normal conftest, qualified Java/tmux, owned engine. Separate collection and execution runs must have identical node IDs, with no skips or errors. Covers both sizes of the latency test; does not replace the human-run UX tasks. |
| `recall` | `--arcade-dist /.../pinned-distribution --chat-model qwen3.5:9b --embedding-model qwen3-embedding:0.6b` | Real `evaluation.run_evaluation(clients=False)` and Ollama. Requires 20 trials, at least 90% accuracy, all critical facts and deterministic isolation. The original scorer is called exactly once; Progress records the result of each individual case. RAM/GPU are monitored every 5 s until completion. Does not start or stop the Ollama service. |

The `frontend` phase was removed together with the browser cockpit (`devbrain/frontend/`, `web_static/`); it is no longer release evidence.

Recall may receive an explicit `--ollama-url http://127.0.0.1:11434` and `--num-ctx 16384`. These are test parameters; they do not change production settings. Sources, gate and runtime are checked before and after each stage. The JVM is terminated only through the retained pidfd, with the owner/cgroup re-checked. The controller observes 30 s with no remaining owned child processes; the external launcher separately observes its finalization. A forced cleanup or loss of evidence results in FAIL.

In the wrapper's `required_artifacts`, always list `gate.json`, `regression-source.json`, `regressions-results.json`, `controller-report.json`, `supervisor-report.json`, plus:

- Guards: `guards-collection.json`, `guards.xml`.
- UX: `ux-collection.json`, `ux.xml`.
- Recall: `recall-results.json`, `recall/checkpoint.json`, `recall/cases.jsonl`, `recall/resources.jsonl`.

The controller's final status is `regressions_completed_pending_external_watch`. The report has scope `devbrain_targeted_regressions`, an explicit `phase`, `full_suite_pass=false` and `eight_hour_qualification=false`. Passing one phase does not mean passing the others, the full backend suite or eight hours of stability. Reports and JUnit files remain in the evidence directory; private test directories are kept for operator inspection.

Explicit guards set: `test_account080_guards.py`, `test_devbrain_update080_guards.py`, `test_full_suite_080_guards.py`, `test_interactive_ux080_guards.py`, `test_queue_replay080_guards.py`, `test_regressions080_guards.py`, `test_scope_080_guards.py`, `test_stability_080_guards.py`. Adding a new module requires updating this list in the controller; it will not be silently skipped. Historical tests and their paths are neither modified nor aliased.
