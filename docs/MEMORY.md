# Project memory

## Portable project memory

A full snapshot in `.memory/cairnmind/` carries one project's memory together with the repository: conversations and the exact source content, findings, plans, tasks, checkpoints, revisions and links. Saving and importing do not require Ollama. The memory panel offers preview, save, import check, import and auto-save settings; the same operations are available from the CLI. Merely detecting the files loads nothing.

```bash
cairnmind memory snapshot my-app --preview
cairnmind memory snapshot my-app
cairnmind memory auto my-app --enable --interval-minutes 15
cairnmind memory status my-app
cairnmind memory auto my-app --disable
```

Auto-save is off by default. When enabled, it saves changed memory while the service is running, including after the terminal UI is closed. It keeps the five most recent snapshots and shares identical objects between them. It does not run models, make commits or push anything to GitHub. Full conversations may contain private information: review the export before adding `.memory/cairnmind/` to the repository. Machine configuration, authentication files and configured keys are not exported; the content of saved conversations is not automatically anonymized.

On a second machine, after installing Cairnmind and cloning the repository:

```bash
cairnmind memory inspect ~/src/my-app/.memory/cairnmind
cairnmind memory import ~/src/my-app/.memory/cairnmind --root ~/src/my-app --dry-run
cairnmind memory import ~/src/my-app/.memory/cairnmind --root ~/src/my-app
```

## Reading a snapshot without importing it

To read a snapshot without importing it, use `memory show`. It needs no Java, engine, model or service, and writes nothing. Inside a Cairnmind coding session the running service first confirms that the package belongs to the session's project, as it does for `memory inspect`:

```bash
cairnmind memory show ~/src/my-app/.memory/cairnmind
cairnmind memory show ~/src/my-app/.memory/cairnmind --kind decision --grep login
cairnmind memory show ~/src/my-app/.memory/cairnmind --kind decision --sources
cairnmind memory show ~/src/my-app/.memory/cairnmind --kind plan
```

It validates the package exactly as `memory inspect` does, then prints the snapshot header and the matching records with their status, version, time of last change and source addresses, and plans with their steps and source addresses. `--kind` takes a record kind or `plan`, `--status` a record status, and `--grep` a case-insensitive text. Check runs stored in the snapshot are printed as historical. A task also lists its acceptance criteria, each with the check it is linked to or "no check", and a record that replaced another, or was replaced, names it (`replaces:`, `replaced by:`). `--sources` also prints the text of each record's sources under it, up to 1,200 characters per source and 60,000 in all with a note of what was left out; a source whose text is empty or binary is named and not printed, and a source that supports several records is printed under each. Source text is untrusted, so control characters and terminal escapes are printed as visible escapes. `--grep` also searches the acceptance criteria and check names it prints for tasks, and with `--sources` the source text shown, and only that.

## Evidence states

Each record also has an evidence state, derived whenever it is read and never stored. `anchored` means that at least one supporting source still stands: a file version that is current, or whose identical content is current at the same or, after a move, another path. `evidence_replaced` means that no supporting version stands and at least one supporting file has different content now; when the record carries a quoted passage that occurs in the cited version, Cairnmind also says whether the passage still occurs in the changed file. `orphaned` means that no supporting source has a current version, for example because the only supporting file was deleted. `unverified` means that the record rests on file snapshots imported from another machine and the local files have not been indexed yet. Notes, conversations, plan snapshots and check outputs are statements made once and always stand. The state is as fresh as the last indexing of the project's files. The memory inspector shows it. When a resume packet is prepared, it warns about orphaned and changed records; the warning is advice, so it does not block the resume, and a later change of evidence alone does not invalidate an acknowledged packet. This command lists the records:

```bash
cairnmind memory orphans my-app
```

When a record is anchored to a function, class or method (`metadata.symbol_anchor`), Cairnmind compares the symbol's cross-OS normalised AST body hash rather than the whole file: edits elsewhere in the file or formatting (`ruff format`, `prettier`, comments, CRLF/LF) leave `symbol_state: unchanged` and keep `evidence_state: anchored`. Real body changes report `symbol_changed` (with the commit or timestamp of change) and deletions report `symbol_gone`, without ever mutating `record.status`. Projects may also opt in to at most one repository decision source (`off` by default) via `cairnmind memory decisions my-app [off|comments|adr|trailers]`, creating only `proposed` records idempotently and reporting `tag_removed` instead of deleting records when a tagged comment disappears. For serverless CI drift checks against a Git diff, see [Symbol-anchored evidence, opt-in decisions and `cairnmind scan`](WORKFLOW.md#symbol-anchored-evidence).

## How extraction fares in review

To see how automatic extraction fares in review, run `cairnmind memory extraction-stats my-app` (`--json` for the numbers, the same data as `GET /inspector/extraction-stats?project=...`). For every extractor version stamped on a record (`metadata.extractor`), it counts the extracted records that are still proposed, confirmed, disputed or superseded, and the share confirmed of those reviewed (confirmed or disputed). A record counts as confirmed only when a person confirmed it on this machine; one a memory package brought confirmed is listed apart as confirmed elsewhere and stays out of the share. It reads only record revisions in bounded pages, runs no model, changes nothing and prints numbers, never record text; a project with more than 50,000 extracted records is reported as truncated.

