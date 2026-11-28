# Releasing

One gate, then a short list of steps. Nothing here is automated except CI; every other step is done by hand by the owner, in WSL.

## The release gate

A release needs all six conditions below on the release commit. Conditions 5 and 6 (together with the three-run rule in condition 1 and the upgrade trial in condition 4) were adopted for `0.13.0` (#454) and **stay permanently for every subsequent release**.

1. **Green CI: three consecutive green runs on `main` (#454).** The required checks are `lint`, `fast-tests`, `macos-fast-tests`, `full-tests`, `windows-fast-tests` and `windows-arcade-tests` (`.github/workflows/ci.yml`), plus `contract-declaration` (`.github/workflows/contract-check.yml`) on pull requests that touch [STABLE_CONTRACT.md](STABLE_CONTRACT.md) or `tests/fixtures/contracts/`. `macos-fast-tests` runs the fast tier on an Apple silicon macOS runner. `windows-fast-tests` and `windows-arcade-tests` run the fast tier and the full ArcadeDB tier (excluding POSIX-only `tmux` tests) on `windows-latest` with Java 21 and without `PYTHONUTF8`. `full-tests` runs the whole suite against the pinned ArcadeDB on Java 21 with tmux, on Linux only. The `coverage` job is informational and is not part of the required checks. To run the same checks yourself: `uv run ruff check src tests`, `uv run pytest -m "not arcade and not tmux"` and `uv run pytest` (see [CONTRIBUTING.md](../CONTRIBUTING.md#running-checks)). `server-tests` and `restore-size` run only on `main` and by hand, never on a pull request: before a release, `main` must record **three consecutive green runs in a row across every job** (`server-tests` and `restore-size` included), with **no single job re-run** (#417, #454). A branch that touches the store, the engine's errors or the graph write has both started on it by hand (`gh workflow run ci.yml --ref BRANCH`) and green before its pull request is merged (#455).
2. **Evaluations, when the release changes the knowledge agent, retrieval or extraction.** Run by hand with the local models; they are not part of CI ([EVALUATION.md](EVALUATION.md)):

   ```bash
   cairnmind eval run --report-dir DIR             # 20 recall cases
   cairnmind eval memory --report-dir DIR          # extraction, portable transfer, cited recall
   cairnmind eval extraction --report-dir DIR      # what extraction proposes from conversations
   cairnmind eval repository . --report-dir DIR    # questions about this repository
   ```

   Exit code 0 is passed, 1 failed, 3 `not_run`, which is never a pass. Record the results in the release pull request (CONTRIBUTING asks for this for such changes), and in [EVALUATION.md](EVALUATION.md) or [evaluation/RESULTS.md](evaluation/RESULTS.md) when they are figures the release states. A release that touches none of these areas has nothing to run; the 0.9.0 release notes say that its recall results were not repeated.
3. **The hand-off pilot.** [pilot/README.md](../pilot/README.md) prepares a fresh repository for a Codex, Claude Code, fresh Codex hand-off. It needs your own WSL2, a GPU, Ollama and both clients signed in. It runs the local evaluation first, then three real sessions, then compaction, restart and `stale` checks. Its section "Measurements and pass criteria" says when it passes: the real models pass the evaluation, the three sessions restore consistent states backed by sources, real tests and the review are recorded, and the compaction, restart and `stale` checks give the expected results. It sets no numeric threshold beyond the evaluation's own. It keeps its reports in a directory outside the repository, and the repository does not say where the outcome is written down, so state it in the release pull request.
4. **A whole manual trial from nothing and an upgrade from the previous release (#454).** Before every release, run both a clean installation from nothing and an upgrade from the previous published release with real data on the supported platforms (WSL/native Windows and macOS), following the published documents step by step (for `0.13.0`, together with the two-machine measurements of #225, #266, #362, #363, #371 and #372). Testers that drive the terminal can do this; for 0.10.0 it was 65 steps in three parts, which found 42 product defects that the automated tests did not; 39 were fixed in #152 to #158 and the rest deferred to #160 and #161. A finding counts only when a second run reproduces it. Each confirmed finding is fixed before the release, or deferred in writing with an issue. State the result in the release pull request: the steps run, the findings, how many were fixed and how many deferred.
5. **Two consecutive dry rounds of attacks on the core (#454).** After every open pull request of the milestone is merged, run independent adversarial attack rounds against the core: the store and the single-writer rule, backup and restore, indexing and the code graph, upgrading from the previous release, a person's acts and what an agent may not do without them, and answer refusals. Every finding becomes an issue of the milestone and is fixed, which resets the counter to zero. Tagging requires **two consecutive rounds with zero findings in the core**. Record each round's scope, attacks and outcome on the milestone's release-gate issue.
6. **Contract fixtures up to date ([STABLE_CONTRACT.md](STABLE_CONTRACT.md), #249).** Every frozen CLI `--json` and MCP tool example matches the release commit, and every previously published release's upgrade and legacy-file fixtures pass.
7. **Product website install commands and static build verified (#251).** `uv run pytest tests/test_website_sync.py` confirms every install command and measured figure on the product website (`website/`) matches [INSTALL.md](INSTALL.md), `README.md`, [EVALUATION.md](EVALUATION.md) and [evaluation/RESULTS.md](evaluation/RESULTS.md) and that `website/` remains excluded from `.whl` and `.tar.gz` release archives; `cd website && npm ci && npm run build && npm run verify` confirms the static HTML, `/docs/` render, SEO metadata, JSON-LD, `robots.txt`, `llms.txt` and accessibility checks pass.

## Major release gate (1.0.0, #159)

In addition to the standard gate above, a major release (`1.0.0` and later `X.0.0` releases) requires a wider manual qualification run on a clean installation from the release candidate:

1. **Full updated checklist:** the manual trial checklist, updated for every feature added since `0.10.0`, run end to end.
2. **Unscripted exploratory round with ten isolated testers:** ten independent testers run concurrently on isolated instances (each with its own data directory, database engine, service ports, tmux sessions and project, never touching the owner's data), given only the release candidate and its documentation, covering ten distinct areas:
   1. installation and first run;
   2. workspace panels and keyboard handling;
   3. sessions and resume;
   4. plans and stages;
   5. the built-in agent and MCP tools;
   6. memory (`ask`, `remember`, review, `impact`);
   7. an external client over MCP;
   8. the command line from the documentation (every documented command and option);
   9. Polish locale and narrow terminals;
   10. a hostile user (invalid input, interrupted operations, killed processes).
3. **Every documented command and option** executed once and compared against the documentation.
4. **Every supported platform** (Linux, Windows via WSL2 and native Windows 10/11, and macOS on Apple silicon) exercised in a real terminal by a person.
5. **Reproduce and resolve:** a finding counts only after an independent second run reproduces it (testers also check the kernel log before reporting an OOM/crash under load). Every confirmed blocker or major finding must be fixed before tagging `1.0.0`; any deferred minor finding requires an issue and written rationale. Attach the full report (steps run, findings, fixed, deferred and why) to the release pull request.

## Release candidate and freeze (1.0.0, #250)

A major release is never published straight from a passing build. It goes through a release candidate and a freeze:

1. **Publish `X.0.0-rc1`** as a GitHub pre-release, installable via the documented installation steps.
2. **Freeze rules:** from `rc1` onward, **only defect fixes** are merged, with no new features and no refactoring.
3. **Qualification during the freeze:** the major release gate (#159: ten-tester exploratory round, upgrade trial, newcomer trial) runs on the candidate while the owner uses it daily.
4. **7-day zero-defect rule:** the freeze ends only after **7 consecutive days with zero confirmed blocking defects**. Blocking means **any confirmed defect** (any finding reproduced a second time). Every confirmed defect is fixed, tagged as the next candidate (`rc2`, `rc3`, ...), and **resets the 7-day clock**.
5. **Final tag and owner sign-off:** the owner decides when the freeze has passed. Final `X.0.0` is built and published from the **exact commit of the last release candidate, unchanged**.

## Steps

As done for 0.9.0 (#66, which changed `pyproject.toml`, `src/cairnmind/__init__.py`, `uv.lock`, `CHANGELOG.md` and the docs).

1. **Release pull request.** Set the version in `pyproject.toml`, `src/cairnmind/__init__.py` and the `cairnmind` entry of `uv.lock`. Turn the `Unreleased` heading of `CHANGELOG.md` into the version heading. Update the docs that name the version (README, INSTALL, UPGRADING), and the supported versions in `SECURITY.md`: the new minor version `Yes`, the one before it `Security fixes only`, older `No`. Check the gate on the pull request. The owner merges it.
2. **Build from the merge commit.** Only tracked files go into the artifacts, so build from an archive, not from the working directory. In WSL, in a clone with the merge commit checked out:

   ```bash
   VERSION=X.Y.Z
   COMMIT=$(git rev-parse HEAD)
   rm -rf /tmp/cairnmind-$VERSION && mkdir /tmp/cairnmind-$VERSION
   git archive "$COMMIT" | tar -x -C /tmp/cairnmind-$VERSION
   cd /tmp/cairnmind-$VERSION
   uv build        # dist/cairnmind-$VERSION-py3-none-any.whl and dist/cairnmind-$VERSION.tar.gz
   cd dist
   sha256sum cairnmind-$VERSION-py3-none-any.whl cairnmind-$VERSION.tar.gz > SHA256SUMS
   ```
3. **Publish.** Write the release notes (those of 0.9.0 have Highlights, Install or upgrade, Known limits and a link to the changelog), then:

   ```bash
   gh release create vX.Y.Z --repo piotrwozn/cairnmind --target "$COMMIT" \
     --title "Cairnmind X.Y.Z" --notes-file NOTES.md \
     cairnmind-$VERSION-py3-none-any.whl cairnmind-$VERSION.tar.gz SHA256SUMS
   ```

   The v0.9.0 release has exactly these three assets. Downloads are verified with `sha256sum -c SHA256SUMS`.

   **The database on another host (#68, #225).** The notes of 0.11.0 said that it was checked locally: one machine, a loopback address or a relay for the network. The notes of the release that includes #225 say instead that it was checked between two machines, once, on 22 November 2026 (the figures and what failed are in [DEPLOYMENT.md](DEPLOYMENT.md#two-installations-and-one-store-what-a-trial-between-two-machines-showed)), and name what was not checked: a model server on another computer, the published image (the trial built its own) and Docker Desktop (it used Colima), indexing after every file changed, a service with models on over the network (#363), more than one run of anything, and `restore` over the connection, which failed (#362). Remove from that sentence what a later trial covers, and no more; the open issues of the trial (#362, #371, #373, #365, #363) stay in Known limits until each is fixed.
4. **Record the release's contract fixtures (#249).** Right after publishing, capture the frozen store, export, backup, portable snapshot and published memory fixtures from the built release wheel and add them to `main` in a follow-up PR so CI upgrades and file-compatibility tests cover `X.Y.Z`:

   ```bash
   uv run python scripts/build_release_fixtures.py --version "$VERSION" --wheel "dist/cairnmind-$VERSION-py3-none-any.whl"
   ```

## The database image

The image of `deploy/arcadedb` is published to `ghcr.io/piotrwozn/cairnmind-arcadedb` by the workflow `Database image` (`.github/workflows/database-image.yml`), for linux/amd64 and linux/arm64, tagged with the pinned ArcadeDB version. It is run by hand, from main only, when the pinned ArcadeDB version or anything under `deploy/arcadedb` changes:

```bash
gh workflow run database-image.yml --repo piotrwozn/cairnmind --ref main
```

The summary of the run gives the digest (`sha256:...`). A later run with the same tag moves the tag, so anything that must name one exact image names it by digest. The release notes of a release that publishes the image name it as `ghcr.io/piotrwozn/cairnmind-arcadedb@sha256:...`, the digest `src/cairnmind/config.py` pins. The first run creates the package as private; the owner makes it public once, in the package's settings on GitHub.

## Upgrades

Since 0.8.0 an upgrade is a backup and the installer, plus `cairnmind install` for each project when the files it writes into a project changed ([UPGRADING.md](UPGRADING.md)). The host runtime gate exists only in the frozen 0.7.2 to 0.8.0 updater attached to the v0.8.0 release; its tooling is not in this repository. No host qualification is part of the gate.
