import { ApiConflictError, ApiReadOnlyError, type CockpitApiAdapter } from '@/api/adapter';
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

export interface HttpAdapterConfig {
  /** Base API prefix. Defaults to `/api/ui`; can be set to `/api` for Rust manager backends. */
  apiBase?: string;
  /** Optional multi-project identifier; when `useProjectPathPrefix` is true, routes use `/projects/{projectId}/...`. */
  projectId?: string | null;
  useProjectPathPrefix?: boolean;
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let rawBody: unknown = null;
    try {
      rawBody = await response.json();
    } catch {
      // ignore non-JSON body
    }

    if (response.status === 409) {
      const candidate =
        rawBody && typeof rawBody === 'object' && 'detail' in rawBody && typeof (rawBody as { detail: unknown }).detail === 'object'
          ? ((rawBody as { detail: ConflictErrorDetail }).detail)
          : (rawBody as ConflictErrorDetail | null);
      if (candidate && typeof candidate === 'object' && 'expected_version' in candidate) {
        throw new ApiConflictError(candidate);
      }
      throw new ApiConflictError({
        code: 'stale_plan_version',
        detail:
          rawBody && typeof rawBody === 'object' && 'detail' in rawBody && typeof (rawBody as { detail: unknown }).detail === 'string'
            ? (rawBody as { detail: string }).detail
            : 'Version conflict (409)',
        entity_kind: 'plan',
        entity_id: 'unknown',
        expected_version: 0,
        current_version: 0,
        diff: [],
      });
    }

    const detail =
      rawBody && typeof rawBody === 'object' && 'detail' in rawBody && typeof (rawBody as { detail: unknown }).detail === 'string'
        ? (rawBody as { detail: string }).detail
        : `${response.status} ${response.statusText}`;

    if (response.status === 403) {
      throw new ApiReadOnlyError(detail);
    }

    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export class HttpCockpitAdapter implements CockpitApiAdapter {
  readonly mode = 'http' as const;
  readonly projectId: string | null;
  private readonly apiBase: string;
  private readonly useProjectPathPrefix: boolean;

  constructor(config: HttpAdapterConfig = {}) {
    this.apiBase = (config.apiBase ?? '/api/ui').replace(/\/+$/, '');
    this.projectId = config.projectId ?? null;
    this.useProjectPathPrefix = Boolean(config.useProjectPathPrefix && this.projectId);
  }

  buildEndpointUrl(
    resourcePath: string,
    extra?: Record<string, string | number | boolean | null | undefined>,
  ): string {
    const cleanResource = resourcePath.startsWith('/') ? resourcePath : `/${resourcePath}`;
    const prefix =
      this.useProjectPathPrefix && this.projectId
        ? `${this.apiBase}/projects/${encodeURIComponent(this.projectId)}`
        : this.apiBase;

    const params = new URLSearchParams();
    if (this.projectId && !this.useProjectPathPrefix) {
      params.set('project', this.projectId);
    }
    if (extra) {
      for (const [k, v] of Object.entries(extra)) {
        if (v !== null && v !== undefined && v !== '') {
          params.set(k, String(v));
        }
      }
    }
    const qs = params.toString();
    return qs ? `${prefix}${cleanResource}?${qs}` : `${prefix}${cleanResource}`;
  }

  async fetchSnapshot(): Promise<UiSnapshot> {
    const res = await fetch(this.buildEndpointUrl('/snapshot'), {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    return parseJson<UiSnapshot>(res);
  }

  async fetchFileContext(
    path: string,
    lineStart: number,
    lineEnd: number,
  ): Promise<FileContextResponse> {
    const res = await fetch(
      this.buildEndpointUrl('/file-context', { path, lineStart, lineEnd }),
      {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      },
    );
    return parseJson<FileContextResponse>(res);
  }

  async triggerReverify(): Promise<ReverifyResponse> {
    const res = await fetch(this.buildEndpointUrl('/actions/reverify'), {
      method: 'POST',
      headers: { Accept: 'application/json' },
    });
    return parseJson<ReverifyResponse>(res);
  }

  async dropSessionLock(sessionId: string): Promise<DropSessionResponse> {
    const res = await fetch(
      this.buildEndpointUrl(`/actions/sessions/${encodeURIComponent(sessionId)}/drop`),
      {
        method: 'POST',
        headers: { Accept: 'application/json' },
      },
    );
    return parseJson<DropSessionResponse>(res);
  }

  async ask(request: AskRequest, signal?: AbortSignal): Promise<AskResponse> {
    const res = await fetch(this.buildEndpointUrl('/ask'), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    });
    return parseJson<AskResponse>(res);
  }

  async fetchDriftMonitor(query: DriftMonitorQuery = {}): Promise<DriftMonitorResponse> {
    const res = await fetch(
      this.buildEndpointUrl('/drift', {
        source: query.source,
        directory: query.directory,
        status: query.status,
        page: query.page,
        page_size: query.page_size,
      }),
      {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      },
    );
    return parseJson<DriftMonitorResponse>(res);
  }

  async decideDetectedDecision(
    decisionId: string,
    action: 'accept' | 'ignore',
  ): Promise<DecideDecisionResponse> {
    const res = await fetch(
      this.buildEndpointUrl(`/detected-decisions/${encodeURIComponent(decisionId)}/decide`),
      {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      },
    );
    return parseJson<DecideDecisionResponse>(res);
  }

  async createPlan(
    input: CreatePlanInput,
  ): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }> {
    const res = await fetch(this.buildEndpointUrl('/plans'), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    });
    return parseJson<{ ok: boolean; plan: PlanSummary; state_rev: number }>(res);
  }

