/**
 * Strict 1:1 TypeScript contracts matching `src/cairnmind/ui_server.py` and `src/cairnmind/ui_cockpit.py`.
 * Designed behind the thin `ui/src/api/` adapter boundary for future Rust multi-project backend compatibility.
 */

export type SemanticStatus = 'VERIFIED' | 'STALE' | 'REJECTED' | 'ACTIVE' | 'UNVERIFIED';

export type SessionSyncStatus = 'SYNCED' | 'OUTDATED_ACK';

export type TestGateStatus = 'PASS' | 'FAIL' | 'STALE_CODE' | 'MISSING';

export type DriftKind =
  | 'MATCH'
  | 'FILE_DRIFT_QUOTE_INTACT'
  | 'QUOTE_MUTATED'
  | 'FILE_MISSING';

export type AuditVerdict = 'ACCEPTED' | 'REJECTED';

export type AuditReasonCode =
  | 'OK'
  | 'OCC_STALE_RESUME_PACKET'
  | 'VERBATIM_QUOTE_NOT_FOUND'
  | 'FILE_FINGERPRINT_MISMATCH'
  | 'TEST_GATE_UNVERIFIED';

export type UiEventType =
  | 'state.updated'
  | 'session.ack'
  | 'evidence.stale'
  | 'mcp.write.accepted'
  | 'mcp.write.rejected'
  | 'repo_fingerprint_changed'
  | 'reindex.progress'
  | 'plan.conflict';

export type OriginCategory = 'ENGINE_VERIFIED' | 'HUMAN_REPO' | 'MCP_DEGRADED';

export type OriginEvidence = 'verified' | 'human' | 'repo_ci' | 'degraded';

export type OriginFilter = 'ALL' | 'HUMAN' | 'AGENT' | 'DEGRADED';

export interface OriginMetadata {
  category: OriginCategory;
  origin_evidence: OriginEvidence;
  origin_client?: string | null;
  source_label?: string | null;
  detail?: string | null;
}

export interface UiCapabilities {
  writable: boolean;
  read_only_reason?: string | null;
  ask_available: boolean;
  llm_configured: boolean;
  multi_project: boolean;
  supported_export_modes: string[];
}

export interface ProjectMetadata {
  id: string;
  name: string;
  root: string;
  branch: string | null;
  head: string | null;
  fingerprint: string | null;
  mode: 'live' | 'mock';
}

export interface AgentSession {
  id: string;
  client: string;
  worktree: string;
  acknowledged_rev: number;
  current_state_rev: number;
  sync_status: SessionSyncStatus;
  packet_id: string;
  packet_hash: string;
  last_seen_at: string;
  degraded: boolean;
  dropped: boolean;
}

export interface TestGateInfo {
  profile_name: string;
  status: TestGateStatus;
  sha256_short: string | null;
  run_id: string | null;
  ran_at: string | null;
}

export interface PlanStep {
  key: string;
  title: string;
  depends_on: string[];
  scope: string[];
  acceptance_criteria: string[];
}

export type PlanStatus = 'draft' | 'active' | 'archived';

export interface PlanSummary {
  id: string;
  title: string;
  goal: string;
  status: PlanStatus;
  version: number;
  external_ref?: string | null;
  constraints: string[];
  linked_files: string[];
  file_fingerprints?: Record<string, string>;
  steps: PlanStep[];
  origin?: OriginMetadata;
  created_at: string;
  updated_at: string;
}

export interface PlanTask {
  id: string;
  step_key: string;
  title: string;
  plan_id: string;
  plan_title: string;
  status: string;
  state: string;
  version: number;
  evidence_verified: number;
  evidence_total: number;
  evidence_status: 'VERIFIED' | 'STALE' | 'UNVERIFIED';
  test_gate: TestGateInfo;
  updated_at: string;
  claim_ids: string[];
  source_paths: string[];
  depends_on: string[];
  acceptance_criteria: string[];
  origin?: OriginMetadata;
  external_ref?: string | null;
}

