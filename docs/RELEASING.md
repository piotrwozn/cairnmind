# Releasing

One gate, then a short list of steps. Nothing here is automated except CI; every other step is done by hand by the owner, in WSL.

## The release gate

A release needs all four, on the release commit.

1. **Green CI.** The required checks are `lint`, `fast-tests`, `macos-fast-tests` and `full-tests` (`.github/workflows/ci.yml`). `macos-fast-tests` runs the fast tier on an Apple silicon macOS runner. `full-tests` runs the whole suite against the pinned ArcadeDB on Java 21 with tmux, on Linux only. The `coverage` job is informational and is not part of the gate. To run the same checks yourself: `uv run ruff check src tests`, `uv run pytest -m "not arcade and not tmux"` and `uv run pytest` (see [CONTRIBUTING.md](../CONTRIBUTING.md#running-checks)). The result is the CI run on the merge commit.
2. **Evaluations, when the release changes the knowledge agent, retrieval or extraction.** Run by hand with the local models; they are not part of CI ([EVALUATION.md](EVALUATION.md)):

   ```bash
   cairnmind eval run --report-dir DIR             # 20 recall cases
   cairnmind eval memory --report-dir DIR          # extraction, portable transfer, cited recall
   cairnmind eval extraction --report-dir DIR      # what extraction proposes from conversations
   cairnmind eval repository . --report-dir DIR    # questions about this repository
   ```

   Exit code 0 is passed, 1 failed, 3 `not_run`, which is never a pass. Record the results in the release pull request (CONTRIBUTING asks for this for such changes), and in [EVALUATION.md](EVALUATION.md) or [evaluation/RESULTS.md](evaluation/RESULTS.md) when they are figures the release states. A release that touches none of these areas has nothing to run; the 0.9.0 release notes say that its recall results were not repeated.
3. **The hand-off pilot.** [pilot/README.md](../pilot/README.md) prepares a fresh repository for a Codex, Claude Code, fresh Codex hand-off. It needs your own WSL2, a GPU, Ollama and both clients signed in. It runs the local evaluation first, then three real sessions, then compaction, restart and `stale` checks. Its section "Measurements and pass criteria" says when it passes: the real models pass the evaluation, the three sessions restore consistent states backed by sources, real tests and the review are recorded, and the compaction, restart and `stale` checks give the expected results. It sets no numeric threshold beyond the evaluation's own. It keeps its reports in a directory outside the repository, and the repository does not say where the outcome is written down, so state it in the release pull request.
4. **A manual trial on a real installation.** Before a release, install the release candidate on a clean installation and run the trial checklist end to end in a real terminal. Testers that drive the terminal can do this; for 0.10.0 it was 65 steps in three parts, which found 42 product defects that the automated tests did not; 39 were fixed in #152 to #158 and the rest deferred to #160 and #161. A finding counts only when a second run reproduces it. Each confirmed finding is fixed before the release, or deferred in writing with an issue. State the result in the release pull request: the steps run, the findings, how many were fixed and how many deferred.

A major release has a wider gate, described in issue #159.

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

## The database image

The image of `deploy/arcadedb` is published to `ghcr.io/piotrwozn/cairnmind-arcadedb` by the workflow `Database image` (`.github/workflows/database-image.yml`), for linux/amd64 and linux/arm64, tagged with the pinned ArcadeDB version. It is run by hand, from main only, when the pinned ArcadeDB version or anything under `deploy/arcadedb` changes:

```bash
gh workflow run database-image.yml --repo piotrwozn/cairnmind --ref main
```

The summary of the run gives the digest (`sha256:...`). A later run with the same tag moves the tag, so anything that must name one exact image names it by digest. The release notes of a release that publishes the image name it as `ghcr.io/piotrwozn/cairnmind-arcadedb@sha256:...`, the digest `src/cairnmind/config.py` pins. The first run creates the package as private; the owner makes it public once, in the package's settings on GitHub.

## Upgrades

Since 0.8.0 an upgrade is a backup and the installer, plus `cairnmind install` for each project when the files it writes into a project changed ([UPGRADING.md](UPGRADING.md)). The host runtime gate exists only in the frozen 0.7.2 to 0.8.0 updater attached to the v0.8.0 release; its tooling is not in this repository. No host qualification is part of the gate.
