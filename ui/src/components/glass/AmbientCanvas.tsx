import type { ReactNode } from 'react';

interface AmbientCanvasProps {
  children: ReactNode;
}

/**
 * Obsidian Precision Glass root canvas:
 * #060709 base, ultra-subtle top center sky radial vignette, and faint 1px dot matrix.
 */
export function AmbientCanvas({ children }: AmbientCanvasProps) {
  return (
    <div className="relative h-screen w-screen overflow-hidden bg-[#060709] text-zinc-100">
      {/* Ultra-subtle radial ambient vignette at top center */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 z-0"
        style={{
          backgroundImage:
            'radial-gradient(ellipse 60% 35% at 50% 0%, rgba(56, 189, 248, 0.06), transparent 70%)',
        }}
      />
      {/* Faint 1px dot/grid matrix */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 z-0 opacity-[0.03]"
        style={{
          backgroundImage: 'radial-gradient(rgba(255, 255, 255, 0.9) 1px, transparent 1px)',
          backgroundSize: '20px 20px',
        }}
      />
      <div className="relative z-10 flex h-full w-full flex-col gap-2 overflow-hidden p-2">
        {children}
      </div>
    </div>
  );
}
