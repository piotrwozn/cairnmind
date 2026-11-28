import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  BookmarkPlus,
  Check,
  ClipboardCopy,
  Edit3,
  FileCode2,
  FilePlus2,
  GitFork,
  History,
  ListPlus,
  Lock,
  Rocket,
} from 'lucide-react';
import { ApiConflictError, promotePlan } from '@/api/client';
import { ConflictBanner } from '@/components/drafts/ConflictBanner';
import type { DraftModalState } from '@/components/drafts/DraftModals';
import { GlassBadge } from '@/components/glass/GlassBadge';
import { OriginBadge } from '@/components/glass/OriginBadge';
import { formatShortTime, shortHash } from '@/lib/utils';
import type {
  ConflictErrorDetail,
  PlanSummary,
  PromotePlanPrecheck,
  UiSnapshot,
} from '@/types/contracts';

interface DraftStateManagerProps {
  snapshot: UiSnapshot | undefined;
  onOpenModal: (modal: DraftModalState) => void;
  onPreviewBlastRadius: (plan: PlanSummary) => void;
  onOpenPlanVersions: (planId: string) => void;
  onNotify: (msg: string) => void;
}

function buildHandoffBlock(plan: PlanSummary, projectName: string): string {
  const lines = [
    `# Cairnmind Agent Handoff`,
    `- Project: ${projectName}`,
    `- Plan ID: ${plan.id}`,
    `- Version: v${plan.version} (status: ${plan.status})`,
    ...(plan.external_ref ? [`- External Ref: ${plan.external_ref}`] : []),
    `- Title: ${plan.title}`,
    `- Goal: ${plan.goal || '—'}`,
    `- Linked Files: ${plan.linked_files.length ? plan.linked_files.join(', ') : 'none'}`,
    `- Steps (${plan.steps.length}):`,
    ...plan.steps.map((s, i) => `  ${i + 1}. [${s.key}] ${s.title}`),
    '',
    `Before writing memory or task progress, call Cairnmind MCP \`resume_context\` + \`ack_resume\` and supply \`expected_version: ${plan.version}\`.`,
  ];
  return lines.join('\n');
}

