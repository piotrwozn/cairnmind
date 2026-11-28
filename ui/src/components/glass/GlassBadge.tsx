import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export type BadgeTone = 'emerald' | 'amber' | 'rose' | 'sky' | 'zinc';

interface GlassBadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
  mono?: boolean;
}

const TONE_CLASSES: Record<BadgeTone, string> = {
  emerald: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/25',
  amber: 'text-amber-400 bg-amber-500/10 border-amber-500/25',
  rose: 'text-rose-400 bg-rose-500/10 border-rose-500/25',
  sky: 'text-sky-400 bg-sky-500/10 border-sky-500/25',
  zinc: 'text-zinc-300 bg-white/[0.04] border-white/[0.08]',
};

export function GlassBadge({
  tone = 'zinc',
  children,
  className,
  mono = true,
}: GlassBadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-[11px] leading-4 font-medium tracking-tight',
        mono && 'font-mono-tabular',
        TONE_CLASSES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function statusToTone(status: string): BadgeTone {
  switch (status.toUpperCase()) {
    case 'VERIFIED':
    case 'SYNCED':
    case 'PASS':
    case 'ACCEPTED':
    case 'MATCH':
    case 'DONE':
    case 'OK':
      return 'emerald';
    case 'STALE':
    case 'OUTDATED_ACK':
    case 'STALE_CODE':
    case 'FILE_DRIFT_QUOTE_INTACT':
    case 'NEEDS_TEST':
    case 'NEEDS_REVIEW':
    case 'MISSING':
      return 'amber';
    case 'REJECTED':
    case 'CONFLICT':
    case 'FAIL':
    case 'QUOTE_MUTATED':
    case 'FILE_MISSING':
    case 'BLOCKED':
      return 'rose';
    case 'ACTIVE':
    case 'IN_PROGRESS':
      return 'sky';
    default:
      return 'zinc';
  }
}
