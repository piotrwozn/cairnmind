import { AlertTriangle, ShieldCheck, UserCheck } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { OriginCategory, OriginFilter, OriginMetadata } from '@/types/contracts';

interface OriginBadgeProps {
  origin?: OriginMetadata | null;
  category?: OriginCategory;
  compact?: boolean;
  className?: string;
}

const ORIGIN_META: Record<
  OriginCategory,
  {
    label: string;
    shortLabel: string;
    classes: string;
    icon: typeof ShieldCheck;
    defaultTooltip: string;
  }
> = {
  ENGINE_VERIFIED: {
    label: 'ENGINE VERIFIED',
    shortLabel: 'ENGINE',
    classes: 'text-emerald-300 bg-emerald-500/10 border-emerald-500/25',
    icon: ShieldCheck,
    defaultTooltip:
      'ENGINE VERIFIED — Hard proof verified directly by Cairnmind engine against SHA-256 hashes and verbatim quotes.',
  },
  HUMAN_REPO: {
    label: 'HUMAN / REPO',
    shortLabel: 'HUMAN',
    classes: 'text-sky-300 bg-sky-500/10 border-sky-500/25',
    icon: UserCheck,
    defaultTooltip:
      'HUMAN / REPO — Authored manually by an engineer in UI or detected from repository files, GitHub PRs, or CI.',
  },
  MCP_DEGRADED: {
    label: 'MCP DEGRADED',
    shortLabel: 'DEGRADED',
    classes: 'text-amber-300 bg-amber-500/12 border-amber-500/35',
    icon: AlertTriangle,
    defaultTooltip:
      'MCP DEGRADED (origin_evidence: degraded) — External agent write without archived conversation or direct engine verification. Review before trusting.',
  },
};

export function resolveOriginCategory(
  origin?: OriginMetadata | null,
  fallback?: OriginCategory,
): OriginCategory {
  if (origin?.category) return origin.category;
  if (origin?.origin_evidence === 'degraded') return 'MCP_DEGRADED';
  if (origin?.origin_evidence === 'human' || origin?.origin_evidence === 'repo_ci') return 'HUMAN_REPO';
  return fallback ?? 'ENGINE_VERIFIED';
}

export function matchesOriginFilter(
  origin: OriginMetadata | null | undefined,
  filter: OriginFilter,
): boolean {
  if (filter === 'ALL') return true;
  const cat = resolveOriginCategory(origin);
  if (filter === 'HUMAN') return cat === 'HUMAN_REPO';
  if (filter === 'DEGRADED') return cat === 'MCP_DEGRADED';
  // AGENT covers engine-verified agent writes and degraded MCP agent writes
  return cat === 'ENGINE_VERIFIED' || cat === 'MCP_DEGRADED';
}

/**
 * Unified Obsidian Precision Glass provenance pill badge (`ENGINE VERIFIED`, `HUMAN / REPO`, `MCP DEGRADED`).
 */
export function OriginBadge({ origin, category, compact = false, className }: OriginBadgeProps) {
  const resolved = category ?? resolveOriginCategory(origin);
  const spec = ORIGIN_META[resolved];
  const Icon = spec.icon;
  const tooltip = origin?.detail
    ? `${spec.label}${origin.source_label ? ` (${origin.source_label})` : ''} — ${origin.detail}`
    : spec.defaultTooltip;

  return (
    <span
      title={tooltip}
      className={cn(
        'inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] leading-3.5 font-medium tracking-tight font-mono-tabular whitespace-nowrap',
        spec.classes,
        className,
      )}
    >
      <Icon className="h-2.5 w-2.5 shrink-0" strokeWidth={1.75} />
      <span>{compact ? spec.shortLabel : spec.label}</span>
    </span>
  );
}

interface OriginFilterBarProps {
  value: OriginFilter;
  onChange: (next: OriginFilter) => void;
  className?: string;
}

const FILTER_OPTIONS: Array<{ id: OriginFilter; label: string }> = [
  { id: 'ALL', label: 'All' },
  { id: 'HUMAN', label: 'Human' },
  { id: 'AGENT', label: 'Agent' },
  { id: 'DEGRADED', label: 'Degraded only' },
];

export function OriginFilterBar({ value, onChange, className }: OriginFilterBarProps) {
  return (
    <div
      role="group"
      aria-label="Filter by provenance origin"
      className={cn(
        'inline-flex items-center gap-0.5 rounded-md border border-white/[0.08] bg-zinc-950/70 p-0.5',
        className,
      )}
    >
      {FILTER_OPTIONS.map((opt) => {
        const active = value === opt.id;
        return (
          <button
            key={opt.id}
            type="button"
            onClick={() => onChange(opt.id)}
            className={cn(
              'rounded px-2 py-0.5 text-[11px] font-medium transition-colors font-mono-tabular',
              active
                ? opt.id === 'DEGRADED'
                  ? 'border border-amber-500/35 bg-amber-500/15 text-amber-300'
                  : 'border border-white/[0.12] bg-white/[0.09] text-zinc-100'
                : 'border border-transparent text-zinc-500 hover:text-zinc-300',
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
