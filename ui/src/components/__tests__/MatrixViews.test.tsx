import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { SessionStrip } from '@/components/matrix/SessionStrip';
import { TaskGateTable } from '@/components/matrix/TaskGateTable';
import type { AgentSession, PlanTask } from '@/types/contracts';

const SESSIONS: AgentSession[] = [
  {
    id: 'sess-synced',
    client: 'codex-cli',
    worktree: '/repo/demo',
    acknowledged_rev: 21,
    current_state_rev: 21,
    sync_status: 'SYNCED',
    packet_id: 'pkt-a',
    packet_hash: 'aaaa1111',
    last_seen_at: '2026-11-28T08:10:00Z',
    degraded: false,
    dropped: false,
  },
  {
    id: 'sess-stale',
    client: 'external-mcp',
    worktree: '/repo/demo',
    acknowledged_rev: 17,
    current_state_rev: 21,
    sync_status: 'OUTDATED_ACK',
    packet_id: 'pkt-b',
    packet_hash: 'bbbb2222',
    last_seen_at: '2026-11-28T08:05:00Z',
    degraded: true,
    dropped: false,
  },
];

const TASKS: PlanTask[] = [
  {
    id: 'task-1',
    step_key: 'auth-middleware',
    title: 'Enforce loopback CSRF cookie',
    plan_id: 'plan-1',
    plan_title: 'Security Hardening',
    status: 'confirmed',
    state: 'in_progress',
    version: 3,
    evidence_verified: 2,
    evidence_total: 2,
    evidence_status: 'VERIFIED',
    test_gate: {
      profile_name: 'pytest-auth',
      status: 'PASS',
      sha256_short: 'a1b2c3d',
      run_id: 'run-1',
      ran_at: '2026-11-28T08:00:00Z',
    },
    updated_at: '2026-11-28T08:00:00Z',
    claim_ids: ['claim-1'],
    source_paths: ['src/cairnmind/ui/ui_auth.py'],
    depends_on: [],
    acceptance_criteria: ['Reject cross-origin POST'],
    origin: {
      category: 'HUMAN_REPO',
      origin_evidence: 'human',
      origin_client: 'ui-human',
      source_label: 'human-ui',
    },
  },
  {
    id: 'task-2',
    step_key: 'vec0-knn',
    title: 'Wire sqlite-vec KNN search',
    plan_id: 'plan-1',
    plan_title: 'Security Hardening',
    status: 'proposed',
    state: 'needs_test',
    version: 1,
    evidence_verified: 0,
    evidence_total: 2,
    evidence_status: 'STALE',
    test_gate: {
      profile_name: 'pytest-vec',
      status: 'STALE_CODE',
      sha256_short: 'deadbee',
      run_id: 'run-2',
      ran_at: '2026-11-28T07:50:00Z',
    },
    updated_at: '2026-11-28T07:55:00Z',
    claim_ids: [],
    source_paths: ['src/cairnmind/storage/sqlite_vectors.py'],
    depends_on: ['auth-middleware'],
    acceptance_criteria: ['Exact parity with cosine fallback'],
    origin: {
      category: 'MCP_DEGRADED',
      origin_evidence: 'degraded',
      origin_client: 'external-mcp',
      source_label: 'external-mcp',
    },
  },
];

describe('SessionStrip', () => {
  it('renders SYNCED, OUTDATED_ACK, and DEGRADED indicators and triggers select/drop', async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const onDrop = vi.fn();

    render(
      <SessionStrip
        sessions={SESSIONS}
        selectedSessionId="sess-synced"
        onSelectSession={onSelect}
        onDropSession={onDrop}
        droppingSessionId={null}
        writable
      />,
    );

    expect(screen.getByText('(2 connected)')).toBeInTheDocument();
    expect(screen.getByText('SYNCED')).toBeInTheDocument();
    expect(screen.getByText(/OUTDATED_ACK/)).toBeInTheDocument();
    expect(screen.getByText('DEGRADED')).toBeInTheDocument();

    const dropButtons = screen.getAllByRole('button', { name: 'Drop Session' });
    expect(dropButtons).toHaveLength(2);
    await user.click(dropButtons[1]);
    expect(onDrop).toHaveBeenCalledWith('sess-stale');
    expect(onSelect).not.toHaveBeenCalled();

    await user.click(screen.getByText('codex-cli'));
    expect(onSelect).toHaveBeenCalledWith(SESSIONS[0]);
  });

  it('disables drop buttons in read-only mode and handles empty sessions', () => {
    const { rerender } = render(
      <SessionStrip
        sessions={SESSIONS}
        selectedSessionId={null}
        onSelectSession={vi.fn()}
        onDropSession={vi.fn()}
        droppingSessionId={null}
        writable={false}
        readOnlyReason="Read-only preview"
      />,
    );

    const dropButtons = screen.getAllByRole('button', { name: 'Drop Session' });
    expect(dropButtons).toHaveLength(2);
    for (const btn of dropButtons) {
      expect(btn).toBeDisabled();
    }

    rerender(
      <SessionStrip
        sessions={[]}
        selectedSessionId={null}
        onSelectSession={vi.fn()}
        onDropSession={vi.fn()}
        droppingSessionId={null}
      />,
    );
    expect(screen.getByText(/No active MCP sessions/i)).toBeInTheDocument();
  });
});

describe('TaskGateTable', () => {
  it('renders task rows, evidence ratios, test gates, and filters by origin', async () => {
    const user = userEvent.setup();
    const onSelectTask = vi.fn();
    const onReverify = vi.fn();
    const onChangeOriginFilter = vi.fn();

    const { rerender } = render(
      <TaskGateTable
        tasks={TASKS}
        selectedTaskId={null}
        onSelectTask={onSelectTask}
        onReverify={onReverify}
        reverifying={false}
        writable
        stalePulseIds={new Set()}
        originFilter="ALL"
        onChangeOriginFilter={onChangeOriginFilter}
      />,
    );

    expect(screen.getByText('Enforce loopback CSRF cookie')).toBeInTheDocument();
    expect(screen.getByText('Wire sqlite-vec KNN search')).toBeInTheDocument();
    expect(screen.getByText('2/2 Verified')).toBeInTheDocument();
    expect(screen.getByText('0/2 Verified')).toBeInTheDocument();
    expect(screen.getByText('STALE_CODE')).toBeInTheDocument();

    await user.click(screen.getByText('Enforce loopback CSRF cookie'));
    expect(onSelectTask).toHaveBeenCalledWith(TASKS[0]);

    await user.click(screen.getByRole('button', { name: /Re-verify/i }));
    expect(onReverify).toHaveBeenCalledTimes(1);

    rerender(
      <TaskGateTable
        tasks={TASKS}
        selectedTaskId={null}
        onSelectTask={onSelectTask}
        onReverify={onReverify}
        reverifying={false}
        writable={false}
        stalePulseIds={new Set()}
        originFilter="DEGRADED"
        onChangeOriginFilter={onChangeOriginFilter}
      />,
    );

    expect(screen.queryByText('Enforce loopback CSRF cookie')).not.toBeInTheDocument();
    expect(screen.getByText('Wire sqlite-vec KNN search')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Re-verify/i })).toBeDisabled();
  });
});
