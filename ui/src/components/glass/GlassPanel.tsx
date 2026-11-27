import type { HTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/utils';

interface GlassPanelProps extends HTMLAttributes<HTMLDivElement> {
  variant?: 'surface' | 'elevated';
  children: ReactNode;
}

export function GlassPanel({
  variant = 'surface',
  className,
  children,
  ...rest
}: GlassPanelProps) {
  return (
    <div
      className={cn(
        'rounded-lg',
        variant === 'surface'
          ? 'bg-zinc-950/65 backdrop-blur-xl border border-white/[0.08] shadow-[0_8px_32px_rgba(0,0,0,0.45),inset_0_1px_0_0_rgba(255,255,255,0.06)]'
          : 'bg-zinc-900/75 backdrop-blur-2xl border border-white/[0.12] shadow-[0_16px_48px_rgba(0,0,0,0.65),inset_0_1px_0_0_rgba(255,255,255,0.1)]',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}
