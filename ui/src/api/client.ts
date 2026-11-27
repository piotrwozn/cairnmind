import type { CockpitApiAdapter } from '@/api/adapter';
import { HttpCockpitAdapter } from '@/api/httpAdapter';
import { MockCockpitAdapter } from '@/mocks/mockAdapter';
import type {
  AskRequest,
  AskResponse,
  CloneVersionResponse,
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

export { ApiConflictError, ApiReadOnlyError } from '@/api/adapter';

export interface QueryContext {
  project: string | null;
  mock: boolean;
  apiBase: string;
  projectPrefix: boolean;
}

export function readQueryContext(): QueryContext {
  const params = new URLSearchParams(window.location.search);
  const mockParam = params.get('mock');
  return {
    project: params.get('project') || params.get('project_id'),
    mock: mockParam === '1' || mockParam === 'true',
    apiBase: params.get('apiBase') || '/api/ui',
    projectPrefix: params.get('projectPrefix') === '1',
  };
}

let cachedAdapter: CockpitApiAdapter | null = null;
let cachedKey: string | null = null;

export function getCockpitAdapter(): CockpitApiAdapter {
  const ctx = readQueryContext();
  const key = `${ctx.mock ? 'mock' : 'http'}|${ctx.project ?? ''}|${ctx.apiBase}|${ctx.projectPrefix}`;
  if (cachedAdapter && cachedKey === key) {
    return cachedAdapter;
  }
  cachedKey = key;
  cachedAdapter = ctx.mock
    ? new MockCockpitAdapter(ctx.project)
    : new HttpCockpitAdapter({
        apiBase: ctx.apiBase,
        projectId: ctx.project,
        useProjectPathPrefix: ctx.projectPrefix,
      });
  return cachedAdapter;
}

export function buildEventsUrl(): string {
  const ctx = readQueryContext();
  const http = new HttpCockpitAdapter({
    apiBase: ctx.apiBase,
    projectId: ctx.project,
    useProjectPathPrefix: ctx.projectPrefix,
  });
  return http.buildEndpointUrl('/events', ctx.mock ? { mock: 1 } : undefined);
}

// Named facade functions delegating cleanly to the active adapter
export const fetchSnapshot = (): Promise<UiSnapshot> => getCockpitAdapter().fetchSnapshot();

export const fetchFileContext = (
  path: string,
  lineStart: number,
  lineEnd: number,
): Promise<FileContextResponse> => getCockpitAdapter().fetchFileContext(path, lineStart, lineEnd);

export const triggerReverify = (): Promise<ReverifyResponse> =>
  getCockpitAdapter().triggerReverify();

export const dropSessionLock = (sessionId: string): Promise<DropSessionResponse> =>
  getCockpitAdapter().dropSessionLock(sessionId);

export const askCockpit = (request: AskRequest, signal?: AbortSignal): Promise<AskResponse> =>
  getCockpitAdapter().ask(request, signal);

export const fetchDriftMonitor = (query?: DriftMonitorQuery): Promise<DriftMonitorResponse> =>
  getCockpitAdapter().fetchDriftMonitor(query);

export const decideDetectedDecision = (
  decisionId: string,
  action: 'accept' | 'ignore',
): Promise<DecideDecisionResponse> => getCockpitAdapter().decideDetectedDecision(decisionId, action);

export const createPlan = (
  input: CreatePlanInput,
): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }> =>
  getCockpitAdapter().createPlan(input);

export const updatePlan = (
  planId: string,
  input: UpdatePlanInput,
): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }> =>
  getCockpitAdapter().updatePlan(planId, input);

export const promotePlan = (
  planId: string,
  expectedVersion: number,
  force = false,
): Promise<PromotePlanResponse> => getCockpitAdapter().promotePlan(planId, expectedVersion, force);

export const createTask = (
  input: CreateTaskInput,
): Promise<{ ok: boolean; task: PlanTask; state_rev: number }> =>
  getCockpitAdapter().createTask(input);

export const createFact = (
  input: CreateFactInput,
): Promise<{ ok: boolean; claim: EvidenceClaim; state_rev: number }> =>
  getCockpitAdapter().createFact(input);

export const fetchVersionHistory = (
  entityKind: VersionEntityKind,
  entityId: string,
  fromVersion?: number,
  toVersion?: number,
): Promise<VersionHistoryResponse> =>
  getCockpitAdapter().fetchVersionHistory(entityKind, entityId, fromVersion, toVersion);

export const cloneVersionAsDraft = (
  entityKind: VersionEntityKind,
  entityId: string,
  sourceVersion: number,
  titleSuffix?: string,
): Promise<CloneVersionResponse> =>
  getCockpitAdapter().cloneVersionAsDraft(entityKind, entityId, sourceVersion, titleSuffix);

export const triggerReindex = (): Promise<ReindexResponse> => getCockpitAdapter().triggerReindex();

export const fetchDoctor = (): Promise<DoctorResponse> => getCockpitAdapter().fetchDoctor();

export const exportTeamSnapshot = (preview = false): Promise<ExportSnapshotResponse> =>
  getCockpitAdapter().exportTeamSnapshot(preview);
