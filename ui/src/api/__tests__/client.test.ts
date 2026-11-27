import { describe, expect, it } from 'vitest';
import { buildEventsUrl, readQueryContext } from '@/api/client';

describe('client query context sanitization', () => {
  it('defaults apiBase to /api/ui and rejects external origins in apiBase param', () => {
    window.history.replaceState({}, '', '/?apiBase=https://evil.example/api&project=demo');
    const ctx = readQueryContext();
    expect(ctx.apiBase).toBe('/api/ui');
    expect(ctx.project).toBe('demo');

    window.history.replaceState({}, '', '/?apiBase=//evil.example/api');
    expect(readQueryContext().apiBase).toBe('/api/ui');
  });

  it('accepts relative path apiBase and builds SSE URL', () => {
    window.history.replaceState({}, '', '/?apiBase=/api/ui&project=p1&mock=1');
    expect(buildEventsUrl()).toBe('/api/ui/events?project=p1&mock=1');
  });
});
