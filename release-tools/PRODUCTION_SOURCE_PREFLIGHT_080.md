# Read-only source comparison before and after DevBrain deployment

Prepared, not executed. The frozen 38-helper bundle, updater, ZIP, manifest and app remain unchanged. This standalone check supplements the deployment procedure; it is not a registered controller, a release qualification, or permission to overwrite local edits.

The updater's `--check-only` validates the 60 changed payload paths. It does not compare the other 138 paths. Its post-install wheel check covers the complete installed Python package, while the deployment `source_manifest` covers `pyproject.toml`, `uv.lock`, `src`, `tests` and optional `data`; it does not cover every documentation, frontend and script file. The new comparison covers all members of the immutable release ZIP: 182 files before deployment and 198 after deployment.

## Inputs and exact argv

Use the qualified base `/home/piotr/.cache/devbrain-runtime-080/runtimes/python/bin/python3.12` with `-I -B`. Never use the old installed `devbrain` or system interpreter. `gate.json` must have the exact qualified runtime-gate SHA a6bd365cea68986ed6795990272c766679bbd29fd9407e904a12c2956bcdf7fb. The script verifies the base executable and its actual SHA without launching another process.

Baseline command, through a separately reviewed standalone Windows observer:

```text
<qualified-base> -I -B <frozen-copy-of-check-production-source080.py>
--execute --mode baseline072
--source-dir /home/piotr/src/devbrain
--release-zip <v6-physical-root>/releases/devbrain-0.7.2.zip
--runtime-gate <fresh-artifact-root>/gate.json
--output <fresh-artifact-root>/source-comparison.json
```

After successful deployment and cleanup, before any production resume, use the same contract with `--mode target080` and `<v6-physical-root>/releases/candidates/0.8.0/devbrain-0.8.0.zip`. ZIPs are pinned inside the script: baseline `6b65e7315895b8c44b84bafc9146dfcdae732c3890f959d0e5e58cbf4fd9aa2c`; target `070469b5a63e28a19ed1e3d12ac96965c182a03a077dbe410bdcb6071bb35e7f`.

The output parent must already exist in the mapped private qualification area. It never creates or changes a production file. The only writes are a new public result file in that fresh artifact root. Neither an existing result nor a symlink can be overwritten. The report publishes only known mode/status, fixed reason codes, counts/booleans, elapsed time and the public expected ZIP hash. It does not publish actual source hashes, unknown extra filenames, file contents, config, account data, models or process arguments.

## Admission and observation

The first baseline check may run after the active backend/copy scopes have exited and root explicitly hands off the serial Linux slot, to discover source drift early. Repeat it immediately before actual deployment, after all eleven release proofs have been admitted. An early result is not fresh deployment admission.

The script has a 30-second internal monotonic budget. The standalone parent should allow at most 60 seconds, use a fresh envelope with a retained exact process handle/identity, and observe actual process exit plus at least 30 seconds of boot/kernel/dump/power monitoring after cleanup. Bind the script SHA, exact argv, both runtime binary identity and gate SHA, chosen ZIP SHA, result SHA and the actual process exit in a separate standalone observation. Do not alias the check as a registered controller or claim a normal wrapper receipt. `matched_pending_external_observation` alone is not a final PASS; only actual exit0 plus the clean external tail admits this read-only check.

At execution, require a fresh root and freeze the script, ZIP references, runtime gate and launch plan first. Use the existing reviewed mechanical observer mechanism in a fresh, explicitly reviewed envelope; the prepared envelope below still has no runtime outcome until explicitly executed and independently observed. An interrupted/error/nonzero check is not a match.

## Comparison policy

Every expected release member is a regular, owner-matching, single-link file reached through verified directory descriptors with `O_NOFOLLOW`. All expected bytes are hashed. Two complete passes must have identical file/directory identity metadata and identical comparison counts. Unsafe ZIP members, paths, symlinks, special files, hardlinks, missing files, modified files, extra managed files/directories or a read/identity race fail closed. Unknown files/subtrees are counted without reading their content or publishing their names. The tool never repairs, removes, renames or defaults an unexpected file.

