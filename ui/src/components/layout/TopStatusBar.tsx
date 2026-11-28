import {
  Activity,
  Cpu,
  Download,
  FilePlus2,
  FolderGit2,
  GitBranch,
  Lock,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { getCockpitAdapter } from '@/api/client';
import type { ConnectionState } from '@/api/events';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { GlassPanel } from '@/components/glass/GlassPanel';
import { StatusDot } from '@/components/glass/StatusPulse';
import { cn } from '@/lib/utils';
import type { ReindexProgressState, UiSnapshot } from '@/types/contracts';

interface TopStatusBarProps {
  snapshot: UiSnapshot | undefined;
  connection: ConnectionState;
  reindexProgress: ReindexProgressState | null;
  reindexing: boolean;
  onOpenPalette: () => void;
  onOpenAskDrawer: () => void;
  onNewDraft: () => void;
  onReindex: () => void;
  onOpenDoctor: () => void;
  onOpenExport: () => void;
}

export function TopStatusBar({
  snapshot,
  connection,
  reindexProgress,
  reindexing,
  onOpenPalette,
  onOpenAskDrawer,
  onNewDraft,
  onReindex,
  onOpenDoctor,
  onOpenExport,
}: TopStatusBarProps) {
  const project = snapshot?.project;
  const writable = snapshot?.capabilities?.writable ?? true;
  const readOnlyReason = snapshot?.capabilities?.read_only_reason;
  const activeSessions = snapshot?.sessions.length ?? 0;
  const outdatedSessions =
    snapshot?.sessions.filter((s) => s.sync_status === 'OUTDATED_ACK').length ?? 0;
  const stateRev = snapshot?.state_rev ?? 0;

  const adapter = getCockpitAdapter();
  const isMock = adapter.mode === 'mock';

  return (
    <GlassPanel className="flex h-10 shrink-0 items-center justify-between gap-2 px-3">
      {/* Left: Brand mark + Project identity + repo root path */}
      <div className="flex min-w-0 items-center gap-2">
        <div className="flex items-center gap-1.5 text-zinc-100">
          <ShieldCheck className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold tracking-tight">cairnmind</span>
        </div>

        <span className="h-3.5 w-px bg-white/[0.1]" />

        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-xs font-medium text-zinc-200">
            {project?.name ?? 'Connecting…'}
          </span>
          <span className="hidden items-center gap-1 truncate text-[11px] text-zinc-500 font-mono-tabular 2xl:inline-flex">
            <FolderGit2 className="h-3.5 w-3.5 shrink-0 text-zinc-600" strokeWidth={1.5} />
            <span className="truncate max-w-[200px]">{project?.root ?? '—'}</span>
          </span>
          {project?.branch && (
            <GlassBadge tone="zinc" className="hidden xl:inline-flex">
              <GitBranch className="h-3 w-3 text-zinc-400" strokeWidth={1.5} />
              <span>{project.branch}</span>
              {project.head && <span className="text-zinc-500">@ {project.head.slice(0, 7)}</span>}
            </GlassBadge>
          )}
          {project?.mode === 'mock' && <GlassBadge tone="sky">MOCK</GlassBadge>}
          {!writable && (
            <GlassBadge tone="amber">
              <Lock className="h-2.5 w-2.5" strokeWidth={1.75} />
              <span>READ-ONLY</span>
            </GlassBadge>
          )}
        </div>
      </div>

      {/* Center / Right Quick Actions + Live Status + Cmd+K */}
      <div className="flex shrink-0 items-center gap-1.5">
        {/* Live SSE Re-index Progress Bar */}
        {reindexProgress && (
          <div className="hidden items-center gap-2 rounded-md border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] text-sky-200 font-mono-tabular lg:flex">
            <RefreshCw
              className={cn('h-3 w-3 text-sky-400', reindexProgress.active && 'animate-spin')}
              strokeWidth={1.5}
            />
            <span className="max-w-[110px] truncate">{reindexProgress.stage}</span>
            <div className="h-1.5 w-14 overflow-hidden rounded-full bg-white/[0.1]">
              <div
                className="h-full rounded-full bg-sky-400 transition-all duration-200"
                style={{ width: `${reindexProgress.percent}%` }}
              />
            </div>
            <span>{reindexProgress.percent}%</span>
          </div>
        )}

        {/* Quick Actions Bar */}
        <div className="flex items-center gap-1 border-r border-white/[0.08] pr-1.5">
          <button
            type="button"
            onClick={onOpenAskDrawer}
            title="Open Quick Ask Drawer from any view"
            className="flex h-7 items-center gap-1 rounded-md border border-sky-500/30 bg-sky-500/10 px-2 text-[11px] font-medium text-sky-300 transition-colors hover:bg-sky-500/20"
          >
            <Sparkles className="h-3.5 w-3.5" strokeWidth={1.5} />
            <span className="hidden sm:inline">Ask</span>
          </button>

          <button
            type="button"
            disabled={!writable}
            onClick={onNewDraft}
            title={
              writable
                ? 'New Draft Plan (Jira / planning without execution)'
                : readOnlyReason || 'Read-only mode: creating drafts is disabled'
            }
            className="flex h-7 items-center gap-1 rounded-md border border-white/[0.08] bg-white/[0.03] px-2 text-[11px] font-medium text-zinc-200 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <FilePlus2 className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
            <span className="hidden md:inline">Draft</span>
          </button>

          <button
            type="button"
            disabled={!writable || reindexing}
            onClick={onReindex}
            title={
              writable
                ? 'Re-index repository (POST /api/actions/reindex with SSE progress)'
                : readOnlyReason || 'Read-only mode: re-index disabled'
            }
            className="flex h-7 items-center gap-1 rounded-md border border-white/[0.08] bg-white/[0.03] px-2 text-[11px] font-medium text-zinc-300 transition-colors hover:bg-white/[0.08] hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', reindexing && 'animate-spin text-sky-400')} strokeWidth={1.5} />
            <span className="hidden xl:inline">Re-index</span>
          </button>

          <button
            type="button"
            onClick={onOpenDoctor}
            title="Run Doctor / Healthcheck (GET /api/doctor)"
            className="flex h-7 items-center gap-1 rounded-md border border-white/[0.08] bg-white/[0.03] px-2 text-[11px] font-medium text-zinc-300 transition-colors hover:bg-white/[0.08] hover:text-zinc-100"
          >
            <Activity className="h-3.5 w-3.5 text-emerald-400" strokeWidth={1.5} />
            <span className="hidden xl:inline">Doctor</span>
          </button>

          <button
            type="button"
            onClick={onOpenExport}
            title="Export Safe Snapshot (team) with Redaction Gate"
            className="flex h-7 items-center gap-1 rounded-md border border-white/[0.08] bg-white/[0.03] px-2 text-[11px] font-medium text-zinc-300 transition-colors hover:bg-white/[0.08] hover:text-zinc-100"
          >
            <Download className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
            <span className="hidden xl:inline">Export</span>
          </button>

          {isMock && adapter.mockControls && (
            <button
              type="button"
              onClick={() => {
                const flags = adapter.mockControls!.getMockFlags();
                adapter.mockControls!.setReadOnly(!flags.readOnly);
              }}
              title="Toggle simulated --read-only capability in ?mock=1"
              className={cn(
                'flex h-7 items-center gap-1 rounded-md border px-1.5 text-[10px] font-mono-tabular transition-colors',
                !writable
                  ? 'border-amber-500/40 bg-amber-500/15 text-amber-300'
                  : 'border-white/[0.08] bg-white/[0.02] text-zinc-500 hover:text-zinc-300',
              )}
            >
              <Lock className="h-3 w-3" strokeWidth={1.5} />
              <span>RO</span>
            </button>
          )}
        </div>

        {/* Status pills */}
        <GlassBadge tone={connection === 'LIVE' ? 'emerald' : 'amber'}>
          <StatusDot tone={connection === 'LIVE' ? 'emerald' : 'amber'} pulse />
          <span>{connection}</span>
        </GlassBadge>

        <GlassBadge tone="zinc" className="hidden sm:inline-flex">
          <span className="text-zinc-500">rev:</span>
          <span className="text-zinc-100">#{stateRev}</span>
        </GlassBadge>

        <GlassBadge tone={outdatedSessions > 0 ? 'amber' : 'sky'} className="hidden lg:inline-flex">
          <Cpu className="h-3 w-3" strokeWidth={1.5} />
          <span>{activeSessions}</span>
          {outdatedSessions > 0 && <span className="text-amber-300">({outdatedSessions} stale)</span>}
        </GlassBadge>

        <button
          type="button"
          onClick={onOpenPalette}
          className="flex h-7 items-center gap-1.5 rounded-md border border-white/[0.08] bg-white/[0.03] px-2 text-xs text-zinc-400 transition-colors duration-150 hover:border-white/[0.14] hover:bg-white/[0.06] hover:text-zinc-200"
        >
          <Search className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
          <span className="hidden 2xl:inline">Ask, drafts, hashes…</span>
          <kbd className="rounded border border-white/[0.1] bg-zinc-900/90 px-1 py-0.5 text-[10px] text-zinc-400 font-mono-tabular">
            Ctrl+K
          </kbd>
        </button>
      </div>
    </GlassPanel>
  );
}
