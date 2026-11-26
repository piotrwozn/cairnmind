import { AnimatePresence, motion } from 'framer-motion';
import {
  CheckCircle2,
  Cpu,
  ExternalLink,
  FileSearch,
  FlaskConical,
  GitFork,
  Layers,
  ShieldAlert,
  Trash2,
  X,
} from 'lucide-react';
import { McpPayloadInspector } from '@/components/audit/McpPayloadInspector';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { computeBlastRadius } from '@/components/graph/BlastRadiusGraph';
import { VerbatimCodeViewer } from '@/components/provenance/VerbatimCodeViewer';
import { formatShortTime } from '@/lib/utils';
import type { ActiveView, InspectorSelection, UiSnapshot } from '@/types/contracts';

interface InspectorDrawerProps {
  selection: InspectorSelection;
  snapshot: UiSnapshot | undefined;
  onClose: () => void;
  onJumpTo: (view: ActiveView, selection: InspectorSelection) => void;
  onDropSession: (sessionId: string) => void;
  droppingSessionId: string | null;
}

export function InspectorDrawer({
  selection,
  snapshot,
  onClose,
  onJumpTo,
  onDropSession,
  droppingSessionId,
}: InspectorDrawerProps) {
  const renderBody = () => {
    if (!selection || !snapshot) return null;

    if (selection.kind === 'claim') {
      const claim = snapshot.claims.find((c) => c.id === selection.id);
      if (!claim) return null;
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between gap-2">
              <GlassBadge tone="zinc">{claim.kind.toUpperCase()}</GlassBadge>
              <GlassBadge tone={statusToTone(claim.provenance_status)}>
                {claim.provenance_status}
              </GlassBadge>
            </div>
            <h3 className="text-sm font-semibold text-zinc-100 font-mono-tabular">{claim.key}</h3>
            <p className="text-xs leading-relaxed text-zinc-300">{claim.content}</p>
          </div>

          <VerbatimCodeViewer claim={claim} />

          {claim.task_ids.length > 0 && (
            <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-white/[0.02] p-2.5">
              <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
                Linked Plan Tasks ({claim.task_ids.length})
              </span>
              <div className="flex flex-wrap gap-1.5">
                {claim.task_ids.map((tid) => (
                  <button
                    key={tid}
                    type="button"
                    onClick={() => onJumpTo('matrix', { kind: 'task', id: tid })}
                    className="flex items-center gap-1 rounded border border-white/[0.08] bg-zinc-950/70 px-2 py-1 text-[11px] text-sky-300 font-mono-tabular hover:bg-white/[0.06]"
                  >
                    <span>{tid}</span>
                    <ExternalLink className="h-3 w-3" strokeWidth={1.5} />
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      );
    }

    if (selection.kind === 'task') {
      const task = snapshot.tasks.find((t) => t.id === selection.id);
      if (!task) return null;
      const linkedClaims = snapshot.claims.filter((c) => task.claim_ids.includes(c.id));
      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-semibold text-sky-400 font-mono-tabular">
                {task.id} · v{task.version}
              </span>
              <GlassBadge tone={statusToTone(task.state)}>{task.state.toUpperCase()}</GlassBadge>
            </div>
            <h3 className="text-sm font-semibold text-zinc-100">{task.title}</h3>
            <span className="text-[11px] text-zinc-500 font-mono-tabular">{task.plan_title}</span>
          </div>

          {/* Test Gate detail */}
          <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-zinc-950/60 p-2.5 text-xs">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
                <FlaskConical className="h-3.5 w-3.5 text-zinc-500" strokeWidth={1.5} />
                <span>Test Gate</span>
              </span>
              <GlassBadge tone={statusToTone(task.test_gate.status)}>
                {task.test_gate.status}
              </GlassBadge>
            </div>
            <div className="grid grid-cols-2 gap-1.5 text-[11px] font-mono-tabular">
              <div>
                <span className="text-zinc-500">profile: </span>
                <span className="text-zinc-200">{task.test_gate.profile_name}</span>
              </div>
              <div>
                <span className="text-zinc-500">sha256: </span>
                <span className="text-zinc-200">{task.test_gate.sha256_short ?? '—'}</span>
              </div>
              <div>
                <span className="text-zinc-500">run_id: </span>
                <span className="text-zinc-300">{task.test_gate.run_id ?? '—'}</span>
              </div>
              <div>
                <span className="text-zinc-500">ran_at: </span>
                <span className="text-zinc-300">{formatShortTime(task.test_gate.ran_at)}</span>
              </div>
            </div>
          </div>

          {/* Acceptance criteria */}
          {task.acceptance_criteria.length > 0 && (
            <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-white/[0.02] p-2.5">
              <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
                Acceptance Criteria
              </span>
              <ul className="flex flex-col gap-1 text-xs text-zinc-300">
                {task.acceptance_criteria.map((item) => (
                  <li key={item} className="flex items-start gap-1.5">
                    <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-500" strokeWidth={1.5} />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Backing Claims */}
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
              Evidence Claims ({task.evidence_verified}/{task.evidence_total} Verified)
            </span>
            {linkedClaims.length === 0 ? (
              <div className="rounded border border-white/[0.06] bg-white/[0.02] p-2 text-xs text-zinc-500 font-mono-tabular">
                No linked claims.
              </div>
            ) : (
              linkedClaims.map((claim) => (
                <button
                  key={claim.id}
                  type="button"
                  onClick={() => onJumpTo('provenance', { kind: 'claim', id: claim.id })}
                  className="flex items-center justify-between gap-2 rounded-md border border-white/[0.08] bg-zinc-950/60 p-2 text-left transition-colors hover:bg-white/[0.05]"
                >
                  <div className="min-w-0">
                    <div className="truncate text-xs font-medium text-zinc-200 font-mono-tabular">
                      {claim.key}
                    </div>
                    <div className="truncate text-[11px] text-zinc-500 font-mono-tabular">
                      {claim.source_path}:L{claim.line_start}
                    </div>
                  </div>
                  <GlassBadge tone={statusToTone(claim.provenance_status)}>
                    {claim.provenance_status}
                  </GlassBadge>
                </button>
              ))
            )}
          </div>
        </div>
      );
    }

    if (selection.kind === 'audit') {
      const entry = snapshot.audit_log.find((e) => e.id === selection.id);
      if (!entry) return null;
      return <McpPayloadInspector entry={entry} />;
    }

    if (selection.kind === 'session') {
      const session = snapshot.sessions.find((s) => s.id === selection.id);
      if (!session) return null;
      const isSynced = session.sync_status === 'SYNCED';
      return (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold text-zinc-100 font-mono-tabular">
              {session.client}
            </span>
            <GlassBadge tone={isSynced ? 'emerald' : 'amber'}>{session.sync_status}</GlassBadge>
          </div>
          <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-zinc-950/60 p-2.5 text-[11px] font-mono-tabular">
            <div>
              <span className="text-zinc-500">session_id: </span>
              <span className="text-zinc-200">{session.id}</span>
            </div>
            <div>
              <span className="text-zinc-500">acknowledged_rev: </span>
              <span className={isSynced ? 'text-emerald-400' : 'text-amber-400'}>
                #{session.acknowledged_rev}
              </span>
              <span className="text-zinc-500"> / #{session.current_state_rev}</span>
            </div>
            <div>
              <span className="text-zinc-500">packet_id: </span>
              <span className="text-zinc-300">{session.packet_id || '—'}</span>
            </div>
            <div>
              <span className="text-zinc-500">packet_hash: </span>
              <span className="text-zinc-300">{session.packet_hash || '—'}</span>
            </div>
            <div className="truncate">
              <span className="text-zinc-500">worktree: </span>
              <span className="text-zinc-300">{session.worktree}</span>
            </div>
            <div>
              <span className="text-zinc-500">last_seen: </span>
              <span className="text-zinc-300">{formatShortTime(session.last_seen_at)}</span>
            </div>
          </div>

          <button
            type="button"
            disabled={droppingSessionId === session.id}
            onClick={() => onDropSession(session.id)}
            className="flex h-8 items-center justify-center gap-1.5 rounded-md border border-rose-500/30 bg-rose-500/10 text-xs font-medium text-rose-300 transition-colors hover:bg-rose-500/20 disabled:opacity-50"
          >
            <Trash2 className="h-3.5 w-3.5" strokeWidth={1.5} />
            <span>
              {droppingSessionId === session.id ? 'Dropping session lock…' : 'Drop Session Lock'}
            </span>
          </button>
        </div>
      );
    }

    if (selection.kind === 'node') {
      const node = snapshot.graph.nodes.find((n) => n.id === selection.id);
      if (!node) return null;
      const blast = computeBlastRadius(snapshot.graph, node.id);
      const downstreamNodes = snapshot.graph.nodes.filter(
        (n) => n.id !== node.id && blast.nodes.has(n.id),
      );
      const linkedClaim =
        node.category === 'claim' || node.category === 'evidence'
          ? snapshot.claims.find((c) => c.id === node.ref_id)
          : undefined;

      return (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between gap-2">
              <GlassBadge tone="sky">{node.category.toUpperCase()}</GlassBadge>
              <GlassBadge tone={statusToTone(node.status)}>{node.status}</GlassBadge>
            </div>
            <h3 className="text-sm font-semibold text-zinc-100 font-mono-tabular">{node.label}</h3>
            <p className="text-xs text-zinc-400 font-mono-tabular">{node.subtitle}</p>
          </div>

          <div className="flex flex-col gap-1.5 rounded-md border border-white/[0.08] bg-zinc-950/60 p-2.5">
            <span className="text-[11px] font-medium uppercase tracking-wider text-zinc-500">
              Downstream Blast Radius ({downstreamNodes.length} impacted)
            </span>
            {downstreamNodes.length === 0 ? (
              <span className="text-xs text-zinc-500 font-mono-tabular">
                Terminal node — no downstream dependents.
              </span>
            ) : (
              <div className="flex flex-col gap-1">
                {downstreamNodes.map((dn) => (
                  <div
                    key={dn.id}
                    className="flex items-center justify-between gap-2 rounded border border-white/[0.06] bg-white/[0.02] px-2 py-1 text-[11px] font-mono-tabular"
                  >
                    <span className="truncate text-zinc-200">{dn.label}</span>
                    <GlassBadge tone={statusToTone(dn.status)}>{dn.status}</GlassBadge>
                  </div>
                ))}
              </div>
            )}
          </div>

          {linkedClaim && <VerbatimCodeViewer claim={linkedClaim} />}
        </div>
      );
    }

    return null;
  };

  const headerTitle = () => {
    if (!selection) return { label: 'Inspector', icon: FileSearch };
    switch (selection.kind) {
      case 'task':
        return { label: 'Task & Gate Detail', icon: Layers };
      case 'claim':
        return { label: 'Provenance & Quote Viewer', icon: FileSearch };
      case 'audit':
        return { label: 'MCP Payload Diff Inspector', icon: ShieldAlert };
      case 'session':
        return { label: 'OCC Session Lock', icon: Cpu };
      case 'node':
        return { label: 'Graph Blast-Radius Node', icon: GitFork };
    }
  };

  const { label, icon: HeaderIcon } = headerTitle();

  return (
    <AnimatePresence initial={false}>
      {selection && (
        <motion.aside
          key="inspector-drawer"
          initial={{ opacity: 0, x: 16 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 16 }}
          transition={{ duration: 0.16, ease: 'easeOut' }}
          className="flex h-full w-full flex-col overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/75 shadow-[0_16px_48px_rgba(0,0,0,0.65),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
        >
          <div className="flex h-10 shrink-0 items-center justify-between border-b border-white/[0.08] px-3">
            <div className="flex items-center gap-2">
              <HeaderIcon className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
              <span className="text-xs font-semibold text-zinc-100">{label}</span>
            </div>
            <button
              type="button"
              onClick={onClose}
              title="Close inspector (Esc)"
              className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-white/[0.08] hover:text-zinc-100"
            >
              <X className="h-3.5 w-3.5" strokeWidth={1.5} />
            </button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-3">{renderBody()}</div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
