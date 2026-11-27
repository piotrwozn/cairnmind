import { useMemo } from 'react';
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { CheckCircle2, FlaskConical, History, RefreshCw, ShieldAlert } from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { matchesOriginFilter, OriginBadge, OriginFilterBar } from '@/components/glass/OriginBadge';
import { cn, formatShortTime } from '@/lib/utils';
import type { OriginFilter, PlanSummary, PlanTask } from '@/types/contracts';

interface TaskGateTableProps {
  tasks: PlanTask[];
  activePlan?: PlanSummary | null;
  selectedTaskId: string | null;
  onSelectTask: (task: PlanTask) => void;
  onReverify: () => void;
  reverifying: boolean;
  writable?: boolean;
  readOnlyReason?: string | null;
  stalePulseIds: ReadonlySet<string>;
  originFilter: OriginFilter;
  onChangeOriginFilter: (filter: OriginFilter) => void;
  onOpenPlanVersions?: (planId: string) => void;
}

const columnHelper = createColumnHelper<PlanTask>();

export function TaskGateTable({
  tasks,
  activePlan,
  selectedTaskId,
  onSelectTask,
  onReverify,
  reverifying,
  writable = true,
  readOnlyReason,
  stalePulseIds,
  originFilter,
  onChangeOriginFilter,
  onOpenPlanVersions,
}: TaskGateTableProps) {
  const filteredTasks = useMemo(
    () => tasks.filter((t) => matchesOriginFilter(t.origin, originFilter)),
    [originFilter, tasks],
  );

  const columns = useMemo(
    () => [
      columnHelper.accessor('id', {
        header: 'Task ID',
        cell: (info) => (
          <div className="flex flex-col gap-0.5">
            <span className="whitespace-nowrap text-sky-400 font-mono-tabular font-medium">
              {info.getValue()}
            </span>
            {info.row.original.external_ref && (
              <span className="text-[10px] text-zinc-500 font-mono-tabular">
                {info.row.original.external_ref}
              </span>
            )}
          </div>
        ),
      }),
      columnHelper.accessor('title', {
        header: 'Title',
        cell: (info) => (
          <div className="min-w-0">
            <div className="truncate text-zinc-100 font-medium">{info.getValue()}</div>
            <div className="truncate text-[11px] text-zinc-500 font-mono-tabular">
              {info.row.original.step_key} · v{info.row.original.version}
            </div>
          </div>
        ),
      }),
      columnHelper.display({
        id: 'origin',
        header: 'Origin',
        cell: ({ row }) => <OriginBadge origin={row.original.origin} />,
      }),
      columnHelper.accessor('state', {
        header: 'Status',
        cell: (info) => (
          <GlassBadge tone={statusToTone(info.getValue())} className="whitespace-nowrap">
            {info.getValue().toUpperCase()}
          </GlassBadge>
        ),
      }),
      columnHelper.display({
        id: 'evidence_health',
        header: 'Evidence Health',
        cell: ({ row }) => {
          const { evidence_verified: verified, evidence_total: total } = row.original;
          const pct = total > 0 ? Math.round((verified / total) * 100) : 0;
          const tone = total > 0 && verified === total ? 'emerald' : 'amber';
          return (
            <div className="flex items-center gap-2 whitespace-nowrap">
              <GlassBadge tone={tone} className="whitespace-nowrap">
                {tone === 'emerald' ? (
                  <CheckCircle2 className="h-3 w-3 shrink-0" strokeWidth={1.5} />
                ) : (
                  <ShieldAlert className="h-3 w-3 shrink-0" strokeWidth={1.5} />
                )}
                <span>
                  {verified}/{total} Verified
                </span>
              </GlassBadge>
              <div className="hidden h-1.5 w-10 overflow-hidden rounded-full bg-white/[0.06] 2xl:block">
                <div
                  className={cn(
                    'h-full rounded-full transition-all duration-300',
                    tone === 'emerald' ? 'bg-emerald-400' : 'bg-amber-400',
                  )}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          );
        },
      }),
      columnHelper.display({
        id: 'test_gate',
        header: 'Test Gate',
        cell: ({ row }) => {
          const gate = row.original.test_gate;
          const tone = statusToTone(gate.status);
          return (
            <div className="flex items-center gap-1.5 whitespace-nowrap font-mono-tabular">
              <FlaskConical className="h-3.5 w-3.5 shrink-0 text-zinc-500" strokeWidth={1.5} />
              <span className="truncate text-zinc-300 max-w-[115px]">{gate.profile_name}</span>
              <GlassBadge tone={tone} className="whitespace-nowrap">
                <span>{gate.status}</span>
                {gate.sha256_short && (
                  <span className="opacity-75">@ {gate.sha256_short}</span>
                )}
              </GlassBadge>
            </div>
          );
        },
      }),
      columnHelper.accessor('updated_at', {
        header: 'Updated At',
        cell: (info) => (
          <span className="whitespace-nowrap text-zinc-500 font-mono-tabular">
            {formatShortTime(info.getValue())}
          </span>
        ),
      }),
    ],
    [],
  );

  const table = useReactTable({
    data: filteredTasks,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const planTitle = activePlan?.title ?? tasks[0]?.plan_title ?? 'Active Plan';

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-xs font-semibold text-zinc-100">Task &amp; Gate Tree</span>
          <span className="truncate text-[11px] text-zinc-500 font-mono-tabular">
            {planTitle}
          </span>
          {activePlan && (
            <>
              <OriginBadge origin={activePlan.origin} compact />
              {onOpenPlanVersions && (
                <button
                  type="button"
                  onClick={() => onOpenPlanVersions(activePlan.id)}
                  title="Open plan version history & time-travel diff"
                  className="flex h-6 items-center gap-1 rounded border border-white/[0.1] bg-white/[0.03] px-1.5 text-[10px] font-medium text-zinc-300 font-mono-tabular transition-colors hover:border-sky-500/35 hover:bg-sky-500/10 hover:text-sky-200"
                >
                  <History className="h-3 w-3 text-sky-400" strokeWidth={1.5} />
                  <span>v{activePlan.version}</span>
                </button>
              )}
            </>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <OriginFilterBar value={originFilter} onChange={onChangeOriginFilter} />

          <button
            type="button"
            disabled={reverifying || !writable}
            title={!writable ? readOnlyReason || 'Read-only mode enabled' : 'Scan worktree hashes and re-verify claims'}
            onClick={onReverify}
            className="flex h-7 items-center gap-1.5 rounded-md border border-sky-500/30 bg-sky-500/10 px-2.5 text-xs font-medium text-sky-300 transition-colors duration-150 hover:bg-sky-500/20 hover:text-sky-200 disabled:cursor-not-allowed disabled:opacity-45"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', reverifying && 'animate-spin')} strokeWidth={1.5} />
            <span>{reverifying ? 'Re-verifying…' : 'Re-verify Fingerprints'}</span>
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {filteredTasks.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-xs text-zinc-500 font-mono-tabular">
            No tasks match the current filter.
          </div>
        ) : (
          <table className="w-full border-collapse text-left text-xs">
            <thead className="sticky top-0 z-10 bg-zinc-950/90 backdrop-blur-md">
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id} className="border-b border-white/[0.08]">
                  {headerGroup.headers.map((header) => (
                    <th
                      key={header.id}
                      className="px-3 py-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500"
                    >
                      {header.isPlaceholder
                        ? null
                        : flexRender(header.column.columnDef.header, header.getContext())}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map((row) => {
                const task = row.original;
                const isSelected = task.id === selectedTaskId;
                const pulsing = task.claim_ids.some((cid) => stalePulseIds.has(cid));
                return (
                  <tr
                    key={row.id}
                    onClick={() => onSelectTask(task)}
                    className={cn(
                      'cursor-pointer border-b border-white/[0.05] transition-colors duration-150',
                      isSelected
                        ? 'bg-sky-500/[0.09] hover:bg-sky-500/[0.12]'
                        : 'hover:bg-white/[0.04] active:bg-white/[0.06]',
                      pulsing && 'bg-amber-500/[0.12]',
                    )}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="max-w-[300px] px-3 py-2 align-middle">
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
