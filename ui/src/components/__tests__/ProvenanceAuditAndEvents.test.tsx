import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { GuardrailAuditTable } from '@/components/audit/GuardrailAuditTable';
import { EventDock } from '@/components/layout/EventDock';
import { ClaimList } from '@/components/provenance/ClaimList';
import type { EvidenceClaim, GuardrailAuditEntry, UiSseEvent } from '@/types/contracts';

const CLAIMS: EvidenceClaim[] = [
  {
    id: 'claim-ok',
    kind: 'decision',
    key: 'timeout-policy',
    title: 'Default request timeout is 30s',
    content: 'Keep timeout at 30 seconds.',
    status: 'confirmed',
    provenance_status: 'VERIFIED',
    source_path: 'src/app.py',
    line_start: 10,
    line_end: 12,
    verbatim_quote: 'REQUEST_TIMEOUT = 30',
    recorded_sha256: '1111111111111111',
    current_disk_sha256: '1111111111111111',
    quote_survives: true,
    drift_kind: 'MATCH',
    updated_at: '2026-11-28T08:00:00Z',
    task_ids: ['task-1'],
    version: 1,
  },
  {
    id: 'claim-stale',
    kind: 'fact',
    key: 'retry-limit',
    title: 'Retries capped at 3',
    content: 'Retry loop uses 3 attempts.',
    status: 'proposed',
    provenance_status: 'STALE',
    source_path: 'src/retry.py',
    line_start: 4,
    line_end: 6,
    verbatim_quote: 'MAX_RETRIES = 3',
    recorded_sha256: '2222222222222222',
    current_disk_sha256: '3333333333333333',
    quote_survives: false,
    drift_kind: 'QUOTE_MUTATED',
    updated_at: '2026-11-28T08:02:00Z',
    task_ids: [],
    version: 2,
  },
];

const AUDIT_ENTRIES: GuardrailAuditEntry[] = [
  {
    id: 'aud-1',
    timestamp: '2026-11-28T08:05:00.123Z',
    session_id: 'sess-1',
    client: 'claude-code',
    mcp_tool: 'cairnmind_remember',
    verdict: 'ACCEPTED',
    reason_code: 'OK',
    message: 'Recorded decision timeout-policy',
    target_id: 'claim-ok',
    arguments: {},
    violation_detail: {},
  },
  {
    id: 'aud-2',
    timestamp: '2026-11-28T08:06:00.456Z',
    session_id: 'sess-2',
    client: 'external-mcp',
    mcp_tool: 'cairnmind_plan_update',
    verdict: 'REJECTED',
    reason_code: 'OCC_STALE_RESUME_PACKET',
    message: 'Session ack revision behind current state_rev',
    target_id: 'plan-1',
    arguments: {},
    violation_detail: { acknowledged_rev: 3, current_rev: 7 },
  },
];

const SSE_EVENTS: UiSseEvent[] = [
  {
    id: 'evt-1',
    type: 'state.updated',
    timestamp: '2026-11-28T08:10:00.000Z',
    payload: { state_rev: 12, reason: 'connected' },
  },
  {
    id: 'evt-2',
    type: 'mcp.write.rejected',
    timestamp: '2026-11-28T08:10:05.000Z',
    payload: { message: 'OCC_STALE_RESUME_PACKET rejected write' },
  },
];

describe('ClaimList', () => {
  it('filters by VERIFIED / STALE tabs and search query and triggers selection', async () => {
    const user = userEvent.setup();
    const onSelectClaim = vi.fn();
    const onReverify = vi.fn();

    render(
      <ClaimList
        claims={CLAIMS}
        selectedClaimId={null}
        onSelectClaim={onSelectClaim}
        onlyStaleAndConflicts={false}
        stalePulseIds={new Set()}
        onReverify={onReverify}
        reverifying={false}
        writable
        originFilter="ALL"
        onChangeOriginFilter={vi.fn()}
      />,
    );

    expect(screen.getByText('Default request timeout is 30s')).toBeInTheDocument();
    expect(screen.getByText('Retries capped at 3')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'STALE' }));
    expect(screen.queryByText('Default request timeout is 30s')).not.toBeInTheDocument();
    expect(screen.getByText('Retries capped at 3')).toBeInTheDocument();

    await user.click(screen.getByText('Retries capped at 3'));
    expect(onSelectClaim).toHaveBeenCalledWith(CLAIMS[1]);
  });
});

describe('GuardrailAuditTable', () => {
  it('renders ACCEPTED and REJECTED verdicts with reason codes and selects rows', async () => {
    const user = userEvent.setup();
    const onSelectAudit = vi.fn();

    render(
      <GuardrailAuditTable
        entries={AUDIT_ENTRIES}
        selectedAuditId={null}
        onSelectAudit={onSelectAudit}
        rejectedPulseIds={new Set(['aud-2'])}
        originFilter="ALL"
        onChangeOriginFilter={vi.fn()}
      />,
    );

    expect(screen.getByText('cairnmind_remember')).toBeInTheDocument();
    expect(screen.getByText('OCC_STALE_RESUME_PACKET')).toBeInTheDocument();
    expect(screen.getByText('ACCEPTED')).toBeInTheDocument();
    expect(screen.getByText('REJECTED')).toBeInTheDocument();

    await user.click(screen.getByText('OCC_STALE_RESUME_PACKET'));
    expect(onSelectAudit).toHaveBeenCalledWith(AUDIT_ENTRIES[1]);
  });
});

describe('EventDock', () => {
  it('displays live SSE frames and supports toggling and clearing', async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    const onClear = vi.fn();

    render(<EventDock open onToggle={onToggle} events={SSE_EVENTS} onClear={onClear} />);

    expect(screen.getByText('Live Event Stream')).toBeInTheDocument();
    expect(screen.getByText(/connected state_rev=#12/)).toBeInTheDocument();
    expect(screen.getByText(/OCC_STALE_RESUME_PACKET rejected write/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Clear/i }));
    expect(onClear).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: /Live Event Stream/i }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});
