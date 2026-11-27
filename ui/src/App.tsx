import { useCallback, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, Loader2, RefreshCw, X } from 'lucide-react';
import {
  dropSessionLock,
  fetchSnapshot,
  triggerReindex,
  triggerReverify,
} from '@/api/client';
import { AskDrawer } from '@/components/ask/AskDrawer';
import { AskPanel } from '@/components/ask/AskPanel';
import { GuardrailAuditTable } from '@/components/audit/GuardrailAuditTable';
import { DraftModals, type DraftModalState } from '@/components/drafts/DraftModals';
import { DraftStateManager } from '@/components/drafts/DraftStateManager';
import { PlanDriftMonitor } from '@/components/drift/PlanDriftMonitor';
import { BlastRadiusGraph } from '@/components/graph/BlastRadiusGraph';
import { AppShell } from '@/components/layout/AppShell';
import { CommandPalette } from '@/components/layout/CommandPalette';
import { EventDock } from '@/components/layout/EventDock';
import { InspectorDrawer } from '@/components/layout/InspectorDrawer';
import { NavRail } from '@/components/layout/NavRail';
import { TopStatusBar } from '@/components/layout/TopStatusBar';
import { SessionStrip } from '@/components/matrix/SessionStrip';
import { TaskGateTable } from '@/components/matrix/TaskGateTable';
import { DoctorModal } from '@/components/ops/DoctorModal';
import { ExportSnapshotModal } from '@/components/ops/ExportSnapshotModal';
import { ClaimList } from '@/components/provenance/ClaimList';
import {
  VersionHistoryModal,
  type VersionModalTarget,
} from '@/components/versions/VersionHistoryModal';
import { useKeyboardNavigation } from '@/hooks/useKeyboardNavigation';
import { useLiveInvalidation } from '@/hooks/useLiveInvalidation';
import type {
  ActiveView,
  AskCitation,
  AskResponse,
  InspectorSelection,
  OriginFilter,
  PlanSummary,
} from '@/types/contracts';

function readInitialRoute(): {
  view: ActiveView;
  selection: InspectorSelection;
  blastRootId: string | null;
} {
  const params = new URLSearchParams(window.location.search);
  const rawView = params.get('view');
  const view: ActiveView =
    rawView === 'provenance' ||
    rawView === 'graph' ||
    rawView === 'audit' ||
    rawView === 'ask' ||
    rawView === 'drift' ||
    rawView === 'drafts'
      ? rawView
      : 'matrix';
  const rawSelect = params.get('select');
  let selection: InspectorSelection = null;
  if (rawSelect && rawSelect.includes(':')) {
    const idx = rawSelect.indexOf(':');
    const kind = rawSelect.slice(0, idx);
    const id = rawSelect.slice(idx + 1);
    if (
      id &&
      (kind === 'task' ||
        kind === 'claim' ||
        kind === 'node' ||
        kind === 'audit' ||
        kind === 'session' ||
        kind === 'plan')
    ) {
      selection = { kind, id };
    }
  }
  const blastRootId = params.get('blast') || null;
  return { view, selection, blastRootId };
}