After the extractor changes (`EXTRACTOR_VERSION`), `cairnmind memory reextract my-app [--below-version N] [--dry-run]` extracts again the sources whose extracted records are older. It selects only records the extraction worker wrote and nobody has touched since (still proposed, with the store's extraction mark) whose extractor version is below N (default: this build's; a record with no stamp counts as 0; a larger N is refused), and queues one extract job per source under the key `extract:{source_id}:v{VERSION}`. `--dry-run` prints the counts of records and sources per extractor version and what a real run would queue, and queues nothing; a real run prints the same plus `queued`. Running it again for the same version queues nothing: the job for that source and version already exists (a failed one is retried with `cairnmind jobs retry`). When the new extraction commits, the same transaction supersedes that source's untouched proposals of an older extractor version: one that the new extraction says again (same kind and key or text) is superseded by the new record (`supersedes`), the others are superseded without a replacement; every one stays readable in its revision history. A confirmed, disputed or edited record is never superseded or edited. A failed extraction, or one that writes no record (it proposes nothing, or every record it proposes says what a record already says), changes nothing. Records extracted before 0.11.0 carry no extraction mark and are never selected. An outdated snapshot of a changed file is extracted again like any other source with such proposals, and its new proposals replace the old ones. An acknowledgement stays valid: untouched worker proposals are not part of what an ACK is given for, and only they are superseded. Without models (deterministic-only mode) the real run refuses, as `rebuild-vectors` does, because the jobs loop does not run; `--dry-run` needs no model. A source whose class the chat model server may not receive (`memory_remote_classes`) stays pending as in any extraction. A source the extractor now skips entirely (as the Cairnmind block of `AGENTS.md` since extractor version 2) is handled by the job `reextract` queued, and only by it: that job supersedes the source's untouched proposals of an older extractor version, without a replacement, in one transaction, with the reason "The source is no longer extracted: the current extractor skips it." in each superseded version's `metadata.superseded_reason`; a failure leaves them as they are, and confirmed, disputed and edited records are never touched. An ordinary first extraction of a skipped source supersedes nothing. `--dry-run` lists such sources apart as `skipped_sources` and `skipped_records` (a source with masked secrets is counted as read there; the job decides). With `--below-version N`, the dry run counts only records below N, but the job supersedes every untouched proposal of such a source below the current extractor version. As above, this does not make an open session's acknowledgement stale.

## Possible conflicts

When extraction finds a fact or a decision under a key that is already taken, it stores the new finding beside the old one and marks the pair as a possible conflict. The **Conflicts** view of the memory inspector shows each pending pair with both texts, their evidence times, scope and sources, and says which finding has the newer evidence and whether that order is reliable. You decide: one finding replaces the other and becomes confirmed, both stay because they hold in different scopes, or the alarm is dismissed. A fact and a decision cannot replace each other, so such a pair can only be kept or dismissed. When a marked finding has been replaced in the meantime, the pair is shown against the finding that replaced it. Each decision is saved with an audit note and a new revision, and the scopes you describe are kept on the finding; no source is removed. A model never resolves a pair: the fields that record a decision are ignored when any other writer sends them.

## Proposed findings waiting for review

A proposed finding stays proposed until you confirm or dispute it, and old ones are easy to forget. `cairnmind proposed my-app --days 30` lists the proposed findings that have not been written for 30 days or more, the longest untouched first, each with its id, kind, number of sources and the time of its newest evidence (stated by a source, or only known as "no later than"); `--worktree` narrows it to one worktree and `--json` prints the list. Checkpoints and tasks are left out, as in `brief`. The **Proposed** view of the memory inspector shows the same list for the open project (it reads `GET /inspector/proposed`, which answers what `--json` prints); **Filters** sets the number of days. The list only reads: it changes no record and asks no model. To act on an entry, open it and use **Confirm** or **Dispute**, the inspector's ordinary review with its version check and audit note.

## Import

Import keeps the project's identity and does not replace other projects in the database. Re-importing the same state duplicates nothing; diverging changes require conflict resolution. Search indexes are rebuilt locally. Historical `done` states are preserved, but checks carried over from another machine are not local `fresh` evidence. Logins, client configuration and new sessions are set up locally. See [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md) for the format contract, limits and restore rules.

## Team memory

A snapshot carries one person's whole memory. To share what a team agreed on, publish it: only records a person confirmed (decisions, constraints, facts and tasks, and plans) are written, one readable file per record, to `.memory/cairnmind-published/` in the repository, and they go through review like code.

1. Confirm the records in the memory inspector (`Ctrl+B i`); a proposed record is never published.
2. `cairnmind memory publish my-app --preview` shows what would be written and what is held back, and why; `cairnmind memory publish my-app` writes it. Every publication first passes a secrets scan.
3. Commit the directory with `git add .memory/cairnmind-published && git commit` and open a pull request; Cairnmind never stages, commits or pushes. `cairnmind memory diff BASE HEAD` (or the `.github/workflows/memory.yml` workflow) shows the change for review, and `cairnmind memory check-published DIR` checks a directory in CI without a store or model.
4. After the merge, each teammate imports it: `cairnmind memory import-published ~/src/my-app/.memory/cairnmind-published --project my-app`, or `cairnmind memory import-commit main --project my-app`, which also checks every quoted passage against that commit's code. Add `--dry-run` first to see what it would change. Nothing imported is shown as verified or as confirmed by you. When the import would replace a record you confirmed or disputed on this computer, the command line refuses it and lists those records. Run it from the memory view of the workspace instead: **Team memory** asks for the published folder or a commit, checks it, shows every record of yours it would replace and every record it takes from the published side, and imports only after you agree. To take the published side of a record that diverged, paste the file names the check lists into its last field (from a folder; a commit cannot take a diverged record).

Another clone or worktree of the same repository is a folder of the same project: register it with `cairnmind project root my-app ~/src/my-app-other` before publishing or importing there. The format, the trust rules, the secrets scan and the CI workflow are in [MEMORY_CONTRACTS.md](MEMORY_CONTRACTS.md#published-memory-version-1).
