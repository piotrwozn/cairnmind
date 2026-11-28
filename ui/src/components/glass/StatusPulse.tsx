import { motion } from 'framer-motion';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

interface StatusDotProps {
  tone: 'emerald' | 'amber' | 'rose' | 'sky';
  pulse?: boolean;
  className?: string;
}

const DOT_BG: Record<StatusDotProps['tone'], string> = {
  emerald: 'bg-emerald-400',
  amber: 'bg-amber-400',
  rose: 'bg-rose-400',
  sky: 'bg-sky-400',
};

export function StatusDot({ tone, pulse = false, className }: StatusDotProps) {
  return (
    <span className={cn('relative inline-flex h-2 w-2 shrink-0', className)}>
      {pulse && (
        <span
          className={cn(
            'absolute inline-flex h-full w-full animate-ping rounded-full opacity-60',
            DOT_BG[tone],
          )}
        />
      )}
      <span className={cn('relative inline-flex h-2 w-2 rounded-full', DOT_BG[tone])} />
    </span>
  );
}

interface StatusPulseHighlightProps {
  active: boolean;
  tone: 'amber' | 'rose';
  children: ReactNode;
  className?: string;
}

/**
 * Triggers a deterministic 600ms semantic border pulse on live SSE state flips.
 */
export function StatusPulseHighlight({
  active,
  tone,
  children,
  className,
}: StatusPulseHighlightProps) {
  const glow =
    tone === 'amber'
      ? '0 0 0 1px rgba(245, 158, 11, 0.55), inset 0 0 16px rgba(245, 158, 11, 0.14)'
      : '0 0 0 1px rgba(244, 63, 94, 0.55), inset 0 0 16px rgba(244, 63, 94, 0.16)';

  return (
    <motion.div
      animate={
        active
          ? { boxShadow: ['0 0 0 0 rgba(0,0,0,0)', glow, '0 0 0 0 rgba(0,0,0,0)'] }
          : { boxShadow: '0 0 0 0 rgba(0,0,0,0)' }
      }
      transition={{ duration: 0.6, ease: 'easeOut' }}
      className={className}
    >
      {children}
    </motion.div>
  );
}
