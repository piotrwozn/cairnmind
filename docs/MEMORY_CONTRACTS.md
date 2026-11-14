# Portable project memory version 1

Cairnmind writes one project's archived knowledge under `.memory/cairnmind/`, with the format name `cairnmind-portable-memory`. Versions from DevBrain 0.4.0 to Cairnmind 0.9.0 wrote it under `.memory/devbrain/` with the format name `devbrain-portable-memory`. Both directories are reserved and both format names are read; a manifest is never rewritten, because the format name is part of what its identifier is computed from. When both directories hold a package, the one in `.memory/cairnmind/` is read, and `memory status` says under `earlier_package` whether the other one is part of its history (`superseded`) or not (`diverged`).
The package is data, not executable configuration. An explicit import is separate
from the administrative whole-database `restore` command.

## Identity and completeness

The manifest identifies the stable project UUID, the selected origin worktree,
snapshot and parent hashes, counts, and processing state at capture. Canonical
JSON objects are stored by SHA-256. Sources retain exact UTF-8 content and content
hashes, including archived conversation events and historical evidence. Records,
record revisions, plans, plan revisions, and logical graph relationships retain
their identities. Foreign code graphs and file snapshots do not establish the
state of the current local checkout.

Only committed, project-assigned rows are captured. Pending import, extraction,
or embedding work is reported rather than represented as finished. The package
does not contain environment configuration, credential files, embeddings,
sessions, resume packets, ACKs, cursors, or active job leases. Raw archived text
can itself contain private information; a full package is not automatically
sanitized for publication.

## Publication and local scheduling

Publication locks the reserved destination, captures a consistent database view,
then writes immutable objects before atomically replacing the manifest. Five
available successful manifests are retained, with shared objects deduplicated.
Unchanged knowledge produces no new snapshot. Git history is not erased by this
retention policy. Cairnmind does not stage, commit, or push files.

The per-project scheduler is opt-in and defaults to 15 minutes. One registered
worktree is its configured destination. The service must be running; it coalesces
missed intervals instead of replaying every missed tick. The first eligible tick
can create the initial snapshot. Conflicts preserve existing package contents and
are reported in status. An unknown or externally modified manifest requires
explicit inspection and import before further publication.

Only the reserved subtree is excluded from indexing and code file manifests.
Other `.memory/` content remains covered. A Git commit still changes HEAD and can
therefore invalidate checks and resume acknowledgements.

## Import and authority

Inspection validates hashes, declared identity, evidence closure, and package
structure without opening a database. Import binds the stable project identity
to an explicitly chosen existing directory; names and Git URLs do not establish
identity. A dry run compares the destination through an already running service
without initializing configuration or changing database rows.

The importer transaction preserves unrelated projects and historical completion.
Repeated import of the same snapshot is a no-op. Fast-forward updates require a
compatible baseline and unchanged local knowledge. Divergent heads, contradictory
immutable IDs, missing history, or ambiguous path bindings are rejected. No
automatic two-way merge or deletion of a project is performed.

Imported check runs have `origin=portable_import` and cannot satisfy local
verification. Existing genuine local runs are preserved when memory returns to
its original computer. New sessions require the normal packet read and exact
ACK. Imported task state `done` remains historical progress; it does not imply
that tests are fresh on this machine. Named checks use the local checkout and
must be run explicitly. No imported command or check profile is executed.

Text chunks are rebuilt from archived source views during import; semantic
embeddings and current-code analysis rebuild locally. Original paths remain
provenance, with local scope bindings kept separately.

## Validation budgets and quality evaluation

Version 1 limits an individual JSON object to 16 MiB, a manifest to 32 MiB, and a
complete package to 512 MiB and one million object descriptors. A package exceeding
these limits fails explicitly before replacing the current manifest; data is
never silently truncated. Source text is not sent to a model by export or import.

