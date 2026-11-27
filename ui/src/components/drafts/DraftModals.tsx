import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  BookmarkPlus,
  FileCode2,
  FilePlus2,
  ListPlus,
  Loader2,
  Plus,
  Trash2,
  X,
  Zap,
} from 'lucide-react';
import {
  ApiConflictError,
  createFact,
  createPlan,
  createTask,
  fetchFileContext,
  getCockpitAdapter,
  updatePlan,
} from '@/api/client';
import { ConflictBanner } from '@/components/drafts/ConflictBanner';
import { GlassBadge } from '@/components/glass/GlassBadge';
import type {
  ConflictErrorDetail,
  PlanStep,
  PlanSummary,
  UiSnapshot,
} from '@/types/contracts';

export type DraftModalState =
  | { kind: 'plan'; editingPlan?: PlanSummary | null }
  | { kind: 'task'; defaultPlanId?: string | null }
  | {
      kind: 'fact';
      prefill?: {
        kind?: 'fact' | 'decision' | 'constraint';
        key?: string;
        title?: string;
        content?: string;
        source_path?: string;
        line_start?: number;
        line_end?: number;
        verbatim_quote?: string;
      };
    }
  | null;

interface DraftModalsProps {
  modal: DraftModalState;
  snapshot: UiSnapshot | undefined;
  onClose: () => void;
  onSuccess: (message: string) => void;
}

