import { AnimatePresence, motion } from 'framer-motion';
import { Activity, ChevronDown, ChevronUp, Trash2 } from 'lucide-react';
import { GlassBadge, type BadgeTone } from '@/components/glass/GlassBadge';
import { GlassPanel } from '@/components/glass/GlassPanel';
import { StatusDot } from '@/components/glass/StatusPulse';
import { cn, formatTimeMs } from '@/lib/utils';
import type { UiEventType, UiSseEvent } from '@/types/contracts';

interface EventDockProps {
  open: boolean;
  onToggle: () => void;
  events: UiSseEvent[];
  onClear: () => void;
}

function eventTone(type: UiEventType): BadgeTone {
  switch (type) {
    case 'mcp.write.rejected':
    case 'plan.conflict':
      return 'rose';
    case 'evidence.stale':
    case 'repo_fingerprint_changed':
      return 'amber';
    case 'mcp.write.accepted':
    case 'session.ack':
      return 'emerald';
    default:
      return 'sky';
  }
}

function summarizePayload(event: UiSseEvent): string {
  const p = event.payload;
  if (typeof p.message === 'string' && p.message) return p.message;
  if (event.type === 'reindex.progress') {
    const pct = typeof p.percent === 'number' ? `${p.percent}%` : '';
    const stage = typeof p.stage === 'string' ? p.stage : 'indexing';
    return `${stage} ${pct}`.trim();
  }
  if (event.type === 'repo_fingerprint_changed') {
    const file = typeof p.file_path === 'string' ? p.file_path : 'repo';
    const sha = typeof p.new_sha256 === 'string' ? p.new_sha256.slice(0, 7) : '';
    return `${file} -> ${sha}`.trim();
  }
  if (event.type === 'state.updated') {
    const rev = p.state_rev !== undefined ? `state_rev=#${String(p.state_rev)}` : '';
    const reason = typeof p.reason === 'string' ? p.reason : 'sync';
    const session = typeof p.session_id === 'string' ? ` session=${p.session_id}` : '';
    return `${reason} ${rev}${session}`.trim();
  }
  return JSON.stringify(p);
}

export function EventDock({ open, onToggle, events, onClear }: EventDockProps) {
  return (
    <GlassPanel className="flex shrink-0 flex-col overflow-hidden">
      {/* Header bar */}
      <div className="flex h-8 shrink-0 items-center justify-between border-b border-white/[0.06] px-3">
        <button
          type="button"
          onClick={onToggle}
          className="flex items-center gap-2 text-xs font-medium text-zinc-300 hover:text-zinc-100"
        >
          <Activity className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
          <span>Live Event Stream</span>
          <span className="rounded bg-white/[0.06] px-1.5 py-0.25 text-[10px] text-zinc-400 font-mono-tabular">
            {events.length}
          </span>
          {open ? (
            <ChevronDown className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
          ) : (
            <ChevronUp className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
          )}
        </button>

        <div className="flex items-center gap-2">
          <span className="hidden text-[10px] text-zinc-600 font-mono-tabular sm:inline">
            SSE /api/ui/events · toggle `
          </span>
          {events.length > 0 && (
            <button
              type="button"
              onClick={onClear}
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-zinc-500 transition-colors hover:bg-white/[0.06] hover:text-zinc-300"
            >
              <Trash2 className="h-3 w-3" strokeWidth={1.5} />
              <span>Clear</span>
            </button>
          )}
        </div>
      </div>

      {/* Collapsible body */}
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 112, opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.16, ease: 'easeOut' }}
            className="overflow-y-auto px-2 py-1"
          >
            {events.length === 0 ? (
              <div className="flex h-full items-center justify-center gap-2 text-xs text-zinc-500 font-mono-tabular">
                <StatusDot tone="emerald" pulse />
                <span>Listening for MCP calls, repo_fingerprint_changed, reindex, and OCC events...</span>
              </div>
            ) : (
              <div className="flex flex-col gap-0.5">
                {events.map((event) => (
                  <div
                    key={event.id}
                    className={cn(
                      'flex items-center gap-2.5 rounded px-2 py-1 text-[11px] transition-colors hover:bg-white/[0.04]',
                      (event.type === 'mcp.write.rejected' || event.type === 'plan.conflict') &&
                        'bg-rose-500/[0.06]',
                      (event.type === 'evidence.stale' ||
                        event.type === 'repo_fingerprint_changed') &&
                        'bg-amber-500/[0.05]',
                    )}
                  >
                    <span className="w-[84px] shrink-0 text-zinc-500 font-mono-tabular">
                      {formatTimeMs(event.timestamp)}
                    </span>
                    <GlassBadge tone={eventTone(event.type)} className="w-[172px] shrink-0 justify-center">
                      {event.type}
                    </GlassBadge>
                    <span className="truncate text-zinc-300 font-mono-tabular">
                      {summarizePayload(event)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </GlassPanel>
  );
}
