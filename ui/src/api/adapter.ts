import type {
  AskRequest,
  AskResponse,
  CloneVersionResponse,
  ConflictErrorDetail,
  CreateFactInput,
  CreatePlanInput,
  CreateTaskInput,
  DecideDecisionResponse,
  DoctorResponse,
  DriftMonitorQuery,
  DriftMonitorResponse,
  DropSessionResponse,
  EvidenceClaim,
  ExportSnapshotResponse,
  FileContextResponse,
  PlanSummary,
  PlanTask,
  PromotePlanResponse,
  ReindexResponse,
  ReverifyResponse,
  UiSnapshot,
  UpdatePlanInput,
  VersionEntityKind,
  VersionHistoryResponse,
} from '@/types/contracts';

export class ApiConflictError extends Error {
  readonly status = 409;
  readonly conflict: ConflictErrorDetail;

  constructor(conflict: ConflictErrorDetail) {
    super(conflict.detail || 'Version conflict (409 stale_plan_version)');
    this.name = 'ApiConflictError';
    this.conflict = conflict;
  }
}

export class ApiReadOnlyError extends Error {
  readonly status = 403;

  constructor(message: string) {
    super(message || 'Cockpit is in read-only mode.');
    this.name = 'ApiReadOnlyError';
  }
}

export interface MockControls {
  simulateConflict: (planId: string, client?: string) => Promise<{ current_version: number; state_rev: number }>;
  simulateFileEdit: (filePath: string) => Promise<{ file_path: string; new_sha256: string; claim_ids: string[] }>;
  setReadOnly: (readOnly: boolean) => void;
  setLlmOffline: (offline: boolean) => void;
  getMockFlags: () => { readOnly: boolean; llmOffline: boolean };
}

/**
 * Thin backend adapter contract.
 * Keeps every React component decoupled from URL layout so the UI can target either
 * the local Python FastAPI server (`/api/ui/*`) or a future multi-project Rust manager
 * (`/api/projects/{project_id}/*`) without rewriting any view component.
 */
export interface CockpitApiAdapter {
  readonly mode: 'http' | 'mock';
  readonly projectId: string | null;

  fetchSnapshot(): Promise<UiSnapshot>;
  fetchFileContext(path: string, lineStart: number, lineEnd: number): Promise<FileContextResponse>;
  triggerReverify(): Promise<ReverifyResponse>;
  dropSessionLock(sessionId: string): Promise<DropSessionResponse>;

  // Phase 1: Interactive Ask
  ask(request: AskRequest, signal?: AbortSignal): Promise<AskResponse>;

  // Phase 2: Plan & Drift Monitor
  fetchDriftMonitor(query?: DriftMonitorQuery): Promise<DriftMonitorResponse>;
  decideDetectedDecision(decisionId: string, action: 'accept' | 'ignore'): Promise<DecideDecisionResponse>;

  // Phase 3: Draft State Manager (CAS with expected_version)
  createPlan(input: CreatePlanInput): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }>;
  updatePlan(planId: string, input: UpdatePlanInput): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }>;
  promotePlan(planId: string, expectedVersion: number, force?: boolean): Promise<PromotePlanResponse>;
  createTask(input: CreateTaskInput): Promise<{ ok: boolean; task: PlanTask; state_rev: number }>;
  createFact(input: CreateFactInput): Promise<{ ok: boolean; claim: EvidenceClaim; state_rev: number }>;

  // Phase 4: Versioning & Operational Quick Actions
  fetchVersionHistory(
    entityKind: VersionEntityKind,
    entityId: string,
    fromVersion?: number,
    toVersion?: number,
  ): Promise<VersionHistoryResponse>;
  cloneVersionAsDraft(
    entityKind: VersionEntityKind,
    entityId: string,
    sourceVersion: number,
    titleSuffix?: string,
  ): Promise<CloneVersionResponse>;
  triggerReindex(): Promise<ReindexResponse>;
  fetchDoctor(): Promise<DoctorResponse>;
  exportTeamSnapshot(preview?: boolean): Promise<ExportSnapshotResponse>;

  // Optional mock-only interactive testing controls
  mockControls?: MockControls;
}
