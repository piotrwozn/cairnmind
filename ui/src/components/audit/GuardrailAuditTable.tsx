import { useMemo } from 'react';
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { motion } from 'framer-motion';
import { CheckCircle2, ShieldAlert, XCircle } from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { matchesOriginFilter, OriginBadge, OriginFilterBar } from '@/components/glass/OriginBadge';
import { cn, formatTimeMs } from '@/lib/utils';
import type { GuardrailAuditEntry, OriginFilter } from '@/types/contracts';

interface GuardrailAuditTableProps {
  entries: GuardrailAuditEntry[];
  selectedAuditId: string | null;
  onSelectAudit: (entry: GuardrailAuditEntry) => void;
  rejectedPulseIds: ReadonlySet<string>;
  originFilter: OriginFilter;
  onChangeOriginFilter: (filter: OriginFilter) => void;
}

const columnHelper = createColumnHelper<GuardrailAuditEntry>();

export function GuardrailAuditTable({
  entries,
  selectedAuditId,
  onSelectAudit,
  rejectedPulseIds,
  originFilter,
  onChangeOriginFilter,
}: GuardrailAuditTableProps) {
  const filteredEntries = useMemo(
    () => entries.filter((e) => matchesOriginFilter(e.origin, originFilter)),
    [entries, originFilter],
  );

  const columns = useMemo(
    () => [
      columnHelper.accessor('timestamp', {
        header: 'Time (HH:mm:ss.SSS)',
        cell: (info) => (
          <span className="text-zinc-400 font-mono-tabular">{formatTimeMs(info.getValue())}</span>
        ),
      }),
      columnHelper.accessor('session_id', {
        header: 'Session',
        cell: (info) => (
          <div className="font-mono-tabular">
            <div className="text-zinc-200">{info.getValue()}</div>
            <div className="text-[10px] text-zinc-500">{info.row.original.client}</div>
          </div>
        ),
      }),
      columnHelper.display({
        id: 'origin',
        header: 'Origin',
        cell: ({ row }) => <OriginBadge origin={row.original.origin} />,
      }),
      columnHelper.accessor('mcp_tool', {
        header: 'MCP Tool',
        cell: (info) => (
          <span className="rounded bg-white/[0.04] px-1.5 py-0.5 text-sky-300 font-mono-tabular">
            {info.getValue()}
          </span>
        ),
      }),
      columnHelper.accessor('verdict', {
        header: 'Verdict',
        cell: (info) => {
          const verdict = info.getValue();
          return (
            <GlassBadge tone={statusToTone(verdict)}>
              {verdict === 'ACCEPTED' ? (
                <CheckCircle2 className="h-3 w-3" strokeWidth={1.5} />
              ) : (
                <XCircle className="h-3 w-3" strokeWidth={1.5} />
              )}
              <span>{verdict}</span>
            </GlassBadge>
          );
        },
      }),
      columnHelper.accessor('reason_code', {
        header: 'Rule / Reason Code',
        cell: (info) => {
          const code = info.getValue();
          return (
            <div className="min-w-0">
              <div
                className={cn(
                  'font-mono-tabular text-[11px] font-medium',
                  code === 'OK' ? 'text-emerald-400' : 'text-rose-400',
                )}
              >
                {code}
              </div>
              <div className="truncate text-[11px] text-zinc-400">{info.row.original.message}</div>
            </div>
          );
        },
      }),
    ],
    [],
  );

  const table = useReactTable({
    data: filteredEntries,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const rejectedCount = filteredEntries.filter((e) => e.verdict === 'REJECTED').length;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-rose-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold text-zinc-100">
            Guardrail &amp; Rejection Audit Log
          </span>
          <span className="text-[11px] text-zinc-500 font-mono-tabular">
            Hallucination Firewall
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <OriginFilterBar value={originFilter} onChange={onChangeOriginFilter} />
          <GlassBadge tone="rose">{rejectedCount} REJECTED</GlassBadge>
          <GlassBadge tone="zinc">{filteredEntries.length} TOTAL</GlassBadge>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {filteredEntries.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-xs text-zinc-500 font-mono-tabular">
            No MCP guardrail events match the active filter.
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
                const entry = row.original;
                const isSelected = entry.id === selectedAuditId;
                const isPulsing = rejectedPulseIds.has(entry.id);
                return (
                  <motion.tr
                    key={entry.id}
                    initial={isPulsing ? { opacity: 0, y: -6 } : false}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.18 }}
                    onClick={() => onSelectAudit(entry)}
                    className={cn(
                      'cursor-pointer border-b border-white/[0.05] transition-colors duration-150',
                      isSelected
                        ? 'bg-sky-500/[0.09] hover:bg-sky-500/[0.12]'
                        : isPulsing
                          ? 'bg-rose-500/[0.15]'
                          : 'hover:bg-white/[0.04] active:bg-white/[0.06]',
                    )}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="max-w-[360px] px-3 py-2 align-middle">
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </motion.tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