Only these exact non-source/generated subtrees are excluded: root `.git`, `.venv`, `.pytest_cache`, `.ruff_cache`; frontend `node_modules`, `dist`, `test-results`, `playwright-report`, `.vite`. Their own entry must still be a real owner-matching directory; contents are not inspected. Within `src`/`tests`, `__pycache__` permits only regular single-link CPython/PyPy tagged `.pyc` filenames, never arbitrary files. There is no blanket ignore for unknown root directories, `.pyc` files elsewhere, `*.json`, secrets or user files. Archive members cannot be hidden by these exclusions because the pinned release inventories contain none of them.

A mismatch blocks deployment or resume and requires explicit investigation. It grants no permission to overwrite the mismatch. This remains a point-in-time observation: keep production stopped, all application/queue/engine scopes empty and source editing quiescent; do not edit the checkout between the fresh baseline comparison and deployment. The unchanged deployment controller does not itself consume this supplementary report or eliminate a concurrent external writer between check and apply. Any intervening source change invalidates the comparison and requires a fresh check, not an automatic retry.

## Publication without changing candidate evidence

The candidate manifest intentionally retains `stage: candidate` and `qualified: false`. The deployment controller admits the exact updater via its separately verified eleven-proof admission, final smoke and qualified immutable 0.7.2 backup interpreter. No manifest flag mutation or rebuild is needed for apply.

After actual qualification, release copies (if desired) must be byte-identical copies of the updater, ZIP, candidate manifest and ZIP `.sha256` file. Publish a separate new qualification index containing their exact hashes, current source freeze, all eleven actual proof references, canonical full-suite/8h attestations and eventually deployment/backup/retry/installed-UI results. Do not rewrite the admitted v6 build context, manifest003411 or historical trial reports. Applying directly from the immutable v6 candidate path is valid once the separate deployment admission has passed; copying to a public release folder is not an execution prerequisite.

## Prepared standalone observer (not executed)

The new `evidence/stage-production-source-preflight-080.ps1` is inert unless passed `-Stage`. It performs only Windows reads/copies of public evidence, the pinned ZIP, gate, checker and reviewed observer into a fresh physical qualification directory. It never launches WSL or reads production. `-Mode baseline072` stages the 182-file/64-package-file comparison; `-Mode target080` stages the 198-file/69-package-file comparison. Each invocation creates a distinct root and returns its `prepared-context.json` and input-freeze SHA. Preserve every earlier root, including failures.

After explicit root serial handoff, the caller retains and observes the actual outer Windows process while invoking that fresh root's launcher:

```text
pwsh -NoProfile -File <fresh-root>/run-source-preflight080.ps1
  -InputFreezeSha256 <exact digest from prepared-context.json> -Execute
```

The direct command is a legitimate unregistered checker, not a controller alias. The observer uses the approved standalone mechanism: exact qualified base Python with `-I -B`, a 60-second GNU timeout around the checker's 30-second internal budget, a 72-second Windows proxy bound, retained Windows process identity/stdio, and boot/kernel/WSL-dump/power monitoring through at least 30 seconds after actual exit. It temporarily inhibits Windows sleep and restores inhibition in `finally`. It verifies all staged input and helper hashes before and after execution. The exact reviewed checker does not create child processes or import the application.

A fresh exclusive attempt marker prevents reusing an already attempted root. A nonzero/timeout, interrupted observer, changed boot/power/native evidence, missing streams, input mutation or output mismatch never passes. A forced proxy stop does not prove Linux cleanup: it sets `manual_pid_birth_cleanup_admission_required` and blocks the next workload pending an independent ownership-safe cleanup observation. No numeric PID kill, process-tree kill, production engine stop or service action is performed by this envelope.

The output verifier accepts only the fixed public schema and complete counts, zero missing/modified/unexpected paths, two stable scans, and read-only/privacy flags. Twenty-nine Windows-only synthetic cases exercise the exact verifier with in-memory reports; they are not Linux execution, a source match, runtime qualification or deployment admission. Final operational evidence requires the checker's result, `source-preflight-observation.json`, and the caller's actual outer process exit. Even a fully observed match remains point-in-time only and cannot authorize overwriting an unknown edit.