`cairnmind eval memory` uses two disposable databases and local Ollama to exercise
positive extraction, transfer, fresh-agent cited recall, and project isolation.
The three explicit fixture facts must all survive extraction; an empty extraction
does not pass this fixture. This is separate from the existing pre-seeded recall
evaluation. It launches no Codex or Claude processes. Exit codes are 0 for passed,
1 for an attempted failure, and 3 when required models are unavailable before the
test begins. Controlled-adapter regression results and real inference results are
reported separately.

# Published memory version 1

The full archive above is one person's backup: it is too large to commit, and two branches
that both write it always conflict in its manifest. Published memory is a second package, with
its own format name `cairnmind-published-memory` and its own version, versioned apart from the
archive. It holds only knowledge a person confirmed, one file per record or plan, so that it can
be committed with a pull request and merged by Git. The archive format, its import and its
validation are unchanged.

## Directory

```
<registered project root>/.memory/cairnmind-published/   (or PATH/<identity>/ in a separate repository; see Carriers)
  FORMAT.json            {"format":"cairnmind-published-memory","format_version":1,"project_id":"<team id>"} + "\n"
  .gitattributes         "* text eol=lf\n"
  .gitignore             ".cairnmind-memory-*\n"  (temporary files of an interrupted write)
  decision/<uuid>.md  constraint/<uuid>.md  fact/<uuid>.md  task/<uuid>.md
  question/<uuid>.md  preference/<uuid>.md  (allowed by the format; version 1 does not write them)
  plan/<uuid>.md
```

A path depends only on immutable identity: record and plan IDs are UUIDs and a record's kind
never changes. There is no manifest, index or counter, so no file is shared by all publications;
the three static files are written once and are the same on every branch. Nothing else may be in
the directory: unknown entries, subdirectories and symbolic links are refused by name. The
directory is reserved like the archive: it is not indexed and does not count as project code, so
a commit that only changes memory changes no code fingerprint (it still moves HEAD). The publish
lock is in the data directory (`runtime/publish-<project>.lock`), never in the package.

`FORMAT.json` names the team's publication identity. A project's own ID becomes it at the first
publication; it is stored on the project as `publication_id`. Publishing into a directory whose
identity differs is refused ("belongs to X; import it first"); importing binds an unbound project,
and `--bind` replaces a binding explicitly (the way out when two branches each created
`FORMAT.json`, which Git shows as an add/add conflict in that one file).

## What is published, and whose act it is

Records of kind decision, constraint, fact and task, and plans. Checkpoints, proposed wordings,
transcripts, chunks, embeddings, the code graph, check runs, sessions, acknowledgements, jobs,
store versions and plan revisions are never published, and neither is any absolute path.

A record state travels only when a person's act covers it:

- a review audit (`review://`, origin `local_user_review`) written by an inspector review, a
  conflict decision that chose which finding stands, or `confirm_plan`, whose `content_sha256`
  equals the SHA-256 of the text and, for a task, whose `definition_sha256` equals the hash of what
  the task asks for (its text, plan, step key, dependencies, test and review requirements, profiles,
  acceptance criteria, their check links and its scope), and whose `identity_sha256` equals the hash
  of the record's kind, key and `supersedes`; the state's status must be what the act decided. Audits
  recorded before #170 carry no `identity_sha256`: for them only the text, the status and a task's
  definition are covered, so the kind, key and `supersedes` of such a revision rest on the file alone;
- a finding typed in the workspace form and saved as confirmed, at its first version only;
- after one of these, a task's progress (a state that differs only in `state`), or a mechanical
  supersession (status `superseded` with text, key, `supersedes` and definition unchanged) when what
  replaced the record is published too. A reader requires the replacement in the same tree: a record
  that names it in `supersedes`, or the task's plan whose last definition no longer lists it.

`remember --confirmed`, `tasks add`, session and MCP writes are not acts: agents can make all of
them. Such records, and a confirmed record an agent edited after its review, stay local until a
person reviews them; the inspector allows confirming a confirmed record again when no act on this
machine covers its current version. Audits record `actor_name` (`git config user.name` of the
project), `branch` and `commit`, read when the act happens and never at publish time.

