import { AnimatePresence, motion } from 'framer-motion';
import { Maximize2, X } from 'lucide-react';
import { AskPanel } from '@/components/ask/AskPanel';
import type { AskCitation, AskResponse, UiSnapshot } from '@/types/contracts';

interface AskDrawerProps {
  open: boolean;
  onClose: () => void;
  onExpandToTab: () => void;
  snapshot: UiSnapshot | undefined;
  initialQuery?: string | null;
  onConsumeInitialQuery?: () => void;
  changedFingerprints: Readonly<Record<string, string>>;
  liveStaleClaimIds: ReadonlySet<string>;
  onSelectCitation: (citation: AskCitation) => void;
  onPinAsFact: (answer: AskResponse) => void;
}

/**
 * Slide-over Obsidian Glass Ask Drawer accessible from any of the 4 existing Inspector views.
 */
export function AskDrawer({
  open,
  onClose,
  onExpandToTab,
  snapshot,
  initialQuery,
  onConsumeInitialQuery,
  changedFingerprints,
  liveStaleClaimIds,
  onSelectCitation,
  onPinAsFact,
}: AskDrawerProps) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          className="fixed inset-0 z-40 flex justify-end bg-black/50 backdrop-blur-[2px]"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) onClose();
          }}
        >
          <motion.aside
            initial={{ x: 32, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: 32, opacity: 0 }}
            transition={{ duration: 0.16, ease: 'easeOut' }}
            className="m-2 flex h-[calc(100vh-16px)] w-full max-w-[600px] flex-col overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/85 shadow-[0_16px_48px_rgba(0,0,0,0.7),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
          >
            <div className="flex h-9 shrink-0 items-center justify-between border-b border-white/[0.08] px-3">
              <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-400 font-mono-tabular">
                Quick Ask Drawer · Esc to close
              </span>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => {
                    onClose();
                    onExpandToTab();
                  }}
                  title="Open full Interactive Ask tab"
                  className="flex h-6 items-center gap-1 rounded px-2 text-[11px] text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100"
                >
                  <Maximize2 className="h-3 w-3" strokeWidth={1.5} />
                  <span>Full View</span>
                </button>
                <button
                  type="button"
                  onClick={onClose}
                  title="Close Ask drawer"
                  className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100"
                >
                  <X className="h-3.5 w-3.5" strokeWidth={1.5} />
                </button>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-hidden">
              <AskPanel
                snapshot={snapshot}
                initialQuery={initialQuery}
                onConsumeInitialQuery={onConsumeInitialQuery}
                changedFingerprints={changedFingerprints}
                liveStaleClaimIds={liveStaleClaimIds}
                onSelectCitation={(cit) => {
                  onClose();
                  onSelectCitation(cit);
                }}
                onPinAsFact={(ans) => {
                  onClose();
                  onPinAsFact(ans);
                }}
                compact
              />
            </div>
          </motion.aside>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
