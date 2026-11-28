import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { UI_CSRF_COOKIE, UI_CSRF_HEADER } from '@/api/httpAdapter';
import type { UiSnapshot } from '@/types/contracts';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  private listeners = new Map<string, Set<EventListener>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, fn: EventListener) {
    const set = this.listeners.get(type) ?? new Set<EventListener>();
    set.add(fn);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, fn: EventListener) {
    this.listeners.get(type)?.delete(fn);
  }

  emit(type: string, data?: string) {
    const event = data === undefined ? new Event(type) : new MessageEvent(type, { data });
    for (const fn of this.listeners.get(type) ?? []) {
      fn(event);
    }
  }

  close() {}
}

function makeLiveSnapshot(): UiSnapshot {
  return {
    project: {
      id: 'proj-live',
      name: 'cairnmind-live-app',
      root: '/repo/cairnmind',
      branch: 'consolidation-wave-2',
      head: '1234567890abcdef',
      fingerprint: 'fp-live',
      mode: 'live',
    },
    capabilities: {
      writable: true,
      read_only_reason: null,
      ask_available: true,
      llm_configured: false,
      multi_project: false,
      supported_export_modes: ['team_snapshot'],
    },
    state_rev: 9,
    updated_at: '2026-11-28T08:15:00Z',
    sessions: [
      {
        id: 'sess-live-1',
        client: 'codex-cli',
        worktree: '/repo/cairnmind',
        acknowledged_rev: 9,
        current_state_rev: 9,
        sync_status: 'SYNCED',
        packet_id: 'pkt-live',
        packet_hash: 'abcd1234',
        last_seen_at: '2026-11-28T08:15:00Z',
        degraded: false,
        dropped: false,
      },
    ],
    plans: [],
    tasks: [
      {
        id: 'task-live-1',
        step_key: 'step-ui-tests',
        title: 'Verify lazy cockpit views',
        plan_id: 'plan-live',
        plan_title: 'Wave 2',
        status: 'confirmed',
        state: 'in_progress',
        version: 1,
        evidence_verified: 1,
        evidence_total: 1,
        evidence_status: 'VERIFIED',
        test_gate: {
          profile_name: 'vitest',
          status: 'PASS',
          sha256_short: 'abc1234',
          run_id: 'run-live',
          ran_at: '2026-11-28T08:14:00Z',
        },
        updated_at: '2026-11-28T08:15:00Z',
        claim_ids: ['claim-live-1'],
        source_paths: ['src/app.py'],
        depends_on: [],
        acceptance_criteria: [],
      },
    ],
    claims: [
      {
        id: 'claim-live-1',
        kind: 'decision',
        key: 'ui.lazy',
        title: 'Cockpit views load lazily',
        content: 'Split heavy views via React.lazy.',
        status: 'confirmed',
        provenance_status: 'VERIFIED',
        source_path: 'ui/src/App.tsx',
        line_start: 1,
        line_end: 5,
        verbatim_quote: 'const ClaimList = lazy(',
        recorded_sha256: 'aaaa1111bbbb2222',
        current_disk_sha256: 'aaaa1111bbbb2222',
        quote_survives: true,
        drift_kind: 'MATCH',
        updated_at: '2026-11-28T08:15:00Z',
        task_ids: ['task-live-1'],
        version: 1,
      },
    ],
    graph: { nodes: [], edges: [] },
    audit_log: [
      {
        id: 'aud-live-1',
        timestamp: '2026-11-28T08:15:00.000Z',
        session_id: 'sess-live-1',
        client: 'codex-cli',
        mcp_tool: 'cairnmind_remember',
        verdict: 'ACCEPTED',
        reason_code: 'OK',
        message: 'Accepted grounded write',
        target_id: 'claim-live-1',
        arguments: {},
        violation_detail: {},
      },
    ],
  };
}

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

describe('App live authentication, lazy views, SSE, and CSRF flow', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
    window.history.replaceState({}, '', '/');
    document.cookie = `${UI_CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it('shows an error banner with Retry when /api/ui/snapshot returns 401', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'UI session authentication required.' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    renderApp();

    await waitFor(() => {
      expect(screen.getByText('Unable to load Cairnmind UI snapshot')).toBeInTheDocument();
    });
    expect(screen.getByText(/UI session authentication required/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument();
  });

  it('hydrates snapshot, connects SSE, switches lazy views, and sends CSRF token on Re-verify', async () => {
    const user = userEvent.setup();
    document.cookie = `${UI_CSRF_COOKIE}=csrf-token-xyz; path=/`;

    const snapshot = makeLiveSnapshot();
    fetchMock.mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes('/actions/reverify')) {
        return new Response(
          JSON.stringify({
            ok: true,
            state_rev: 10,
            checked_files: 1,
            stale_claims: 0,
            verified_claims: 1,
            timestamp: '2026-11-28T08:16:00Z',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response(JSON.stringify(snapshot), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    renderApp();

    await waitFor(() => {
      expect(screen.getByText('cairnmind-live-app')).toBeInTheDocument();
    });
    expect(screen.getByText('Verify lazy cockpit views')).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('/api/ui/events');

    // Trigger Re-verify mutation and verify X-Cairnmind-UI-Token CSRF header is attached
    await user.click(screen.getByRole('button', { name: /Re-verify/i }));
    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/actions/reverify'));
      expect(postCall).toBeDefined();
      const headers = (postCall?.[1]?.headers ?? {}) as Record<string, string>;
      expect(headers[UI_CSRF_HEADER]).toBe('csrf-token-xyz');
    });

    // Navigate to lazily-loaded Provenance & Quotes view
    await user.click(screen.getByRole('button', { name: /Provenance & Quotes/i }));
    await waitFor(() => {
      expect(screen.getByText('Cockpit views load lazily')).toBeInTheDocument();
    });

    // Navigate to lazily-loaded Guardrail Audit view
    await user.click(screen.getByRole('button', { name: /Guardrail Audit/i }));
    await waitFor(() => {
      expect(screen.getByText('Accepted grounded write')).toBeInTheDocument();
    });
  });
});