export interface EvidenceClaim {
  id: string;
  kind: string;
  key: string;
  title: string;
  content: string;
  status: string;
  provenance_status: 'VERIFIED' | 'STALE' | 'CONFLICT' | 'UNVERIFIED';
  source_path: string;
  line_start: number;
  line_end: number;
  verbatim_quote: string;
  recorded_sha256: string;
  current_disk_sha256: string;
  quote_survives: boolean | null;
  drift_kind: DriftKind;
  updated_at: string;
  task_ids: string[];
  version: number;
  origin?: OriginMetadata;
}

export type GraphCategory = 'source_file' | 'evidence' | 'claim' | 'task';

export interface GraphNodeData {
  id: string;
  column: 0 | 1 | 2 | 3;
  category: GraphCategory;
  label: string;
  subtitle: string;
  status: string;
  ref_id: string;
  meta: Record<string, string | number | boolean | null>;
}

export interface GraphEdgeData {
  id: string;
  source: string;
  target: string;
  kind: string;
  status: string;
}

export interface DependencyGraph {
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
}

export interface GuardrailAuditEntry {
  id: string;
  timestamp: string;
  session_id: string;
  client: string;
  mcp_tool: string;
  verdict: AuditVerdict;
  reason_code: AuditReasonCode;
  message: string;
  target_id: string | null;
  arguments: Record<string, unknown>;
  violation_detail: Record<string, unknown>;
  origin?: OriginMetadata;
}

export interface DriftSummary {
  drifted_files_count: number;
  stale_tests_count: number;
  pending_decisions_count: number;
  last_approved_plan_id: string | null;
  last_approved_plan_version: number | null;
}

export interface UiSnapshot {
  project: ProjectMetadata;
  capabilities?: UiCapabilities;
  state_rev: number;
  updated_at: string;
  sessions: AgentSession[];
  plans?: PlanSummary[];
  tasks: PlanTask[];
  claims: EvidenceClaim[];
  graph: DependencyGraph;
  audit_log: GuardrailAuditEntry[];
  drift_summary?: DriftSummary;
}

export interface FileContextLine {
  number: number;
  text: string;
}

export interface FileContextResponse {
  path: string;
  line_start: number;
  line_end: number;
  total_lines: number;
  current_disk_sha256: string;
  lines: FileContextLine[];
}

export interface ReverifyResponse {
  ok: boolean;
  state_rev: number;
  checked_files: number;
  stale_claims: number;
  verified_claims: number;
  timestamp: string;
}

export interface DropSessionResponse {
  ok: boolean;
  session_id: string;
  dropped: boolean;
  state_rev: number;
  timestamp: string;
}

