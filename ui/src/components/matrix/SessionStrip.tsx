import { AlertTriangle, CheckCircle2, Cpu, PlugZap, Trash2 } from 'lucide-react';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { cn, formatShortTime } from '@/lib/utils';
import type { AgentSession } from '@/types/contracts';

interface SessionStripProps {
  sessions: AgentSession[];
  selectedSessionId: string | null;
  onSelectSession: (session: AgentSession) => void;
  onDropSession: (sessionId: string) => void;
  droppingSessionId: string | null;
  writable?: boolean;
  readOnlyReason?: string | null;
}

export function SessionStrip({
  sessions,
  selectedSessionId,
  onSelectSession,
  onDropSession,
  droppingSessionId,
  writable = true,
  readOnlyReason,
}: SessionStripProps) {
  return (
    <div className="shrink-0 border-b border-white/[0.06] px-3 py-2.5">
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Cpu className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
          <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
            Active Agent Sessions
          </span>
          <span className="text-[11px] text-zinc-500 font-mono-tabular">
            ({sessions.length} connected)
          </span>
        </div>
        <span className="text-[11px] text-zinc-500 font-mono-tabular">
          OCC write alignment vs state_rev
        </span>
      </div>

      {sessions.length === 0 ? (
        <div className="flex h-16 items-center justify-center rounded-md border border-dashed border-white/[0.08] bg-white/[0.015] text-xs text-zinc-500 font-mono-tabular">
          No active MCP sessions bound to this project.
        </div>
      ) : (
        <div className="flex gap-2 overflow-x-auto pb-0.5">
          {sessions.map((session) => {
            const isSynced = session.sync_status === 'SYNCED';
            const isSelected = selectedSessionId === session.id;
            const isDropping = droppingSessionId === session.id;

            return (
              <div
                key={session.id}
                role="button"
                tabIndex={0}
                onClick={() => onSelectSession(session)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onSelectSession(session);
                  }
                }}
                className={cn(
                  'group flex min-w-[290px] max-w-[340px] shrink-0 cursor-pointer flex-col gap-1.5 rounded-md border p-2.5 transition-colors duration-150',
                  isSelected
                    ? 'border-sky-500/40 bg-sky-500/[0.08]'
                    : isSynced
                      ? 'border-white/[0.08] bg-white/[0.02] hover:bg-white/[0.04]'
                      : 'border-amber-500/25 bg-amber-500/[0.04] hover:bg-amber-500/[0.07]',
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <PlugZap
                      className={cn(
                        'h-3.5 w-3.5 shrink-0',
                        isSynced ? 'text-emerald-400' : 'text-amber-400',
                      )}
                      strokeWidth={1.5}
                    />
                    <span className="truncate text-xs font-semibold text-zinc-100 font-mono-tabular">
                      {session.client}
                    </span>
                    {session.degraded && (
                      <GlassBadge tone="zinc" className="text-[10px]">
                        DEGRADED
                      </GlassBadge>
                    )}
                  </div>

                  <button
                    type="button"
                    disabled={isDropping || !writable}
                    onClick={(e) => {
                      e.stopPropagation();
                      onDropSession(session.id);
                    }}
                    title={!writable ? readOnlyReason || 'Read-only mode enabled' : 'Drop stale or orphaned session lock'}
                    className="flex items-center gap-1 rounded border border-white/[0.08] bg-zinc-950/60 px-1.5 py-0.5 text-[10px] font-medium text-zinc-400 transition-colors duration-150 hover:border-rose-500/35 hover:bg-rose-500/15 hover:text-rose-300 disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    <Trash2 className="h-3 w-3" strokeWidth={1.5} />
                    <span>{isDropping ? 'Dropping…' : 'Drop Session'}</span>
                  </button>
                </div>

                <div className="flex items-center justify-between gap-2 text-[11px] font-mono-tabular">
                  <span className="truncate text-zinc-400">{session.id}</span>
                  <span className="shrink-0 text-zinc-500">
                    ack_rev:{' '}
                    <span className={isSynced ? 'text-emerald-400' : 'text-amber-400'}>
                      #{session.acknowledged_rev}
                    </span>
                    <span className="text-zinc-600"> / #{session.current_state_rev}</span>
                  </span>
                </div>

                <div className="flex items-center justify-between gap-2 pt-0.5">
                  {isSynced ? (
                    <GlassBadge tone="emerald">
                      <CheckCircle2 className="h-3 w-3" strokeWidth={1.5} />
                      <span>SYNCED</span>
                    </GlassBadge>
                  ) : (
                    <GlassBadge tone="amber" className="max-w-full truncate">
                      <AlertTriangle className="h-3 w-3 shrink-0" strokeWidth={1.5} />
                      <span className="truncate">OUTDATED_ACK — Next write will reject</span>
                    </GlassBadge>
                  )}
                  <span className="shrink-0 text-[10px] text-zinc-500 font-mono-tabular">
                    {formatShortTime(session.last_seen_at)}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
