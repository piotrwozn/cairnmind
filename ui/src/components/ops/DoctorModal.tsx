import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Database,
  Loader2,
  RefreshCw,
  Server,
  X,
} from 'lucide-react';
import { fetchDoctor } from '@/api/client';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { cn, formatShortTime } from '@/lib/utils';

interface DoctorModalProps {
  open: boolean;
  onClose: () => void;
}

export function DoctorModal({ open, onClose }: DoctorModalProps) {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['ui-doctor'],
    queryFn: fetchDoctor,
    enabled: open,
    refetchOnWindowFocus: false,
  });

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) onClose();
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.98, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -6 }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
            className="flex max-h-[85vh] w-full max-w-[640px] flex-col overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/90 shadow-[0_20px_60px_rgba(0,0,0,0.75),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
          >
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-white/[0.08] px-4">
              <div className="flex items-center gap-2">
                <Activity className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
                <span className="text-xs font-semibold text-zinc-100">
                  Doctor &amp; System Healthcheck
                </span>
                {data && (
                  <GlassBadge
                    tone={
                      data.status === 'HEALTHY'
                        ? 'emerald'
                        : data.status === 'WARNING'
                          ? 'amber'
                          : 'rose'
                    }
                  >
                    {data.status}
                  </GlassBadge>
                )}
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => void refetch()}
                  disabled={isFetching}
                  className="flex h-6 items-center gap-1 rounded border border-white/[0.08] bg-white/[0.03] px-2 text-[11px] text-zinc-300 hover:bg-white/[0.08]"
                >
                  <RefreshCw className={cn('h-3 w-3', isFetching && 'animate-spin')} strokeWidth={1.5} />
                  <span>Re-run</span>
                </button>
                <button
                  type="button"
                  onClick={onClose}
                  className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100"
                >
                  <X className="h-4 w-4" strokeWidth={1.5} />
                </button>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-4 space-y-3">
              {isLoading || !data ? (
                <div className="flex h-40 items-center justify-center gap-2 text-xs text-zinc-400 font-mono-tabular">
                  <Loader2 className="h-4 w-4 animate-spin text-sky-400" strokeWidth={1.5} />
                  <span>Running GET /api/doctor…</span>
                </div>
              ) : (
                <>
                  {/* Storage & LLM cards */}
                  <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                    <div className="rounded-md border border-white/[0.08] bg-zinc-950/70 p-3 space-y-1.5">
                      <div className="flex items-center justify-between">
                        <span className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
                          <Database className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                          <span>Storage Backend</span>
                        </span>
                        <GlassBadge tone="sky">{data.backend_mode.toUpperCase()}</GlassBadge>
                      </div>
                      <div className="text-[11px] font-mono-tabular text-zinc-300">
                        Access:{' '}
                        <span className={data.writable ? 'text-emerald-400' : 'text-amber-400'}>
                          {data.writable ? 'READ-WRITE' : 'READ-ONLY'}
                        </span>
                      </div>
                      <div className="text-[11px] font-mono-tabular text-zinc-500">
                        Index freshness:{' '}
                        <span
                          className={
                            data.index_freshness.status === 'FRESH'
                              ? 'text-emerald-400'
                              : 'text-amber-400'
                          }
                        >
                          {data.index_freshness.status}
                        </span>{' '}
                        · {data.index_freshness.files_indexed} files
                      </div>
                    </div>

                    <div className="rounded-md border border-white/[0.08] bg-zinc-950/70 p-3 space-y-1.5">
                      <div className="flex items-center justify-between">
                        <span className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
                          <Server className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                          <span>Knowledge Models</span>
                        </span>
                        <GlassBadge tone={data.llm_status.configured ? 'emerald' : 'amber'}>
                          {data.llm_status.configured ? 'ENABLED' : 'TIER 0 FTS'}
                        </GlassBadge>
                      </div>
                      <div className="truncate text-[11px] font-mono-tabular text-zinc-300">
                        chat: {data.llm_status.chat_model ?? 'disabled'}
                      </div>
                      <div className="truncate text-[11px] font-mono-tabular text-zinc-500">
                        embed: {data.llm_status.embedding_model ?? 'disabled'} · fallback:{' '}
                        {data.llm_status.fallback}
                      </div>
                    </div>
                  </div>

                  {/* Scaling Thresholds */}
                  <div className="rounded-md border border-white/[0.08] bg-zinc-950/65 p-3 space-y-2">
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                      Scaling Thresholds &amp; Capacity
                    </span>
                    <div className="space-y-2">
                      {data.scaling_thresholds.map((item) => {
                        const pct = Math.min(100, Math.round((item.current / Math.max(1, item.threshold)) * 100));
                        return (
                          <div key={item.metric} className="space-y-1 text-[11px] font-mono-tabular">
                            <div className="flex items-center justify-between">
                              <span className="text-zinc-300">{item.metric}</span>
                              <span className="flex items-center gap-1.5">
                                <span className="text-zinc-400">
                                  {item.current} / {item.threshold} {item.unit}
                                </span>
                                <GlassBadge
                                  tone={
                                    item.status === 'OK'
                                      ? 'emerald'
                                      : item.status === 'WARN'
                                        ? 'amber'
                                        : 'rose'
                                  }
                                >
                                  {item.status}
                                </GlassBadge>
                              </span>
                            </div>
                            <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
                              <div
                                className={cn(
                                  'h-full rounded-full transition-all',
                                  item.status === 'OK'
                                    ? 'bg-emerald-400'
                                    : item.status === 'WARN'
                                      ? 'bg-amber-400'
                                      : 'bg-rose-400',
                                )}
                                style={{ width: `${Math.max(4, pct)}%` }}
                              />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  {/* Warnings */}
                  <div className="rounded-md border border-white/[0.08] bg-zinc-950/65 p-3 space-y-1.5">
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                      Diagnostics ({data.warnings.length})
                    </span>
                    {data.warnings.length === 0 ? (
                      <div className="flex items-center gap-2 text-xs text-emerald-300">
                        <CheckCircle2 className="h-3.5 w-3.5" strokeWidth={1.5} />
                        <span>All storage, index, and provenance checks passed.</span>
                      </div>
                    ) : (
                      <ul className="space-y-1 text-xs text-amber-200">
                        {data.warnings.map((w) => (
                          <li key={w} className="flex items-start gap-1.5">
                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" strokeWidth={1.5} />
                            <span>{w}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className="pt-1 text-[10px] text-zinc-500 font-mono-tabular">
                      checked at {formatShortTime(data.checked_at)} · worktree fp:{' '}
                      {data.index_freshness.worktree_fingerprint ?? '—'}
                    </div>
                  </div>
                </>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
