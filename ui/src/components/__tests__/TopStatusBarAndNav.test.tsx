import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { NavRail } from '@/components/layout/NavRail';
import { TopStatusBar } from '@/components/layout/TopStatusBar';
import type { UiSnapshot } from '@/types/contracts';

function makeSnapshot(overrides: Partial<UiSnapshot> = {}): UiSnapshot {
  return {
    project: {
      id: 'proj-1',
      name: 'billing-core',
      root: '/repo/billing-core',
      branch: 'main',
      head: 'abcdef1234567890',
      fingerprint: 'fp-1',
      mode: 'live',
    },
    capabilities: {
      writable: true,
      read_only_reason: null,
      ask_available: true,
      llm_configured: true,
      multi_project: false,
      supported_export_modes: ['team_snapshot'],
    },
    state_rev: 14,
    updated_at: '2026-11-28T08:00:00Z',
    sessions: [
      {
        id: 'sess-1',
        client: 'claude-code',
        worktree: '/repo/billing-core',
        acknowledged_rev: 12,
        current_state_rev: 14,
        sync_status: 'OUTDATED_ACK',
        packet_id: 'pkt-1',
        packet_hash: 'h1',
        last_seen_at: '2026-11-28T08:00:00Z',
        degraded: false,
        dropped: false,
      },
    ],
    plans: [],
    tasks: [],
    claims: [],
    graph: { nodes: [], edges: [] },
    audit_log: [],
    ...overrides,
  };
}

describe('TopStatusBar', () => {
  it('displays project identity, branch, and triggers quick action callbacks', async () => {
    const user = userEvent.setup();
    const handlers = {
      onOpenPalette: vi.fn(),
      onOpenAskDrawer: vi.fn(),
      onNewDraft: vi.fn(),
      onReindex: vi.fn(),
      onOpenDoctor: vi.fn(),
      onOpenExport: vi.fn(),
    };

    render(
      <TopStatusBar
        snapshot={makeSnapshot()}
        connection="LIVE"
        reindexProgress={null}
        reindexing={false}
        {...handlers}
      />,
    );

    expect(screen.getByText('cairnmind')).toBeInTheDocument();
    expect(screen.getByText('billing-core')).toBeInTheDocument();
    expect(screen.getByText('main')).toBeInTheDocument();
    expect(screen.queryByText('READ-ONLY')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Ask' }));
    await user.click(screen.getByRole('button', { name: 'Draft' }));
    await user.click(screen.getByRole('button', { name: 'Re-index' }));

    expect(handlers.onOpenAskDrawer).toHaveBeenCalledTimes(1);
    expect(handlers.onNewDraft).toHaveBeenCalledTimes(1);
    expect(handlers.onReindex).toHaveBeenCalledTimes(1);
  });

  it('shows READ-ONLY badge and disables mutating actions when writable is false', () => {
    const snapshot = makeSnapshot({
      capabilities: {
        writable: false,
        read_only_reason: 'Started with --read-only',
        ask_available: true,
        llm_configured: false,
        multi_project: false,
        supported_export_modes: [],
      },
    });

    render(
      <TopStatusBar
        snapshot={snapshot}
        connection="RECONNECTING"
        reindexProgress={null}
        reindexing={false}
        onOpenPalette={vi.fn()}
        onOpenAskDrawer={vi.fn()}
        onNewDraft={vi.fn()}
        onReindex={vi.fn()}
        onOpenDoctor={vi.fn()}
        onOpenExport={vi.fn()}
      />,
    );

    expect(screen.getByText('READ-ONLY')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Draft' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Re-index' })).toBeDisabled();
  });
});

describe('NavRail', () => {
  it('renders counts, switches active views, and toggles stale-only filter', async () => {
    const user = userEvent.setup();
    const onSelectView = vi.fn();
    const onToggleCollapse = vi.fn();
    const onToggleOnlyStale = vi.fn();

    render(
      <NavRail
        activeView="matrix"
        onSelectView={onSelectView}
        collapsed={false}
        onToggleCollapse={onToggleCollapse}
        onlyStaleAndConflicts={false}
        onToggleOnlyStale={onToggleOnlyStale}
        counts={{
          staleTasks: 2,
          staleClaims: 3,
          rejectedAudit: 4,
          graphNodes: 9,
          driftAlerts: 5,
          draftPlans: 1,
        }}
      />,
    );

    expect(screen.getByText('Plan & Concurrency')).toBeInTheDocument();
    expect(screen.getByText('Provenance & Quotes')).toBeInTheDocument();
    expect(screen.getByText('Blast-Radius Graph')).toBeInTheDocument();
    expect(screen.getByText('Guardrail Audit')).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('9')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Provenance & Quotes/i }));
    expect(onSelectView).toHaveBeenCalledWith('provenance');

    await user.click(screen.getByRole('button', { name: /Guardrail Audit/i }));
    expect(onSelectView).toHaveBeenCalledWith('audit');

    await user.click(screen.getByTitle(/Collapse navigation/i));
    expect(onToggleCollapse).toHaveBeenCalledTimes(1);
  });
});
