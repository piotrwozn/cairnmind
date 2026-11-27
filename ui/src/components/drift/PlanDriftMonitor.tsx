import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Compass,
  ExternalLink,
  FileCode2,
  FlaskConical,
  FolderTree,
  GitBranch,
  GitPullRequest,
  History,
  Loader2,
  Radio,
  Terminal,
  X,
  XCircle,
} from 'lucide-react';
import { decideDetectedDecision, fetchDriftMonitor, getCockpitAdapter } from '@/api/client';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { OriginBadge } from '@/components/glass/OriginBadge';
import { StatusPulseHighlight } from '@/components/glass/StatusPulse';
import { cn, formatShortTime, shortHash } from '@/lib/utils';
import type {
  ActiveView,
  DetectedDecisionStatus,
  DetectedSourceType,
  InspectorSelection,
  UiSnapshot,
} from '@/types/contracts';

interface PlanDriftMonitorProps {
  snapshot: UiSnapshot | undefined;
  stalePulseIds: ReadonlySet<string>;
  onJumpTo: (view: ActiveView, selection: InspectorSelection) => void;
  onInspectBlastFile: (filePath: string) => void;
  onOpenPlanVersions: (planId: string) => void;
}

const SOURCE_LABELS: Record<'all' | DetectedSourceType, string> = {
  all: 'All Sources',
  local_file: 'Local Files / ADR',
  github: 'GitHub PRs',
  ci: 'CI Scans',
};

function sourceIcon(sourceType: DetectedSourceType) {
  if (sourceType === 'github') return GitPullRequest;
  if (sourceType === 'ci') return Terminal;
  return FileCode2;
}

