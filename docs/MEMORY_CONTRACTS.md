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
sanitized for publication. Published memory (below) passes a secrets gate before
it is written.

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

A record the archive brings as confirmed stays confirmed and is marked as
confirmed elsewhere (see "Only a person confirms a record" below): the dry run
and the import both report `confirmed_records` with the number of such records,
the names the package gives for who confirmed them, and how many name nobody.

An import is run only by a person. `POST /memory/import`, `/memory/import-published`
and `/memory/import-commit` refuse a request that names a session, except its dry
run, which writes nothing. An import that would put another text or status in
the place of a record that was confirmed or disputed on this machine is refused
and lists those records (`replaces_confirmed_here` in the dry run); a person
applies it in the memory view of the workspace (an archive; a published
directory or a commit cannot replace such records yet, `--take-published`
included, since 0.13.0). The import dialog states the number before the person
applies the import.

## How an extracted record was made

A record with `origin: automatic_extraction` carries `metadata.extractor`
beside `extracted_by`: `version` (the hand-bumped `EXTRACTOR_VERSION` of
`src/cairnmind/extraction_input.py`), `prompt_sha256` (the first 16 hex
characters of the sha256 over the extraction instructions, the schema and its
passage windows, the correction and the output budgets, and the requests built
from a few fixed sample messages, so that a change to how the prompt is put
together counts too) and `model` as name@digest. A model server that lists no
digest (one that is not Ollama), or one that does not answer in the time the job
has left, gives name@unknown, and extraction goes on. A very long model name is
cut and ends with a hash of the whole name. Code that only decides what to keep
from the reply, such as the evidence check, is not part of the hash. Such a
record without `extractor` was extracted before the stamp existed and counts as
version 0. Only the extraction worker sets the stamp: a model reply that names it
is rejected, and a client can neither set, change nor clear it; later writes,
such as a review, keep it. `cairnmind eval extraction` writes the same object
into its report. The stamp says how a record was made, not who trusts it, so a
package carries it in and out, unlike the local `extraction_source_id` mark
(#168) that a package never carries. Import keeps only a stamp the worker could
have written: well formed and on a record with `origin: automatic_extraction`;
anything else is dropped. An imported stamp is what the other computer recorded
and proves nothing about this one.

## Only a person confirms a record

A record has the status `confirmed` only because a person confirmed it. The rule
is one check in the store, in the function every local write of a record goes
through, and it holds for every route:

- A write stores `confirmed` when it is a person's act. The code of a person's
  route opens the act and names the note it has just written; the store reads
  that source itself and checks that it belongs to the project and is the kind
  of note that route writes. Nothing in a request, a record or a file opens an
  act, and an act opened while the write belongs to a session is refused.
  Since 0.13.0 the act also has to be proven (#214): the request that opened it
  carried the key of the open workspace, which the service checked before the
  route wrote anything ("What the person's key is" below). The store counts an
  act only then. An open act without the proof is refused with `Only a person can
  store a record as confirmed, and a person's act needs the person's workspace
  ...` and takes no confirmation away, like a session's write. Evaluation seed
  data needs no proof and is never a person.
- A write keeps `confirmed` when it does not change what was confirmed: the
  text, the kind, the key, the record it replaces, the quoted passage and every
  metadata field. A field is covered unless it is named as not covered, so a
  summary, a next step or any other text a writer puts beside a confirmed record
  counts like a changed text does. Not covered are the fields the
  store derives, the bookkeeping of conflict decisions, and on a task its
  progress: `metadata.state`, reported evidence, check runs, a declared scope, a
  proposed command and its placement in a plan. Of these only the path patterns
  of a task's scope are text a writer chooses.
- Any other write that asks for `confirmed` is refused and stores nothing. The
  error is `Only a person can store a record as confirmed, ...` (HTTP 400), and
  it says what to do: store the record as proposed and let the person confirm it.
- A write that is not a person's act never takes a person's confirmation away.
  On a record a person confirmed here, and on one confirmed before this rule
  (nothing says it was not a person's), such a write is refused and stores
  nothing when it names the record in `supersedes`, sets the status `disputed`,
  `proposed` or `superseded` on it, or changes anything the confirmation covers
  (the list above), with or without asking for a status. The error is
  `A person confirmed record <id> (<kind>), and this write is not a person's
  act: ... Only a person takes a confirmation away. Nothing was saved. ...`
  (HTTP 400, never 409: there is nothing to read again and retry), and it says
  what to do instead; see "Changing what a person confirmed" below. When the
  write would change what was confirmed, the error names what would differ:
  `text`, `key`, `supersedes` or `metadata.<field>`.
- Both refusals carry `"refusal": "confirmation"` beside `detail`. The built-in
  agent hands the model that text as the tool result `confirmation_refused`
  with its `message`, so the model does not take it for an outage or a conflict
  to retry; no other service text reaches the model.
- A write that is not a person's act and changes nothing of what was confirmed
  is stored (progress on a task, or a write that only cites a source), and it
  does not make the confirmation worth less. The record keeps every source it
  had, the note of the person's act among them: the write adds its sources and
  never replaces the list. A write that stores nothing new does not stamp its
  origin on the record either, so an external session cannot turn a record a
  person confirmed into one of degraded origin by writing it back unchanged.
  Progress an external session reports on a confirmed task is something that
  session wrote: the task then carries its origin mark, and stays confirmed.
- A confirmation that is not a person's verdict given here can still end
  without a person: one a package brought (`by: import`) and evaluation seed
  data. A write that changes what such a confirmation covers and asks for no
  status is stored as `proposed`; one that sets another status or replaces the
  record is stored too. Either leaves `confirmation.by = "lapsed"` with the new
  status, which stays on later versions until a person confirms or disputes the
  record.
- A person's act leaves no such mark: a dispute, a conflict decision.

### Changing what a person confirmed

A session, an MCP client, the built-in agent, `POST /remember` without a
session and automatic extraction cannot correct a record a person confirmed.
What they can do, and what the refusal tells them:

- **A fact or a decision.** Store what was found as a new proposed record under
  another key, with `metadata.possible_conflicts` naming the confirmed record.
  The pair then waits in the conflict inbox, where the person decides which one
  stands, keeps both or dismisses the alarm. Automatic extraction does exactly
  this when a transcript contradicts a confirmed finding under the same key: it
  never updates or replaces an existing record.
- **Any other kind** (constraint, preference, question). The conflict inbox
  shows facts and decisions only. Store a new proposed record and tell the
  person; they dispute the old record or confirm the new one in the inspector.
- **A task.** Progress is not part of what was confirmed and is stored as
  before: `metadata.state`, reported evidence, check runs, a declared scope, a
  proposed command. The task's text, criteria, dependencies and requirements
  are changed only by a person.
- **A plan step.** A plan save that changes or removes a step a person
  confirmed is refused as a whole, and the error names the step (`Step 'key' of
  this plan (task <id>) was confirmed by a person ...`). A save that leaves such
  steps exactly as they are is stored: it may add steps and change steps that
  are not confirmed. "Exactly" means every field of the stored step, as `plan
  show` returns it: criteria that are declared again without their
  `criterion_checks` drop that link, which is a change, and the error names it
  (`metadata.criterion_checks would differ from the confirmed version`). The
  person disputes the step in the inspector, after which a save, from a
  session or from `cairnmind plan save`, may rewrite it. No plan save is a
  person's act: the command line confirms nothing, in a terminal or not.
- **A plan's title, goal and constraints.** A person who confirms a plan
  confirms these with its steps (the audit hashes them), and later sessions
  read the constraints as the plan's rules. While at least one step still
  carries the confirmation given with the plan (`route: confirm_plan`), a save
  that is not a person's act and changes the title, the goal or the constraints
  is refused as a whole and says which of them (`A person confirmed this plan
  with its title, goal and constraints ...`). A save that leaves them out, or
  sends them unchanged, is stored. The person changes them by disputing the
  steps they confirmed with the plan in the inspector; once every one is
  disputed, nothing of it is confirmed and a save may change them. A step
  confirmed one by one in the inspector, or before the rule, does not cover the
  plan's own fields.
- **Adopting old tasks.** `cairnmind plan adopt` gives each adopted task a
  plan key, which changes what was confirmed. On a task a person confirmed, or
  one confirmed before the rule, it is refused whoever runs it (`Task '<key>'
  (task <id>) ... The person disputes the task in the inspector of the
  workspace`). The person disputes the task there, after which it can be
  adopted, and its step is proposed until they confirm the plan. Up to 0.10.0
  `task add` stored every task as confirmed, so the tasks of an old group all
  count as confirmed before the rule.
- **The checkpoint record** is the one exception. It is a slot that holds the
  latest reported state, not a statement, and every session writes it. If a
  person confirmed it in the inspector, the next `checkpoint` is still stored:
  the record is proposed again and carries the lapsed mark, and the version the
  person confirmed stays in the record's history with their mark. A
  `/remember` on the checkpoint record is a write like any other and is
  refused.

A person takes their own confirmation away by disputing the record in the
inspector, by a conflict decision, or by an import run from the workspace that
replaces what was confirmed or disputed here (`replace_confirmed`). A disputed record is no longer a
person's confirmation, and any writer may change it, its status included: a
session can set a record a person disputed back to `proposed`. Only a
confirmation is kept. The store records who confirmed a record and not who
disputed it, and a session may dispute a record itself, so it cannot tell a
person's dispute from a session's; an import is the one place that treats
every local dispute as a verdict (see "Confirmed elsewhere").

Nothing writes in the background over a person's confirmation: extraction
only adds records, verification writes check runs and never a task record, and
imports are a person's own command. A refused write is therefore always the
direct answer to the session, tool call or command that asked for it.

The person's routes, and what each ends in. Every one of them needs the key of
the open workspace (below) on top of the service token. Without it the route
answers 403 and says where the person acts, before anything is written, an
audit note included:

| Route | Ends in |
| --- | --- |
| Inspector review (`POST /inspector/records/{id}/review`) | A confirmed record, or a person's dispute. Its body has no session field, no MCP tool and no tool of the built-in agent calls it, and it writes the `review://` audit itself. |
| Conflict decision (`POST /inspector/conflicts/resolve`) | Superseding by a person; the finding chosen to stand becomes confirmed. |
| Plan confirmation (`POST /inspector/plans/{id}/confirm`) | A confirmed plan and its steps. The person confirms a plan in the inspector (Plans, then Confirm). |
| Record kind (`POST /inspector/record-kinds/accept` and `/withdraw`) | An accepted or withdrawn transcript record kind. |
| Workspace form (`POST /findings` without `session_id`) | The person's own finding, proposed or confirmed. With a `session_id` it is a session's finding, and asking for `confirmed` is refused before anything is stored. |
| An import that replaces a verdict given here (`POST /memory/import`, `/memory/import-published`, `/memory/import-commit` with `replace_confirmed`, not a dry run) | Records confirmed or disputed here replaced by a package. The refusal is `Replacing what a person confirmed or disputed on this machine needs the person's workspace ...`; the importer itself refuses too when the proof is missing. |

The command line confirms nothing (since 0.13.0, #214). A program that runs as
the user can open a pseudo-terminal and unset the session variables, so no test
of the terminal tells it from a person. `remember --confirmed`, `memory
confirm-plan`, `record-kind accept` and `record-kind withdraw` are refused with
exit code 2 and say that the person does it in the inspector of the workspace;
`remember --confirmed` stores nothing, not even a proposed record. `task add`
stores a proposed task and says so on standard error. `plan save` and `plan
adopt` are never a person's act: they are refused when they would change or
remove a step a person confirmed, and any other save is stored. `memory import`,
`memory import-published` and `memory import-commit` with `--replace-confirmed`
are refused by the service. Records that 0.12.0 or earlier confirmed on the
command line keep their mark and say `on the command line`.

Not a person's route, and unchanged: `POST /inspector/impact/verdict` and `POST
/inspector/records/{id}/release` write a person's note but confirm nothing and
change no record's status, so they stay reachable with the token (the release
decides what the secrets gate lets into published memory; #214 does not change
it). An import that only brings a package's confirmations (`by: import`) is
reachable with the token too: such a record is "confirmed elsewhere ... not
verified here" and is never a person's confirmation here.

### What the person's key is

When the workspace creates its tmux server it puts a random key into it, as a
user option (`@cairnmind_person_key`), through tmux's standard input: the key is
in no file, log, environment variable, command line or pane. The inspector and
the finding form read it from their own tmux server for each request that is a
person's act and send it, with the socket, in the headers `X-Cairnmind-Person`
and `X-Cairnmind-Workspace`. The broker passes the two headers on and keeps
nothing. The service accepts the socket only when it has the workspace's shape (a
directory named like the workspace's socket directory, private and the user's,
not a link, and a socket of the user; on Linux and WSL the directory's location
is not checked, because TMPDIR can differ between processes, on macOS it is),
reads the key from that tmux server and compares it in constant
time; only then does the request count as a person's, for that request alone.

The service keeps no state about the key. A service started by `cairnmind
serve`, by the MCP bridge or before the workspace works with a workspace opened
later, and a broker that restarts changes nothing. Closing the workspace ends
the key and opening it makes a new one. A workspace that an earlier version
started holds no key: the refusal says to stop it (`cairnmind workspace stop`)
and open it again. The service has to be able to run tmux (`tmux_executable`);
when it cannot, it refuses and says so. `cairnmind workspace status` reports
`person_key` as `present`, `absent` or `unavailable`, never the key.

Every other route is not a person's: MCP `remember`, `checkpoint` and
`save_plan`, the built-in agent's memory tools, `POST /remember` with or without
a session, a session's finding, automatic extraction. They store proposed
records, and none of them changes, replaces or lowers a record a person
confirmed. A plan a session saves has proposed steps until the person confirms
the plan; activating a plan does not confirm it.

What the rule does not claim. A person's act needs the key that the open
workspace keeps in its tmux server: the inspector and the finding form read it
there and send it with the request, and the service reads the same server before
it lets the request count as a person's. The service token alone reaches no
person's route, the command line confirms nothing, and no MCP tool or tool of
the built-in agent can send the key.

Cairnmind runs as the user, and so does every program a coding session starts.
Such a program can still act as the person, by going through the workspace or
around Cairnmind:

- use the workspace's tmux server, whose socket belongs to the user: read the
  key from it, run a program inside it, or type into the inspector (`tmux
  send-keys`); or start a workspace of its own in a pseudo-terminal and type
  into that;
- run a tmux server of its own in a directory named like the workspace's socket
  directory and put a key of its choosing into it (the service cannot tell that
  directory from the workspace's on Linux and WSL);
- use Cairnmind's library code, or the database engine with the password the
  user can read, and write any row, marks and audit notes included;
- restore a backup: `cairnmind restore` is the one documented command that
  still writes the rows of a backup as they are, marks included. With `cairnmind
  stop` and `cairnmind backup` it can bring back a confirmation a person has
  since disputed, by restoring an older genuine backup, and accepted record
  kinds with it (#214 continues with this);
- replace or change the installed program;
- read the memory of a Cairnmind process where the system lets a process read
  another process of the same user (Linux with `kernel.yama.ptrace_scope` 0, or
  as root).

Except restore, none of these is a documented command, route or tool, and a record confirmed
this way carries the confirmation line of the route it imitated; nothing in the
store tells it from the person's own act. What limits it is the client's
approval of commands: the built-in agent asks the person to approve each command
and shows it in full, and a coding client does so unless the person turned that
off.

Records confirmed with `cairnmind remember --confirmed` or `cairnmind task add`
before 0.13.0 keep their mark and say `on the command line`; nothing tells such
a confirmation from one a program made in a pseudo-terminal, and the person can
review them in the inspector.

### How a confirmation is recorded and shown

The store keeps `metadata.confirmation` on the record. It is derived: a client
can neither send it nor remove it, and a package never carries it.

| `by` | Meaning | Fields |
| --- | --- | --- |
| `person` | A person confirmed it on this machine. | `route`, `source_id` (the audit or note), `version`, `actor_name`, `at`; `marked_at_upgrade` when the upgrade step below gave it |
| `import` | Confirmed elsewhere and brought by a package. Never verified here. | `package` (`archive` or `published`), `where`, `confirmed_by` (claims: name, action, branch, commit, time), `imported_at`, `version` |
| `seed` | Written by `cairnmind eval` into its own store. Not a person. | `route`, `source_id`, `version` |
| `lapsed` | No longer confirmed: a write that was not a person's act changed what was confirmed or set another status. Written only over a confirmation that was not a person's verdict given here (an imported one, seed data, the checkpoint record); a store may also hold it from before that was so. Kept until a person gives a verdict again. | `confirmed_version`, `version`, `status` |

A confirmed record without the field was confirmed before this rule existed
(0.10.0 or earlier). Its status is kept, nothing is claimed about who confirmed
it, and it is shown as "confirmed before only a person could confirm; who
confirmed it was not recorded". The person can confirm such a record in the
inspector, once, and it is then theirs.

One step at upgrade (#215, `confirmation_upgrade.py`) marks such a record as a
person's when the store can show the act: a review audit (`review://`, origin
`local_user_review`) that covers the record as it stands (the hashes of its text,
of kind, key and replaced record and of the quoted passage when the audit has
them, and of a task's definition; and the record says what the first confirmed
revision citing the audit said, by `said`, which also covers every field an
audit of 0.10.0 does not hash), or the workspace form note of a finding the
form saved as confirmed whose record still says what was typed. Only a note
written on this machine counts: a source that a memory package brought (listed
in the import's receipt) does not, and neither does a command line note, which
hashes nothing. The mark is the one the route would have written, with the note
as `source_id`, the time of the act as `at` and `marked_at_upgrade`. The step
runs once when this installation opens the store as its writer, and again after
a restore; it records in `devbrain_meta` (`person_marks_before_rule`) that it
ran and how many records it marked and left, and changes no status, version,
source or evidence. Every other such record stays before the rule.

Where it is shown: the inspector's record view and each side of its conflict
view (line "Confirmation", in English and Polish), the inspector's list rows
(after the state: "confirmed elsewhere", "confirmed before the rule" or
"confirmation lapsed"; a person's own confirmation adds nothing), `memory show`
(line `confirmed:`, read from the package's own notes), the record lists of
`context` and the inspector (`confirmed_by`, and for an import the sentence in
`confirmation`), the search hits of `ask` as the answering model gets them
(`confirmed_by`, and the sentence for every confirmation that is not a person's
here), and each confirmed record of a resume packet (`confirmed_by`: `person`,
`import`, `before_rule` or `seed`). Names, branches, commits and paths in that
line come from memory or from a package and are escaped; they are never
translated, and the words that stand for a missing value are.

### Confirmed elsewhere

An imported record keeps the fact that it was confirmed, with who confirmed it
and where, and it never becomes this machine's own confirmation:

- The full archive: a record whose head is confirmed and is new here, or moves
  this store's head on, gets `by: import`, `package: archive`, the package's
  origin root as `where`, and as `confirmed_by` what the package's own notes
  say: a review audit that covers the record as it stands, or the workspace form
  note of a finding saved as confirmed. A record confirmed on the command line,
  or before audits existed, names nobody. A head that does not move keeps the
  mark this store gave it, so memory that returns to its own computer keeps the
  person's confirmations made there.
- Published memory: `by: import`, `package: published`, the file's path as
  `where`, and the file's `reviewed_by` up to that revision.
- An audit that arrived in a package does not cover a record here. The
  inspector's question "did a person on this machine cover this version" is
  answered by the mark, never by a source and never by a field a client can
  send.
- It is not verification: evidence states, check runs and freshness are what
  they were. A local person's confirm replaces the mark with their own.
- A later local write follows the rule above: progress keeps the imported
  confirmation, and a changed statement makes the record proposed again with
  the lapsed mark. An imported confirmation is not a verdict given here, so a
  session may change or replace such a record; once a person confirms it in the
  inspector it is theirs and is kept like any other.
- An import never rewrites a version, and it never replaces a verdict given
  here without the person's choice. A record counts as having one when it is
  confirmed or disputed and that version was not itself written by an import: a
  person's confirmation, a confirmation from before the rule (nothing says it
  was not a person's) and any dispute. When a package moves such a record on and
  leaves it confirmed and saying the same (progress on a task), the confirmation
  given here is kept. When it brings another text or status, the import is
  refused with the list, and a person applies it in the memory view of the
  workspace; the record is then confirmed elsewhere, like anything else the
  package brings. A record that diverged from a published file refuses the
  import as before, and `--take-published` names it; when that record was
  confirmed or disputed here, taking the published side replaces that verdict
  and is refused like any other replacement.
- Only a person runs an import, `--replace-confirmed` and `--take-published`. A
  request that names a session is refused, because an import brings records that
  count as confirmed, and a session can prepare the files it imports.

## Sensitivity classes

Every source and record has a sensitivity class, computed on each read by one
function (`cairnmind.sensitivity`, also `store.source_sensitivity` and
`store.record_sensitivity`) from what the store already holds: the source kind
and the origin mark the service writes. The class is stored nowhere, not in the
database and not in a package, so a client cannot set it, it cannot go stale,
and a row written before classes existed gets one without being rewritten or
migrated. An imported record is judged by the sources and origin mark the
package carries for it.

| Class | Sources |
| --- | --- |
| `project` | code, document, file, check_output, check_profile, verification, test_result, plan |
| `conversation` | transcript, transcript_unknown, agent_transcript, manual (`remember`), checkpoint, findings, and any other kind |
| `third_party` | any kind written by an external client's session (`origin_history: unarchived`) or degraded by a third-party tool result (`origin_evidence: degraded`) |

A record's class is the highest of the classes of the sources behind it
(project < conversation < third_party), and `conversation` when it has none or
one of them cannot be read. The sources its earlier versions cited count too
(`record_revisions`): an update replaces the sources and may keep the text, so
citing a project file never lowers the class of text that came from a
conversation. `cairnmind memory show` reads a package, which carries no
revisions, so there a record is judged by its current sources. A record or plan
whose own origin mark says an external session or a third-party result wrote it
is `third_party`. The class
says how private text is, not whether it is true: the origin mark and the
evidence state still say that.

The inspector returns the class as `sensitivity` for a source (`/inspector/sources`
and a source's detail, which `read_source` uses) and a record (`/inspector/records`,
which `memory_records` uses, and a record's detail). The terminal detail view and
`cairnmind memory show` print it. Which classes may go to a memory model server on
another computer is the setting `memory_remote_classes` (see [API.md](API.md#the-knowledge-agents-models)).

## Validation budgets and quality evaluation

Version 1 limits an individual JSON object to 16 MiB, a manifest to 32 MiB, and a
complete package to 512 MiB and one million object descriptors. A package exceeding
these limits fails explicitly before replacing the current manifest; data is
never silently truncated. Source text is not sent to a model by export or import.

`cairnmind memory status` shows the package a snapshot would write now against these limits
(`package_size`: rows, bytes, manifest bytes and the largest object, each with its limit and fraction) and
warns from 70% of any of them, so that publishing does not stop without notice (#191). The figures are an
estimate: rows are counted and bytes summed inside the database as UTF-8 bytes, as a snapshot counts them
(text in Japanese, Cyrillic or emoji is wider than its characters), nothing is exported and no row is read
into the service. If the engine stops the exact byte count (it limits regular expressions to about a second
per statement, and a stopped statement is repeated in parts first), `exact_bytes` is false, the figure is a
lower bound and the terminal says the package can be larger. The measurement of a large project takes
seconds to a minute (about 0.05 to 0.15 s per MB of text), so it runs in a thread: a status that cannot wait
shows the last figure with its age, or says the size is still being measured. The result is kept for a
minute, or 30 times as long as it took, so rows written meanwhile are not counted until it expires; the
terminal shows the age from 10 seconds. The manifest has its own, much smaller, limit: at about 240 bytes per
object descriptor it is reached near 130,000 rows, long before the one million. Status also splits the
bytes of the sources into current versions, replaced versions and agent transcripts.

Open question for the owner: should the package carry replaced source versions and agent transcripts at all?
Both are the bulk of a large package and exist for audit, not for recall; leaving them out would change the
format and what an import restores, so it is not done here. The split in `package_size.sources_bytes`
is the evidence for the decision.

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
the three static files are written once and are the same on every branch. A second worktree or clone
of the repository is a root of the same project only once it is registered with
`cairnmind project root PROJECT PATH`; until then publishing or importing there is refused because the
memory worktree is not a registered project root. Nothing else may be in
the directory: unknown entries, subdirectories and symbolic links are refused by name. The
directory is reserved like the archive: it is not indexed and does not count as project code, so
a commit that only changes memory changes no code fingerprint (it still moves HEAD). The publish
lock is in the data directory (`runtime/publish-<project>.lock`), never in the package.

`FORMAT.json` names the team's publication identity. A project's own ID becomes it at the first
publication; it is stored on the project as `publication_id`. Publishing into a directory whose
identity differs is refused ("belongs to X; import it first"); importing binds an unbound project,
and `--bind` replaces a binding explicitly (the way out when two branches each created
`FORMAT.json`, which Git shows as an add/add conflict in that one file).

## One directory per project in a repository

One repository can hold the projects of several teams, each registered at its own subdirectory
(`cairnmind project add team-a ~/src/monorepo/services/a`). Each publishes into its own root, never into the
repository's top level: `services/a/.memory/cairnmind-published/`. The paths in its source
references are relative to that root (`src/app.py`, not `services/a/src/app.py`), so the project
can move inside the repository. A project registered at the repository root works as before.

- The reserved directories (`.memory/cairnmind`, `.memory/devbrain` and
  `.memory/cairnmind-published`) are reserved at the root of every registered project. A project
  registered higher up, such as one at the repository root, neither indexes nor fingerprints the
  memory directories of the projects registered below it, and they are not in its scope or impact
  checks. Writing one project's memory, by `memory publish`, `memory snapshot` or an import, therefore
  makes no other project's checks or acknowledgements stale. This holds in every worktree of the
  repository: a project registered at `services/a` of the main checkout reserves
  `services/a/.memory/...` in a second worktree too. A directory of the same name where no project
  is registered is ordinary files.
- A project registered at a subdirectory is that directory: its code state, checks and scope cover
  the files below it, listed by Git relative to it, and nothing else. An uncommitted edit outside the
  directory, such as shared code in `services/common` or a configuration file at the repository root
  that a check reads, does not make that project's checks stale, and `extra_inputs` cannot name a file
  outside it. Only the next commit does: HEAD is the repository's, so a commit anywhere in the
  repository, a memory-only commit included, makes the checks of every project in it stale, as a
  commit always did. A project whose checks depend on code outside its directory should be registered
  at a directory that holds that code.
- A subdirectory the repository ignores (listed in a `.gitignore`), or one in which Git lists no file,
  has no fresh code state: Git would see no edit there. Its checks and scope stay unknown.
- Each project imports only its own directory. `memory import-published` refuses a
  `.memory/cairnmind-published` directory held by another local project's root, or by any other
  directory of the project's own repository, registered here or not. The path is resolved first,
  symbolic links in its parents included, and its name compared without case. This is a guard against
  mistakes inside one repository, not a proof of where a directory came from: a copy of another team's
  directory taken elsewhere is decided by its `FORMAT.json` and the binding alone, and on a machine
  where that team's records are not held, nothing else stops it. If Git does not answer while the guard
  runs, the import is refused.

A `CODEOWNERS` file lets each team review its own memory:

```
/services/a/.memory/cairnmind-published/  @example-org/team-a
/services/b/.memory/cairnmind-published/  @example-org/team-b
```

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
  definition are covered, so the kind, key and `supersedes` of such a revision rest on the file alone.
  Audits recorded since #172 also carry `evidence_sha256`, the hash of the passage the record quoted
  (CRLF normalised; null without one): a published quote must be that passage, while a withheld quote
  (null) is always allowed. A quote no audit hashes can be edited in a file unnoticed, so it never
  decides anchoring (see "Import on the main branch");
- a finding typed in the workspace form and saved as confirmed, at its first version only;
- after one of these, a task's progress (a state that differs only in `state`), or a mechanical
  supersession (status `superseded` with text, key, `supersedes` and definition unchanged) when what
  replaced the record is published too. A reader requires the replacement in the same tree: a record
  that names it in `supersedes`, or the task's plan whose last definition no longer lists it.

Only a person stores a record as confirmed at all (#177, "Only a person confirms a record" above):
session, MCP and agent writes are proposed, and a confirmed record an agent edits becomes proposed
again. `remember --confirmed` and `task add` in a person's terminal confirmed a record locally up to 0.12.0
(the command line confirms nothing now, #214), but they wrote no audit that hashes what was judged, so they are
not acts that travel. Such records,
and records confirmed before that rule existed, stay local until a person reviews them; the
inspector allows confirming a confirmed record again when no review, conflict decision, plan
confirmation or workspace form on this machine covers its current version.

A record that was imported is published again only as what its file said: its sources are the stubs
of the published sources, so the same audit, with the name of the person who confirmed it elsewhere,
is written back, byte for byte. This machine's name appears only on revisions a person here
reviewed. Audits record `actor_name` (`git config user.name` of the
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
- a release of the secrets gate: `{"at","kind":"release"}`; its passage is the release, and it is the
  only source that no revision cites (see [Secrets gate](#secrets-gate));
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
plan, their step key and their dependencies. The inspector (Plans, then Confirm) and its API confirm a plan
and all its active steps in one transaction with one audit; `memory confirm-plan` is refused (#214).

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
  importer sets and which later local edits keep; a confirmed one also carries `confirmation` with
  `by: import`: confirmed elsewhere, by whom the file says, never this machine's own (#177);
- the result and the dry run report `confirmed_records`: how many records the tree brings as
  confirmed, and the names its files give for who confirmed them;
- a record that was confirmed or disputed on this machine (and whose head no import wrote) is not
  fast-forwarded to another text or status without the person's choice: the import is refused with
  the paths, the dry run lists them as `replaces_confirmed_here`, and only a person in the workspace
  applies it (not the command line, since 0.13.0; `--take-published` on such a record counts the same).
  A fast-forward that leaves the record confirmed and saying the same keeps the confirmation
  given here (#177);
- the import itself, `--replace-confirmed` and `--take-published` are refused inside a coding
  session; only the dry run is open to one. `memory import-commit` follows the same rules;
- plans are applied after their tasks. A diverged plan refuses the import; `--take-published` takes
  records only, so the team resolves it in Git by keeping the definition the importing store holds;
  IDs that belong to another local project are refused, as for
  the archive. A repeat import of the same tree writes nothing, and `--dry-run` writes nothing.

Laptop import never deletes: a file that is absent says nothing (the main-branch import below reports
it as withdrawn and still changes nothing). After a published import, a full
archive from another machine no longer imports into the same project (its baseline differs): the
archive is one person's backup.

## Trust

Nothing imported is verified. No check run is published, so imported tasks are never fresh; reported
flags are never published; imported state `done` is historical progress. An imported source is not
anchored by the import: a record resting only on such sources is `unverified` with
`unverified_by: published_elsewhere`, and an imported review neither anchors a record nor counts as
this machine's own review. Only `memory import-commit` (below) can anchor an imported file source, by
finding its passage in the code of a commit, and that is a fact about this commit's code, not
verification. No field in a file can raise trust above "reviewed elsewhere, not verified here".

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

## Reading a change

`memory diff BASE HEAD [--project-root DIR] [--package ID] [--markdown] [--max-chars N] [--json]` compares the
published memory of two commits with no store, service, network or model. It reads the directory of
each commit with `git ls-tree` and `git cat-file` (nothing is checked out or written); the project
root is the directory it runs on, so a project in a subdirectory of a repository reads its own
directory. The entries of a commit are held to the rules of a directory on disk (only the static
files and `<kind>/<uuid>.md`, no links or submodules, the budgets above), and the head must pass every
check of `check-published`, the secrets scan included with the head commit's
`.cairnmind/publication.toml`. In a checkout of a separate memory repository (#173), `--package ID`
compares the package `<project root>/<ID>/` of that publication identity instead. Records are compared
by ID:

- a new file is `added` (a new task is `opened`, or `closed` when it arrives done);
- new revisions of a known file are `changed`, `superseded` or `disputed`; a task whose state becomes
  `done` is `closed`, one that leaves `done` is `opened` again; a plan with a new definition is
  `redefined`;
- a removed file is `withdrawn`;
- a head file that keeps only the first of the base's revisions, byte for byte, is `reverted` (a
  `git revert` of the change that appended the rest; the head still imports, and a store that has the
  dropped revisions stays ahead), shown with its text now and before, and without the dropped
  revisions' reviewers;
- a head file whose revisions differ from the base's is an error: published revisions are only ever
  appended.

The base is only compared with: a base entry that would not be accepted (a stray file, say) is left
out and counted, and the rest of the base is still compared. When the base's directory cannot be read
at all (a link in its place, or over the package budget), the output says so and every head record
shows as added. A link, submodule or file in place of `.memory` or of the published directory is
refused in a head, as it is on disk.

Each entry carries the text it has now (and the text before, when it changed), the person and branch
its file says acted on it (a claim, never verified), and its quoted passage with its path and lines,
or its sources when it quotes nothing. Exit 0 when the head would import, 1 when it would not (the
problems are listed with their paths), 2 when a commit cannot be read.

The text form passes every value from a file through the same terminal escaping as the rest of the
product. The Markdown form is a pull request comment. Text from records, quotes and error listings
goes into four-space literal blocks, where Markdown renders nothing; short values (names, branches,
paths) go into code spans whose fence is longer than any run of backticks in them. Every heading,
sentence and HTML tag around them is written by Cairnmind, so a record cannot add markup, mentions,
links, headings or a copy of the comment's marker. Blocks are cut at 40 lines or 4,000 characters,
the first entries are open, later ones are folded into one `<details>`, the rest are only counted,
and the comment is at most 60,000 characters (`--max-chars` lowers it). Its first line is the marker
`<!-- cairnmind-memory-diff -->`, and its second says `changed`, `unchanged` or `refused`.

## The pull request workflow

`.github/workflows/memory.yml` runs on every pull request of this repository and can be copied as it
is. It checks out the merge commit with its history, runs `memory diff` from the base commit to it,
writes the Markdown to the job summary and, through `scripts/memory_pr_comment.sh`, posts it as one
comment or updates the comment it posted before (found by the marker and the workflow's own account).
A pull request that changes no published memory gets no comment, unless one exists to update. The
job fails when the head would not import (conflict markers, a broken or hand-edited file), after the
comment says why. It needs no secret, ArcadeDB or model: the default token, with permission to write
pull request comments, is enough. A pull request from a fork gets a read-only token from GitHub, so
the comment is skipped there and only the check and the job summary remain. It is not a required
check, and the required jobs `lint`, `fast-tests` and `full-tests` are unchanged.

## Import on the main branch

`memory import-commit REVISION --project P [--worktree W] [--dry-run] [--bind]` is what a CI job or a
server runs after a merge. It reads the published directory of the commit from the project's
repository (nothing is checked out), imports it exactly as `import-published` does, and in the same
transaction compares every imported file source with the project's files at that commit:

- whether the file is still at its path, and whether its content is the one that was published;
- whether each passage a record quotes from it is still there.

The result is kept on the source stub as `rechecked` (`file`: `same`, `changed` or `gone`; the hash of
each quoted passage and whether it was found; the path; the commit where this result was first
seen) and changes only when the result does. Evidence reads it: a record whose quoted passage is
still there is `anchored`, one whose passage or file is gone is `orphaned` (shown so in the command's
output and the inspector), and a record without a quote follows its file (`anchored` when it is the
same, `evidence_replaced` when it changed). Once a file source was compared, a conversation or note
seen elsewhere, which can never be compared, no longer keeps the record `unverified`; a record
resting only on such statements stays `unverified`. A full local source with the same ID (the
author's own machine) is left to local indexing. Files over 32 MiB are left unchecked.

Only a quote whose covering review hashed it (`evidence_sha256`) is compared; a record whose quote no
audit hashes is judged by its file, as if it quoted nothing. A passage is compared with the project's
own files only: paths inside its reserved memory directories (the published files, which always hold
their own quotes, and the archive), and inside those of the projects registered below it (#174), read
as gone. A directory of the same name anywhere else is ordinary files, as indexing reads it.

The commit is held to the secrets gate (#171) like a directory: the commit's own
`.cairnmind/publication.toml` and this machine's registered secrets. A project whose carrier is a
separate repository (#173) is refused with the command that imports its package instead: the commits
of its own repository hold none of its memory, and those of the memory repository hold no code to
compare passages with.

The output adds `commit`, `rechecked` (the sources whose result changed), `unchecked`, `evidence`
(the imported records by state, with path and first line) and `withdrawn`: records imported earlier
whose file is no longer in the commit. They are reported, never deleted or changed. A repeat import of
the same commit writes nothing; a later commit that changes only code writes only the results that
changed. Anchoring is never verification: no check run, flag or review is written, and the reviewer a
file names stays a claim. No model is called.

## Publishing

`memory publish PROJECT [--worktree W] [--preview]` reads the store in one transaction, selects and
renders, refuses when the directory has revisions this store does not have ("import first"), when a
source is published differently, or when the result would not be a valid tree, and then writes only
the files whose bytes changed. Rendering uses no clock and no Git call, so publishing twice with
nothing new changes no file. The output lists the files written and unchanged, what is held and why,
and the files and bytes of the whole package. It never stages, commits or pushes. `memory status`
shows the destination, whether `FORMAT.json` exists and whose it is, the project's publication ID and
the file count. `memory show-published DIR` reads a directory as text. Every publication, its
preview included, first passes the secrets gate below; the earlier switch
`CAIRNMIND_ENABLE_PUBLICATION` is no longer needed and no longer read.

## Secrets gate

Git history is permanent, so publication refuses instead of masking: a masked record would still
look fine to a reviewer, and whatever the masks do not know would pass. Before any file is written,
every text a published file would carry is scanned: each revision's text, key, quote, step key,
profiles, acceptance criteria, criterion links and scope; each plan definition's title, goal,
constraints and step keys; every passage (workspace notes, review audits, releases); and every
string of a source reference. One hit refuses the whole publication with a non-zero exit. The message
names each file (and so the record), the field and the kind of hit, never any part of the value or
its hash. Files already in the directory that this store does not write (a teammate's records, or
records held here) are scanned too, but a hit there does not refuse: it is listed under `not_ours`,
with the advice to fix or release it where it was written, and the file is left as it is. So a
teammate's file never blocks this store's own records or a release. This store's own records, on the
other hand, are scanned with the rules in force now, also those published before: a rule turned on
after a publish (for example `detect = ["email"]`) refuses the next publish until the value is removed
from the record or the record is released, just as CI would refuse that file.
Format characters (Unicode category Cf, such as zero-width spaces and soft hyphens) are removed
before the scan, so a secret split by an invisible character is still found.

What is detected:

- the secrets registered with Cairnmind on the publishing machine: the service token and the ArcadeDB
  password (the values the agent journal redacts), the API keys saved in the credential store (a
  key entered for one session only, without saving it, lives in the cockpit and is not registered
  with the memory service that publishes), the values behind the `env:` references of the saved
  connection profiles and memory model profiles, and the values of the credential variables the runtime keeps from child
  processes (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GH_TOKEN`, `GITHUB_TOKEN` and the others of
  `api_credentials.credential_environment_names`, and any variable ending in `_API_KEY`,
  `_AUTH_TOKEN` or `_GITHUB_TOKEN`), each at least 8 characters, found exactly as they are. If the
  saved keys or profiles cannot be read, publication refuses rather than going on without them. Ingestion
  replaces the same registered secrets, private key blocks and key shapes in conversation text before
  it is indexed (`secret_mask`, [API.md](API.md#keys-and-environment-variables));
- private key blocks (`-----BEGIN ... PRIVATE KEY-----`, OpenSSH, PGP);
- key and token shapes: AWS access key IDs, GitHub, GitLab, Slack, npm and Hugging Face tokens,
  OpenAI and Anthropic keys, Google API keys, Stripe live keys, Azure storage account keys, JSON
  Web Tokens, and bearer or basic credentials after `Bearer` or `Authorization:`;
- connection strings with a password: `scheme://user:password@host`, and `Password=` or `Pwd=` in a
  string that also names a server, host, data source, user or database. Placeholders such as
  `${DB_PASSWORD}`, `<password>` or `***` are not hits;
- long strings that look random: 32 characters or more of letters, digits and `+/_-=`, mixing at
  least two of lower case, upper case and digits, with at least 4 bits of entropy per character, and
  not made only of word-like pieces. Hex digests and commit hashes, UUIDs, `objects/<digest>` paths,
  file paths and snake_case or CamelCase names are therefore not hits; on this repository's own
  documentation, example memory and source files the rule finds nothing outside test fixtures that
  hold deliberate fake secrets. A random string of 32 characters is missed about once in 500 draws,
  and one of 40 or more almost never;
- personal data, only when the project turns it on, in `.cairnmind/publication.toml`:

  ```toml
  [personal]
  detect = ["email", "phone"]
  patterns = [{name = "employee-id", regex = "EMP-[0-9]{6}"}]
  ```

  E-mail addresses and phone numbers are off by default, because records about who owns, reviews or
  is contacted for something legitimately name people, and a record about a person's role is not a
  leak. A project that must keep them out turns them on; its own patterns (at most 20, each a
  regular expression of at most 200 characters, reported as `personal:<name>`) are for identifiers
  such as employee or customer numbers. A file that cannot be read refuses publication.

What is not detected, and is never promised: a secret of a shape not listed above, a short or
low-entropy password outside a connection string, a registered secret that is split, encoded or
changed, a key that is not registered on the machine that publishes, private facts written in
plain words (names, health, salaries, customer details), and timestamps that reveal working hours.
Git authorship of a review (`actor_name`) is published by design. The scan reduces what can pass; it
does not promise that nothing private can.

A person can release one record after reading it, when a hit is a false alarm (an example key from
a provider's documentation, a test value): `cairnmind memory release PROJECT RECORD --reason TEXT`,
refused inside a coding session, or `POST /inspector/records/{id}/release`. The release covers the
record's current hits only: it is a source of its own, `release://` with origin
`local_user_release`, holding `{"action":"release","actor","actor_name","at","branch","commit",
"hits":[{"field","kind","value_sha256"}],"reason","record_id"}`. It is published in the record's
file under Sources as `Source <id>: {"at":...,"kind":"release"}` with that JSON as its passage, so the
reason is visible in the diff, and it is cited by no revision. A hit is let through only when a
release in the same file names its field, its kind and the SHA-256 of its exact value; a new value
needs a new release. A registered secret is never released, a release needs a reason that itself
holds no hit, and a release in a plan file, or one that names another record, refuses the file. An
importer keeps the release as a stub and publishes it again with the record.

A release is a claim the file makes, like a review audit: nothing ties it to a person's act that a
check can see. Someone writing a file by hand can add a release for their own value, of any kind
but a registered secret, and `check-published` accepts it. That is why `check-published` prints
every release in the tree (the file, the name it carries and what it lets through) when the tree is
valid, so that the pull request's reviewer reads each one; accountability comes from git authorship
and review, as for audits.

The same scan runs without Cairnmind's store in CI: `cairnmind memory check-published DIR` refuses a
tree with an unreleased hit, naming the file, the field and the kind, and exits 1. It applies the
project's `.cairnmind/publication.toml` from the checkout that holds DIR at
`.memory/cairnmind-published` (or the file named by `--config`). CI has no registered secrets: those
exist only on the machine that publishes, so a pull request whose files were written by hand is
checked for every other kind. An example workflow is
[`docs/examples/published-memory-check.yml`](examples/published-memory-check.yml); it runs on the
pull request's merge commit. `memory import-published` refuses a tree with an unreleased hit too,
applying the same `.cairnmind/publication.toml` of the checkout and, on the laptop, this machine's
registered secrets, so that nothing comes in that this store would refuse to publish. A refused
entry whose file name itself holds a secret is not named by that name.

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
