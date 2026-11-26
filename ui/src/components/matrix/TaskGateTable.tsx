import { useMemo } from 'react';
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { CheckCircle2, FlaskConical, RefreshCw, ShieldAlert } from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { cn, formatShortTime } from '@/lib/utils';
import type { PlanTask } from '@/types/contracts';

interface TaskGateTableProps {
  tasks: PlanTask[];
  selectedTaskId: string | null;
  onSelectTask: (task: PlanTask) => void;
  onReverify: () => void;
  reverifying: boolean;
  stalePulseIds: ReadonlySet<string>;
}

const columnHelper = createColumnHelper<PlanTask>();

export function TaskGateTable({
  tasks,
  selectedTaskId,
  onSelectTask,
  onReverify,
  reverifying,
  stalePulseIds,
}: TaskGateTableProps) {
  const columns = useMemo(
    () => [
      columnHelper.accessor('id', {
        header: 'Task ID',
        cell: (info) => (
          <span className="whitespace-nowrap text-sky-400 font-mono-tabular font-medium">
            {info.getValue()}
          </span>
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
    data: tasks,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const planTitle = tasks[0]?.plan_title ?? 'Active Plan';

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex h-10 shrink-0 items-center justify-between border-b border-white/[0.06] px-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-xs font-semibold text-zinc-100">Task &amp; Gate Tree</span>
          <span className="truncate text-[11px] text-zinc-500 font-mono-tabular">
            {planTitle}
          </span>
        </div>

        <button
          type="button"
          disabled={reverifying}
          onClick={onReverify}
          className="flex h-7 items-center gap-1.5 rounded-md border border-sky-500/30 bg-sky-500/10 px-2.5 text-xs font-medium text-sky-300 transition-colors duration-150 hover:bg-sky-500/20 hover:text-sky-200 disabled:opacity-50"
        >
          <RefreshCw className={cn('h-3.5 w-3.5', reverifying && 'animate-spin')} strokeWidth={1.5} />
          <span>{reverifying ? 'Re-verifying…' : 'Re-verify Fingerprints'}</span>
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {tasks.length === 0 ? (
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
