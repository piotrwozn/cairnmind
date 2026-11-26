import { Cpu, FolderGit2, GitBranch, Search, ShieldCheck } from 'lucide-react';
import type { ConnectionState } from '@/api/events';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { GlassPanel } from '@/components/glass/GlassPanel';
import { StatusDot } from '@/components/glass/StatusPulse';
import type { UiSnapshot } from '@/types/contracts';

interface TopStatusBarProps {
  snapshot: UiSnapshot | undefined;
  connection: ConnectionState;
  onOpenPalette: () => void;
}

export function TopStatusBar({ snapshot, connection, onOpenPalette }: TopStatusBarProps) {
  const project = snapshot?.project;
  const activeSessions = snapshot?.sessions.length ?? 0;
  const outdatedSessions =
    snapshot?.sessions.filter((s) => s.sync_status === 'OUTDATED_ACK').length ?? 0;
  const stateRev = snapshot?.state_rev ?? 0;

  return (
    <GlassPanel className="flex h-10 shrink-0 items-center justify-between gap-3 px-3">
      {/* Left: Brand mark + Project identity + repo root path */}
      <div className="flex min-w-0 items-center gap-2.5">
        <div className="flex items-center gap-1.5 text-zinc-100">
          <ShieldCheck className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold tracking-tight">cairnmind</span>
        </div>

        <span className="h-3.5 w-px bg-white/[0.1]" />

        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-xs font-medium text-zinc-200">
            {project?.name ?? 'Connecting…'}
          </span>
          <span className="hidden items-center gap-1 truncate text-[11px] text-zinc-500 font-mono-tabular md:inline-flex">
            <FolderGit2 className="h-3.5 w-3.5 shrink-0 text-zinc-600" strokeWidth={1.5} />
            <span className="truncate max-w-[260px] lg:max-w-[380px]">{project?.root ?? '—'}</span>
          </span>
          {project?.branch && (
            <GlassBadge tone="zinc" className="hidden xl:inline-flex">
              <GitBranch className="h-3 w-3 text-zinc-400" strokeWidth={1.5} />
              <span>{project.branch}</span>
              {project.head && <span className="text-zinc-500">@ {project.head.slice(0, 7)}</span>}
            </GlassBadge>
          )}
          {project?.mode === 'mock' && (
            <GlassBadge tone="sky">MOCK</GlassBadge>
          )}
        </div>
      </div>

      {/* Right: SSE live pill + state_rev + sessions badge + Cmd+K trigger */}
      <div className="flex shrink-0 items-center gap-2">
        <GlassBadge tone={connection === 'LIVE' ? 'emerald' : 'amber'}>
          <StatusDot tone={connection === 'LIVE' ? 'emerald' : 'amber'} pulse />
          <span>{connection}</span>
        </GlassBadge>

        <GlassBadge tone="zinc">
          <span className="text-zinc-500">state_rev:</span>
          <span className="text-zinc-100">#{stateRev}</span>
        </GlassBadge>

        <GlassBadge tone={outdatedSessions > 0 ? 'amber' : 'sky'}>
          <Cpu className="h-3 w-3" strokeWidth={1.5} />
          <span>
            {activeSessions} {activeSessions === 1 ? 'session' : 'sessions'}
          </span>
          {outdatedSessions > 0 && (
            <span className="text-amber-300">({outdatedSessions} stale)</span>
          )}
        </GlassBadge>

        <button
          type="button"
          onClick={onOpenPalette}
          className="flex h-7 items-center gap-2 rounded-md border border-white/[0.08] bg-white/[0.03] px-2.5 text-xs text-zinc-400 transition-colors duration-150 hover:border-white/[0.14] hover:bg-white/[0.06] hover:text-zinc-200"
        >
          <Search className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
          <span className="hidden sm:inline">Search quotes, hashes, tasks...</span>
          <kbd className="rounded border border-white/[0.1] bg-zinc-900/90 px-1 py-0.5 text-[10px] text-zinc-400 font-mono-tabular">
            Ctrl+K
          </kbd>
        </button>
      </div>
    </GlassPanel>
  );
}
