/**
 * Strict 1:1 TypeScript contracts matching `src/cairnmind/ui_server.py`.
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
  | 'mcp.write.rejected';

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
}

export interface UiSnapshot {
  project: ProjectMetadata;
  state_rev: number;
  updated_at: string;
  sessions: AgentSession[];
  tasks: PlanTask[];
  claims: EvidenceClaim[];
  graph: DependencyGraph;
  audit_log: GuardrailAuditEntry[];
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

export type ActiveView = 'matrix' | 'provenance' | 'graph' | 'audit';

export type InspectorSelection =
  | { kind: 'task'; id: string }
  | { kind: 'claim'; id: string }
  | { kind: 'node'; id: string }
  | { kind: 'audit'; id: string }
  | { kind: 'session'; id: string }
  | null;
