import { useCallback, useMemo } from 'react';
import dagre from '@dagrejs/dagre';
import '@xyflow/react/dist/style.css';
import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  type NodeMouseHandler,
} from '@xyflow/react';
import { Crosshair, Eye, GitFork, RotateCcw, X } from 'lucide-react';
import { GlassBadge } from '@/components/glass/GlassBadge';
import {
  AnimatedStatusEdge,
  type CairnmindFlowEdge,
} from '@/components/graph/AnimatedStatusEdge';
import {
  GlassFlowNode,
  type CairnmindFlowNode,
} from '@/components/graph/GlassFlowNode';
import type { DependencyGraph, GraphNodeData, PlanSummary } from '@/types/contracts';

interface BlastRadiusGraphProps {
  graph: DependencyGraph;
  blastRootId: string | null;
  onSelectBlastRoot: (nodeId: string | null) => void;
  onInspectNode: (node: GraphNodeData) => void;
  onlyStaleAndConflicts: boolean;
  /** Optional Draft Plan preview overlay (`Preview Blast Radius` from Phase 3). */
  previewDraftPlan?: PlanSummary | null;
  onClearDraftPreview?: () => void;
}

const NODE_WIDTH = 236;
const NODE_HEIGHT = 64;
const COLUMN_X_STEP = 304;

const COLUMN_LABELS = [
  '1 · Source Files',
  '2 · Evidence (Quotes & Tests)',
  '3 · Memory Claims',
  '4 · Plan Tasks',
];

const nodeTypes = { glassNode: GlassFlowNode };
const edgeTypes = { statusEdge: AnimatedStatusEdge };

/**
 * Compute all downstream node and edge IDs reachable from one or more root node IDs.
 */
export function computeBlastRadius(
  graph: DependencyGraph,
  rootId: string | null,
): { nodes: Set<string>; edges: Set<string> } {
  return computeMultiRootBlastRadius(graph, rootId ? [rootId] : []);
}

export function computeMultiRootBlastRadius(
  graph: DependencyGraph,
  rootIds: string[],
): { nodes: Set<string>; edges: Set<string> } {
  const visitedNodes = new Set<string>();
  const visitedEdges = new Set<string>();
  if (rootIds.length === 0) return { nodes: visitedNodes, edges: visitedEdges };

  const outgoing = new Map<string, Array<{ edgeId: string; target: string }>>();
  for (const edge of graph.edges) {
    const list = outgoing.get(edge.source) ?? [];
    list.push({ edgeId: edge.id, target: edge.target });
    outgoing.set(edge.source, list);
  }

  const queue: string[] = [];
  for (const rid of rootIds) {
    if (!visitedNodes.has(rid)) {
      visitedNodes.add(rid);
      queue.push(rid);
    }
  }
  while (queue.length > 0) {
    const current = queue.shift()!;
    for (const next of outgoing.get(current) ?? []) {
      visitedEdges.add(next.edgeId);
      if (!visitedNodes.has(next.target)) {
        visitedNodes.add(next.target);
        queue.push(next.target);
      }
    }
  }
  return { nodes: visitedNodes, edges: visitedEdges };
}

function layoutLayeredGraph(graph: DependencyGraph): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'LR', ranksep: 90, nodesep: 28, marginx: 24, marginy: 24 });

  for (const node of graph.nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of graph.edges) {
    g.setEdge(edge.source, edge.target);
  }
  dagre.layout(g);

  // Enforce deterministic 4-column horizontal alignment while preserving dagre vertical ordering
  const positions = new Map<string, { x: number; y: number }>();
  const byColumn: Record<number, GraphNodeData[]> = { 0: [], 1: [], 2: [], 3: [] };
  for (const node of graph.nodes) {
    byColumn[node.column]?.push(node);
  }

  for (const col of [0, 1, 2, 3]) {
    const colNodes = byColumn[col];
    colNodes.sort((a, b) => {
      const ay = g.node(a.id)?.y ?? 0;
      const by = g.node(b.id)?.y ?? 0;
      return ay - by || a.id.localeCompare(b.id);
    });
    colNodes.forEach((node, idx) => {
      const dagrePos = g.node(node.id);
      const y = dagrePos ? dagrePos.y - NODE_HEIGHT / 2 : idx * (NODE_HEIGHT + 28);
      positions.set(node.id, { x: 24 + col * COLUMN_X_STEP, y });
    });
  }

  // Prevent any accidental vertical overlap inside a column
  for (const col of [0, 1, 2, 3]) {
    const colNodes = byColumn[col];
    let lastBottom = -Infinity;
    for (const node of colNodes) {
      const pos = positions.get(node.id)!;
      const minY = lastBottom + 20;
      if (pos.y < minY) {
        pos.y = minY;
      }
      lastBottom = pos.y + NODE_HEIGHT;
    }
  }

  return positions;
}

