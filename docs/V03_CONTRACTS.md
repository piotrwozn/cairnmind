> Historical: these are the 0.3 implementation contracts. Module names such as `tui.py` refer to that release; current behaviour is described in [CONTRACTS.md](CONTRACTS.md) and the other guides in this directory.

# DevBrain 0.3 implementation contracts

The accepted scope: atomic first-class plans, strict launcher resume preflight+actual
model ACK, CLI-only named check runner in current Git worktree, memory evaluation.
No task leases or hierarchical architecture summaries in this increment.

## Ownership

- storage agent: store.py, tests/test_store.py + tests/test_migrations.py
- plans agent: plans.py, tests/test_plans.py
- verification agent: verification.py, tests/test_verification.py
- sessions agent: sessions.py, tests/test_sessions.py
- integrations agent: integrations.py,mcp_server.py, tests/test_integrations.py
- evaluation/UI/docs agent: evaluation.py,tui.py, tests/test_evaluation.py,test_tui.py, README/docs/installer
- root: service.py,cli.py,config.py,projectmetadata,root API/client tests and integration.

## Storage and migration

SCHEMA_VERSION 2, export format_version 3, unchanged backup envelope version1. Add
DOCUMENT_TYPES: plans,plan_revisions,plan_requests,worktree_states,sessions,
resume_packets,resume_acks,session_events,check_runs. All id STRING unique, project_id scoped.
Old live schema1 migrates additively with backup/versionlast/restartability.
Old backups payload2 checksum validated BEFORE conversion adding empty new tables.
Historical flags remain reported/legacy, never synthesized machine runs. Legacy
metadata.plan_id remains an arbitrary group label; new managed plans use
metadata.plan_ref (UUID), metadata.step_key, metadata.required_profiles(list[str]).

Store._remember gains internal keyword `managed_batch=False`. New managed task creation
and definition/membership edits only through PlanManager (managed_batch=True).
Generic managed-task progress writes require record.expected_plan_version and invoke
plans.note_task_mutation(store,tx,task,expected_plan_version), advancing plan revision.
PlanManager uses Store._remember(... managed_batch=True) for batch, one planrevision.
Task `done` explicit assertion/newcompletion validates verification.validate_completion
for requires_test. Existing done preserved when unrelated/definition metadata changes:
verification freshness is separate, not automatically reopening historical progress.
Store._validate_task structure/dependency/test/review gate types retained. Legacy
source flags alone no longer qualify for NEW test-gated completion. Review/acceptance
remain explicit separate evidence and are not implied by test exit0.

check_runs immutable: only verification.py internal runner may insert it. No generic
remember/checkpoint/model payload may create or amend machine evidence. CLI/API
readviews may derive verification_status fresh|stale|reported|unknown, not stored
counterfeit status from clients.

## Plans (PlanManager(store))

- save(project_id, plan:dict, steps:list[dict], idempotency_key:str,
       expected_version:int|None=None, task_versions:dict|None=None,
       worktree:str|None=None, activate:bool=False) -> {plan,tasks,replayed}
- get(project_id,plan_id)->{plan,tasks}; list(project_id)->list[dict]
- activate(project_id,plan_id,worktree=None)->dict worktree_state
- next_task(project_id,plan_id=None,worktree=None)->{plan,task,reason,blocked}
- adopt_legacy(project_id,group:str,...)->same save result (explicit operation)
- note_task_mutation(store,tx,task:dict,expected_plan_version:int)->None

Plan fields id,project_id,version,title,goal,constraints(list[str]),step_ids(list IDs),
created_at,updated_at. Input step {key,content,depends_on:[step keys],metadata:{...}}
with optional existing `id`. Allocate stable IDs, validate FINAL DAG, then commit all
steps + header + history atomically. Reused request key samecanonicalinput returns
original result; differentpayload rejects. New step state todo; batch cannot inject
verified flags/done into new steps. Existing progress preserved. Key removals archive
steps/history, not destructive deletion. Scope worktree must be registered.

## Verification (module functions)

- capture_code_state(store,project_id,worktree=None,extra_inputs=None)->dict
  {status:fresh|unknown,worktree,head,branch,fingerprint,manifest_hash,unknowns}
  content hashes tracked+nonignoreduntracked, modes,deletions,symlink targets; no RAG
  2MiB/exclusion cap. No file content or secret env dumps persisted. Additional ignored
  inputs explicitly configured in profile; outputs outside worktree.