Held back, each listed by `memory publish` with its reason, without blocking the others: records no
act covers ("review it in the inspector"); records an external or third-party session wrote, or that
cite such a source; text with control or bidi characters (held for a person to fix, never escaped);
a workspace note over 16,000 characters; a file source outside the project root; a file that would
exceed 1 MiB; and, computed to a fixpoint, anything whose supersedes, dependencies, plan or plan
steps name something that is not published itself.

## Record files

Every value written as JSON is canonical (`sql.json_text`: sorted keys, compact, UTF-8, no NaN),
and JSON is read refusing duplicate keys. Free text is a block: a label line `<Label>, <N> lines:`
(`1 line:` when N is 1), an empty line, then each of the N lines of the text indented by four spaces
(an empty line stays empty). Text never starts at column 0, so no line of text can look like a field,
a header or a Git conflict marker. Every text must be printable as it is: no C0 control except tab and
newline, no DEL, C1 or bidi control, no U+2028 or U+2029.

```
---
cairnmind: "published-record 1"
kind: <kind>
id: <id>
created_at: <when the record was created>
status: <status of the last revision>
revisions: <m>
state: <state of the last revision>          tasks only
plan: <plan of the last revision>            tasks only (null for a standalone task)
reviewed_by: [{"action","at","branch","commit","name","revision"}, ...]
---

# <Kind> <first 8 characters of the id>

Current text block

## Sources

Source <id>: <reference>
[Passage block]                              notes and review audits only

## Revision 1 ... ## Revision m
at: ...
covered: ...
<fields>
[Quote block] [Text block]
```

A revision's fields are written in this order: `at`, `covered`, `status`, `key`, `supersedes`,
`sources`, `quote`, and for tasks `state`, `plan`, `step_key`, `depends_on`, `requires_test`,
`requires_review`, `required_profiles`, `acceptance_criteria`, `criterion_checks`, `scope`.
Revision 1 writes all of them and its text; a later revision writes `at` and `covered`, then only the
fields that changed, and its text only when it changed. `covered` is `{"by":"review","source":S}`,
`{"by":"workspace_form","source":S}`, `{"by":"progress"}` or `{"by":"superseded"}`. `reviewed_by` is
derived from the covering passages, with `name` "local user" for an audit without `actor_name`.
Revisions are numbered 1..m on their own: they are not store versions. Only what a revision says is
compared anywhere; `at` and `covered` are written but never compared.

A revision's `sources` are the cited source IDs, sorted, with all plan sources collapsed to the
earliest one. Its `quote` is the stored quoted passage with its line range, published only when it is
at most 2,000 characters and occurs in a cited file or workspace note (or in an imported source that
registered it); a quote from a conversation never travels.

A source reference always carries `at` (when the source was stored) and `kind`:

- a file (`code`, `document`, `file`): `commit`, `path` relative to the project root, `sha256` of the
  content that was cited, `snapshot`; no passage and never the file's content;
- a workspace note: `actor_name`, `branch`, `commit`, `"kind":"note"`, `"origin":"workspace_form"`; its
  passage is the whole typed text;
- a review: `{"at","kind":"review"}`; its passage is the whole audit as stored;
- a plan: `{"at","kind":"plan"}`;
- anything else (conversations, session findings, notes from the command line, check output):
  `{"at","kind","origin"}`, with no passage.

## Plan files

A plan file holds only the history of its definition, with a header `cairnmind: "published-plan 1"`,
`definitions: <m>` and `reviewed_by`, a "Current goal" block, the sources, and `## Definition k`
entries with the fields `at`, `covered`, `title`, `constraints`, `steps` (`[[task id, step key], ...]`
in plan order), `sources`, and a Goal block. A definition is appended only when it differs and a
`confirm_plan` audit covers it: an audit with the plan's ID and `plan_sha256` equal to the hash of
title, goal, constraints and the ordered step IDs. Its sources are the earliest plan source and the
covering audits. A plan file has no version, state or step index; steps are task files that carry the
plan, their step key and their dependencies. `memory confirm-plan PROJECT PLAN --expected-version N`
(refused inside a coding session) and the inspector API confirm a plan and all its active steps in one
transaction with one audit.

