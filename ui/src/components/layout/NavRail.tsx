import { motion } from 'framer-motion';
import {
  AlertTriangle,
  Compass,
  FilePlus2,
  FileSearch,
  GitFork,
  Layers,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldAlert,
  Sparkles,
} from 'lucide-react';
import { GlassPanel } from '@/components/glass/GlassPanel';
import { cn, SPRING_PHYSICS } from '@/lib/utils';
import type { ActiveView } from '@/types/contracts';

interface NavRailProps {
  activeView: ActiveView;
  onSelectView: (view: ActiveView) => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  onlyStaleAndConflicts: boolean;
  onToggleOnlyStale: () => void;
  counts: {
    staleTasks: number;
    staleClaims: number;
    rejectedAudit: number;
    graphNodes: number;
    driftAlerts: number;
    draftPlans: number;
  };
}

interface NavItemDef {
  id: ActiveView;
  label: string;
  shortcut: string;
  icon: typeof Layers;
}

const INSPECTOR_ITEMS: NavItemDef[] = [
  { id: 'matrix', label: 'Plan & Concurrency', shortcut: '1', icon: Layers },
  { id: 'provenance', label: 'Provenance & Quotes', shortcut: '2', icon: FileSearch },
  { id: 'graph', label: 'Blast-Radius Graph', shortcut: '3', icon: GitFork },
  { id: 'audit', label: 'Guardrail Audit', shortcut: '4', icon: ShieldAlert },
];

const COCKPIT_ITEMS: NavItemDef[] = [
  { id: 'ask', label: 'Interactive Ask', shortcut: '5', icon: Sparkles },
  { id: 'drift', label: 'Plan & Drift Monitor', shortcut: '6', icon: Compass },
  { id: 'drafts', label: 'Draft State Manager', shortcut: '7', icon: FilePlus2 },
];

export function NavRail({
  activeView,
  onSelectView,
  collapsed,
  onToggleCollapse,
  onlyStaleAndConflicts,
  onToggleOnlyStale,
  counts,
}: NavRailProps) {
  const badgeFor = (
    id: ActiveView,
  ): { value: number; tone: 'amber' | 'rose' | 'sky' | 'zinc' } | null => {
    if (id === 'matrix' && counts.staleTasks > 0) return { value: counts.staleTasks, tone: 'amber' };
    if (id === 'provenance' && counts.staleClaims > 0)
      return { value: counts.staleClaims, tone: 'amber' };
    if (id === 'audit' && counts.rejectedAudit > 0)
      return { value: counts.rejectedAudit, tone: 'rose' };
    if (id === 'graph' && counts.graphNodes > 0) return { value: counts.graphNodes, tone: 'zinc' };
    if (id === 'drift' && counts.driftAlerts > 0)
      return { value: counts.driftAlerts, tone: 'amber' };
    if (id === 'drafts' && counts.draftPlans > 0) return { value: counts.draftPlans, tone: 'sky' };
    return null;
  };

  const renderItem = (item: NavItemDef) => {
    const Icon = item.icon;
    const isActive = activeView === item.id;
    const badge = badgeFor(item.id);
    return (
      <button
        key={item.id}
        type="button"
        onClick={() => onSelectView(item.id)}
        title={collapsed ? `${item.label} (${item.shortcut})` : undefined}
        className={cn(
          'relative flex h-8 items-center gap-2.5 rounded-md px-2 text-xs transition-colors duration-150',
          collapsed ? 'justify-center px-0' : 'justify-between',
          isActive ? 'text-zinc-100' : 'text-zinc-400 hover:bg-white/[0.04] hover:text-zinc-200',
        )}
      >
        {isActive && (
          <motion.span
            layoutId="active-nav-pill"
            transition={SPRING_PHYSICS}
            className="pointer-events-none absolute inset-0 rounded-md border border-sky-500/25 bg-sky-500/10"
          />
        )}
        <span className="relative z-10 flex min-w-0 items-center gap-2">
          <Icon
            className={cn('h-4 w-4 shrink-0', isActive ? 'text-sky-400' : 'text-zinc-400')}
            strokeWidth={1.5}
          />
          {!collapsed && <span className="truncate font-medium">{item.label}</span>}
        </span>
        {!collapsed && badge && (
          <span
            className={cn(
              'relative z-10 rounded px-1.5 py-0.25 text-[10px] font-mono-tabular',
              badge.tone === 'amber' && 'bg-amber-500/15 text-amber-400',
              badge.tone === 'rose' && 'bg-rose-500/15 text-rose-400',
              badge.tone === 'sky' && 'bg-sky-500/15 text-sky-300',
              badge.tone === 'zinc' && 'bg-white/[0.06] text-zinc-400',
            )}
          >
            {badge.value}
          </span>
        )}
      </button>
    );
  };

  return (
    <GlassPanel
      className={cn(
        'flex shrink-0 flex-col justify-between transition-[width] duration-150 overflow-hidden',
        collapsed ? 'w-14' : 'w-52',
      )}
    >
      <div className="flex flex-col gap-2 overflow-y-auto p-1.5">
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between px-1.5 py-1">
            {!collapsed && (
              <span className="text-[10px] font-medium uppercase tracking-wider text-zinc-500">
                Inspector
              </span>
            )}
            <button
              type="button"
              onClick={onToggleCollapse}
              title={collapsed ? 'Expand navigation ([)' : 'Collapse navigation ([)'}
              className={cn(
                'flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors duration-150 hover:bg-white/[0.06] hover:text-zinc-200',
                collapsed && 'mx-auto',
              )}
            >
              {collapsed ? (
                <PanelLeftOpen className="h-3.5 w-3.5" strokeWidth={1.5} />
              ) : (
                <PanelLeftClose className="h-3.5 w-3.5" strokeWidth={1.5} />
              )}
            </button>
          </div>

          <nav className="flex flex-col gap-0.5" aria-label="Inspector Views">
            {INSPECTOR_ITEMS.map(renderItem)}
          </nav>
        </div>

        <div className="border-t border-white/[0.06] pt-2 flex flex-col gap-1">
          {!collapsed && (
            <span className="px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-zinc-500">
              Engineer Cockpit
            </span>
          )}
          <nav className="flex flex-col gap-0.5" aria-label="Cockpit Views">
            {COCKPIT_ITEMS.map(renderItem)}
          </nav>
        </div>
      </div>

      {/* Bottom quick-toggle: [ ! Only Stale & Conflicts ] */}
      <div className="border-t border-white/[0.06] p-1.5">
        <button
          type="button"
          onClick={onToggleOnlyStale}
          aria-pressed={onlyStaleAndConflicts}
          title="Filter workspace to stale items and conflicts only"
          className={cn(
            'flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left text-[11px] font-medium transition-colors duration-150',
            collapsed && 'justify-center px-0',
            onlyStaleAndConflicts
              ? 'border-amber-500/30 bg-amber-500/12 text-amber-300'
              : 'border-white/[0.08] bg-white/[0.02] text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-200',
          )}
        >
          <AlertTriangle
            className={cn(
              'h-3.5 w-3.5 shrink-0',
              onlyStaleAndConflicts ? 'text-amber-400' : 'text-zinc-500',
            )}
            strokeWidth={1.5}
          />
          {!collapsed && <span className="truncate">Only Stale &amp; Conflicts</span>}
        </button>

        {!collapsed && (
          <div className="mt-1.5 flex items-center justify-between px-1 text-[10px] text-zinc-600 font-mono-tabular">
            <span>j/k nav</span>
            <span>1-7 views</span>
            <span>Esc close</span>
          </div>
        )}
      </div>
    </GlassPanel>
  );
}
