import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiAuthError, ApiConflictError, ApiReadOnlyError } from '@/api/adapter';
import {
  HttpCockpitAdapter,
  UI_CSRF_COOKIE,
  UI_CSRF_HEADER,
  readCookie,
  readCsrfToken,
} from '@/api/httpAdapter';

describe('HttpCockpitAdapter security & contract handling', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    document.cookie = `${UI_CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it('reads URL-encoded CSRF cookie safely', () => {
    document.cookie = 'other=1; path=/';
    document.cookie = `${UI_CSRF_COOKIE}=tok%2Babc%3D1; path=/`;
    expect(readCookie('other')).toBe('1');
    expect(readCsrfToken()).toBe('tok+abc=1');
  });

  it('sends same-origin credentials on GET snapshot without requiring CSRF header', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ state_rev: 7 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const adapter = new HttpCockpitAdapter({ apiBase: '/api/ui', projectId: 'proj-1' });
    const snap = await adapter.fetchSnapshot();
    expect(snap.state_rev).toBe(7);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/ui/snapshot?project=proj-1');
    expect(init?.credentials).toBe('same-origin');
    expect((init?.headers as Record<string, string>)[UI_CSRF_HEADER]).toBeUndefined();
  });

  it('attaches X-Cairnmind-UI-Token CSRF header on mutations and file-context reads', async () => {
    document.cookie = `${UI_CSRF_COOKIE}=csrf-secret-42; path=/`;
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ path: 'a.py', lines: [], total_lines: 0, current_disk_sha256: '0' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true, state_rev: 8, checked_files: 1, stale_claims: 0, verified_claims: 1, timestamp: '' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const adapter = new HttpCockpitAdapter();
    await adapter.fetchFileContext('a.py', 1, 10);
    await adapter.triggerReverify();

    expect((fetchMock.mock.calls[0][1]?.headers as Record<string, string>)[UI_CSRF_HEADER]).toBe(
      'csrf-secret-42',
    );
    expect(fetchMock.mock.calls[0][1]?.credentials).toBe('same-origin');
    expect((fetchMock.mock.calls[1][1]?.headers as Record<string, string>)[UI_CSRF_HEADER]).toBe(
      'csrf-secret-42',
    );
    expect(fetchMock.mock.calls[1][1]?.method).toBe('POST');
  });

  it('maps 401, 403, and 409 responses to typed error classes', async () => {
    const adapter = new HttpCockpitAdapter();

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'UI session authentication required.' }), { status: 401 }),
    );
    await expect(adapter.fetchSnapshot()).rejects.toBeInstanceOf(ApiAuthError);

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Read-only mode enabled.' }), { status: 403 }),
    );
    await expect(adapter.triggerReverify()).rejects.toBeInstanceOf(ApiReadOnlyError);

    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: {
            code: 'stale_plan_version',
            detail: 'Conflict',
            entity_kind: 'plan',
            entity_id: 'p1',
            expected_version: 1,
            current_version: 2,
            diff: [],
          },
        }),
        { status: 409 },
      ),
    );
    await expect(
      adapter.updatePlan('p1', { expected_version: 1, goal: 'x' }),
    ).rejects.toBeInstanceOf(ApiConflictError);
  });

  it('builds project-prefixed endpoints cleanly', () => {
    const adapter = new HttpCockpitAdapter({
      apiBase: '/api/ui/',
      projectId: 'my proj',
      useProjectPathPrefix: true,
    });
    expect(adapter.buildEndpointUrl('/events', { mock: 1 })).toBe(
      '/api/ui/projects/my%20proj/events?mock=1',
    );
  });
});