export function BlastRadiusGraph({
  graph,
  blastRootId,
  onSelectBlastRoot,
  onInspectNode,
  onlyStaleAndConflicts,
  previewDraftPlan,
  onClearDraftPreview,
}: BlastRadiusGraphProps) {
  // Overlay virtual preview nodes/edges when previewing a Draft Plan's Blast Radius
  const augmentedGraph = useMemo<{
    graph: DependencyGraph;
    draftNodeIds: Set<string>;
    draftRootIds: string[];
  }>(() => {
    const draftNodeIds = new Set<string>();
    const draftRootIds: string[] = [];
    if (!previewDraftPlan) {
      return { graph, draftNodeIds, draftRootIds };
    }

    const nodes = [...graph.nodes];
    const edges = [...graph.edges];
    const nodeIds = new Set(nodes.map((n) => n.id));
    const edgeIds = new Set(edges.map((e) => e.id));

    const allFiles = Array.from(
      new Set([
        ...previewDraftPlan.linked_files,
        ...previewDraftPlan.steps.flatMap((s) => s.scope),
      ]),
    );

    for (const filePath of allFiles) {
      const fid = `file:${filePath}`;
      draftRootIds.push(fid);
      if (!nodeIds.has(fid)) {
        nodeIds.add(fid);
        draftNodeIds.add(fid);
        nodes.push({
          id: fid,
          column: 0,
          category: 'source_file',
          label: filePath.split('/').pop() ?? filePath,
          subtitle: `${filePath} (new/untracked)`,
          status: 'ACTIVE',
          ref_id: filePath,
          meta: { draft_preview: true },
        });
      }
    }

    for (const step of previewDraftPlan.steps) {
      const tid = `draft-step:${previewDraftPlan.id}:${step.key}`;
      if (!nodeIds.has(tid)) {
        nodeIds.add(tid);
        draftNodeIds.add(tid);
        nodes.push({
          id: tid,
          column: 3,
          category: 'task',
          label: `DRAFT · ${step.key}`,
          subtitle: step.title,
          status: 'ACTIVE',
          ref_id: previewDraftPlan.id,
          meta: {
            draft_preview: true,
            origin_category: 'HUMAN_REPO',
          },
        });
      }
      const stepFiles = step.scope.length > 0 ? step.scope : allFiles;
      for (const filePath of stepFiles) {
        const fid = `file:${filePath}`;
        const eid = `${fid}->${tid}`;
        if (!edgeIds.has(eid)) {
          edgeIds.add(eid);
          edges.push({ id: eid, source: fid, target: tid, kind: 'draft_impacts', status: 'ACTIVE' });
        }
      }
    }

    return { graph: { nodes, edges }, draftNodeIds, draftRootIds };
  }, [graph, previewDraftPlan]);

  const filteredGraph = useMemo<DependencyGraph>(() => {
    if (!onlyStaleAndConflicts || previewDraftPlan) return augmentedGraph.graph;
    const staleNodes = new Set(
      augmentedGraph.graph.nodes
        .filter((n) => n.status.toUpperCase() !== 'VERIFIED' && n.status.toUpperCase() !== 'PASS')
        .map((n) => n.id),
    );
    return {
      nodes: augmentedGraph.graph.nodes.filter((n) => staleNodes.has(n.id)),
      edges: augmentedGraph.graph.edges.filter(
        (e) => staleNodes.has(e.source) && staleNodes.has(e.target),
      ),
    };
  }, [augmentedGraph.graph, onlyStaleAndConflicts, previewDraftPlan]);

  const positions = useMemo(() => layoutLayeredGraph(filteredGraph), [filteredGraph]);

  const blast = useMemo(() => {
    if (previewDraftPlan && augmentedGraph.draftRootIds.length > 0) {
      return computeMultiRootBlastRadius(filteredGraph, augmentedGraph.draftRootIds);
    }
    return computeBlastRadius(filteredGraph, blastRootId);
  }, [augmentedGraph.draftRootIds, blastRootId, filteredGraph, previewDraftPlan]);

  const flowNodes = useMemo<CairnmindFlowNode[]>(() => {
    const hasBlast = Boolean((blastRootId || previewDraftPlan) && blast.nodes.size > 0);
    return filteredGraph.nodes.map((node) => ({
      id: node.id,
      type: 'glassNode',
      position: positions.get(node.id) ?? { x: node.column * COLUMN_X_STEP, y: 0 },
      draggable: false,
      data: {
        node,
        dimmed: hasBlast && !blast.nodes.has(node.id),
        highlighted: hasBlast && blast.nodes.has(node.id),
        selectedFocus:
          node.id === blastRootId ||
          (Boolean(previewDraftPlan) && augmentedGraph.draftRootIds.includes(node.id)),
        draftPreview: augmentedGraph.draftNodeIds.has(node.id),
      },
    }));
  }, [
    augmentedGraph.draftNodeIds,
    augmentedGraph.draftRootIds,
    blast.nodes,
    blastRootId,
    filteredGraph.nodes,
    positions,
    previewDraftPlan,
  ]);

  const flowEdges = useMemo<CairnmindFlowEdge[]>(() => {
    const hasBlast = Boolean((blastRootId || previewDraftPlan) && blast.nodes.size > 0);
    return filteredGraph.edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: 'statusEdge',
      data: {
        status: edge.status,
        illuminated: hasBlast && blast.edges.has(edge.id),
        dimmed: hasBlast && !blast.edges.has(edge.id),
      },
    }));
  }, [blast.edges, blast.nodes.size, blastRootId, filteredGraph.edges, previewDraftPlan]);

  const handleNodeClick: NodeMouseHandler<CairnmindFlowNode> = useCallback(
    (_, flowNode) => {
      const raw = flowNode.data.node;
      if (!flowNode.data.draftPreview) {
        onInspectNode(raw);
      }
      if (raw.category === 'source_file' || raw.category === 'evidence' || raw.category === 'claim') {
        onSelectBlastRoot(blastRootId === raw.id ? null : raw.id);
      } else {
        onSelectBlastRoot(null);
      }
    },
    [blastRootId, onInspectNode, onSelectBlastRoot],
  );

  const affectedCount = previewDraftPlan
    ? Math.max(0, blast.nodes.size - augmentedGraph.draftRootIds.length)
    : blastRootId
      ? Math.max(0, blast.nodes.size - 1)
      : 0;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* Header bar with 4-column taxonomy legend & Blast Radius indicator */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex items-center gap-2">
          <GitFork className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold text-zinc-100">
            Dependency &amp; Blast-Radius Graph
          </span>
          <div className="hidden items-center gap-1.5 text-[10px] text-zinc-500 font-mono-tabular xl:flex">
            {COLUMN_LABELS.map((label, idx) => (
              <span key={label} className="flex items-center gap-1.5">
                {idx > 0 && <span className="text-zinc-700">-&gt;</span>}
                <span className="rounded border border-white/[0.06] bg-white/[0.02] px-1.5 py-0.5 text-zinc-400">
                  {label}
                </span>
              </span>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {previewDraftPlan ? (
            <>
              <GlassBadge tone="sky">
                <Eye className="h-3 w-3" strokeWidth={1.5} />
                <span>
                  Draft Preview: {previewDraftPlan.external_ref || previewDraftPlan.title} ·{' '}
                  {affectedCount} impacted
                </span>
              </GlassBadge>
              <button
                type="button"
                onClick={onClearDraftPreview}
                className="flex h-6 items-center gap-1 rounded-md border border-white/[0.1] bg-white/[0.04] px-2 text-[11px] text-zinc-300 transition-colors hover:bg-white/[0.08]"
              >
                <X className="h-3 w-3" strokeWidth={1.5} />
                <span>Exit Preview</span>
              </button>
            </>
          ) : blastRootId ? (
            <>
              <GlassBadge tone="amber">
                <Crosshair className="h-3 w-3" strokeWidth={1.5} />
                <span>Blast Radius: {affectedCount} downstream</span>
              </GlassBadge>
              <button
                type="button"
                onClick={() => onSelectBlastRoot(null)}
                className="flex h-6 items-center gap-1 rounded-md border border-white/[0.1] bg-white/[0.04] px-2 text-[11px] text-zinc-300 transition-colors hover:bg-white/[0.08]"
              >
                <RotateCcw className="h-3 w-3" strokeWidth={1.5} />
                <span>Reset</span>
              </button>
            </>
          ) : (
            <span className="text-[11px] text-zinc-500 font-mono-tabular">
              Click any Source File or Evidence node to illuminate downstream blast radius
            </span>
          )}
        </div>
      </div>

      {/* Interactive React Flow Canvas */}
      <div className="relative min-h-0 flex-1">
        {flowNodes.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-zinc-500 font-mono-tabular">
            No dependency nodes match the active filter.
          </div>
        ) : (
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodeClick={handleNodeClick}
            onPaneClick={() => onSelectBlastRoot(null)}
            fitView
            fitViewOptions={{ padding: 0.16, maxZoom: 1.05 }}
            minZoom={0.35}
            maxZoom={1.6}
            proOptions={{ hideAttribution: true }}
          >
            <Background
              variant={BackgroundVariant.Dots}
              gap={20}
              size={1}
              color="rgba(255, 255, 255, 0.08)"
            />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>
    </div>
  );
}