- task_definition_hash(task:dict)->str (content,criteria,depends_on,gates,required_profiles,
  plan_context_hash; NOT task status/version)
- load_profiles(store,project_id,worktree=None)->dict profilemap
- run_check(store,settings,project_id,profile_id,task_id,worktree=None)->dict run
- verification_status(store,task,worktree=None)->dict {status,reasons,run_ids}
- validate_completion(store,tx,project_id,task:dict)->None raises ValueError unless
  current successful fresh machine runs cover required_profiles for task definition.

`.cairnmind/checks.toml`: [checks.<name>] argv=[executable,args...], cwd=".",
timeout_seconds default600, output_limit_bytes default10485760, extra_inputs=[]
(registeredroot-relative filepaths/globs). Commands use subprocess argv shell=False,
process group timeout/cancel/overflow, bounded persisted stdout/stderr sources.
CLI-only execution via native client/user permissions: `devbrain check run PROJECT
PROFILE --task ID [--worktree ROOT]`. No MCP/raw-command runner. CodeState captured
before+after, mismatches/unsupported/unstable => stale/ineligible even exit0.
Immutable run binds profilehash,taskdefinitionhash,codebefore/after,exitstatus+sources.
Review/acceptance separate. metadata.check_run_ids links results; validation also can
find matching scoped runs by task_id, never trusts user booleans.

## Sessions (SessionManager(store,settings))

- prepare(project_id,client,worktree=None,session_id=None,reason='startup',timeout=30)->dict
  {session_id,epoch,status:ready|partial|unavailable,packet,unknowns}
- resume_context(session_id)->same result (refresh stale epoch as needed)
- ack(session_id,packet_id,packet_hash,readback:dict)->dict
- require_ack(session_id,packet_id=None)->None (raises resume_required on stale/missing)
- note_event(payload:dict,client:str)->None (service drains spool, never DB in hook)
- status(session_id)->dict

Packet immutable id/hash, project/worktree/client/session/epoch, current scoped
activeplan/task versions, goal/constraints/nextaction/blockers, code state, bounded
records+source refs, archivewatermarks. Goal is committed DB state at explicit
captured horizon, no claim to every future transcript byte. First emptyproject ready
with nohistory explicit. Complete only when known required transcript ranges durably
archived through EOF; pending/unknownschema/missingrange =>partial. Archive,indexing,
extraction statuses separate; never waitGPU merely to produce deterministic packet.
Launcher blocks start on nonready; afterready startsnativeCLI bootstrap and real MCP
ack required for session writes. ACK validates packet ID/hash and readback active
plan/task IDs+versions. Actual readback, not automatic ACK on HTTPcontext delivery.
New epoch on resume/compact; relevant versions/scope/code change invalidates ACK,
not each technical transcriptline. Raw nativeCLI operations retain nativecontrols;
no complete hardtoolgate claimed.

Clients: codex, claude (launcher), api, copilot (own runtime) and external (any MCP
client whose conversation cannot be read: Cursor, Cline, Zed). No product name is a
client and nothing is detected by guessing. An external session is EVIDENCE-DEGRADED:
there is no archived conversation to quote or verify against. Its packet adds
history:'unarchived', evidence:'degraded', a first warning saying so, no_history:false
and archive.scope naming the unarchived own conversation; native packets are unchanged
byte for byte. archive.status still covers every other client's captured transcript in
the worktree, so an unknown or unarchived native record keeps it partial. Same ready
packet, readback ACK and require_ack as any client. status() adds history/evidence.
Writes in an external session (records, sources, plans) carry server-derived
origin_client/origin_session_id/origin_history/origin_evidence, set in write_scoped
from the session row; clients can neither set nor clear them, and they stay on every
later version. Evidence state 'degraded' (never 'anchored') for such records and for
records resting only on such sources; check evidence still comes only from local runs.

## Integrations and HTTP (root owns server/CLI)

run_client creates/prepares verified session BEFORE subprocess; sets inherited
CAIRNMIND_SESSION_ID, CAIRNMIND_PACKET_ID and, with the same values, the earlier DEVBRAIN_SESSION_ID, DEVBRAIN_PACKET_ID; native MCP bridges bind to that session.
SessionStart spools logicalsession/nativeID, returns EXACT preparedpacket (no silent
replacement). End hooks only fast spool enriched known transcript EOF where possible.
Drain durably queues archival work and invokes SessionManager.note_event immediately,
before removing the spool file; archive completion is independently proven later.
No DB/network in endhooks.
API /remember,/checkpoint,/plans/save from session-bound clients require_ack; no-session
manualCLI/admin operations stay available for repair. Bootstrap read/ack always allowed.

