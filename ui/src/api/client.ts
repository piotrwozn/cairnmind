import type {
  DropSessionResponse,
  FileContextResponse,
  ReverifyResponse,
  UiSnapshot,
} from '@/types/contracts';

export interface QueryContext {
  project: string | null;
  mock: boolean;
}

export function readQueryContext(): QueryContext {
  const params = new URLSearchParams(window.location.search);
  const mockParam = params.get('mock');
  return {
    project: params.get('project'),
    mock: mockParam === '1' || mockParam === 'true',
  };
}

function buildUrl(path: string, extra?: Record<string, string | number | null | undefined>): string {
  const ctx = readQueryContext();
  const params = new URLSearchParams();
  if (ctx.project) params.set('project', ctx.project);
  if (ctx.mock) params.set('mock', '1');
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v !== null && v !== undefined && v !== '') {
        params.set(k, String(v));
      }
    }
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload?.detail) detail = payload.detail;
    } catch {
      // ignore non-JSON error body
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export async function fetchSnapshot(): Promise<UiSnapshot> {
  const res = await fetch(buildUrl('/api/ui/snapshot'), {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  });
  return parseJson<UiSnapshot>(res);
}

export async function fetchFileContext(
  path: string,
  lineStart: number,
  lineEnd: number,
): Promise<FileContextResponse> {
  const res = await fetch(
    buildUrl('/api/ui/file-context', { path, lineStart, lineEnd }),
    {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    },
  );
  return parseJson<FileContextResponse>(res);
}

export async function triggerReverify(): Promise<ReverifyResponse> {
  const res = await fetch(buildUrl('/api/ui/actions/reverify'), {
    method: 'POST',
    headers: { Accept: 'application/json' },
  });
  return parseJson<ReverifyResponse>(res);
}

export async function dropSessionLock(sessionId: string): Promise<DropSessionResponse> {
  const res = await fetch(buildUrl(`/api/ui/actions/sessions/${encodeURIComponent(sessionId)}/drop`), {
    method: 'POST',
    headers: { Accept: 'application/json' },
  });
  return parseJson<DropSessionResponse>(res);
}

export function buildEventsUrl(): string {
  return buildUrl('/api/ui/events');
}
