import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
  history.replaceState(null, '', '/');
});
describe('browser authentication boundary', () => {
  it('consumes a one-use fragment once and removes it before making a request', async () => {
    history.replaceState(null, '', '/#ticket=fixture-only-capability');
    const fetcher = vi.fn(async (path: string, init: RequestInit) => {
      expect(location.hash).toBe('');
      expect(path).toBe('/auth/exchange');
      expect(JSON.parse(String(init.body))).toEqual({ ticket: 'fixture-only-capability' });
      return new Response(JSON.stringify({ csrf: 'fixture-csrf' }), { status: 200 });
    });
    vi.stubGlobal('fetch', fetcher);
    const { bootstrap } = await import('../src/api');
    await Promise.all([bootstrap(), bootstrap()]);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(localStorage.length).toBe(0);
  });
  it('uses cookie credentials and in-memory CSRF for mutations without exposing service tokens', async () => {
    const calls: Array<{ path: string; init: RequestInit }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string, init: RequestInit) => {
        calls.push({ path, init });
        return new Response(
          JSON.stringify(
            path === '/auth/session' ? { csrf: 'memory-only-csrf' } : { status: 'ok' },
          ),
          { status: 200 },
        );
      }),
    );
    const { bootstrap, post, api } = await import('../src/api');
    await bootstrap();
    await post('/api/memory/snapshot', { project: 'project-a' });
    await api('/api/state');
    const mutation = calls.find((item) => item.path === '/api/memory/snapshot')!;
    expect(mutation.init.credentials).toBe('same-origin');
    expect(new Headers(mutation.init.headers).get('X-CSRF-Token')).toBe('memory-only-csrf');
    expect(new Headers(calls.at(-1)!.init.headers).get('Authorization')).toBeNull();
  });
  it('encodes terminal text as UTF-8 and keeps socket controls scoped to run generation', async () => {
    const { encodeInput, decodeOutput, terminalUrl } = await import('../src/api');
    expect(new TextDecoder().decode(decodeOutput(encodeInput('Zażółć\r')))).toBe('Zażółć\r');
    expect(terminalUrl('run/a', 7)).toContain('/api/client-runs/run%2Fa/terminal?');
    expect(terminalUrl('run/a', 7)).toContain('generation=7');
  });
});