POST /plans/save {project,plan,steps,idempotency_key,expected_version?,task_versions?,worktree?,activate?,session_id?}
POST /plans/next {project,plan_id?,worktree?}; GET /plans?project=...
POST /sessions/prepare {project,client,worktree?,session_id?,reason?} (client: codex|claude|external|api|copilot)
POST /sessions/context {session_id}; POST /sessions/ack {session_id,packet_id,packet_hash,readback}
GET /sessions/{id}; GET /checks?project=...&task_id=...
Existing /context enhanced current plan/scope/verification, old public tools retained.
MCP new save_plan,next_task,resume_context,ack_resume,check_status (read-only checks).
ServiceClient autoadds bound sessionid for writes and scoped ask/context/next/check reads;
rejects request session mismatch. Direct CLI workflow reads inherit the same worktree.
`cairnmind mcp --client external [--project P] [--worktree W]` binds nothing at start;
its first resume_context prepares one external session for the process (refused when
launcher session/packet variables are set), later calls refresh that session.
The answer cache includes complete code fingerprints, normalized profile hashes and
explicit ignored check inputs; unknown code state prevents cache reuse.
No arbitrary execution endpoint/MCP tool.

## 0.3.9 reliability additions

Schema version remains 2. Optional job diagnostics and retry audit fields are
included in existing job export/backup; old job rows remain readable. ASK keeps
its existing answer/status fields and adds safe diagnostics without raw prompts
or model output. Model termination/token data is unknown when not reported.
The single tool-free ASK format repair uses compact already-inspected evidence;
source scope, literal quotes, freshness and caller answer contracts still apply.
Extraction uses a bounded output schema and at most one format correction within
the existing 30-second inference deadline. It validates before atomically writing
accepted proposed records and graph effects; source archives/cursors are not reset.

GET /jobs?project=...&state=...&kind=...&limit=...
GET /jobs/{id}?project=...
POST /jobs/{id}/retry {project,expected_attempts,session_id?,packet_id?}
All operations are project-scoped; retry requires failed state and exact attempt
readback in one transaction, preserves prior failure audit, and obeys session ACK.
Legacy global retry remains an explicit admin action, never a models-pull side effect.
Status reads stored coverage proof without preparing/reparsing a session. A legacy
path/client-only import job cannot acquire a fabricated generation or horizon.
Unmatched coverage is unknown; failed-job history remains visible independently.
CLI help/status/jobs diagnostics do not initialize configuration or start a service.

## 0.3.10 extraction grounding

Extraction generation receives the scoped visible source without existing-record
content. A per-source schema constrains generated evidence to bounded literal
source passages; static shape and independent substring/provenance validation
still reject invalid backend output. Atomic commit performs fresh deduplication.
The existing one format correction, token budget and 30-second deadline remain.
An empty record list is valid when no durable supported fact is identified.
Optional diagnostics.rejection_counts adds bounded overlapping counters for
blank_content, blank_evidence, quote_absent_source and reserved_metadata.

POST /jobs/{id}/retry additionally accepts include_omissions:boolean=false.
The opt-in permits only completed extract jobs with code completed_with_omissions,
proposal_rejected and integer counts proving every nonempty candidate batch was
skipped, with zero accepted/written records. Completed success, genuine empty
output, deduplication and partially written batches remain ineligible. The same
project/ACK/attempt CAS and immutable attempt audit apply; job ID is unchanged.
CLI exposes --include-omissions; absent flag preserves the previous request body.

## Evaluation

`devbrain eval run` isolated temporaryfixtureGitworktree+separateArcadeDB namespace.
20 golden scenarios score deterministic facts/IDs/versions/scopes/current decisions,
unknown abstention, provenance, status/readback. Local Ollama actual optionalavailability
unavailable=>not_run neverpass. `--clients` explicit end-to-end native CLI current
savedlogins/readOnly fixturecontext; no paidAPI configured/fallback. Raw clienttools
cannot mutate userprojects. Report factsaccuracy>=90%, zero criticalstate/isolation
errors, latency/contextsize and tokenusage when provider reports it (missing !=zero).
Store evalreports outsideprojects, don't promote synthetic answers as memoryevidence.
