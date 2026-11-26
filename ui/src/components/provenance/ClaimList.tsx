import { useMemo, useState } from 'react';
import { motion } from 'framer-motion';
import { FileCode2, RefreshCw, Search } from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { StatusPulseHighlight } from '@/components/glass/StatusPulse';
import { cn, formatShortTime, shortHash, SPRING_PHYSICS } from '@/lib/utils';
import type { EvidenceClaim } from '@/types/contracts';

type ProvenanceFilter = 'ALL' | 'VERIFIED' | 'STALE';

interface ClaimListProps {
  claims: EvidenceClaim[];
  selectedClaimId: string | null;
  onSelectClaim: (claim: EvidenceClaim) => void;
  onlyStaleAndConflicts: boolean;
  stalePulseIds: ReadonlySet<string>;
  onReverify: () => void;
  reverifying: boolean;
}

const TABS: ProvenanceFilter[] = ['ALL', 'VERIFIED', 'STALE'];

export function ClaimList({
  claims,
  selectedClaimId,
  onSelectClaim,
  onlyStaleAndConflicts,
  stalePulseIds,
  onReverify,
  reverifying,
}: ClaimListProps) {
  const [filter, setFilter] = useState<ProvenanceFilter>('ALL');
  const [search, setSearch] = useState('');

  const effectiveFilter: ProvenanceFilter = onlyStaleAndConflicts ? 'STALE' : filter;

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return claims.filter((claim) => {
      if (effectiveFilter === 'VERIFIED' && claim.provenance_status !== 'VERIFIED') return false;
      if (effectiveFilter === 'STALE' && claim.provenance_status === 'VERIFIED') return false;
      if (!q) return true;
      return (
        claim.key.toLowerCase().includes(q) ||
        claim.title.toLowerCase().includes(q) ||
        claim.source_path.toLowerCase().includes(q) ||
        claim.verbatim_quote.toLowerCase().includes(q) ||
        claim.recorded_sha256.toLowerCase().includes(q)
      );
    });
  }, [claims, effectiveFilter, search]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex items-center gap-1 rounded-md border border-white/[0.08] bg-zinc-950/60 p-0.5">
          {TABS.map((tab) => {
            const active = effectiveFilter === tab;
            return (
              <button
                key={tab}
                type="button"
                disabled={onlyStaleAndConflicts && tab !== 'STALE'}
                onClick={() => setFilter(tab)}
                className={cn(
                  'relative rounded px-2.5 py-1 text-[11px] font-medium transition-colors font-mono-tabular',
                  active ? 'text-zinc-100' : 'text-zinc-500 hover:text-zinc-300',
                  onlyStaleAndConflicts && tab !== 'STALE' && 'opacity-40 cursor-not-allowed',
                )}
              >
                {active && (
                  <motion.span
                    layoutId="active-tab-indicator"
                    transition={SPRING_PHYSICS}
                    className="pointer-events-none absolute inset-0 rounded border border-white/[0.12] bg-white/[0.08]"
                  />
                )}
                <span className="relative z-10">{tab}</span>
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500"
              strokeWidth={1.5}
            />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter claims, paths, quotes..."
              className="h-7 w-52 rounded-md border border-white/[0.08] bg-zinc-950/60 pl-7 pr-2 text-xs text-zinc-200 placeholder:text-zinc-500 focus:border-sky-500/40 focus:outline-none"
            />
          </div>

          <button
            type="button"
            disabled={reverifying}
            onClick={onReverify}
            className="flex h-7 items-center gap-1.5 rounded-md border border-sky-500/30 bg-sky-500/10 px-2.5 text-xs font-medium text-sky-300 transition-colors hover:bg-sky-500/20 disabled:opacity-50"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', reverifying && 'animate-spin')} strokeWidth={1.5} />
            <span>Re-verify</span>
          </button>
        </div>
      </div>

      {/* Master List */}
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {filtered.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-xs text-zinc-500 font-mono-tabular">
            No claims match the active provenance filter.
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            {filtered.map((claim) => {
              const isSelected = claim.id === selectedClaimId;
              const pulsing = stalePulseIds.has(claim.id);
              return (
                <StatusPulseHighlight key={claim.id} active={pulsing} tone="amber" className="rounded-md">
                  <button
                    type="button"
                    onClick={() => onSelectClaim(claim)}
                    className={cn(
                      'flex w-full flex-col gap-1.5 rounded-md border p-2.5 text-left transition-colors duration-150',
                      isSelected
                        ? 'border-sky-500/40 bg-sky-500/[0.09]'
                        : 'border-white/[0.07] bg-white/[0.02] hover:bg-white/[0.04]',
                    )}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <GlassBadge tone="zinc">{claim.kind.toUpperCase()}</GlassBadge>
                          <span className="truncate text-xs font-semibold text-zinc-100 font-mono-tabular">
                            {claim.key}
                          </span>
                        </div>
                        <p className="mt-1 truncate text-xs text-zinc-300">{claim.title}</p>
                      </div>

                      <div className="flex shrink-0 items-center gap-1.5">
                        <GlassBadge tone={statusToTone(claim.drift_kind)}>
                          {claim.drift_kind}
                        </GlassBadge>
                        <GlassBadge tone={statusToTone(claim.provenance_status)}>
                          {claim.provenance_status}
                        </GlassBadge>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono-tabular text-zinc-500">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <FileCode2 className="h-3.5 w-3.5 shrink-0 text-zinc-500" strokeWidth={1.5} />
                        <span className="truncate text-zinc-400">
                          {claim.source_path || 'unscoped'}:L{claim.line_start}–{claim.line_end}
                        </span>
                      </span>
                      <span className="shrink-0">
                        rec:{shortHash(claim.recorded_sha256, 7)} · disk:
                        <span
                          className={
                            claim.recorded_sha256 === claim.current_disk_sha256
                              ? 'text-emerald-400'
                              : 'text-amber-400'
                          }
                        >
                          {shortHash(claim.current_disk_sha256, 7)}
                        </span>{' '}
                        · {formatShortTime(claim.updated_at)}
                      </span>
                    </div>
                  </button>
                </StatusPulseHighlight>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