export function App() {
  const initialRoute = useMemo(() => readInitialRoute(), []);
  const queryClient = useQueryClient();
  const [activeView, setActiveView] = useState<ActiveView>(initialRoute.view);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [dockOpen, setDockOpen] = useState(true);
  const [onlyStale, setOnlyStale] = useState(false);
  const [originFilter, setOriginFilter] = useState<OriginFilter>('ALL');
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [askDrawerOpen, setAskDrawerOpen] = useState(false);
  const [pendingAskQuery, setPendingAskQuery] = useState<string | null>(null);
  const [draftModal, setDraftModal] = useState<DraftModalState>(null);
  const [versionModal, setVersionModal] = useState<VersionModalTarget | null>(null);
  const [doctorOpen, setDoctorOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [previewDraftPlan, setPreviewDraftPlan] = useState<PlanSummary | null>(null);
  const [selection, setSelection] = useState<InspectorSelection>(initialRoute.selection);
  const [blastRootId, setBlastRootId] = useState<string | null>(initialRoute.blastRootId);
  const [droppingSessionId, setDroppingSessionId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => {
      setToast((cur) => (cur === msg ? null : cur));
    }, 3600);
  }, []);

  const {
    connection,
    events,
    clearEvents,
    stalePulseIds,
    rejectedPulseIds,
    changedFingerprints,
    liveStaleClaimIds,
    reindexProgress,
  } = useLiveInvalidation();

  const {
    data: snapshot,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['ui-snapshot'],
    queryFn: fetchSnapshot,
    refetchOnWindowFocus: false,
  });

  const writable = snapshot?.capabilities?.writable ?? true;
  const readOnlyReason = snapshot?.capabilities?.read_only_reason;

  const reverifyMutation = useMutation({
    mutationFn: triggerReverify,
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
      void queryClient.invalidateQueries({ queryKey: ['ui-file-context'] });
      void queryClient.invalidateQueries({ queryKey: ['ui-drift'] });
      showToast(
        `Re-verified ${res.checked_files} files (${res.verified_claims} verified, ${res.stale_claims} stale).`,
      );
    },
    onError: (err) => {
      showToast(err instanceof Error ? err.message : 'Re-verify failed');
    },
  });

  const reindexMutation = useMutation({
    mutationFn: triggerReindex,
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
      void queryClient.invalidateQueries({ queryKey: ['ui-drift'] });
      showToast(`Re-indexed ${res.files_scanned} files in ${res.duration_ms}ms (rev #${res.state_rev}).`);
    },
    onError: (err) => {
      showToast(err instanceof Error ? err.message : 'Re-index failed');
    },
  });

  const dropSessionMutation = useMutation({
    mutationFn: (sessionId: string) => {
      setDroppingSessionId(sessionId);
      return dropSessionLock(sessionId);
    },
    onSettled: () => {
      setDroppingSessionId(null);
      void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
    },
    onSuccess: (_, sessionId) => {
      if (selection?.kind === 'session' && selection.id === sessionId) {
        setSelection(null);
      }
      showToast(`Dropped session lock ${sessionId}.`);
    },
    onError: (err) => {
      showToast(err instanceof Error ? err.message : 'Unable to drop session');
    },
  });

  const activePlan = useMemo(
    () => (snapshot?.plans ?? []).find((p) => p.status === 'active') ?? null,
    [snapshot?.plans],
  );

  const visibleTasks = useMemo(() => {
    const list = snapshot?.tasks ?? [];
    if (!onlyStale) return list;
    return list.filter(
      (t) =>
        t.evidence_status !== 'VERIFIED' ||
        t.test_gate.status === 'STALE_CODE' ||
        t.test_gate.status === 'FAIL' ||
        t.test_gate.status === 'MISSING',
    );
  }, [onlyStale, snapshot?.tasks]);

  const visibleClaims = useMemo(() => {
    const list = snapshot?.claims ?? [];
    if (!onlyStale) return list;
    return list.filter((c) => c.provenance_status !== 'VERIFIED');
  }, [onlyStale, snapshot?.claims]);

  const visibleAudit = useMemo(() => {
    const list = snapshot?.audit_log ?? [];
    if (!onlyStale) return list;
    return list.filter((e) => e.verdict === 'REJECTED');
  }, [onlyStale, snapshot?.audit_log]);

  const navCounts = useMemo(() => {
    const tasks = snapshot?.tasks ?? [];
    const claims = snapshot?.claims ?? [];
    const audit = snapshot?.audit_log ?? [];
    const plans = snapshot?.plans ?? [];
    const drift = snapshot?.drift_summary;
    return {
      staleTasks: tasks.filter(
        (t) => t.evidence_status !== 'VERIFIED' || t.test_gate.status !== 'PASS',
      ).length,
      staleClaims: claims.filter((c) => c.provenance_status !== 'VERIFIED').length,
      rejectedAudit: audit.filter((a) => a.verdict === 'REJECTED').length,
      graphNodes: snapshot?.graph.nodes.length ?? 0,
      driftAlerts:
        (drift?.drifted_files_count ?? 0) + (drift?.pending_decisions_count ?? 0),
      draftPlans: plans.filter((p) => p.status === 'draft').length,
    };
  }, [snapshot]);

  const activeItemIds = useMemo(() => {
    if (activeView === 'matrix') return visibleTasks.map((t) => t.id);
    if (activeView === 'provenance') return visibleClaims.map((c) => c.id);
    if (activeView === 'audit') return visibleAudit.map((a) => a.id);
    return [];
  }, [activeView, visibleAudit, visibleClaims, visibleTasks]);

  const handleSelectById = useCallback(
    (id: string) => {
      if (activeView === 'matrix') setSelection({ kind: 'task', id });
      else if (activeView === 'provenance') setSelection({ kind: 'claim', id });
      else if (activeView === 'audit') setSelection({ kind: 'audit', id });
    },
    [activeView],
  );

  const handleEscape = useCallback(() => {
    if (paletteOpen) {
      setPaletteOpen(false);
      return;
    }
    if (draftModal) {
      setDraftModal(null);
      return;
    }
    if (versionModal) {
      setVersionModal(null);
      return;
    }
    if (doctorOpen) {
      setDoctorOpen(false);
      return;
    }
    if (exportOpen) {
      setExportOpen(false);
      return;
    }
    if (askDrawerOpen) {
      setAskDrawerOpen(false);
      return;
    }
    if (previewDraftPlan) {
      setPreviewDraftPlan(null);
      return;
    }
    if (blastRootId) {
      setBlastRootId(null);
      return;
    }
    if (selection) {
      setSelection(null);
    }
  }, [
    askDrawerOpen,
    blastRootId,
    doctorOpen,
    draftModal,
    exportOpen,
    paletteOpen,
    previewDraftPlan,
    selection,
    versionModal,
  ]);

  const handleJumpTo = useCallback((view: ActiveView, nextSelection: InspectorSelection) => {
    setActiveView(view);
    setSelection(nextSelection);
  }, []);

  const handleSelectAskCitation = useCallback(
    (citation: AskCitation) => {
      if (citation.claim_id && snapshot?.claims.some((c) => c.id === citation.claim_id)) {
        setActiveView('provenance');
        setSelection({ kind: 'claim', id: citation.claim_id });
        return;
      }
      setSelection({ kind: 'citation', id: citation.id, citation });
    },
    [snapshot?.claims],
  );

  const handlePinAskAnswerAsFact = useCallback((answer: AskResponse) => {
    const firstCit = answer.citations[0];
    const slug = answer.query
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '.')
      .replace(/^\.|\.$/g, '')
      .slice(0, 48);
    setDraftModal({
      kind: 'fact',
      prefill: {
        kind: 'fact',
        key: `ask.${slug || 'pinned_fact'}`,
        title: answer.query.slice(0, 140),
        content: answer.answer,
        source_path: firstCit?.file_path ?? '',
        line_start: firstCit?.line_start ?? 1,
        line_end: firstCit?.line_end ?? 20,
        verbatim_quote: firstCit?.verbatim_quote ?? '',
      },
    });
  }, []);

  const handlePreviewDraftBlast = useCallback((plan: PlanSummary) => {
    setPreviewDraftPlan(plan);
    setBlastRootId(null);
    setActiveView('graph');
  }, []);

  const handleInspectBlastFile = useCallback((filePath: string) => {
    setPreviewDraftPlan(null);
    setBlastRootId(`file:${filePath}`);
    setActiveView('graph');
  }, []);

  useKeyboardNavigation({
    itemIds: activeItemIds,
    selectedId: selection?.id ?? null,
    onSelectId: handleSelectById,
    onEscape: handleEscape,
    onOpenCommandPalette: () => setPaletteOpen((open) => !open),
    onSwitchView: setActiveView,
    onToggleRail: () => setRailCollapsed((c) => !c),
    onToggleDock: () => setDockOpen((o) => !o),
    paletteOpen: paletteOpen || Boolean(draftModal) || Boolean(versionModal) || doctorOpen || exportOpen,
  });

  const renderMainStage = () => {
    if (isLoading) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-2 text-xs text-zinc-400 font-mono-tabular">
          <Loader2 className="h-5 w-5 animate-spin text-sky-400" strokeWidth={1.5} />
          <span>Hydrating project state from Cairnmind adapter...</span>
        </div>
      );
    }

    if (error || !snapshot) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
          <AlertCircle className="h-6 w-6 text-rose-400" strokeWidth={1.5} />
          <div className="max-w-md space-y-1">
            <div className="text-sm font-semibold text-zinc-100">
              Unable to load Cairnmind UI snapshot
            </div>
            <p className="text-xs text-zinc-400 font-mono-tabular">
              {error instanceof Error ? error.message : 'Backend unavailable'}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refetch()}
            className="flex items-center gap-1.5 rounded-md border border-white/[0.1] bg-white/[0.04] px-3 py-1.5 text-xs font-medium text-zinc-200 hover:bg-white/[0.08]"
          >
            <RefreshCw className="h-3.5 w-3.5" strokeWidth={1.5} />
            <span>Retry</span>
          </button>
        </div>
      );
    }

    if (activeView === 'matrix') {
      return (
        <div className="flex h-full min-h-0 flex-col overflow-hidden">
          <SessionStrip
            sessions={snapshot.sessions}
            selectedSessionId={selection?.kind === 'session' ? selection.id : null}
            onSelectSession={(s) => setSelection({ kind: 'session', id: s.id })}
            onDropSession={(id) => dropSessionMutation.mutate(id)}
            droppingSessionId={droppingSessionId}
            writable={writable}
            readOnlyReason={readOnlyReason}
          />
          <TaskGateTable
            tasks={visibleTasks}
            activePlan={activePlan}
            selectedTaskId={selection?.kind === 'task' ? selection.id : null}
            onSelectTask={(t) => setSelection({ kind: 'task', id: t.id })}
            onReverify={() => reverifyMutation.mutate()}
            reverifying={reverifyMutation.isPending}
            writable={writable}
            readOnlyReason={readOnlyReason}
            stalePulseIds={stalePulseIds}
            originFilter={originFilter}
            onChangeOriginFilter={setOriginFilter}
            onOpenPlanVersions={(planId) => setVersionModal({ kind: 'plan', id: planId })}
          />
        </div>
      );
    }

    if (activeView === 'provenance') {
      return (
        <ClaimList
          claims={snapshot.claims}
          selectedClaimId={selection?.kind === 'claim' ? selection.id : null}
          onSelectClaim={(c) => setSelection({ kind: 'claim', id: c.id })}
          onlyStaleAndConflicts={onlyStale}
          stalePulseIds={stalePulseIds}
          onReverify={() => reverifyMutation.mutate()}
          reverifying={reverifyMutation.isPending}
          writable={writable}
          readOnlyReason={readOnlyReason}
          originFilter={originFilter}
          onChangeOriginFilter={setOriginFilter}
        />
      );
    }

    if (activeView === 'graph') {
      return (
        <BlastRadiusGraph
          graph={snapshot.graph}
          blastRootId={blastRootId}
          onSelectBlastRoot={setBlastRootId}
          onInspectNode={(node) => setSelection({ kind: 'node', id: node.id })}
          onlyStaleAndConflicts={onlyStale}
          previewDraftPlan={previewDraftPlan}
          onClearDraftPreview={() => setPreviewDraftPlan(null)}
        />
      );
    }

    if (activeView === 'audit') {
      return (
        <GuardrailAuditTable
          entries={visibleAudit}
          selectedAuditId={selection?.kind === 'audit' ? selection.id : null}
          onSelectAudit={(entry) => setSelection({ kind: 'audit', id: entry.id })}
          rejectedPulseIds={rejectedPulseIds}
          originFilter={originFilter}
          onChangeOriginFilter={setOriginFilter}
        />
      );
    }

    if (activeView === 'ask') {
      return (
        <AskPanel
          snapshot={snapshot}
          initialQuery={pendingAskQuery}
          onConsumeInitialQuery={() => setPendingAskQuery(null)}
          changedFingerprints={changedFingerprints}
          liveStaleClaimIds={liveStaleClaimIds}
          onSelectCitation={handleSelectAskCitation}
          onPinAsFact={handlePinAskAnswerAsFact}
        />
      );
    }

    if (activeView === 'drift') {
      return (
        <PlanDriftMonitor
          snapshot={snapshot}
          stalePulseIds={stalePulseIds}
          onJumpTo={handleJumpTo}
          onInspectBlastFile={handleInspectBlastFile}
          onOpenPlanVersions={(planId) => setVersionModal({ kind: 'plan', id: planId })}
        />
      );
    }

    return (
      <DraftStateManager
        snapshot={snapshot}
        onOpenModal={setDraftModal}
        onPreviewBlastRadius={handlePreviewDraftBlast}
        onOpenPlanVersions={(planId) => setVersionModal({ kind: 'plan', id: planId })}
        onNotify={showToast}
      />
    );
  };

  return (
    <AppShell
      topBar={
        <TopStatusBar
          snapshot={snapshot}
          connection={connection}
          reindexProgress={reindexProgress}
          reindexing={reindexMutation.isPending}
          onOpenPalette={() => setPaletteOpen(true)}
          onOpenAskDrawer={() => setAskDrawerOpen(true)}
          onNewDraft={() => setDraftModal({ kind: 'plan', editingPlan: null })}
          onReindex={() => reindexMutation.mutate()}
          onOpenDoctor={() => setDoctorOpen(true)}
          onOpenExport={() => setExportOpen(true)}
        />
      }
      navRail={
        <NavRail
          activeView={activeView}
          onSelectView={setActiveView}
          collapsed={railCollapsed}
          onToggleCollapse={() => setRailCollapsed((c) => !c)}
          onlyStaleAndConflicts={onlyStale}
          onToggleOnlyStale={() => setOnlyStale((v) => !v)}
          counts={navCounts}
        />
      }
      mainStage={renderMainStage()}
      showInspector={selection !== null}
      inspectorDrawer={
        <InspectorDrawer
          selection={selection}
          snapshot={snapshot}
          onClose={() => setSelection(null)}
          onJumpTo={handleJumpTo}
          onDropSession={(id) => dropSessionMutation.mutate(id)}
          droppingSessionId={droppingSessionId}
          onOpenVersions={(kind, id, label) => setVersionModal({ kind, id, label })}
        />
      }
      bottomDock={
        <EventDock
          open={dockOpen}
          onToggle={() => setDockOpen((o) => !o)}
          events={events}
          onClear={clearEvents}
        />
      }
      overlay={
        <>
          <CommandPalette
            open={paletteOpen}
            onClose={() => setPaletteOpen(false)}
            snapshot={snapshot}
            onSelectView={setActiveView}
            onSelectEntity={handleJumpTo}
            onReverify={() => reverifyMutation.mutate()}
            onReindex={() => reindexMutation.mutate()}
            onOpenDoctor={() => setDoctorOpen(true)}
            onOpenExport={() => setExportOpen(true)}
            onAskQuestion={(q) => {
              setPendingAskQuery(q);
              setAskDrawerOpen(true);
            }}
            onOpenAskDrawer={() => setAskDrawerOpen(true)}
            onOpenDraftModal={setDraftModal}
          />

          <AskDrawer
            open={askDrawerOpen}
            onClose={() => setAskDrawerOpen(false)}
            onExpandToTab={() => setActiveView('ask')}
            snapshot={snapshot}
            initialQuery={pendingAskQuery}
            onConsumeInitialQuery={() => setPendingAskQuery(null)}
            changedFingerprints={changedFingerprints}
            liveStaleClaimIds={liveStaleClaimIds}
            onSelectCitation={handleSelectAskCitation}
            onPinAsFact={handlePinAskAnswerAsFact}
          />

          <DraftModals
            modal={draftModal}
            snapshot={snapshot}
            onClose={() => setDraftModal(null)}
            onSuccess={(msg) => {
              void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
              void queryClient.invalidateQueries({ queryKey: ['ui-drift'] });
              showToast(msg);
            }}
          />

          <VersionHistoryModal
            target={versionModal}
            writable={writable}
            readOnlyReason={readOnlyReason}
            onClose={() => setVersionModal(null)}
            onJumpToAudit={(auditId) => handleJumpTo('audit', { kind: 'audit', id: auditId })}
            onClonedDraft={(msg) => {
              showToast(msg);
              setActiveView('drafts');
            }}
          />

          <DoctorModal open={doctorOpen} onClose={() => setDoctorOpen(false)} />

          <ExportSnapshotModal
            open={exportOpen}
            writable={writable}
            readOnlyReason={readOnlyReason}
            onClose={() => setExportOpen(false)}
          />

          {toast && (
            <div
              role="status"
              className="fixed bottom-12 right-4 z-50 flex items-center gap-2 rounded-lg border border-sky-500/35 bg-zinc-900/90 px-3 py-2 text-xs text-zinc-100 shadow-[0_12px_36px_rgba(0,0,0,0.6)] backdrop-blur-2xl"
            >
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-400" strokeWidth={1.5} />
              <span>{toast}</span>
              <button
                type="button"
                onClick={() => setToast(null)}
                className="ml-1 text-zinc-500 hover:text-zinc-200"
              >
                <X className="h-3 w-3" strokeWidth={1.5} />
              </button>
            </div>
          )}
        </>
      }
    />
  );
}

export default App;
