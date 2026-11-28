import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowRight,
  CopyPlus,
  ExternalLink,
  History,
  Loader2,
  ShieldAlert,
  User,
  Bot,
  X,
} from 'lucide-react';
import { cloneVersionAsDraft, fetchVersionHistory } from '@/api/client';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { OriginBadge } from '@/components/glass/OriginBadge';
import { cn, formatShortTime } from '@/lib/utils';
import type { VersionEntityKind } from '@/types/contracts';

export interface VersionModalTarget {
  kind: VersionEntityKind;
  id: string;
  label?: string;
}

interface VersionHistoryModalProps {
  target: VersionModalTarget | null;
  writable: boolean;
  readOnlyReason?: string | null;
  onClose: () => void;
  onJumpToAudit: (auditId: string) => void;
  onClonedDraft: (message: string) => void;
}

function renderVal(val: unknown): string {
  if (val === null || val === undefined) return '—';
  if (typeof val === 'string') return val;
  try {
    return JSON.stringify(val, null, 2);
  } catch {
    return String(val);
  }
}

export function VersionHistoryModal({
  target,
  writable,
  readOnlyReason,
  onClose,
  onJumpToAudit,
  onClonedDraft,
}: VersionHistoryModalProps) {
  const queryClient = useQueryClient();
  const [fromVersion, setFromVersion] = useState<number | undefined>(undefined);
  const [toVersion, setToVersion] = useState<number | undefined>(undefined);
  const [cloning, setCloning] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    setFromVersion(undefined);
    setToVersion(undefined);
    setErrorMsg(null);
  }, [target?.kind, target?.id]);

  const { data, isLoading } = useQuery({
    queryKey: ['ui-versions', target?.kind, target?.id, fromVersion, toVersion],
    queryFn: () =>
      fetchVersionHistory(target!.kind, target!.id, fromVersion, toVersion),
    enabled: Boolean(target),
    refetchOnWindowFocus: false,
  });

  const handleCloneAsDraft = async (sourceVersion: number) => {
    if (!target) return;
    setCloning(true);
    setErrorMsg(null);
    try {
      await cloneVersionAsDraft(target.kind, target.id, sourceVersion);
      void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
      void queryClient.invalidateQueries({ queryKey: ['ui-versions'] });
      onClonedDraft(
        `Cloned ${target.kind} ${target.id} v${sourceVersion} as a new Draft (audit history preserved).`,
      );
      onClose();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to clone version as draft');
    } finally {
      setCloning(false);
    }
  };

  return (
    <AnimatePresence>
      {target && (
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
            className="flex max-h-[86vh] w-full max-w-[780px] flex-col overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/90 shadow-[0_20px_60px_rgba(0,0,0,0.75),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
          >
            {/* Header */}
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-white/[0.08] px-4">
              <div className="flex items-center gap-2 min-w-0">
                <History className="h-4 w-4 shrink-0 text-sky-400" strokeWidth={1.5} />
                <span className="truncate text-xs font-semibold text-zinc-100">
                  Time-Travel Version Switcher &amp; Diff
                </span>
                <GlassBadge tone="sky">
                  {target.kind.toUpperCase()} · {target.id}
                </GlassBadge>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100"
              >
                <X className="h-4 w-4" strokeWidth={1.5} />
              </button>
            </div>

            {isLoading || !data ? (
              <div className="flex h-56 items-center justify-center gap-2 text-xs text-zinc-400 font-mono-tabular">
                <Loader2 className="h-4 w-4 animate-spin text-sky-400" strokeWidth={1.5} />
                <span>Loading version snapshots…</span>
              </div>
            ) : (
              <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
                {/* Version Selector Bar */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] bg-zinc-950/60 px-4 py-2.5">
                  <div className="flex flex-wrap items-center gap-2 text-xs font-mono-tabular">
                    <span className="text-zinc-500">Compare:</span>
                    <select
                      aria-label="Compare from version"
                      value={data.from_version}
                      onChange={(e) => setFromVersion(Number(e.target.value))}
                      className="h-7 rounded border border-white/[0.1] bg-zinc-950 px-2 text-xs text-zinc-100 focus:border-sky-500/40 focus:outline-none"
                    >
                      {data.versions.map((v) => (
                        <option key={v.version} value={v.version}>
                          v{v.version} ({v.reason})
                        </option>
                      ))}
                    </select>
                    <ArrowRight className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
                    <select
                      aria-label="Compare to version"
                      value={data.to_version}
                      onChange={(e) => setToVersion(Number(e.target.value))}
                      className="h-7 rounded border border-white/[0.1] bg-zinc-950 px-2 text-xs text-zinc-100 focus:border-sky-500/40 focus:outline-none"
                    >
                      {data.versions.map((v) => (
                        <option key={v.version} value={v.version}>
                          v{v.version} ({v.reason})
                        </option>
                      ))}
                    </select>
                  </div>

                  <button
                    type="button"
                    disabled={!writable || cloning}
                    title={
                      writable
                        ? `Clone v${data.from_version} into a brand-new Draft without mutating audit history`
                        : readOnlyReason || 'Read-only mode enabled'
                    }
                    onClick={() => void handleCloneAsDraft(data.from_version)}
                    className="flex h-7 items-center gap-1.5 rounded-md border border-sky-500/35 bg-sky-500/15 px-2.5 text-xs font-medium text-sky-200 transition-colors hover:bg-sky-500/25 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <CopyPlus className="h-3.5 w-3.5" strokeWidth={1.5} />
                    <span>
                      {cloning ? 'Cloning…' : `Clone v${data.from_version} as Draft`}
                    </span>
                  </button>
                </div>

                {errorMsg && (
                  <div className="mx-4 mt-3 rounded border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-200">
                    {errorMsg}
                  </div>
                )}

                <div className="grid min-h-0 flex-1 grid-cols-1 divide-y divide-white/[0.06] overflow-hidden md:grid-cols-5 md:divide-x md:divide-y-0">
                  {/* Left: Version Timeline */}
                  <div className="overflow-y-auto p-3 md:col-span-2 space-y-2">
                    <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                      Version History ({data.versions.length})
                    </div>
                    {[...data.versions].reverse().map((entry) => {
                      const isFrom = entry.version === data.from_version;
                      const isTo = entry.version === data.to_version;
                      return (
                        <div
                          key={entry.version}
                          className={cn(
                            'flex flex-col gap-1.5 rounded-md border p-2.5 text-xs transition-colors',
                            isFrom || isTo
                              ? 'border-sky-500/35 bg-sky-500/[0.08]'
                              : 'border-white/[0.07] bg-white/[0.02]',
                          )}
                        >
                          <div className="flex flex-wrap items-center justify-between gap-1.5">
                            <div className="flex items-center gap-1.5">
                              <span className="font-semibold text-sky-300 font-mono-tabular">
                                v{entry.version}
                              </span>
                              {isFrom && <GlassBadge tone="amber">FROM</GlassBadge>}
                              {isTo && <GlassBadge tone="emerald">TO</GlassBadge>}
                            </div>
                            <span className="text-[10px] text-zinc-500 font-mono-tabular">
                              {formatShortTime(entry.timestamp)}
                            </span>
                          </div>

                          <div className="flex flex-wrap items-center gap-1.5">
                            <GlassBadge tone={entry.actor_type === 'human' ? 'sky' : 'zinc'}>
                              {entry.actor_type === 'human' ? (
                                <User className="h-2.5 w-2.5" strokeWidth={1.5} />
                              ) : (
                                <Bot className="h-2.5 w-2.5" strokeWidth={1.5} />
                              )}
                              <span>
                                {entry.actor_type === 'human' ? 'Human' : 'Agent'} ·{' '}
                                {entry.actor_label}
                              </span>
                            </GlassBadge>
                            <OriginBadge origin={entry.origin} compact />
                          </div>

                          <p className="text-[11px] text-zinc-300">{entry.summary}</p>

                          <div className="flex flex-wrap items-center justify-between gap-1.5 pt-0.5">
                            <div className="flex items-center gap-1">
                              <button
                                type="button"
                                onClick={() => setFromVersion(entry.version)}
                                className="rounded border border-white/[0.08] bg-white/[0.03] px-1.5 py-0.5 text-[10px] text-zinc-300 font-mono-tabular hover:bg-white/[0.08]"
                              >
                                Set From
                              </button>
                              <button
                                type="button"
                                onClick={() => setToVersion(entry.version)}
                                className="rounded border border-white/[0.08] bg-white/[0.03] px-1.5 py-0.5 text-[10px] text-zinc-300 font-mono-tabular hover:bg-white/[0.08]"
                              >
                                Set To
                              </button>
                            </div>

                            {entry.audit_entry_id && (
                              <button
                                type="button"
                                onClick={() => {
                                  onClose();
                                  onJumpToAudit(entry.audit_entry_id!);
                                }}
                                className="flex items-center gap-1 rounded border border-rose-500/25 bg-rose-500/10 px-1.5 py-0.5 text-[10px] text-rose-300 font-mono-tabular hover:bg-rose-500/20"
                              >
                                <ShieldAlert className="h-2.5 w-2.5" strokeWidth={1.5} />
                                <span>{entry.audit_entry_id}</span>
                                <ExternalLink className="h-2.5 w-2.5" strokeWidth={1.5} />
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {/* Right: Diff Output */}
                  <div className="overflow-y-auto p-3 md:col-span-3 space-y-2.5">
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                        Diff: v{data.from_version} -&gt; v{data.to_version} ({data.diff.length} fields changed)
                      </span>
                    </div>

                    {data.diff.length === 0 ? (
                      <div className="rounded-md border border-white/[0.07] bg-white/[0.02] p-4 text-center text-xs text-zinc-500 font-mono-tabular">
                        No structural field differences between v{data.from_version} and v{data.to_version}.
                      </div>
                    ) : (
                      <div className="space-y-2">
                        {data.diff.map((row) => (
                          <div
                            key={row.field}
                            className="rounded-md border border-white/[0.08] bg-zinc-950/75 p-2.5 text-[11px] font-mono-tabular"
                          >
                            <div className="mb-1.5 flex items-center justify-between">
                              <span className="font-semibold text-sky-300">{row.field}</span>
                              <GlassBadge
                                tone={
                                  row.change_type === 'added'
                                    ? 'emerald'
                                    : row.change_type === 'removed'
                                      ? 'rose'
                                      : 'amber'
                                }
                              >
                                {row.change_type.toUpperCase()}
                              </GlassBadge>
                            </div>
                            <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                              <div className="rounded border border-rose-500/20 bg-rose-500/[0.05] p-2">
                                <div className="text-[10px] text-rose-400">v{data.from_version}</div>
                                <pre className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap break-words text-[10px] text-zinc-300">
                                  {renderVal(row.before)}
                                </pre>
                              </div>
                              <div className="rounded border border-emerald-500/20 bg-emerald-500/[0.05] p-2">
                                <div className="text-[10px] text-emerald-400">v{data.to_version}</div>
                                <pre className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap break-words text-[10px] text-zinc-100">
                                  {renderVal(row.after)}
                                </pre>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