export interface UiSseEvent {
  id: string;
  seq?: number;
  type: UiEventType;
  timestamp: string;
  payload: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Phase 1: Interactive Ask & Onboarding
// ---------------------------------------------------------------------------

export type AskStatus = 'VERIFIED' | 'PARTIAL' | 'UNKNOWN';

export type AskVerifierMode = 'auto' | 'strict' | 'fts_fallback';

export interface AskRequest {
  query: string;
  scope?: string | null;
  verifier_mode?: AskVerifierMode;
}

export interface AskCitation {
  id: string;
  claim_id?: string | null;
  file_path: string;
  line_start: number;
  line_end: number;
  fingerprint: string;
  verbatim_quote: string;
  origin?: OriginMetadata;
  stale_now?: boolean;
}

export interface AskResponse {
  id: string;
  query: string;
  status: AskStatus;
  answer: string;
  citations: AskCitation[];
  tier_used: string;
  latency_ms: number;
  cached: boolean;
  fallback_mode?: boolean;
  fallback_reason?: string | null;
  unknowns?: string[];
  timestamp: string;
}

// ---------------------------------------------------------------------------
// Phase 2: Plan & Drift Monitor + Detected Decisions
// ---------------------------------------------------------------------------

export type DetectedSourceType = 'local_file' | 'github' | 'ci';
export type DetectedDecisionStatus = 'pending' | 'accepted' | 'ignored';

export interface DriftItem {
  id: string;
  file_path: string;
  drift_type: 'FILE_MODIFIED' | 'QUOTE_MUTATED' | 'SCOPE_VIOLATION' | 'TEST_STALE';
  actor: 'human' | 'agent' | 'unknown';
  origin: OriginMetadata;
  recorded_sha256: string;
  current_sha256: string;
  summary: string;
  impacted_task_ids: string[];
  impacted_claim_ids: string[];
  detected_at: string;
}

export interface TestFreshnessItem {
  task_id: string;
  task_title: string;
  plan_id: string;
  profile_name: string;
  freshness: 'FRESH' | 'STALE' | 'REJECTED';
  gate_status: TestGateStatus;
  run_id: string | null;
  sha256_short: string | null;
  ran_at: string | null;
  reason: string;
  origin?: OriginMetadata;
}

export interface DetectedDecision {
  id: string;
  title: string;
  summary: string;
  kind: 'decision' | 'constraint' | 'plan' | string;
  source_type: DetectedSourceType;
  source_ref: string;
  directory: string;
  file_path: string;
  line_start: number;
  line_end: number;
  verbatim_quote: string;
  status: DetectedDecisionStatus;
  detected_at: string;
  origin: OriginMetadata;
}

export interface DriftMonitorQuery {
  source?: 'all' | DetectedSourceType;
  directory?: string;
  status?: 'all' | DetectedDecisionStatus;
  page?: number;
  page_size?: number;
}

export interface DriftMonitorResponse {
  approved_plan: PlanSummary | null;
  repo_state: {
    branch: string | null;
    head: string | null;
    fingerprint: string | null;
    checked_at: string;
    dirty_files_count: number;
  };
  drift_items: DriftItem[];
  test_freshness: TestFreshnessItem[];
  detected_decisions: {
    items: DetectedDecision[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
    directories: string[];
    source_counts: Record<DetectedSourceType, number>;
  };
}

export interface DecideDecisionResponse {
  ok: boolean;
  decision: Pick<DetectedDecision, 'id' | 'status'> & Partial<DetectedDecision>;
  created_claim_id?: string | null;
  state_rev: number;
}

// ---------------------------------------------------------------------------
// Phase 3: Draft State Manager & CAS Conflict (409)
// ---------------------------------------------------------------------------

export interface CreatePlanInput {
  title: string;
  goal: string;
  status: 'draft' | 'active';
  external_ref?: string | null;
  constraints?: string[];
  linked_files?: string[];
  steps?: PlanStep[];
  expected_version?: number;
}

export interface UpdatePlanInput {
  expected_version: number;
  title?: string;
  goal?: string;
  external_ref?: string | null;
  constraints?: string[];
  linked_files?: string[];
  steps?: PlanStep[];
  status?: PlanStatus;
}

export interface PromotePlanPrecheck {
  missing_files: string[];
  drifted_files: Array<{
    path: string;
    recorded_sha256: string;
    current_sha256: string;
  }>;
  verified_files: string[];
}

export interface PromotePlanResponse {
  ok: boolean;
  requires_confirmation: boolean;
  precheck: PromotePlanPrecheck;
  plan: PlanSummary;
  state_rev: number;
}

export interface CreateTaskInput {
  title: string;
  step_key?: string;
  plan_id?: string | null;
  external_ref?: string | null;
  scope?: string[];
  acceptance_criteria?: string[];
  status?: 'draft' | 'proposed' | 'confirmed';
  expected_version: number;
}

export interface CreateFactInput {
  kind: 'fact' | 'decision' | 'constraint';
  key: string;
  title: string;
  content: string;
  source_path?: string | null;
  line_start?: number;
  line_end?: number;
  verbatim_quote?: string;
  expected_version: number;
}

export interface VersionDiffField {
  field: string;
  change_type: 'added' | 'removed' | 'modified';
  before: unknown;
  after: unknown;
}

export interface ConflictErrorDetail {
  code: 'stale_plan_version' | string;
  detail: string;
  entity_kind: 'plan' | 'task' | 'claim';
  entity_id: string;
  expected_version: number;
  current_version: number;
  modified_by?: OriginMetadata | null;
  server_snapshot?: PlanSummary | Record<string, unknown>;
  diff: VersionDiffField[];
}

// ---------------------------------------------------------------------------
// Phase 4: Time-Travel Versioning & Quick Actions
// ---------------------------------------------------------------------------

export type VersionEntityKind = 'plan' | 'task' | 'claim';

export interface VersionEntry {
  version: number;
  timestamp: string;
  reason: string;
  actor_type: 'human' | 'agent';
  actor_label: string;
  origin: OriginMetadata;
  audit_entry_id?: string | null;
  summary: string;
  snapshot: Record<string, unknown>;
}

export interface VersionHistoryResponse {
  entity_kind: VersionEntityKind;
  entity_id: string;
  current_version: number;
  from_version: number;
  to_version: number;
  versions: VersionEntry[];
  diff: VersionDiffField[];
}

export interface CloneVersionResponse {
  ok: boolean;
  source_version: number;
  draft_plan?: PlanSummary;
  draft_claim?: EvidenceClaim;
  draft_task?: PlanTask;
  state_rev: number;
}

export interface ReindexProgressState {
  active: boolean;
  job_id?: string;
  percent: number;
  stage: string;
  message: string;
}

export interface ReindexResponse {
  ok: boolean;
  job_id: string;
  files_scanned: number;
  changed_files: number;
  state_rev: number;
  duration_ms: number;
  timestamp: string;
}

export interface DoctorResponse {
  status: 'HEALTHY' | 'WARNING' | 'DEGRADED';
  backend_mode: 'arcadedb' | 'server' | 'sqlite' | 'mock';
  writable: boolean;
  read_only_reason?: string | null;
  llm_status: {
    configured: boolean;
    mode: string;
    chat_model: string | null;
    embedding_model: string | null;
    fallback: string;
  };
  index_freshness: {
    status: string;
    files_indexed: number;
    stale_claims: number;
    last_indexed_at: string;
    worktree_fingerprint: string | null;
  };
  scaling_thresholds: Array<{
    metric: string;
    current: number;
    threshold: number;
    status: 'OK' | 'WARN' | 'CRITICAL';
    unit: string;
  }>;
  warnings: string[];
  checked_at: string;
}

export interface ExportSnapshotResponse {
  ok: boolean;
  mode: 'team';
  preview: boolean;
  redaction_gate: {
    status: 'PASS' | 'REDACTED' | 'BLOCKED';
    secrets_scanned: number;
    secrets_redacted: number;
    findings: Array<{ rule: string; location: string; action: string }>;
  };
  summary: {
    confirmed_records: number;
    plans_included: number;
    held_proposed: number;
    held_degraded: number;
    excluded_transcripts: number;
  };
  artifact_filename: string;
  snapshot_sha256: string;
  exported_at: string;
  payload_preview: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Navigation & UI Selection
// ---------------------------------------------------------------------------

export type ActiveView = 'matrix' | 'provenance' | 'graph' | 'audit' | 'ask' | 'drift' | 'drafts';

export type InspectorSelection =
  | { kind: 'task'; id: string }
  | { kind: 'claim'; id: string }
  | { kind: 'node'; id: string }
  | { kind: 'audit'; id: string }
  | { kind: 'session'; id: string }
  | { kind: 'citation'; id: string; citation: AskCitation }
  | { kind: 'plan'; id: string }
  | null;