export function PlanDriftMonitor({
  snapshot,
  stalePulseIds,
  onJumpTo,
  onInspectBlastFile,
  onOpenPlanVersions,
}: PlanDriftMonitorProps) {
  const queryClient = useQueryClient();
  const [sourceFilter, setSourceFilter] = useState<'all' | DetectedSourceType>('all');
  const [directoryFilter, setDirectoryFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<'all' | DetectedDecisionStatus>('pending');
  const [page, setPage] = useState(1);

  const writable = snapshot?.capabilities?.writable ?? true;
  const readOnlyReason = snapshot?.capabilities?.read_only_reason;
  const adapter = getCockpitAdapter();
  const isMock = adapter.mode === 'mock';

  const { data: drift, isLoading } = useQuery({
    queryKey: ['ui-drift', sourceFilter, directoryFilter, statusFilter, page],
    queryFn: () =>
      fetchDriftMonitor({
        source: sourceFilter,
        directory: directoryFilter || undefined,
        status: statusFilter,
        page,
        page_size: 4,
      }),
    refetchOnWindowFocus: false,
  });

  const decideMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'accept' | 'ignore' }) =>
      decideDetectedDecision(id, action),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['ui-drift'] });
      void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
    },
  });

  const freshCounts = {
    FRESH: drift?.test_freshness.filter((t) => t.freshness === 'FRESH').length ?? 0,
    STALE: drift?.test_freshness.filter((t) => t.freshness === 'STALE').length ?? 0,
    REJECTED: drift?.test_freshness.filter((t) => t.freshness === 'REJECTED').length ?? 0,
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex items-center gap-2">
          <Compass className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold text-zinc-100">Plan &amp; Drift Monitor</span>
          <span className="hidden text-[11px] text-zinc-500 font-mono-tabular md:inline">
            Approved Plan vs Live Repository State
          </span>
        </div>

        <div className="flex items-center gap-2">
          {isMock && adapter.mockControls && (
            <button
              type="button"
              onClick={() => {
                void adapter.mockControls!.simulateFileEdit('src/cairnmind/evidence.py');
              }}
              title="Simulate a live IDE edit on src/cairnmind/evidence.py to verify repo_fingerprint_changed SSE refresh"
              className="flex h-7 items-center gap-1.5 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 text-[11px] font-medium text-amber-300 transition-colors hover:bg-amber-500/20"
            >
              <Radio className="h-3.5 w-3.5" strokeWidth={1.5} />
              <span>Simulate IDE File Edit (SSE)</span>
            </button>
          )}
          <GlassBadge tone="sky">SSE repo_fingerprint_changed</GlassBadge>
        </div>
      </div>

      {isLoading && !drift ? (
        <div className="flex flex-1 items-center justify-center gap-2 text-xs text-zinc-400 font-mono-tabular">
          <Loader2 className="h-4 w-4 animate-spin text-sky-400" strokeWidth={1.5} />
          <span>Comparing approved plan against repository state…</span>
        </div>
      ) : drift ? (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="flex flex-col gap-3">
            {/* Top Comparison Strip: Last Approved Plan vs Current Repo State */}
            <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2">
              <div className="flex flex-col gap-2 rounded-md border border-white/[0.08] bg-zinc-950/65 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                    Last Approved Plan
                  </span>
                  {drift.approved_plan && (
                    <div className="flex items-center gap-1.5">
                      <OriginBadge origin={drift.approved_plan.origin} compact />
                      <button
                        type="button"
                        onClick={() => onOpenPlanVersions(drift.approved_plan!.id)}
                        className="flex items-center gap-1 rounded border border-white/[0.1] bg-white/[0.04] px-1.5 py-0.5 text-[10px] text-sky-300 font-mono-tabular hover:bg-sky-500/15"
                      >
                        <History className="h-3 w-3" strokeWidth={1.5} />
                        <span>v{drift.approved_plan.version}</span>
                      </button>
                    </div>
                  )}
                </div>

                {drift.approved_plan ? (
                  <>
                    <div className="text-sm font-semibold text-zinc-100">
                      {drift.approved_plan.title}
                    </div>
                    <p className="text-xs text-zinc-400">{drift.approved_plan.goal}</p>
                    <div className="flex flex-wrap items-center gap-2 pt-0.5 text-[11px] font-mono-tabular text-zinc-500">
                      <span>{drift.approved_plan.steps.length} steps</span>
                      <span>·</span>
                      <span>{drift.approved_plan.linked_files.length} linked files</span>
                      <span>·</span>
                      <span>updated {formatShortTime(drift.approved_plan.updated_at)}</span>
                    </div>
                  </>
                ) : (
                  <div className="text-xs text-zinc-500 font-mono-tabular">
                    No active approved plan yet. Promote a draft plan to start tracking drift.
                  </div>
                )}
              </div>

              <div className="flex flex-col gap-2 rounded-md border border-white/[0.08] bg-zinc-950/65 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                    Current Repository State
                  </span>
                  <GlassBadge tone={drift.repo_state.dirty_files_count > 0 ? 'amber' : 'emerald'}>
                    {drift.repo_state.dirty_files_count > 0
                      ? `${drift.repo_state.dirty_files_count} DRIFTED FILES`
                      : 'IN SYNC'}
                  </GlassBadge>
                </div>

                <div className="grid grid-cols-2 gap-2 text-[11px] font-mono-tabular">
                  <div className="rounded border border-white/[0.06] bg-white/[0.02] px-2 py-1.5">
                    <div className="text-[10px] text-zinc-500">Branch / HEAD</div>
                    <div className="flex items-center gap-1 text-zinc-200">
                      <GitBranch className="h-3 w-3 text-sky-400" strokeWidth={1.5} />
                      <span>
                        {drift.repo_state.branch ?? 'detached'} @{' '}
                        {shortHash(drift.repo_state.head, 7)}
                      </span>
                    </div>
                  </div>
                  <div className="rounded border border-white/[0.06] bg-white/[0.02] px-2 py-1.5">
                    <div className="text-[10px] text-zinc-500">Worktree Fingerprint</div>
                    <div className="truncate text-zinc-200">
                      {shortHash(drift.repo_state.fingerprint, 16)}
                    </div>
                  </div>
                </div>

                {/* Test Freshness Quick Summary */}
                <div className="flex flex-wrap items-center gap-2 pt-0.5">
                  <span className="text-[11px] text-zinc-500">Test Freshness:</span>
                  <GlassBadge tone="emerald">{freshCounts.FRESH} FRESH</GlassBadge>
                  <GlassBadge tone="amber">{freshCounts.STALE} STALE</GlassBadge>
                  <GlassBadge tone="rose">{freshCounts.REJECTED} REJECTED</GlassBadge>
                </div>
              </div>
            </div>

            {/* Middle Split: Drifted Files + Test Freshness per Task */}
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {/* Drifted Files */}
              <div className="flex flex-col gap-2 rounded-md border border-white/[0.08] bg-zinc-950/55 p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5">
                    <AlertTriangle className="h-3.5 w-3.5 text-amber-400" strokeWidth={1.5} />
                    <span className="text-xs font-semibold text-zinc-100">
                      File &amp; Evidence Drift ({drift.drift_items.length})
                    </span>
                  </div>
                  <span className="text-[10px] text-zinc-500 font-mono-tabular">
                    Human &amp; Agent Edits
                  </span>
                </div>

                {drift.drift_items.length === 0 ? (
                  <div className="rounded border border-emerald-500/20 bg-emerald-500/[0.05] p-3 text-xs text-emerald-300 font-mono-tabular">
                    Zero drift — all linked source files match recorded plan &amp; evidence fingerprints.
                  </div>
                ) : (
                  <div className="flex flex-col gap-1.5">
                    {drift.drift_items.map((item) => {
                      const pulsing = item.impacted_claim_ids.some((id) => stalePulseIds.has(id));
                      return (
                        <StatusPulseHighlight key={item.id} active={pulsing} tone="amber" className="rounded-md">
                          <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.07] bg-white/[0.02] p-2.5">
                            <div className="flex flex-wrap items-center justify-between gap-1.5">
                              <span className="flex min-w-0 items-center gap-1.5 font-mono-tabular text-xs font-medium text-zinc-100">
                                <FileCode2 className="h-3.5 w-3.5 shrink-0 text-amber-400" strokeWidth={1.5} />
                                <span className="truncate">{item.file_path}</span>
                              </span>
                              <div className="flex items-center gap-1.5">
                                <OriginBadge origin={item.origin} compact />
                                <GlassBadge tone={item.drift_type === 'QUOTE_MUTATED' ? 'rose' : 'amber'}>
                                  {item.drift_type}
                                </GlassBadge>
                              </div>
                            </div>

                            <p className="text-[11px] text-zinc-400">{item.summary}</p>

                            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] font-mono-tabular text-zinc-500">
                              <span>
                                rec:{shortHash(item.recorded_sha256, 7)} -&gt; cur:
                                <span className="text-amber-300">{shortHash(item.current_sha256, 7)}</span>
                              </span>

                              <div className="flex flex-wrap items-center gap-1">
                                {item.impacted_task_ids.map((tid) => (
                                  <button
                                    key={tid}
                                    type="button"
                                    onClick={() => onJumpTo('matrix', { kind: 'task', id: tid })}
                                    className="rounded border border-white/[0.08] bg-zinc-950/70 px-1.5 py-0.5 text-sky-300 hover:bg-white/[0.08]"
                                  >
                                    {tid}
                                  </button>
                                ))}
                                <button
                                  type="button"
                                  onClick={() => onInspectBlastFile(item.file_path)}
                                  className="flex items-center gap-0.5 rounded border border-sky-500/25 bg-sky-500/10 px-1.5 py-0.5 text-sky-300 hover:bg-sky-500/20"
                                >
                                  <span>Blast</span>
                                  <ExternalLink className="h-2.5 w-2.5" strokeWidth={1.5} />
                                </button>
                              </div>
                            </div>
                          </div>
                        </StatusPulseHighlight>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Test Freshness per Task */}
              <div className="flex flex-col gap-2 rounded-md border border-white/[0.08] bg-zinc-950/55 p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5">
                    <FlaskConical className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                    <span className="text-xs font-semibold text-zinc-100">
                      Test Freshness per Task ({drift.test_freshness.length})
                    </span>
                  </div>
                  <span className="text-[10px] text-zinc-500 font-mono-tabular">
                    Green = Fresh · Amber = Stale · Red = Rejected
                  </span>
                </div>

                <div className="flex flex-col gap-1.5">
                  {drift.test_freshness.map((row) => {
                    const tone =
                      row.freshness === 'FRESH'
                        ? 'emerald'
                        : row.freshness === 'STALE'
                          ? 'amber'
                          : 'rose';
                    const Icon =
                      row.freshness === 'FRESH'
                        ? CheckCircle2
                        : row.freshness === 'STALE'
                          ? AlertTriangle
                          : XCircle;
                    return (
                      <button
                        key={row.task_id}
                        type="button"
                        onClick={() => onJumpTo('matrix', { kind: 'task', id: row.task_id })}
                        className="flex items-center justify-between gap-2 rounded-md border border-white/[0.07] bg-white/[0.02] px-2.5 py-2 text-left transition-colors hover:bg-white/[0.05]"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-xs font-semibold text-sky-400 font-mono-tabular">
                              {row.task_id}
                            </span>
                            <OriginBadge origin={row.origin} compact />
                            <span className="truncate text-xs text-zinc-200">{row.task_title}</span>
                          </div>
                          <div className="mt-0.5 truncate text-[11px] text-zinc-500 font-mono-tabular">
                            {row.profile_name} · {row.reason}
                          </div>
                        </div>

                        <GlassBadge tone={tone} className="shrink-0">
                          <Icon className="h-3 w-3" strokeWidth={1.5} />
                          <span>{row.freshness}</span>
                        </GlassBadge>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* Bottom Section: Detected Project Decisions & Plans (Repo / GitHub / CI) with Anti-Noise Filters & Pagination */}
            <div className="flex flex-col gap-2.5 rounded-md border border-white/[0.08] bg-zinc-950/60 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <FolderTree className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
                  <span className="text-xs font-semibold text-zinc-100">
                    Detected Project Decisions &amp; Plans
                  </span>
                  <GlassBadge tone="zinc">{drift.detected_decisions.total} matching</GlassBadge>
                </div>

                {/* Source & Directory & Status filters (Monorepo anti-noise) */}
                <div className="flex flex-wrap items-center gap-1.5">
                  <div className="flex items-center gap-0.5 rounded-md border border-white/[0.08] bg-zinc-950/80 p-0.5">
                    {(['all', 'local_file', 'github', 'ci'] as const).map((src) => {
                      const active = sourceFilter === src;
                      const count =
                        src === 'all'
                          ? Object.values(drift.detected_decisions.source_counts).reduce((a, b) => a + b, 0)
                          : drift.detected_decisions.source_counts[src];
                      return (
                        <button
                          key={src}
                          type="button"
                          onClick={() => {
                            setSourceFilter(src);
                            setPage(1);
                          }}
                          className={cn(
                            'rounded px-2 py-0.5 text-[11px] font-mono-tabular transition-colors',
                            active
                              ? 'border border-white/[0.12] bg-white/[0.09] text-zinc-100'
                              : 'border border-transparent text-zinc-500 hover:text-zinc-300',
                          )}
                        >
                          {SOURCE_LABELS[src]} ({count})
                        </button>
                      );
                    })}
                  </div>

                  <select
                    aria-label="Filter detected decisions by directory"
                    value={directoryFilter}
                    onChange={(e) => {
                      setDirectoryFilter(e.target.value);
                      setPage(1);
                    }}
                    className="h-7 rounded-md border border-white/[0.08] bg-zinc-950/80 px-2 text-[11px] text-zinc-300 font-mono-tabular focus:border-sky-500/40 focus:outline-none"
                  >
                    <option value="">All directories</option>
                    {drift.detected_decisions.directories.map((dir) => (
                      <option key={dir} value={dir}>
                        {dir}
                      </option>
                    ))}
                  </select>

                  <select
                    aria-label="Filter detected decisions by status"
                    value={statusFilter}
                    onChange={(e) => {
                      setStatusFilter(e.target.value as 'all' | DetectedDecisionStatus);
                      setPage(1);
                    }}
                    className="h-7 rounded-md border border-white/[0.08] bg-zinc-950/80 px-2 text-[11px] text-zinc-300 font-mono-tabular focus:border-sky-500/40 focus:outline-none"
                  >
                    <option value="pending">Pending only</option>
                    <option value="all">All statuses</option>
                    <option value="accepted">Accepted</option>
                    <option value="ignored">Ignored</option>
                  </select>
                </div>
              </div>

              {drift.detected_decisions.items.length === 0 ? (
                <div className="rounded border border-dashed border-white/[0.08] p-5 text-center text-xs text-zinc-500 font-mono-tabular">
                  No detected project decisions match the selected directory/source filters.
                </div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {drift.detected_decisions.items.map((item) => {
                    const SrcIcon = sourceIcon(item.source_type);
                    const isPending = item.status === 'pending';
                    return (
                      <div
                        key={item.id}
                        className="flex flex-col gap-2 rounded-md border border-white/[0.07] bg-white/[0.02] p-2.5 sm:flex-row sm:items-center sm:justify-between"
                      >
                        <div className="min-w-0 flex-1 space-y-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <GlassBadge tone="zinc">{item.kind.toUpperCase()}</GlassBadge>
                            <OriginBadge origin={item.origin} compact />
                            <span className="flex items-center gap-1 text-[11px] text-sky-300 font-mono-tabular">
                              <SrcIcon className="h-3 w-3" strokeWidth={1.5} />
                              <span>{item.source_ref}</span>
                            </span>
                            <span className="text-[10px] text-zinc-500 font-mono-tabular">
                              {item.directory}
                            </span>
                            {!isPending && (
                              <GlassBadge tone={statusToTone(item.status === 'accepted' ? 'ACCEPTED' : 'STALE')}>
                                {item.status.toUpperCase()}
                              </GlassBadge>
                            )}
                          </div>
                          <div className="text-xs font-semibold text-zinc-100">{item.title}</div>
                          <p className="text-[11px] text-zinc-400">{item.summary}</p>
                        </div>

                        <div className="flex shrink-0 items-center gap-1.5">
                          {isPending ? (
                            <>
                              <button
                                type="button"
                                disabled={!writable || decideMutation.isPending}
                                title={
                                  writable
                                    ? 'Zaakceptuj do pamięci projektu'
                                    : readOnlyReason || 'Read-only mode enabled'
                                }
                                onClick={() => decideMutation.mutate({ id: item.id, action: 'accept' })}
                                className="flex h-7 items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/12 px-2.5 text-[11px] font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                <Check className="h-3.5 w-3.5" strokeWidth={1.5} />
                                <span>Zaakceptuj do pamięci</span>
                              </button>
                              <button
                                type="button"
                                disabled={!writable || decideMutation.isPending}
                                title={
                                  writable
                                    ? 'Ignoruj wykrytą propozycję'
                                    : readOnlyReason || 'Read-only mode enabled'
                                }
                                onClick={() => decideMutation.mutate({ id: item.id, action: 'ignore' })}
                                className="flex h-7 items-center gap-1 rounded-md border border-white/[0.1] bg-white/[0.03] px-2 text-[11px] font-medium text-zinc-400 transition-colors hover:bg-white/[0.07] hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                <X className="h-3.5 w-3.5" strokeWidth={1.5} />
                                <span>Ignoruj</span>
                              </button>
                            </>
                          ) : (
                            <span className="text-[11px] text-zinc-500 font-mono-tabular">
                              {formatShortTime(item.detected_at)}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Pagination Bar */}
              <div className="flex items-center justify-between border-t border-white/[0.06] pt-2 text-[11px] font-mono-tabular text-zinc-400">
                <span>
                  Page {drift.detected_decisions.page} / {drift.detected_decisions.total_pages} ·{' '}
                  {drift.detected_decisions.total} total
                </span>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    disabled={drift.detected_decisions.page <= 1}
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    className="flex h-6 items-center gap-1 rounded border border-white/[0.08] bg-white/[0.03] px-2 text-zinc-300 hover:bg-white/[0.08] disabled:opacity-40"
                  >
                    <ChevronLeft className="h-3 w-3" strokeWidth={1.5} />
                    <span>Prev</span>
                  </button>
                  <button
                    type="button"
                    disabled={drift.detected_decisions.page >= drift.detected_decisions.total_pages}
                    onClick={() =>
                      setPage((p) => Math.min(drift.detected_decisions.total_pages, p + 1))
                    }
                    className="flex h-6 items-center gap-1 rounded border border-white/[0.08] bg-white/[0.03] px-2 text-zinc-300 hover:bg-white/[0.08] disabled:opacity-40"
                  >
                    <span>Next</span>
                    <ChevronRight className="h-3 w-3" strokeWidth={1.5} />
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
