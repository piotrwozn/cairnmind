import { useCallback, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Loader2, RefreshCw } from 'lucide-react';
import { dropSessionLock, fetchSnapshot, triggerReverify } from '@/api/client';
import { GuardrailAuditTable } from '@/components/audit/GuardrailAuditTable';
import { BlastRadiusGraph } from '@/components/graph/BlastRadiusGraph';
import { AppShell } from '@/components/layout/AppShell';
import { CommandPalette } from '@/components/layout/CommandPalette';
import { EventDock } from '@/components/layout/EventDock';
import { InspectorDrawer } from '@/components/layout/InspectorDrawer';
import { NavRail } from '@/components/layout/NavRail';
import { TopStatusBar } from '@/components/layout/TopStatusBar';
import { SessionStrip } from '@/components/matrix/SessionStrip';
import { TaskGateTable } from '@/components/matrix/TaskGateTable';
import { ClaimList } from '@/components/provenance/ClaimList';
import { useKeyboardNavigation } from '@/hooks/useKeyboardNavigation';
import { useLiveInvalidation } from '@/hooks/useLiveInvalidation';
import type { ActiveView, InspectorSelection } from '@/types/contracts';

function readInitialRoute(): {
  view: ActiveView;
  selection: InspectorSelection;
  blastRootId: string | null;
} {
  const params = new URLSearchParams(window.location.search);
  const rawView = params.get('view');
  const view: ActiveView =
    rawView === 'provenance' || rawView === 'graph' || rawView === 'audit' ? rawView : 'matrix';
  const rawSelect = params.get('select');
  let selection: InspectorSelection = null;
  if (rawSelect && rawSelect.includes(':')) {
    const idx = rawSelect.indexOf(':');
    const kind = rawSelect.slice(0, idx);
    const id = rawSelect.slice(idx + 1);
    if (
      id &&
      (kind === 'task' || kind === 'claim' || kind === 'node' || kind === 'audit' || kind === 'session')
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
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [selection, setSelection] = useState<InspectorSelection>(initialRoute.selection);
  const [blastRootId, setBlastRootId] = useState<string | null>(initialRoute.blastRootId);
  const [droppingSessionId, setDroppingSessionId] = useState<string | null>(null);

  const { connection, events, clearEvents, stalePulseIds, rejectedPulseIds } =
    useLiveInvalidation();

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

  const reverifyMutation = useMutation({
    mutationFn: triggerReverify,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
      void queryClient.invalidateQueries({ queryKey: ['ui-file-context'] });
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
    },
  });

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
    return {
      staleTasks: tasks.filter(
        (t) => t.evidence_status !== 'VERIFIED' || t.test_gate.status !== 'PASS',
      ).length,
      staleClaims: claims.filter((c) => c.provenance_status !== 'VERIFIED').length,
      rejectedAudit: audit.filter((a) => a.verdict === 'REJECTED').length,
      graphNodes: snapshot?.graph.nodes.length ?? 0,
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
    if (blastRootId) {
      setBlastRootId(null);
      return;
    }
    if (selection) {
      setSelection(null);
    }
  }, [blastRootId, paletteOpen, selection]);

  const handleJumpTo = useCallback((view: ActiveView, nextSelection: InspectorSelection) => {
    setActiveView(view);
    setSelection(nextSelection);
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
    paletteOpen,
  });

  const renderMainStage = () => {
    if (isLoading) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-2 text-xs text-zinc-400 font-mono-tabular">
          <Loader2 className="h-5 w-5 animate-spin text-sky-400" strokeWidth={1.5} />
          <span>Hydrating project state from /api/ui/snapshot...</span>
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
          />
          <TaskGateTable
            tasks={visibleTasks}
            selectedTaskId={selection?.kind === 'task' ? selection.id : null}
            onSelectTask={(t) => setSelection({ kind: 'task', id: t.id })}
            onReverify={() => reverifyMutation.mutate()}
            reverifying={reverifyMutation.isPending}
            stalePulseIds={stalePulseIds}
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
        />
      );
    }

    return (
      <GuardrailAuditTable
        entries={visibleAudit}
        selectedAuditId={selection?.kind === 'audit' ? selection.id : null}
        onSelectAudit={(entry) => setSelection({ kind: 'audit', id: entry.id })}
        rejectedPulseIds={rejectedPulseIds}
      />
    );
  };

  return (
    <AppShell
      topBar={
        <TopStatusBar
          snapshot={snapshot}
          connection={connection}
          onOpenPalette={() => setPaletteOpen(true)}
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
        <CommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
          snapshot={snapshot}
          onSelectView={setActiveView}
          onSelectEntity={handleJumpTo}
          onReverify={() => reverifyMutation.mutate()}
        />
      }
    />
  );
}

export default App;