export function DraftStateManager({
  snapshot,
  onOpenModal,
  onPreviewBlastRadius,
  onOpenPlanVersions,
  onNotify,
}: DraftStateManagerProps) {
  const queryClient = useQueryClient();
  const plans = snapshot?.plans ?? [];
  const draftPlans = plans.filter((p) => p.status === 'draft');
  const activePlans = plans.filter((p) => p.status === 'active');
  const archivedPlans = plans.filter((p) => p.status === 'archived');

  const writable = snapshot?.capabilities?.writable ?? true;
  const readOnlyReason = snapshot?.capabilities?.read_only_reason;

  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [handoffPreviewId, setHandoffPreviewId] = useState<string | null>(null);
  const [promotingId, setPromotingId] = useState<string | null>(null);
  const [precheckWarning, setPrecheckWarning] = useState<{
    plan: PlanSummary;
    precheck: PromotePlanPrecheck;
  } | null>(null);
  const [conflict, setConflict] = useState<ConflictErrorDetail | null>(null);

  const handleCopyHandoff = async (plan: PlanSummary) => {
    const block = buildHandoffBlock(plan, snapshot?.project.name ?? 'cairnmind');
    try {
      await navigator.clipboard.writeText(block);
    } catch {
      // Fallback: still open preview box so engineer can copy manually
    }
    setCopiedId(plan.id);
    setHandoffPreviewId(plan.id);
    onNotify(`Copied agent handoff block for ${plan.id} v${plan.version}.`);
    window.setTimeout(() => setCopiedId((cur) => (cur === plan.id ? null : cur)), 2000);
  };

  const handlePromote = async (plan: PlanSummary, force = false) => {
    setPromotingId(plan.id);
    setConflict(null);
    try {
      const res = await promotePlan(plan.id, plan.version, force);
      if (res.requires_confirmation) {
        setPrecheckWarning({ plan, precheck: res.precheck });
      } else {
        setPrecheckWarning(null);
        void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
        void queryClient.invalidateQueries({ queryKey: ['ui-drift'] });
        onNotify(`Promoted '${res.plan.title}' to Active Plan v${res.plan.version}.`);
      }
    } catch (err) {
      if (err instanceof ApiConflictError) {
        setConflict(err.conflict);
      } else {
        onNotify(err instanceof Error ? err.message : 'Promotion failed');
      }
    } finally {
      setPromotingId(null);
    }
  };

  const renderPlanCard = (plan: PlanSummary) => {
    const isDraft = plan.status === 'draft';
    const showHandoff = handoffPreviewId === plan.id;
    const isCopied = copiedId === plan.id;
    const isWarningTarget = precheckWarning?.plan.id === plan.id;

    return (
      <div
        key={plan.id}
        className="flex flex-col gap-2.5 rounded-md border border-white/[0.08] bg-zinc-950/65 p-3 shadow-[0_8px_24px_rgba(0,0,0,0.35)]"
      >
        {/* Top Row */}
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <GlassBadge tone={isDraft ? 'sky' : plan.status === 'active' ? 'emerald' : 'zinc'}>
                {plan.status.toUpperCase()}
              </GlassBadge>
              <OriginBadge origin={plan.origin} compact />
              {plan.external_ref && <GlassBadge tone="zinc">{plan.external_ref}</GlassBadge>}
              <button
                type="button"
                onClick={() => onOpenPlanVersions(plan.id)}
                title="View version history & time-travel diff"
                className="flex items-center gap-1 rounded border border-white/[0.08] bg-white/[0.03] px-1.5 py-0.5 text-[10px] font-mono-tabular text-sky-300 hover:bg-sky-500/15"
              >
                <History className="h-3 w-3" strokeWidth={1.5} />
                <span>v{plan.version}</span>
              </button>
              <span className="text-[10px] text-zinc-500 font-mono-tabular">{plan.id}</span>
            </div>
            <h3 className="text-sm font-semibold text-zinc-100">{plan.title}</h3>
            {plan.goal && <p className="text-xs text-zinc-400">{plan.goal}</p>}
          </div>

          <span className="text-[10px] text-zinc-500 font-mono-tabular">
            updated {formatShortTime(plan.updated_at)}
          </span>
        </div>

        {/* Linked Files + Steps Preview */}
        <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
          <div className="rounded border border-white/[0.06] bg-white/[0.02] p-2">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-zinc-500">
              Steps ({plan.steps.length})
            </div>
            {plan.steps.length === 0 ? (
              <div className="text-[11px] text-zinc-600 font-mono-tabular">No steps defined yet.</div>
            ) : (
              <ol className="space-y-1 text-[11px] text-zinc-300">
                {plan.steps.map((s, idx) => (
                  <li key={`${plan.id}-${s.key}-${idx}`} className="flex items-start gap-1.5">
                    <span className="shrink-0 text-sky-400 font-mono-tabular">{idx + 1}.</span>
                    <span className="min-w-0 truncate">
                      <span className="text-zinc-500 font-mono-tabular">[{s.key}]</span> {s.title}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </div>

          <div className="rounded border border-white/[0.06] bg-white/[0.02] p-2">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-zinc-500">
              Linked Files ({plan.linked_files.length})
            </div>
            {plan.linked_files.length === 0 ? (
              <div className="text-[11px] text-zinc-600 font-mono-tabular">No linked repository paths.</div>
            ) : (
              <div className="flex flex-wrap gap-1">
                {plan.linked_files.map((fp) => (
                  <span
                    key={fp}
                    className="inline-flex items-center gap-1 rounded border border-white/[0.07] bg-zinc-950/80 px-1.5 py-0.5 text-[10px] text-zinc-300 font-mono-tabular"
                  >
                    <FileCode2 className="h-3 w-3 text-sky-400" strokeWidth={1.5} />
                    <span>{fp}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Promote Pre-check Fingerprint Warning (Edge Case: deleted or drifted files before activation) */}
        {isWarningTarget && precheckWarning && (
          <div className="flex flex-col gap-2 rounded-md border border-amber-500/40 bg-amber-500/[0.08] p-2.5 text-xs">
            <div className="flex items-center gap-2 font-semibold text-amber-200">
              <AlertTriangle className="h-4 w-4 text-amber-400" strokeWidth={1.75} />
              <span>Pre-check Fingerprint Warning before Promoting to Active</span>
            </div>

            {precheckWarning.precheck.missing_files.length > 0 && (
              <div className="text-[11px] text-rose-300 font-mono-tabular">
                Missing / deleted files:{' '}
                {precheckWarning.precheck.missing_files.join(', ')}
              </div>
            )}
            {precheckWarning.precheck.drifted_files.length > 0 && (
              <div className="space-y-0.5 text-[11px] text-amber-200 font-mono-tabular">
                {precheckWarning.precheck.drifted_files.map((df) => (
                  <div key={df.path}>
                    Drifted: {df.path} (draft:{shortHash(df.recorded_sha256, 7)} -&gt; disk:
                    {shortHash(df.current_sha256, 7)})
                  </div>
                ))}
              </div>
            )}

            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setPrecheckWarning(null)}
                className="h-6 rounded border border-white/[0.1] bg-white/[0.04] px-2 text-[11px] text-zinc-300 hover:bg-white/[0.08]"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handlePromote(plan, true)}
                className="h-6 rounded border border-amber-500/40 bg-amber-500/20 px-2.5 text-[11px] font-medium text-amber-100 hover:bg-amber-500/30"
              >
                Promote Anyway
              </button>
            </div>
          </div>
        )}

        {/* Copy Handoff Expandable Preview */}
        {showHandoff && (
          <div className="rounded border border-sky-500/25 bg-zinc-950/90 p-2">
            <div className="mb-1 flex items-center justify-between text-[10px] font-mono-tabular text-sky-300">
              <span>Ready for Claude Code / Codex / Cursor</span>
              <button
                type="button"
                onClick={() => setHandoffPreviewId(null)}
                className="text-zinc-500 hover:text-zinc-200"
              >
                Hide
              </button>
            </div>
            <pre className="max-h-36 overflow-auto whitespace-pre-wrap text-[10px] leading-relaxed text-zinc-300 font-mono-tabular">
              {buildHandoffBlock(plan, snapshot?.project.name ?? 'cairnmind')}
            </pre>
          </div>
        )}

        {/* Action Toolbar */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-white/[0.06] pt-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => onPreviewBlastRadius(plan)}
              className="flex h-7 items-center gap-1.5 rounded-md border border-sky-500/30 bg-sky-500/10 px-2.5 text-[11px] font-medium text-sky-300 transition-colors hover:bg-sky-500/20"
            >
              <GitFork className="h-3.5 w-3.5" strokeWidth={1.5} />
              <span>Preview Blast Radius</span>
            </button>

            <button
              type="button"
              onClick={() => void handleCopyHandoff(plan)}
              className="flex h-7 items-center gap-1.5 rounded-md border border-white/[0.1] bg-white/[0.04] px-2.5 text-[11px] font-medium text-zinc-200 transition-colors hover:bg-white/[0.08]"
            >
              {isCopied ? (
                <Check className="h-3.5 w-3.5 text-emerald-400" strokeWidth={1.5} />
              ) : (
                <ClipboardCopy className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
              )}
              <span>{isCopied ? 'Copied Handoff' : 'Copy Handoff'}</span>
            </button>

            <button
              type="button"
              onClick={() => onOpenPlanVersions(plan.id)}
              className="flex h-7 items-center gap-1.5 rounded-md border border-white/[0.08] bg-white/[0.02] px-2.5 text-[11px] font-medium text-zinc-300 transition-colors hover:bg-white/[0.07]"
            >
              <History className="h-3.5 w-3.5 text-zinc-400" strokeWidth={1.5} />
              <span>Versions (v{plan.version})</span>
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            <button
              type="button"
              disabled={!writable}
              title={writable ? 'Edit draft with expected_version CAS' : readOnlyReason || 'Read-only mode'}
              onClick={() => onOpenModal({ kind: 'plan', editingPlan: plan })}
              className="flex h-7 items-center gap-1.5 rounded-md border border-white/[0.1] bg-white/[0.03] px-2.5 text-[11px] font-medium text-zinc-200 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Edit3 className="h-3.5 w-3.5" strokeWidth={1.5} />
              <span>Edit</span>
            </button>

            {isDraft && (
              <button
                type="button"
                disabled={!writable || promotingId === plan.id}
                title={
                  writable
                    ? 'Run fingerprint pre-check and promote draft to Active Plan'
                    : readOnlyReason || 'Read-only mode'
                }
                onClick={() => void handlePromote(plan, false)}
                className="flex h-7 items-center gap-1.5 rounded-md border border-emerald-500/35 bg-emerald-500/15 px-2.5 text-[11px] font-medium text-emerald-200 transition-colors hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Rocket className="h-3.5 w-3.5" strokeWidth={1.5} />
                <span>{promotingId === plan.id ? 'Checking…' : 'Promote to Active'}</span>
              </button>
            )}
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* Header Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex items-center gap-2">
          <FilePlus2 className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold text-zinc-100">Draft State Manager</span>
          <span className="hidden text-[11px] text-zinc-500 font-mono-tabular md:inline">
            Plan ahead (Jira / specs) without starting agent execution
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {!writable && (
            <GlassBadge tone="amber">
              <Lock className="h-3 w-3" strokeWidth={1.5} />
              <span>READ-ONLY</span>
            </GlassBadge>
          )}
          <button
            type="button"
            disabled={!writable}
            title={writable ? 'Create a new draft plan (status: draft)' : readOnlyReason || 'Read-only mode'}
            onClick={() => onOpenModal({ kind: 'plan', editingPlan: null })}
            className="flex h-7 items-center gap-1.5 rounded-md border border-sky-500/35 bg-sky-500/15 px-2.5 text-xs font-medium text-sky-200 transition-colors hover:bg-sky-500/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <FilePlus2 className="h-3.5 w-3.5" strokeWidth={1.5} />
            <span>Draft Plan</span>
          </button>
          <button
            type="button"
            disabled={!writable}
            title={writable ? 'Add a single independent task' : readOnlyReason || 'Read-only mode'}
            onClick={() => onOpenModal({ kind: 'task' })}
            className="flex h-7 items-center gap-1.5 rounded-md border border-white/[0.1] bg-white/[0.04] px-2.5 text-xs font-medium text-zinc-200 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ListPlus className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
            <span>Quick Task</span>
          </button>
          <button
            type="button"
            disabled={!writable}
            title={writable ? 'Record a decision or fact with optional file/line evidence' : readOnlyReason || 'Read-only mode'}
            onClick={() => onOpenModal({ kind: 'fact' })}
            className="flex h-7 items-center gap-1.5 rounded-md border border-white/[0.1] bg-white/[0.04] px-2.5 text-xs font-medium text-zinc-200 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <BookmarkPlus className="h-3.5 w-3.5 text-emerald-400" strokeWidth={1.5} />
            <span>Add Fact</span>
          </button>
        </div>
      </div>

      {/* Main Content */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3 space-y-4">
        {conflict && (
          <ConflictBanner
            conflict={conflict}
            onRefreshFromServer={() => {
              setConflict(null);
              void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
            }}
            onSaveAsSeparateDraft={() => {
              setConflict(null);
              onOpenModal({ kind: 'plan', editingPlan: null });
            }}
          />
        )}

        {/* Draft Plans */}
        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-sky-400">
              Draft Plans ({draftPlans.length}) — Isolated / Non-Blocking
            </span>
          </div>
          {draftPlans.length === 0 ? (
            <div className="rounded-md border border-dashed border-white/[0.08] p-6 text-center text-xs text-zinc-500 font-mono-tabular">
              No draft plans yet. Click &ldquo;Draft Plan&rdquo; to stage upcoming Jira tasks before implementation.
            </div>
          ) : (
            <div className="flex flex-col gap-2.5">{draftPlans.map(renderPlanCard)}</div>
          )}
        </section>

        {/* Active Plan */}
        {activePlans.length > 0 && (
          <section className="space-y-2">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-emerald-400">
              Active Plan ({activePlans.length})
            </span>
            <div className="flex flex-col gap-2.5">{activePlans.map(renderPlanCard)}</div>
          </section>
        )}

        {/* Archived Plans */}
        {archivedPlans.length > 0 && (
          <section className="space-y-2">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
              Archived Plans ({archivedPlans.length})
            </span>
            <div className="flex flex-col gap-2.5">{archivedPlans.map(renderPlanCard)}</div>
          </section>
        )}
      </div>
    </div>
  );
}