export function DraftModals({ modal, snapshot, onClose, onSuccess }: DraftModalsProps) {
  const adapter = getCockpitAdapter();
  const isMock = adapter.mode === 'mock';
  const writable = snapshot?.capabilities?.writable ?? true;

  // Plan state
  const [planTitle, setPlanTitle] = useState('');
  const [planGoal, setPlanGoal] = useState('');
  const [planExternalRef, setPlanExternalRef] = useState('');
  const [planConstraints, setPlanConstraints] = useState('');
  const [planLinkedFiles, setPlanLinkedFiles] = useState<string[]>([]);
  const [customFileInput, setCustomFileInput] = useState('');
  const [planSteps, setPlanSteps] = useState<PlanStep[]>([]);
  const [expectedVersion, setExpectedVersion] = useState(0);

  // Task state
  const [taskTitle, setTaskTitle] = useState('');
  const [taskStepKey, setTaskStepKey] = useState('');
  const [taskPlanId, setTaskPlanId] = useState('');
  const [taskExternalRef, setTaskExternalRef] = useState('');
  const [taskScope, setTaskScope] = useState('');
  const [taskCriteria, setTaskCriteria] = useState('');

  // Fact state
  const [factKind, setFactKind] = useState<'fact' | 'decision' | 'constraint'>('fact');
  const [factKey, setFactKey] = useState('');
  const [factTitle, setFactTitle] = useState('');
  const [factContent, setFactContent] = useState('');
  const [factSourcePath, setFactSourcePath] = useState('');
  const [factLineStart, setFactLineStart] = useState(1);
  const [factLineEnd, setFactLineEnd] = useState(20);
  const [factQuote, setFactQuote] = useState('');
  const [loadingSnippet, setLoadingSnippet] = useState(false);

  // Shared submit & 409 conflict state
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [conflict, setConflict] = useState<ConflictErrorDetail | null>(null);
  const [simNotice, setSimNotice] = useState<string | null>(null);

  const knownFiles = Array.from(
    new Set([
      ...(snapshot?.graph.nodes
        .filter((n) => n.category === 'source_file')
        .map((n) => n.ref_id) ?? []),
      ...(snapshot?.claims.map((c) => c.source_path).filter(Boolean) ?? []),
    ]),
  ).sort();

  useEffect(() => {
    setErrorMsg(null);
    setConflict(null);
    setSimNotice(null);
    if (!modal) return;

    if (modal.kind === 'plan') {
      const p = modal.editingPlan;
      if (p) {
        setPlanTitle(p.title);
        setPlanGoal(p.goal);
        setPlanExternalRef(p.external_ref ?? '');
        setPlanConstraints(p.constraints.join('\n'));
        setPlanLinkedFiles([...p.linked_files]);
        setPlanSteps(
          p.steps.length > 0
            ? structuredClone(p.steps)
            : [{ key: 'step-1', title: '', depends_on: [], scope: [], acceptance_criteria: [] }],
        );
        setExpectedVersion(p.version);
      } else {
        setPlanTitle('');
        setPlanGoal('');
        setPlanExternalRef('');
        setPlanConstraints('');
        setPlanLinkedFiles([]);
        setPlanSteps([
          { key: 'step-1', title: '', depends_on: [], scope: [], acceptance_criteria: [] },
        ]);
        setExpectedVersion(0);
      }
      setCustomFileInput('');
    } else if (modal.kind === 'task') {
      setTaskTitle('');
      setTaskStepKey('');
      setTaskPlanId(modal.defaultPlanId ?? '');
      setTaskExternalRef('');
      setTaskScope('');
      setTaskCriteria('');
    } else if (modal.kind === 'fact') {
      setFactKind(modal.prefill?.kind ?? 'fact');
      setFactKey(modal.prefill?.key ?? '');
      setFactTitle(modal.prefill?.title ?? '');
      setFactContent(modal.prefill?.content ?? '');
      setFactSourcePath(modal.prefill?.source_path ?? '');
      setFactLineStart(modal.prefill?.line_start ?? 1);
      setFactLineEnd(modal.prefill?.line_end ?? 20);
      setFactQuote(modal.prefill?.verbatim_quote ?? '');
    }
  }, [modal]);

  const handleLoadQuoteFromFile = async () => {
    if (!factSourcePath.trim()) return;
    setLoadingSnippet(true);
    setErrorMsg(null);
    try {
      const ctx = await fetchFileContext(
        factSourcePath.trim(),
        Math.max(1, factLineStart),
        Math.max(factLineStart, factLineEnd),
      );
      setFactQuote(ctx.lines.map((l) => l.text).join('\n').trim().slice(0, 1200));
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Unable to read snippet');
    } finally {
      setLoadingSnippet(false);
    }
  };

  const handleSubmitPlan = async () => {
    if (!modal || modal.kind !== 'plan') return;
    if (!planTitle.trim()) {
      setErrorMsg('Plan title is required.');
      return;
    }
    setSubmitting(true);
    setErrorMsg(null);
    setConflict(null);
    const cleanedSteps = planSteps
      .filter((s) => s.title.trim())
      .map((s, idx) => ({
        key: s.key.trim() || `step-${idx + 1}`,
        title: s.title.trim(),
        depends_on: s.depends_on,
        scope: s.scope.length > 0 ? s.scope : planLinkedFiles.slice(0, 2),
        acceptance_criteria: s.acceptance_criteria,
      }));
    const constraintsList = planConstraints
      .split('\n')
      .map((c) => c.trim())
      .filter(Boolean);

    try {
      if (modal.editingPlan) {
        const res = await updatePlan(modal.editingPlan.id, {
          expected_version: expectedVersion,
          title: planTitle.trim(),
          goal: planGoal.trim(),
          external_ref: planExternalRef.trim() || null,
          constraints: constraintsList,
          linked_files: planLinkedFiles,
          steps: cleanedSteps,
        });
        onSuccess(`Saved draft '${res.plan.title}' (v${res.plan.version}).`);
        onClose();
      } else {
        const res = await createPlan({
          title: planTitle.trim(),
          goal: planGoal.trim(),
          status: 'draft',
          external_ref: planExternalRef.trim() || null,
          constraints: constraintsList,
          linked_files: planLinkedFiles,
          steps: cleanedSteps,
          expected_version: 0,
        });
        onSuccess(`Created Draft Plan '${res.plan.title}' (no execution started).`);
        onClose();
      }
    } catch (err) {
      if (err instanceof ApiConflictError) {
        setConflict(err.conflict);
      } else {
        setErrorMsg(err instanceof Error ? err.message : 'Failed to save draft plan');
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleRefreshFromConflict = () => {
    if (!conflict?.server_snapshot) {
      setConflict(null);
      return;
    }
    const srv = conflict.server_snapshot as PlanSummary;
    setPlanTitle(srv.title ?? planTitle);
    setPlanGoal(srv.goal ?? planGoal);
    setPlanExternalRef(srv.external_ref ?? '');
    setPlanConstraints((srv.constraints ?? []).join('\n'));
    setPlanLinkedFiles([...(srv.linked_files ?? [])]);
    setPlanSteps(structuredClone(srv.steps ?? []));
    setExpectedVersion(srv.version ?? conflict.current_version);
    setConflict(null);
    setSimNotice(`Refreshed editor to server v${conflict.current_version}.`);
  };

  const handleSaveAsSeparateDraft = async () => {
    setSubmitting(true);
    setErrorMsg(null);
    const cleanedSteps = planSteps
      .filter((s) => s.title.trim())
      .map((s, idx) => ({
        key: s.key.trim() || `step-${idx + 1}`,
        title: s.title.trim(),
        depends_on: s.depends_on,
        scope: s.scope,
        acceptance_criteria: s.acceptance_criteria,
      }));
    try {
      const res = await createPlan({
        title: `${planTitle.trim()} (conflict fork)`,
        goal: planGoal.trim(),
        status: 'draft',
        external_ref: planExternalRef.trim() || null,
        constraints: planConstraints
          .split('\n')
          .map((c) => c.trim())
          .filter(Boolean),
        linked_files: planLinkedFiles,
        steps: cleanedSteps,
        expected_version: 0,
      });
      onSuccess(`Forked your edits into new Draft Plan '${res.plan.title}' (v1).`);
      onClose();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to fork draft');
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmitTask = async () => {
    if (!taskTitle.trim()) {
      setErrorMsg('Task title is required.');
      return;
    }
    setSubmitting(true);
    setErrorMsg(null);
    const targetPlan = (snapshot?.plans ?? []).find((p) => p.id === taskPlanId);
    try {
      const res = await createTask({
        title: taskTitle.trim(),
        step_key: taskStepKey.trim() || undefined,
        plan_id: targetPlan?.id ?? null,
        external_ref: taskExternalRef.trim() || null,
        scope: taskScope
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        acceptance_criteria: taskCriteria
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean),
        status: 'draft',
        expected_version: targetPlan ? targetPlan.version : (snapshot?.state_rev ?? 0),
      });
      onSuccess(`Added Quick Task ${res.task.id} (${res.task.state}).`);
      onClose();
    } catch (err) {
      if (err instanceof ApiConflictError) {
        setConflict(err.conflict);
      } else {
        setErrorMsg(err instanceof Error ? err.message : 'Failed to create task');
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmitFact = async () => {
    if (!factKey.trim() || !factTitle.trim() || !factContent.trim()) {
      setErrorMsg('Key, title, and content are required.');
      return;
    }
    setSubmitting(true);
    setErrorMsg(null);
    try {
      const res = await createFact({
        kind: factKind,
        key: factKey.trim(),
        title: factTitle.trim(),
        content: factContent.trim(),
        source_path: factSourcePath.trim() || null,
        line_start: factLineStart,
        line_end: Math.max(factLineStart, factLineEnd),
        verbatim_quote: factQuote.trim(),
        expected_version: snapshot?.state_rev ?? 0,
      });
      onSuccess(`Recorded ${res.claim.kind} '${res.claim.key}' (HUMAN / REPO).`);
      onClose();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Failed to save fact');
    } finally {
      setSubmitting(false);
    }
  };

  const addLinkedFile = (filePath: string) => {
    const clean = filePath.trim();
    if (!clean) return;
    setPlanLinkedFiles((prev) => (prev.includes(clean) ? prev : [...prev, clean]));
    setCustomFileInput('');
  };

  return (
    <AnimatePresence>
      {modal && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) onClose();
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.98, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: -6 }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
            className="flex max-h-[88vh] w-full max-w-[680px] flex-col overflow-hidden rounded-lg border border-white/[0.12] bg-zinc-900/90 shadow-[0_20px_60px_rgba(0,0,0,0.75),inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-2xl"
          >
            {/* Modal Header */}
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-white/[0.08] px-4">
              <div className="flex items-center gap-2">
                {modal.kind === 'plan' && <FilePlus2 className="h-4 w-4 text-sky-400" strokeWidth={1.5} />}
                {modal.kind === 'task' && <ListPlus className="h-4 w-4 text-sky-400" strokeWidth={1.5} />}
                {modal.kind === 'fact' && <BookmarkPlus className="h-4 w-4 text-sky-400" strokeWidth={1.5} />}
                <span className="text-xs font-semibold text-zinc-100">
                  {modal.kind === 'plan'
                    ? modal.editingPlan
                      ? `Edit Draft Plan · v${expectedVersion}`
                      : 'New Draft Plan (No Execution)'
                    : modal.kind === 'task'
                      ? 'Quick Single Task (Draft / Backlog)'
                      : 'Add Decision / Fact (HUMAN / REPO)'}
                </span>
                {modal.kind === 'plan' && modal.editingPlan && (
                  <GlassBadge tone="zinc">CAS expected_version={expectedVersion}</GlassBadge>
                )}
              </div>
              <button
                type="button"
                onClick={onClose}
                className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100"
              >
                <X className="h-4 w-4" strokeWidth={1.5} />
              </button>
            </div>

            {/* Modal Body */}
            <div className="min-h-0 flex-1 overflow-y-auto p-4 space-y-3">
              {conflict && (
                <ConflictBanner
                  conflict={conflict}
                  onRefreshFromServer={handleRefreshFromConflict}
                  onSaveAsSeparateDraft={() => void handleSaveAsSeparateDraft()}
                  savingFork={submitting}
                />
              )}

              {simNotice && (
                <div className="rounded border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs text-sky-200 font-mono-tabular">
                  {simNotice}
                </div>
              )}

              {errorMsg && (
                <div className="rounded border border-rose-500/30 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-200">
                  {errorMsg}
                </div>
              )}

              {/* PLAN FORM */}
              {modal.kind === 'plan' && (
                <div className="space-y-3">
                  {isMock && modal.editingPlan && adapter.mockControls && (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-amber-500/25 bg-amber-500/[0.06] px-3 py-2 text-[11px] text-amber-200">
                      <span>
                        Test CAS 409: symuluj równoległą zmianę tego planu przez agenta MCP przed kliknięciem Save.
                      </span>
                      <button
                        type="button"
                        onClick={async () => {
                          const res = await adapter.mockControls!.simulateConflict(
                            modal.editingPlan!.id,
                            'codex-cli',
                          );
                          setSimNotice(
                            `MCP agent codex-cli bumped ${modal.editingPlan!.id} to v${res.current_version} in background — click "Save Draft" to trigger 409!`,
                          );
                        }}
                        className="flex h-6 items-center gap-1 rounded border border-amber-500/40 bg-amber-500/15 px-2 text-[11px] font-medium text-amber-200 hover:bg-amber-500/25"
                      >
                        <Zap className="h-3 w-3" strokeWidth={1.5} />
                        <span>Simulate MCP Edit (409)</span>
                      </button>
                    </div>
                  )}

                  <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                    <label className="sm:col-span-2 flex flex-col gap-1">
                      <span className="text-[11px] font-medium text-zinc-400">Title *</span>
                      <input
                        type="text"
                        value={planTitle}
                        onChange={(e) => setPlanTitle(e.target.value)}
                        placeholder="e.g. Rate limiter rollout for webhook endpoints"
                        className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 focus:border-sky-500/50 focus:outline-none"
                      />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="text-[11px] font-medium text-zinc-400">External ID / Link</span>
                      <input
                        type="text"
                        value={planExternalRef}
                        onChange={(e) => setPlanExternalRef(e.target.value)}
                        placeholder="JIRA-1234"
                        className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 font-mono-tabular focus:border-sky-500/50 focus:outline-none"
                      />
                    </label>
                  </div>

                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-zinc-400">Goal</span>
                    <textarea
                      rows={2}
                      value={planGoal}
                      onChange={(e) => setPlanGoal(e.target.value)}
                      placeholder="What outcome defines done? Saved purely as a draft until promoted."
                      className="rounded-md border border-white/[0.1] bg-zinc-950/80 p-2 text-xs text-zinc-100 focus:border-sky-500/50 focus:outline-none"
                    />
                  </label>

                  {/* Linked Files */}
                  <div className="flex flex-col gap-1.5">
                    <span className="text-[11px] font-medium text-zinc-400">
                      Linked Repository Files (used for Preview Blast Radius &amp; Promote Pre-check)
                    </span>
                    <div className="flex flex-wrap gap-1">
                      {knownFiles.map((f) => {
                        const selected = planLinkedFiles.includes(f);
                        return (
                          <button
                            key={f}
                            type="button"
                            onClick={() =>
                              setPlanLinkedFiles((prev) =>
                                prev.includes(f) ? prev.filter((x) => x !== f) : [...prev, f],
                              )
                            }
                            className={`rounded border px-2 py-0.5 text-[10px] font-mono-tabular transition-colors ${
                              selected
                                ? 'border-sky-500/40 bg-sky-500/15 text-sky-200'
                                : 'border-white/[0.08] bg-white/[0.02] text-zinc-400 hover:text-zinc-200'
                            }`}
                          >
                            {f}
                          </button>
                        );
                      })}
                    </div>
                    <div className="flex items-center gap-1.5">
                      <input
                        type="text"
                        value={customFileInput}
                        onChange={(e) => setCustomFileInput(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.preventDefault();
                            addLinkedFile(customFileInput);
                          }
                        }}
                        placeholder="Add custom repo path (e.g. src/cairnmind/new_module.py)"
                        className="h-7 flex-1 rounded-md border border-white/[0.08] bg-zinc-950/70 px-2 text-xs text-zinc-200 font-mono-tabular focus:border-sky-500/40 focus:outline-none"
                      />
                      <button
                        type="button"
                        onClick={() => addLinkedFile(customFileInput)}
                        className="h-7 rounded-md border border-white/[0.1] bg-white/[0.04] px-2 text-xs text-zinc-300 hover:bg-white/[0.08]"
                      >
                        Add path
                      </button>
                    </div>
                  </div>

                  {/* Steps */}
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center justify-between">
                      <span className="text-[11px] font-medium text-zinc-400">
                        Draft Steps / Tasks ({planSteps.length})
                      </span>
                      <button
                        type="button"
                        onClick={() =>
                          setPlanSteps((prev) => [
                            ...prev,
                            {
                              key: `step-${prev.length + 1}`,
                              title: '',
                              depends_on: [],
                              scope: [],
                              acceptance_criteria: [],
                            },
                          ])
                        }
                        className="flex items-center gap-1 rounded border border-sky-500/25 bg-sky-500/10 px-2 py-0.5 text-[11px] text-sky-300 hover:bg-sky-500/20"
                      >
                        <Plus className="h-3 w-3" strokeWidth={1.5} />
                        <span>Add step</span>
                      </button>
                    </div>

                    <div className="space-y-1.5">
                      {planSteps.map((step, idx) => (
                        <div
                          key={`${idx}-${step.key}`}
                          className="flex items-center gap-1.5 rounded-md border border-white/[0.07] bg-zinc-950/60 p-1.5"
                        >
                          <input
                            type="text"
                            value={step.key}
                            onChange={(e) =>
                              setPlanSteps((prev) =>
                                prev.map((item, i) =>
                                  i === idx ? { ...item, key: e.target.value } : item,
                                ),
                              )
                            }
                            placeholder="step-key"
                            className="h-7 w-32 shrink-0 rounded border border-white/[0.08] bg-zinc-950 px-2 text-[11px] text-sky-300 font-mono-tabular focus:outline-none"
                          />
                          <input
                            type="text"
                            value={step.title}
                            onChange={(e) =>
                              setPlanSteps((prev) =>
                                prev.map((item, i) =>
                                  i === idx ? { ...item, title: e.target.value } : item,
                                ),
                              )
                            }
                            placeholder="Step description"
                            className="h-7 min-w-0 flex-1 rounded border border-white/[0.08] bg-zinc-950 px-2 text-xs text-zinc-100 focus:outline-none"
                          />
                          <button
                            type="button"
                            onClick={() =>
                              setPlanSteps((prev) => prev.filter((_, i) => i !== idx))
                            }
                            className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-zinc-500 hover:bg-rose-500/15 hover:text-rose-300"
                          >
                            <Trash2 className="h-3.5 w-3.5" strokeWidth={1.5} />
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>

                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-zinc-400">
                      Constraints (one per line)
                    </span>
                    <textarea
                      rows={2}
                      value={planConstraints}
                      onChange={(e) => setPlanConstraints(e.target.value)}
                      placeholder="Zero breaking schema changes"
                      className="rounded-md border border-white/[0.1] bg-zinc-950/80 p-2 text-xs text-zinc-200 font-mono-tabular focus:border-sky-500/50 focus:outline-none"
                    />
                  </label>
                </div>
              )}

              {/* QUICK TASK FORM */}
              {modal.kind === 'task' && (
                <div className="space-y-3">
                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-zinc-400">Task Title *</span>
                    <input
                      type="text"
                      value={taskTitle}
                      onChange={(e) => setTaskTitle(e.target.value)}
                      placeholder="e.g. Add retry backoff test for renamed-back directories"
                      className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 focus:border-sky-500/50 focus:outline-none"
                    />
                  </label>

                  <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                    <label className="flex flex-col gap-1">
                      <span className="text-[11px] font-medium text-zinc-400">Step Key</span>
                      <input
                        type="text"
                        value={taskStepKey}
                        onChange={(e) => setTaskStepKey(e.target.value)}
                        placeholder="auto-generated"
                        className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 font-mono-tabular focus:outline-none"
                      />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="text-[11px] font-medium text-zinc-400">External ID</span>
                      <input
                        type="text"
                        value={taskExternalRef}
                        onChange={(e) => setTaskExternalRef(e.target.value)}
                        placeholder="JIRA-1235"
                        className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 font-mono-tabular focus:outline-none"
                      />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="text-[11px] font-medium text-zinc-400">Attach to Plan</span>
                      <select
                        value={taskPlanId}
                        onChange={(e) => setTaskPlanId(e.target.value)}
                        className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2 text-xs text-zinc-200 focus:outline-none"
                      >
                        <option value="">Standalone Draft Task</option>
                        {(snapshot?.plans ?? []).map((p) => (
                          <option key={p.id} value={p.id}>
                            [{p.status}] {p.title} (v{p.version})
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>

                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-zinc-400">
                      Scope Files (comma separated)
                    </span>
                    <input
                      type="text"
                      value={taskScope}
                      onChange={(e) => setTaskScope(e.target.value)}
                      placeholder="src/cairnmind/sessions.py, src/cairnmind/plans.py"
                      className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 font-mono-tabular focus:outline-none"
                    />
                  </label>

                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-zinc-400">
                      Acceptance Criteria (one per line)
                    </span>
                    <textarea
                      rows={3}
                      value={taskCriteria}
                      onChange={(e) => setTaskCriteria(e.target.value)}
                      placeholder="Unit tests pass on current worktree fingerprint"
                      className="rounded-md border border-white/[0.1] bg-zinc-950/80 p-2 text-xs text-zinc-100 focus:outline-none"
                    />
                  </label>
                </div>
              )}

              {/* ADD FACT / DECISION FORM */}
              {modal.kind === 'fact' && (
                <div className="space-y-3">
                  <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                    <label className="flex flex-col gap-1">
                      <span className="text-[11px] font-medium text-zinc-400">Kind</span>
                      <select
                        value={factKind}
                        onChange={(e) =>
                          setFactKind(e.target.value as 'fact' | 'decision' | 'constraint')
                        }
                        className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2 text-xs text-zinc-100 focus:outline-none"
                      >
                        <option value="fact">fact</option>
                        <option value="decision">decision</option>
                        <option value="constraint">constraint</option>
                      </select>
                    </label>
                    <label className="sm:col-span-2 flex flex-col gap-1">
                      <span className="text-[11px] font-medium text-zinc-400">Memory Key *</span>
                      <input
                        type="text"
                        value={factKey}
                        onChange={(e) => setFactKey(e.target.value)}
                        placeholder="e.g. api.rust_adapter.project_prefix"
                        className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 font-mono-tabular focus:outline-none"
                      />
                    </label>
                  </div>

                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-zinc-400">Title *</span>
                    <input
                      type="text"
                      value={factTitle}
                      onChange={(e) => setFactTitle(e.target.value)}
                      placeholder="Concise summary of the project decision or fact"
                      className="h-8 rounded-md border border-white/[0.1] bg-zinc-950/80 px-2.5 text-xs text-zinc-100 focus:outline-none"
                    />
                  </label>

                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-zinc-400">Detail / Rationale *</span>
                    <textarea
                      rows={3}
                      value={factContent}
                      onChange={(e) => setFactContent(e.target.value)}
                      placeholder="Document why this invariant holds or what engineers must know."
                      className="rounded-md border border-white/[0.1] bg-zinc-950/80 p-2 text-xs text-zinc-100 focus:outline-none"
                    />
                  </label>

                  <div className="rounded-md border border-white/[0.08] bg-zinc-950/55 p-2.5 space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-400">
                        <FileCode2 className="h-3.5 w-3.5 text-sky-400" strokeWidth={1.5} />
                        <span>Optional Code Evidence Anchor</span>
                      </span>
                      <button
                        type="button"
                        disabled={!factSourcePath.trim() || loadingSnippet}
                        onClick={() => void handleLoadQuoteFromFile()}
                        className="flex h-6 items-center gap-1 rounded border border-sky-500/30 bg-sky-500/10 px-2 text-[10px] font-mono-tabular text-sky-300 hover:bg-sky-500/20 disabled:opacity-40"
                      >
                        {loadingSnippet && <Loader2 className="h-3 w-3 animate-spin" />}
                        <span>Load quote from file</span>
                      </button>
                    </div>

                    <div className="grid grid-cols-4 gap-2">
                      <input
                        type="text"
                        list="cairnmind-known-files"
                        value={factSourcePath}
                        onChange={(e) => setFactSourcePath(e.target.value)}
                        placeholder="src/cairnmind/plans.py"
                        className="col-span-2 h-7 rounded border border-white/[0.08] bg-zinc-950 px-2 text-[11px] text-zinc-200 font-mono-tabular focus:outline-none"
                      />
                      <datalist id="cairnmind-known-files">
                        {knownFiles.map((f) => (
                          <option key={f} value={f} />
                        ))}
                      </datalist>
                      <input
                        type="number"
                        min={1}
                        value={factLineStart}
                        onChange={(e) => setFactLineStart(Number(e.target.value) || 1)}
                        placeholder="L start"
                        className="h-7 rounded border border-white/[0.08] bg-zinc-950 px-2 text-[11px] text-zinc-200 font-mono-tabular focus:outline-none"
                      />
                      <input
                        type="number"
                        min={1}
                        value={factLineEnd}
                        onChange={(e) => setFactLineEnd(Number(e.target.value) || 20)}
                        placeholder="L end"
                        className="h-7 rounded border border-white/[0.08] bg-zinc-950 px-2 text-[11px] text-zinc-200 font-mono-tabular focus:outline-none"
                      />
                    </div>

                    <textarea
                      rows={2}
                      value={factQuote}
                      onChange={(e) => setFactQuote(e.target.value)}
                      placeholder="Verbatim code quote (optional — verified against disk SHA-256)"
                      className="w-full rounded border border-white/[0.08] bg-zinc-950 p-2 text-[11px] text-zinc-200 font-mono-tabular focus:outline-none"
                    />
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex h-12 shrink-0 items-center justify-between border-t border-white/[0.08] bg-zinc-950/60 px-4">
              <span className="text-[11px] text-zinc-500 font-mono-tabular">
                {modal.kind === 'plan'
                  ? 'Saved with status=draft · does not run agents'
                  : 'Stamped HUMAN / REPO with read-before-write CAS'}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={onClose}
                  className="h-7 rounded-md border border-white/[0.1] bg-white/[0.03] px-3 text-xs text-zinc-300 hover:bg-white/[0.08]"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={submitting || !writable}
                  onClick={() => {
                    if (modal.kind === 'plan') void handleSubmitPlan();
                    else if (modal.kind === 'task') void handleSubmitTask();
                    else void handleSubmitFact();
                  }}
                  className="flex h-7 items-center gap-1.5 rounded-md border border-sky-500/35 bg-sky-500/15 px-3 text-xs font-medium text-sky-200 transition-colors hover:bg-sky-500/25 disabled:opacity-45"
                >
                  {submitting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  <span>
                    {modal.kind === 'plan'
                      ? modal.editingPlan
                        ? 'Save Draft'
                        : 'Create Draft Plan'
                      : modal.kind === 'task'
                        ? 'Add Quick Task'
                        : 'Save to Memory'}
                  </span>
                </button>
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