  async updatePlan(
    planId: string,
    input: UpdatePlanInput,
  ): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }> {
    const res = await fetch(this.buildEndpointUrl(`/plans/${encodeURIComponent(planId)}`), {
      method: 'PATCH',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    });
    return parseJson<{ ok: boolean; plan: PlanSummary; state_rev: number }>(res);
  }

  async promotePlan(
    planId: string,
    expectedVersion: number,
    force = false,
  ): Promise<PromotePlanResponse> {
    const res = await fetch(
      this.buildEndpointUrl(`/plans/${encodeURIComponent(planId)}/promote`),
      {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_version: expectedVersion, force }),
      },
    );
    return parseJson<PromotePlanResponse>(res);
  }

  async createTask(
    input: CreateTaskInput,
  ): Promise<{ ok: boolean; task: PlanTask; state_rev: number }> {
    const res = await fetch(this.buildEndpointUrl('/tasks'), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    });
    return parseJson<{ ok: boolean; task: PlanTask; state_rev: number }>(res);
  }

  async createFact(
    input: CreateFactInput,
  ): Promise<{ ok: boolean; claim: EvidenceClaim; state_rev: number }> {
    const res = await fetch(this.buildEndpointUrl('/facts'), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    });
    return parseJson<{ ok: boolean; claim: EvidenceClaim; state_rev: number }>(res);
  }

  async fetchVersionHistory(
    entityKind: VersionEntityKind,
    entityId: string,
    fromVersion?: number,
    toVersion?: number,
  ): Promise<VersionHistoryResponse> {
    const res = await fetch(
      this.buildEndpointUrl(`/versions/${entityKind}/${encodeURIComponent(entityId)}`, {
        from_version: fromVersion,
        to_version: toVersion,
      }),
      {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      },
    );
    return parseJson<VersionHistoryResponse>(res);
  }

  async cloneVersionAsDraft(
    entityKind: VersionEntityKind,
    entityId: string,
    sourceVersion: number,
    titleSuffix?: string,
  ): Promise<CloneVersionResponse> {
    const res = await fetch(
      this.buildEndpointUrl(
        `/versions/${entityKind}/${encodeURIComponent(entityId)}/clone-draft`,
      ),
      {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_version: sourceVersion, title_suffix: titleSuffix }),
      },
    );
    return parseJson<CloneVersionResponse>(res);
  }

  async triggerReindex(): Promise<ReindexResponse> {
    const res = await fetch(this.buildEndpointUrl('/actions/reindex'), {
      method: 'POST',
      headers: { Accept: 'application/json' },
    });
    return parseJson<ReindexResponse>(res);
  }

  async fetchDoctor(): Promise<DoctorResponse> {
    const res = await fetch(this.buildEndpointUrl('/doctor'), {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    return parseJson<DoctorResponse>(res);
  }

  async exportTeamSnapshot(preview = false): Promise<ExportSnapshotResponse> {
    const res = await fetch(this.buildEndpointUrl('/actions/export-snapshot'), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'team', preview }),
    });
    return parseJson<ExportSnapshotResponse>(res);
  }
}