## Reading a directory

`memory check-published DIR` reads a directory with no store, service, network or model, and is what
a CI job runs on a pull request's merge commit. Any failure refuses the whole tree and names every bad
path; exit 0 is valid, 1 refused, 2 a usage error. A line that is a Git conflict marker
(`<<<<<<<`, `=======`, `>>>>>>>` or `|||||||` at column 0) is reported first, as
"unresolved conflict in PATH line L". Then each file must be UTF-8 without a byte order mark (a file
whose every line ends in CRLF is read as LF), must be at its identity's path, must number its
revisions 1..m with no gap or repeat, must use only the fields above (`verified`, `test_passed` and
the other verification fields are refused as "trust fields are never published"), must have every
revision covered by an act whose passage is in the same file, and must render back to exactly its own
bytes. Every time (`created_at`, each `at`, each source's `at`) must be an ISO 8601 time with a time
zone, and every value an audit passage holds, once its JSON escapes are read, must be free of control
and bidi characters like the rest of the file. Across files: one file per ID (the same ID in two kind
directories is refused), one reference per source, one active record per kind and key, plan steps
that agree with their task files, superseded records that are superseded, a replacement in the tree
for every mechanical supersession, and the archive's structural checks (kinds, statuses, task
contracts, closed references, dependency cycles).

## Import

`memory import-published DIR --project P [--dry-run] [--bind] [--take-published ID ...]` checks the
tree as above, then applies it in one transaction, all or nothing:

- sources arrive as stubs of kind `published` with their original IDs: the passage or nothing as
  content, the reference in their metadata, never current; a local source with the same ID is kept,
  and a revision may cite such a local source only when a local version of the same record already
  cited it (or it is a local review audit, or the source of the task's own plan): a file cannot
  attach this machine's own evidence to a record, and the import is refused naming the path;
- a record unknown here gets one local version per published revision; a record whose local head is
  an earlier published revision is fast-forwarded; a record whose local history already holds the
  published head is left alone (local work ahead is not touched);
- anything else diverged, and the whole import is refused with the paths. `--take-published ID`
  appends the published revisions after the part both histories share as new local versions and
  removes nothing; the next publication then follows the published history, and the person can apply
  their change again as a new revision;
- an imported version carries `publication` metadata (path, revision, reviewed_by), which only the
  importer sets and which later local edits keep;
- plans are applied after their tasks. A diverged plan refuses the import; `--take-published` takes
  records only, so the team resolves it in Git by keeping the definition the importing store holds;
  IDs that belong to another local project are refused, as for
  the archive. A repeat import of the same tree writes nothing, and `--dry-run` writes nothing.

Laptop import never deletes: a file that is absent says nothing. After a published import, a full
archive from another machine no longer imports into the same project (its baseline differs): the
archive is one person's backup.

## Trust

Nothing imported is verified. No check run is published, so imported tasks are never fresh; reported
flags are never published; imported state `done` is historical progress. An imported source is never
anchored here: a record resting only on such sources is `unverified` with
`unverified_by: published_elsewhere`, and an imported review neither anchors a record nor counts as
this machine's own review. No field in a file can raise trust above "reviewed elsewhere, not
verified here".

What import cannot check: that the named person confirmed anything (a hand-written file can carry a
forged audit with a matching hash; the passage is visible in the diff, and accountability comes from
Git authorship and pull request review); that a passage really came from that path at that commit;
that text this store never saw is what was confirmed; that a degraded mark was not removed (which is
why degraded records are never published); that a statement is true.

## Merging

- Different new records on two branches are disjoint new files: Git merges them cleanly and the
  import holds both.
- The same record changed on two branches: both append a revision at the end of the file and change
  its header, so Git conflicts in exactly that file. Resolve it by taking one side whole
  (`git checkout --ours|--theirs PATH`); hand-merged text is no longer covered by its audit and is
  refused, and keeping both blocks gives two revisions with one number, which is refused too.
- Progress on different steps of one plan touches different task files. The plan file conflicts only
  when both branches redefine the plan.
- Conflicts Git cannot see (two active records with one kind and key, a dependency cycle across
  branches, two steps with one key) are refused by `check-published` on the merge commit.

## Publishing

`memory publish PROJECT [--worktree W] [--preview]` reads the store in one transaction, selects and
renders, refuses when the directory has revisions this store does not have ("import first"), when a
source is published differently, or when the result would not be a valid tree, and then writes only
the files whose bytes changed. Rendering uses no clock and no Git call, so publishing twice with
nothing new changes no file. The output lists the files written and unchanged, what is held and why,
and the files and bytes of the whole package. It never stages, commits or pushes. `memory status`
shows the destination, whether `FORMAT.json` exists and whose it is, the project's publication ID and
the file count. `memory show-published DIR` reads a directory as text. Writing stays behind the
switch `CAIRNMIND_ENABLE_PUBLICATION=1` until the secrets gate (#171) refuses registered secrets and
private keys; previews, checks and imports are not switched off.

Budgets of version 1: 1 MiB per file, 100,000 files and 256 MiB per package.

## Carriers: where published memory goes

The package is the same whatever carries it: a carrier only chooses the directory it is written to,
and `import-published DIR` reads any of them the same way, to the same records. Each project has one
carrier, chosen by a person with `cairnmind memory carrier PROJECT --repository` or
`cairnmind memory carrier PROJECT --checkout PATH` (never from inside a coding session). `memory status`
shows it.

- **The repository** (the default): `<project root>/.memory/cairnmind-published/`. Memory is committed
  with the branch that changed it and reviewed beside the code in the same pull request, and merges
  follow the code. Use it whenever the team can commit to the code repository.
- **A separate repository**: `--checkout PATH`, a checkout of a memory repository. The package goes to
  `PATH/<publication identity>/` with the same layout, so one memory repository can hold several
  projects. Use it when the team does not want memory files beside its code, or cannot add generated
  files to a repository it does not own. What it loses: memory is no longer reviewed beside the code
  it describes, a code branch and its memory are committed, merged and reverted separately, and
  `check-published` must run in the memory repository's own CI. Cairnmind writes the files; committing,
  pushing and pulling the checkout stay with the person. A teammate imports with
  `import-published PATH/<identity> --project P`.
- **A server**: not available yet. It needs a receiving Cairnmind service with its own user and TLS
  (#72).

A checkout path is checked when it is chosen and again at every use: it must be an absolute path to
an existing directory inside a Git checkout, with no symbolic link on the way (as everywhere in memory
packages, and no control characters in it), and it may not be or lie inside Cairnmind's data
directory, a registered project's files, or any project's memory directory. Nor may it contain a
registered project root or share a Git repository with one: a package beside a project in its own
repository would sit outside every reserved directory, and whoever registers that repository's top
level would index it as code. A project with no publication identity yet that publishes into a
checkout already holding other packages is refused, as `FORMAT.json` refuses it in the repository:
import this project's package first (which binds it), or start a new one with
`memory publish PROJECT --new-package`.

Changing the carrier moves nothing. The command says where the earlier package is and how many files
it has, to import it first if teammates published there, then to publish to write the package at the
new place, and to remove the earlier files in a commit of one's own once the team reads from the new
place. `memory status` keeps naming it afterwards: under `published` it shows `other_package` (a
package left in the repository while a checkout is used) and `previous_carrier` with
`previous_package` (what the carrier before the last change still holds). The secrets gate (#171) runs on what is about to be written, before any carrier is written to.
